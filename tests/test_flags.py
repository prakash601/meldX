"""Capability flags (product plan §6.1): the standalone personal service boots
without the MCP transport or the agent-execution routes unless explicitly enabled.

These pin the v0.0.1 acceptance "service startup works with those transports
disabled": routes are absent (router-level 404), not erroring behind a flag check.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from starlette.routing import Mount
from starlette.testclient import TestClient

from meldx.app import db
from meldx.app.main import create_app

MISSING = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def sqlite_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    url = f"sqlite+aiosqlite:///{tmp_path}/flags.db"
    monkeypatch.setenv("DATABASE_URL", url)
    db.reset_engine()
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()
    yield
    await db.get_engine().dispose()
    db.reset_engine()


async def test_default_boot_is_personal_service_only(sqlite_file: None) -> None:
    app = create_app()
    assert not any(isinstance(route, Mount) for route in app.routes)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        assert (await client.get("/health")).status_code == 200
        # Route absent: router-level 404, NOT the task-contract 404.
        claim = await client.post(f"/api/tasks/{MISSING}/claim", json={"agent_id": "codex-1"})
        assert claim.status_code == 404
        assert claim.json() == {"detail": "Not Found"}
        handoff = await client.post(f"/api/tasks/{MISSING}/handoff", json={"to_agent": "codex-1"})
        assert handoff.status_code == 404
        assert handoff.json() == {"detail": "Not Found"}
        page = await client.get("/")
        assert page.status_code == 200
        assert "Claim tasks as" not in page.text
        assert ">Claim<" not in page.text


def test_lifespan_boots_without_mcp(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup with the transport not even instantiated."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/boot.db")
    db.reset_engine()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/health").json() == {"ok": True}
            assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping"}).status_code == 404
    finally:
        db.reset_engine()


async def test_execution_enabled_registers_claim_and_handoff(sqlite_file: None) -> None:
    app = create_app(enable_agent_execution=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        # Now the routes exist and enforce the task contract.
        claim = await client.post(f"/api/tasks/{MISSING}/claim", json={"agent_id": "codex-1"})
        assert claim.status_code == 404
        assert claim.json() == {"detail": "Task locked or not found"}
        handoff = await client.post(f"/api/tasks/{MISSING}/handoff", json={"to_agent": "codex-1"})
        assert handoff.status_code == 404
        assert handoff.json() == {"detail": "Task locked or not found"}
        page = await client.get("/")
        assert "Claim tasks as" in page.text


def test_mcp_enabled_mounts_transport(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/mcpboot.db")
    db.reset_engine()
    try:
        app = create_app(enable_mcp=True)
        assert any(isinstance(route, Mount) for route in app.routes)
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    finally:
        db.reset_engine()
