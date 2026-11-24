"""One process serves MCP, REST, SSE, and the Today view."""

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app import auth as authz
from meldx.app import bus, dates
from meldx.app.api import agents, events, tasks
from meldx.app.db import check_db, ensure_schema, get_engine, get_session, is_sqlite
from meldx.app.mcp.tools import mcp as mcp_server
from meldx.app.models import TaskImport
from meldx.app.services import tasks as svc
from meldx.app.web import router as web


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "event": getattr(record, "event", record.getMessage()),
                "task_id": getattr(record, "task_id", None),
                "agent_id": getattr(record, "agent_id", None),
                "status": getattr(record, "status", None),
            }
        )


DESCRIPTION = """
One shared todo queue for one owner and any connected agents.

One process serves the personal task service: REST at `/api` (tasks, agents,
SSE), the Jinja2 + HTMX Today board at `/`, and a readiness probe at `/health`
that runs `SELECT 1`. The MCP transport at `/mcp` (Streamable HTTP) and the
agent-execution routes (`claim`, `handoff`) are opt-in: set `ENABLE_MCP=true` or
`ENABLE_AGENT_EXECUTION=true` to register them. The standalone personal service
boots with both off.

Lease semantics (execution enabled): `POST /api/tasks/{task_id}/claim` takes an
exclusive lease (`LEASE_MINUTES`, default 30) inside a `FOR UPDATE SKIP LOCKED`
transaction. A task with an unexpired lease held by another agent cannot be
claimed; once the lease expires the task becomes claimable again. `complete`
clears the owner and lease.

Task errors mirror the MCP tool messages verbatim so clients can switch transports
without changing their handling: `404` for an unknown task id (`Task locked or not
found`), `409` when a row is locked by an in-flight claim (`Task locked or not
found`) or leased to another agent (`Task leased to {agent} until {ts}`), and `400`
for an unparseable due/snooze string.
""".strip()

OPENAPI_TAGS = [
    {"name": "tasks", "description": "Create, list, plan, edit, snooze, start, complete, reopen."},
    {"name": "agents", "description": "Minimal agent registry: who can hold a lease."},
    {"name": "events", "description": "Server-Sent Events for committed task mutations."},
    {"name": "health", "description": "Readiness probe backed by a database round-trip."},
    {"name": "settings", "description": "Owner timezone and enabled capabilities. No secrets."},
    {"name": "web", "description": "HTML/HTMX pages and fragments serving the board views."},
]


def configure_logging() -> None:
    logger = logging.getLogger("meldx")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def mcp_transport_security() -> TransportSecuritySettings:
    """Accept explicit deployment hosts while retaining DNS rebinding protection."""
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
    origins = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]
    for key in ("RENDER_EXTERNAL_HOSTNAME", "MCP_HOSTNAME"):
        hostname = os.environ.get(key)
        if hostname:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", hostname):
                raise ValueError(f"{key} must contain a hostname without a scheme or port")
            hosts.extend([hostname, f"{hostname}:443"])
            origins.append(f"https://{hostname}")
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger = logging.getLogger("meldx")
    get_engine()  # fail fast on bad DATABASE_URL
    dates.owner_zone()  # fail fast on a bad OWNER_TIMEZONE (F01.5)
    if authz.bootstrap_secret() is None:
        logger.warning(
            "ENABLE_AUTH is on but OWNER_BOOTSTRAP_SECRET is missing; auth is a no-op. "
            "Set the bootstrap secret before exposing the service.",
            extra={"event": "auth.misconfigured", "status": "warn"},
        )
    elif not getattr(app.state, "auth", False):
        logger.warning(
            "authentication is DISABLED: safe only for loopback development. "
            "Set ENABLE_AUTH=true before any non-local exposure.",
            extra={"event": "auth.disabled", "status": "warn"},
        )
    if is_sqlite():
        # No server to start: create the schema the Alembic Postgres migration
        # cannot create here, and say plainly which guarantee is not covered.
        await ensure_schema()
        logger.warning(
            "local SQLite database: claims use an atomic conditional UPDATE. "
            "The Postgres FOR UPDATE SKIP LOCKED race path is not exercised here; "
            "run the race suite against Postgres before trusting lease behaviour.",
            extra={"event": "db.sqlite_dev", "status": "dev"},
        )
    bus.init_bus()  # fresh event queue per boot
    # Mounted sub-apps don't get their lifespan run — nest it manually so the
    # streamable-HTTP task group starts when the MCP transport is enabled.
    mcp_app = getattr(app.state, "mcp_app", None)
    if mcp_app is None:
        yield
    else:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield


def _flag(env: str, override: bool | None) -> bool:
    """Explicit argument wins; otherwise ENABLE_* env vars, default off."""
    if override is not None:
        return override
    return os.environ.get(env, "").strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    *,
    enable_mcp: bool | None = None,
    enable_agent_execution: bool | None = None,
    enable_auth: bool | None = None,
) -> FastAPI:
    mcp_enabled = _flag("ENABLE_MCP", enable_mcp)
    execution_enabled = _flag("ENABLE_AGENT_EXECUTION", enable_agent_execution)
    auth_enabled = _flag("ENABLE_AUTH", enable_auth)
    if auth_enabled and authz.bootstrap_secret() is None:
        raise ValueError("ENABLE_AUTH requires OWNER_BOOTSTRAP_SECRET to be set")
    mcp_app = (
        mcp_server.streamable_http_app(transport_security=mcp_transport_security())
        if mcp_enabled
        else None
    )
    app = FastAPI(
        title="meldX",
        version="0.0.1",
        description=DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.mcp_app = mcp_app
    app.state.agent_execution = execution_enabled
    app.state.auth = auth_enabled
    app.state.login_limiter = authz.LoginRateLimiter.from_env()
    app.router.lifespan_context = lifespan

    @app.get(
        "/health",
        tags=["health"],
        summary="Readiness probe",
        description="Runs `SELECT 1` against Postgres. Returns 503 when the DB is unreachable.",
        responses={503: {"description": "Database unreachable."}},
    )
    async def health() -> dict[str, bool]:
        try:
            await check_db()
        except (OSError, SQLAlchemyError):
            raise HTTPException(status_code=503, detail="db unreachable") from None
        logging.getLogger("meldx.db").info(
            "SELECT 1 ok", extra={"event": "db.select_1", "status": "ok"}
        )
        return {"ok": True}

    @app.get(
        "/api/settings",
        tags=["settings"],
        summary="Service settings",
        description="Owner timezone and enabled capabilities for clients. No secrets.",
    )
    async def settings() -> dict[str, Any]:
        return {
            "timezone": dates.owner_timezone(),
            "lease_minutes": svc.lease_minutes(),
            "capabilities": {
                "mcp": mcp_enabled,
                "agent_execution": execution_enabled,
            },
        }

    # v0.0.4 (plan §6.5): every task-data surface requires the owner. GETs
    # need the read scope, mutations the write scope; a web session carries
    # both. /health and /api/settings stay public (no task data, no secrets).
    async def require_api_auth(
        request: Request, session: Annotated[AsyncSession, Depends(get_session)]
    ) -> None:
        if not auth_enabled:
            return
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            credential = await authz.verify_bearer_token(session, header[7:].strip())
            if credential is None:
                raise HTTPException(status_code=401, detail="Unauthorized")
            needed = (
                authz.READ_SCOPE if request.method in {"GET", "HEAD", "OPTIONS"} else authz.WRITE_SCOPE
            )
            if needed not in credential.scopes:
                raise HTTPException(status_code=403, detail=f"Credential lacks the {needed} scope")
            return
        cookie = request.cookies.get(authz.SESSION_COOKIE)
        if await authz.verify_session_token(session, cookie) is None:
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get(
        "/api/export",
        tags=["tasks"],
        summary="Export all tasks with history",
        description="Schema-versioned JSON snapshot for data portability (plan F10): "
        "every task — archived included — with stable IDs, scheduling fields, and "
        "the durable activity history. No credentials exist in task data.",
    )
    async def export_tasks(
        session: Annotated[AsyncSession, Depends(get_session)],
        _auth: Annotated[None, Depends(require_api_auth)],
    ) -> dict[str, Any]:
        return await svc.export_data(session)

    @app.post(
        "/api/import/preview",
        tags=["tasks"],
        summary="Preview an import",
        description="Validates an export document against the database and reports "
        "counts, invalid rows, duplicates, and missing parent references. Writes "
        "nothing. Identity is the task id — never a title guess.",
    )
    async def import_preview(
        payload: TaskImport,
        session: Annotated[AsyncSession, Depends(get_session)],
        _auth: Annotated[None, Depends(require_api_auth)],
    ) -> dict[str, Any]:
        return await svc.import_preview(session, payload.data)

    @app.post(
        "/api/import",
        tags=["tasks"],
        summary="Apply an import",
        description="Transactional import: the default duplicate policy is `skip`; "
        "`replace` is an explicit choice and refuses imports whose revision is "
        "older than the existing row. Invalid files write nothing — preview first.",
    )
    async def import_tasks(
        payload: TaskImport,
        session: Annotated[AsyncSession, Depends(get_session)],
        _auth: Annotated[None, Depends(require_api_auth)],
    ) -> dict[str, Any]:
        return await svc.import_data(session, payload.data, policy=payload.policy)

    @app.exception_handler(ValueError)
    async def task_error(request: Request, exc: ValueError) -> JSONResponse:
        message = str(exc)
        code = 400
        if isinstance(exc, svc.TaskNotFound):
            code = 404
        elif isinstance(
            exc,
            (
                svc.TaskLocked,
                svc.TaskStateConflict,
                svc.IdempotencyConflict,
                svc.StaleRevision,
            ),
        ) or message.startswith("Task leased to "):
            code = 409
        logging.getLogger("meldx.api").warning(
            "task request failed",
            extra={
                "event": "task.error",
                "task_id": request.path_params.get("task_id"),
                "agent_id": getattr(request.state, "agent_id", None),
                "status": code,
            },
        )
        content: dict[str, object] = {"detail": message}
        if isinstance(exc, svc.StaleRevision):
            # Enough current state for the client to reload and reapply.
            content["current_task"] = exc.task.model_dump(mode="json")
        return JSONResponse(status_code=code, content=content)

    api_auth = [Depends(require_api_auth)]

    # API routes BEFORE the "/" mount: a root mount matches every path,
    # so anything registered after it would be unreachable.
    app.include_router(events.router, prefix="/api", dependencies=api_auth)
    app.include_router(tasks.router, prefix="/api", dependencies=api_auth)
    if execution_enabled:
        app.include_router(tasks.execution_router, prefix="/api", dependencies=api_auth)
    app.include_router(agents.router, prefix="/api", dependencies=api_auth)
    app.include_router(web.router)
    app.include_router(web.auth_router)
    if execution_enabled:
        app.include_router(web.execution_router)

    # MCP Streamable HTTP for ChatGPT/OpenCode/Hermes (inner route /mcp).
    # Opt-in: the standalone personal service boots without it (product plan
    # §6.1 — MCP is aligned with the finished service at v0.0.5).
    if mcp_app is not None:
        app.mount("/", mcp_app)

    return app


app = create_app()
