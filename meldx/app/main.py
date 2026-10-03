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
from meldx.app.db import check_db, get_engine
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
    get_engine()  # fail fast on bad DATABASE_URL
    bus.init_bus()  # fresh event queue per boot
    # Mounted sub-apps don't get their lifespan run — nest it manually so the
    # streamable-HTTP task group starts.
    async with app.state.mcp_app.router.lifespan_context(app.state.mcp_app):
        yield


def create_app() -> FastAPI:
    mcp_app = mcp_server.streamable_http_app(transport_security=mcp_transport_security())
    app = FastAPI(title="meldX")
    app.state.mcp_app = mcp_app
    app.router.lifespan_context = lifespan

    @app.get("/health")
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
