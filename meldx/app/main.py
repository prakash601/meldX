"""One process serves MCP, REST, SSE, and the Today view."""

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.exc import SQLAlchemyError

from meldx.app import bus
from meldx.app.api import agents, events, tasks
from meldx.app.db import check_db, ensure_schema, get_engine, is_sqlite
from meldx.app.mcp.tools import mcp as mcp_server
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
One shared todo queue that humans and agents can claim and complete without colliding.

One process serves four interfaces over the same task services: MCP at `/mcp`
(Streamable HTTP, seven `todo_*` tools for ChatGPT, Codex, OpenCode, Hermes), REST
at `/api` (tasks, agents, SSE), the Jinja2 + HTMX Today board at `/`, and a
readiness probe at `/health` that runs `SELECT 1`.

Lease semantics: `POST /api/tasks/{task_id}/claim` takes an exclusive lease
(`LEASE_MINUTES`, default 30) inside a `FOR UPDATE SKIP LOCKED` transaction. A task
with an unexpired lease held by another agent cannot be claimed; once the lease
expires the task becomes claimable again. `complete` clears the owner and lease.

Task errors mirror the MCP tool messages verbatim so clients can switch transports
without changing their handling: `404` for an unknown task id (`Task locked or not
found`), `409` when a row is locked by an in-flight claim (`Task locked or not
found`) or leased to another agent (`Task leased to {agent} until {ts}`), and `400`
for an unparseable due/snooze string.
""".strip()

OPENAPI_TAGS = [
    {"name": "tasks", "description": "Create, list, claim, complete, update, hand off, snooze."},
    {"name": "agents", "description": "Minimal agent registry: who can hold a lease."},
    {"name": "events", "description": "Server-Sent Events for committed task mutations."},
    {"name": "health", "description": "Readiness probe backed by a database round-trip."},
    {"name": "web", "description": "HTML/HTMX fragments serving the Today board."},
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
    # streamable-HTTP task group starts.
    async with app.state.mcp_app.router.lifespan_context(app.state.mcp_app):
        yield


def create_app() -> FastAPI:
    mcp_app = mcp_server.streamable_http_app(transport_security=mcp_transport_security())
    app = FastAPI(
        title="meldX",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.mcp_app = mcp_app
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

    @app.exception_handler(ValueError)
    async def task_error(request: Request, exc: ValueError) -> JSONResponse:
        message = str(exc)
        code = 400
        if isinstance(exc, svc.TaskNotFound):
            code = 404
        elif isinstance(exc, svc.TaskLocked) or message.startswith("Task leased to "):
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
        return JSONResponse(status_code=code, content={"detail": message})

    # API routes BEFORE the "/" mount: a root mount matches every path,
    # so anything registered after it would be unreachable.
    app.include_router(events.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.include_router(web.router)

    # MCP Streamable HTTP for ChatGPT/OpenCode/Hermes (inner route /mcp)
    app.mount("/", mcp_app)

    return app


app = create_app()
