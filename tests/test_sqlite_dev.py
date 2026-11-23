"""SQLite local-dev mode: schema bootstrap and single-winner claims.

Postgres keeps FOR UPDATE SKIP LOCKED (proven in test_claim_race.py). SQLite
ignores that clause, so the claim path there takes the row with one atomic
conditional UPDATE instead. These tests pin that behaviour.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient

from meldx.app import db
from meldx.app.main import create_app
from meldx.app.models import Task
from meldx.app.services import tasks as svc


@pytest.fixture
async def sqlite_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/dev.db")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _try_claim(maker, task_id: str, agent: str) -> Task | ValueError:
    async with maker() as session:
        try:
            return await svc.claim_task(session, task_id=task_id, agent_id=agent)
        except ValueError as exc:
            return exc


async def test_sqlite_concurrent_claim_has_single_winner(sqlite_maker) -> None:
    """Every round must produce exactly one winner, however the interleaving falls.

    Run repeatedly: a read-then-write claim is flaky rather than consistently
    wrong, so a single round proves nothing.
    """
    for round_number in range(8):
        async with sqlite_maker() as session:
            task = await svc.create_task(session, title=f"sqlite-race-{round_number}")
        tid = str(task.id)

        results = await asyncio.gather(
            _try_claim(sqlite_maker, tid, "opencode-1"),
            _try_claim(sqlite_maker, tid, "hermes-1"),
        )
        winners = [r for r in results if isinstance(r, Task)]
        losers = [r for r in results if isinstance(r, ValueError)]

        assert len(winners) == 1, f"round {round_number}: {results}"
        assert len(losers) == 1, f"round {round_number}: {results}"
        assert "locked" in str(losers[0]) or "leased to" in str(losers[0])
        assert winners[0].status == "doing"
        assert winners[0].lease_expires_at is not None


async def test_sqlite_claim_is_idempotent_for_the_same_agent(sqlite_maker) -> None:
    async with sqlite_maker() as session:
        task = await svc.create_task(session, title="re-claim")
    tid = str(task.id)

    first = await _try_claim(sqlite_maker, tid, "opencode-1")
    second = await _try_claim(sqlite_maker, tid, "opencode-1")
    assert isinstance(first, Task) and isinstance(second, Task)
    assert second.agent_id == "opencode-1"


async def test_sqlite_expired_lease_is_reclaimable(sqlite_maker) -> None:
    async with sqlite_maker() as session:
        task = await svc.create_task(session, title="expiry")
        claimed = await svc.claim_task(session, task_id=str(task.id), agent_id="opencode-1")
        claimed.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.add(claimed)
        await session.commit()

    retaken = await _try_claim(sqlite_maker, str(task.id), "hermes-1")
    assert isinstance(retaken, Task)
    assert retaken.agent_id == "hermes-1"


async def test_sqlite_missing_task_returns_contract_error(sqlite_maker) -> None:
    result = await _try_claim(sqlite_maker, "00000000-0000-0000-0000-000000000000", "opencode-1")
    assert isinstance(result, ValueError)
    assert str(result) == "Task locked or not found"


def test_lifespan_bootstraps_sqlite_schema(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point DATABASE_URL at a bare SQLite file and the app should just run."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/boot.db")
    db.reset_engine()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/health").json() == {"ok": True}
            created = client.post("/api/tasks", json={"title": "boot task"})
            assert created.status_code == 201
            assert client.get("/").status_code == 200
            listed = client.get("/api/tasks").json()
            assert [t["title"] for t in listed] == ["boot task"]
    finally:
        db.reset_engine()
