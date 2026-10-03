"""P4 race proof. Requires real Postgres (SKIP LOCKED is a PG semantic).

URL: DATABASE_URL_TEST, else DATABASE_URL. Skips when PG is unreachable
(e.g. sqlite-only environments) — CI provides the postgres service.
"""
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.models import Task
from meldx.app.services import tasks as svc


def _pg_url() -> str:
    return (
        os.environ.get("DATABASE_URL_TEST")
        or os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://meldx:meldx@localhost:5432/meldx_test"
    )


@pytest.fixture
async def pg_sessions():
    url = _pg_url()
    engine = create_async_engine(url, pool_size=4)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except (OSError, SQLAlchemyError):
        await engine.dispose()
        pytest.skip(f"no postgres reachable at {url}")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _try_claim(maker, tid: str, agent: str):
    async with maker() as session:
        try:
            return await svc.claim_task(session, task_id=tid, agent_id=agent)
        except ValueError as e:
            return e


async def test_race_single_winner(pg_sessions) -> None:
    tag = f"race-{uuid.uuid4().hex[:8]}"
    async with pg_sessions() as s:
        task = await svc.create_task(s, title=tag)
    tid = str(task.id)

    results = await asyncio.gather(
        _try_claim(pg_sessions, tid, "opencode-1"),
        _try_claim(pg_sessions, tid, "hermes-1"),
    )
    wins = [r for r in results if isinstance(r, Task)]
    losses = [r for r in results if isinstance(r, ValueError)]
    assert len(wins) == 1, results
    assert len(losses) == 1, results
    assert "locked" in str(losses[0]) or "leased to" in str(losses[0])
    assert wins[0].agent_id in ("opencode-1", "hermes-1")
    assert wins[0].status == "doing"


async def test_expired_lease_reclaimable(pg_sessions) -> None:
    tag = f"expiry-{uuid.uuid4().hex[:8]}"
    async with pg_sessions() as s:
        task = await svc.create_task(s, title=tag)
        claimed = await svc.claim_task(s, task_id=str(task.id), agent_id="opencode-1")
        assert claimed.agent_id == "opencode-1"
        claimed.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.add(claimed)
        await s.commit()

    async with pg_sessions() as s:
        retaken = await svc.claim_task(s, task_id=str(task.id), agent_id="hermes-1")
    assert retaken.agent_id == "hermes-1"
    assert retaken.lease_expires_at is not None
    assert retaken.lease_expires_at > datetime.now(UTC)
