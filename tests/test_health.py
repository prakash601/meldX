"""Health: 200 when SELECT 1 ok, 503 closed when DB down."""
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from meldx.app import db
from meldx.app.main import create_app


@pytest.fixture
def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    db.reset_engine()
    yield
    db.reset_engine()


@pytest.fixture
def dead_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    db.reset_engine()
    yield
    db.reset_engine()


@pytest.mark.asyncio
async def test_health_ok(sqlite_db: None) -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_health_closed_when_db_down(dead_db: None) -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 503
