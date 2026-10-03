"""P2: boots with /health backed by SELECT 1. DB/MCP/API/Web mount in P3-P5."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from meldx.app.db import check_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # P3: mount MCP app at /mcp
    # P4: create shared event_queue (asyncio.Queue)
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

    # P3: app.mount("/mcp", mcp_app)
    # P4: app.include_router(events.router, prefix="/api")
    # P5: app.include_router(tasks.router, prefix="/api/tasks") + web router
    return app


app = create_app()
