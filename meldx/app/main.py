"""P1 skeleton: boots with /health. DB/MCP/API/Web mount in P2-P5."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # P2: init async engine + SELECT 1 check
    # P3: mount MCP app at /mcp
    # P4: create shared event_queue (asyncio.Queue)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="meldX")
    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # P3: app.mount("/mcp", mcp_app)
    # P4: app.include_router(events.router, prefix="/api")
    # P5: app.include_router(tasks.router, prefix="/api/tasks") + web router
    return app


app = create_app()
