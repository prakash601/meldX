"""P3: FastAPI + MCP (Streamable HTTP at /mcp). API/SSE/Web mount in P4-P5."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from meldx.app.db import check_db, get_engine
from meldx.app.mcp.tools import mcp as mcp_server

# Inner route is /mcp (SDK default). Mounted at "/" so POST /mcp hits it
# directly with no slash-redirect; outer routes are matched first.
mcp_app = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_engine()  # fail fast on bad DATABASE_URL
    # Mounted sub-apps don't get their lifespan run — nest it manually so the
    # streamable-HTTP task group starts.
    async with mcp_app.router.lifespan_context(mcp_app):
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

    # MCP Streamable HTTP for ChatGPT/OpenCode/Hermes
    app.mount("/", mcp_app)

    # P4: app.include_router(events.router, prefix="/api")
    # P5: app.include_router(tasks.router, prefix="/api/tasks") + web router
    return app


app = create_app()
