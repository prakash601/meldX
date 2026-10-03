"""One process serves MCP, REST, SSE, and the Today view."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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


# Inner route is /mcp (SDK default). Mounted at "/" so POST /mcp hits it
# directly with no slash-redirect; outer routes are matched first.
mcp_app = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    get_engine()  # fail fast on bad DATABASE_URL
    bus.init_bus()  # fresh event queue per boot
    # Mounted sub-apps don't get their lifespan run — nest it manually so the
    # streamable-HTTP task group starts.
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="meldX")
    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health() -> dict[str, bool]:
        try:
            await check_db()
        except (OSError, SQLAlchemyError):
            raise HTTPException(status_code=503, detail="db unreachable") from None
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
