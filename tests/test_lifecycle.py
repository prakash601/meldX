"""ADR-004 v0.0.1 semantics at the service layer: dual status sync, revision,
activity, idempotency, start/complete/reopen.

SQLite-backed like the rest of the fast suite; the Postgres claim race stays
in test_claim_race.py.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.models import Task, TaskActivity, TaskLifecycle, TaskProgress
from meldx.app.services import tasks as svc


@pytest.fixture
async def maker(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lifecycle.db")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _activity(maker, task_id) -> list[str]:
    async with maker() as s:
        stmt = (
            select(TaskActivity)
            .where(col(TaskActivity.task_id) == task_id)
            .order_by(col(TaskActivity.occurred_at), col(TaskActivity.id))
        )
        return [row.action for row in (await s.exec(stmt)).all()]


async def test_dual_status_representation(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="journey")
        assert (task.status, task.lifecycle, task.progress) == ("inbox", "open", "ready")
        started = await svc.start_task(s, task_id=str(task.id))
        assert (started.status, started.progress) == ("doing", "doing")
        done = await svc.complete_task(s, task_id=str(task.id))
        assert (done.status, done.lifecycle) == ("done", "done")
        reopened = await svc.reopen_task(s, task_id=str(task.id))
        assert (reopened.status, reopened.lifecycle, reopened.progress) == (
            "inbox",
            "open",
            "ready",
        )
        assert reopened.completed_at is None


async def test_delegate_and_handoff_keep_legacy_planning_marker(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="delegated", delegate_to="hermes-1")
        assert task.status == "today" and task.progress == "ready"
        hand = await svc.handoff_task(s, task_id=str(task.id), to_agent="codex-1")
        assert hand.status == "today" and hand.progress == "ready"


async def test_start_is_idempotent_and_done_tasks_refuse_work(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="focus")
        once = await svc.start_task(s, task_id=str(task.id))
        twice = await svc.start_task(s, task_id=str(task.id))
        assert twice.revision == once.revision  # no-op, no bump
        await svc.complete_task(s, task_id=str(task.id))
        with pytest.raises(svc.TaskStateConflict, match="reopen it first"):
            await svc.start_task(s, task_id=str(task.id))


async def test_claim_cannot_take_a_done_task(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="finished business")
        await svc.complete_task(s, task_id=str(task.id))
    async with maker() as s:
        with pytest.raises(svc.TaskStateConflict, match="reopen it first"):
            await svc.claim_task(s, task_id=str(task.id), agent_id="hermes-1")


async def test_revision_bumps_and_stale_patch_conflicts(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="rev")
        assert task.revision == 1
        bumped = await svc.update_task(s, task_id=str(task.id), title="rev v2")
        assert bumped.revision == 2
        # unchanged values are a no-op, not a revision bump
        same = await svc.update_task(s, task_id=str(task.id), title="rev v2")
        assert same.revision == 2
        with pytest.raises(svc.StaleRevision) as excinfo:
            await svc.update_task(s, task_id=str(task.id), title="rev v3", expected_revision=1)
        assert excinfo.value.task.revision == 2


async def test_patch_null_clears_description_omitted_keeps(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="doc", description="keep me")
        kept = await svc.update_task(s, task_id=str(task.id), description=None)
        assert kept.description == "keep me"
        cleared = await svc.update_task(
            s, task_id=str(task.id), description=None, description_provided=True
        )
        assert cleared.description is None


async def test_complete_idempotent_records_history_once(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="once")
        first = await svc.complete_task(s, task_id=str(task.id))
        again = await svc.complete_task(s, task_id=str(task.id))
        assert again.completed_at == first.completed_at
        assert await _activity(maker, task.id) == ["created", "completed"]


async def test_reopen_preserves_history_and_finishes_again(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="cycle")
        first = await svc.complete_task(s, task_id=str(task.id))
        await svc.reopen_task(s, task_id=str(task.id))
        second = await svc.complete_task(s, task_id=str(task.id))
        assert second.completed_at is not None
        assert second.completed_at >= first.completed_at  # type: ignore[union-attr]
    assert await _activity(maker, task.id) == ["created", "completed", "reopened", "completed"]


async def test_idempotent_create_replays_and_conflicts(maker) -> None:
    async with maker() as s:
        first = await svc.create_task(s, title="retry", idempotency_key="k-1")
        replay = await svc.create_task(s, title="retry", idempotency_key="k-1")
        assert replay.id == first.id
        with pytest.raises(svc.IdempotencyConflict):
            await svc.create_task(s, title="different", idempotency_key="k-1")
        rows = list((await s.exec(select(Task))).all())
        assert len(rows) == 1


async def test_idempotent_replay_returns_the_original_snapshot(maker) -> None:
    async with maker() as s:
        await svc.create_task(s, title="original", idempotency_key="snap")
        task = (await s.exec(select(Task))).first()
        assert task is not None
        await svc.update_task(s, task_id=str(task.id), title="edited later")
    async with maker() as s:
        replay = await svc.create_task(s, title="original", idempotency_key="snap")
        assert replay.title == "original"  # the committed create, not current state


async def test_list_is_bounded_and_stably_ordered(maker) -> None:
    async with maker() as s:
        for i in range(5):
            await svc.create_task(s, title=f"t{i}")
    async with maker() as s:
        page1 = await svc.list_tasks(s, limit=2, offset=0)
        page2 = await svc.list_tasks(s, limit=2, offset=2)
        everything = await svc.list_tasks(s, limit=None)
        assert [t.title for t in page1] == ["t0", "t1"]
        assert [t.title for t in page2] == ["t2", "t3"]
        assert len(everything) == 5


async def test_get_task_missing_keeps_contract(maker) -> None:
    async with maker() as s:
        with pytest.raises(svc.TaskNotFound, match="Task locked or not found"):
            await svc.get_task(s, task_id="00000000-0000-0000-0000-000000000000")
        with pytest.raises(svc.TaskNotFound, match="Task locked or not found"):
            await svc.get_task(s, task_id="not-a-uuid")


def test_enums_are_the_adr_values() -> None:
    assert [e.value for e in TaskLifecycle] == ["open", "done"]
    assert [e.value for e in TaskProgress] == ["ready", "doing"]
