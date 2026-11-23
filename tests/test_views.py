"""v0.0.2 view and planning semantics (product plan §4.1-§4.3, F04-F07).

SQLite-backed like the rest of the fast suite; day-boundary math itself is
covered in test_dates.py.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app import dates
from meldx.app.models import Task
from meldx.app.services import tasks as svc


@pytest.fixture
async def maker(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/views.db")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _mid_today() -> datetime:
    """An instant safely inside today's owner-local day, away from midnight."""
    start, end = dates.local_day_bounds()
    return start + (end - start) / 2


async def test_planned_today_needs_no_deadline(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="plan me")
        planned = await svc.plan_task(s, task_id=str(task.id), date="today")
        assert planned.planned_date == dates.today_date()
        assert planned.due_at is None and planned.due_date is None
        assert planned.status == "today"  # legacy marker follows the plan
        listed = await svc.view_tasks(s, view="today")
        assert [t.id for t in listed] == [task.id]
        # clearing the plan returns it to inbox
        cleared = await svc.plan_task(s, task_id=str(task.id), date=None)
        assert cleared.planned_date is None and cleared.status == "inbox"


async def test_plan_future_date_reads_as_inbox_legacy(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="future work")
        planned = await svc.plan_task(s, task_id=str(task.id), date="tomorrow")
        assert planned.planned_date == dates.today_date() + timedelta(days=1)
        assert planned.status == "inbox"
        assert [t.id for t in await svc.view_tasks(s, view="upcoming")] == [task.id]


async def test_snooze_preserves_the_deadline(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="deadline work", due_date="2026-12-01")
        snoozed = await svc.snooze_task(s, task_id=str(task.id), until="tomorrow 9am")
        assert snoozed.status == "snoozed"
        assert snoozed.due_date is not None and snoozed.due_at is None  # preserved
        assert snoozed.snoozed_until is not None
        # hidden from ordinary views...
        assert [t.id for t in await svc.view_tasks(s, view="today")] == []
        assert [t.id for t in await svc.view_tasks(s, view="inbox")] == []
        # ...but findable in All open, and unsnoozable.
        assert [t.id for t in await svc.view_tasks(s, view="open")] == [task.id]
        awake = await svc.unsnooze_task(s, task_id=str(task.id))
        assert awake.snoozed_until is None and awake.status == "inbox"


async def test_snooze_into_the_past_is_rejected(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="no time travel")
        with pytest.raises(ValueError, match="Cannot snooze into the past"):
            await svc.snooze_task(s, task_id=str(task.id), until="2020-01-01T09:00:00")


async def test_snooze_expiry_needs_no_scheduler(maker) -> None:
    """Visibility is derived at query time; downtime changes nothing."""
    async with maker() as s:
        task = await svc.create_task(s, title="wake me later")
        await svc.snooze_task(s, task_id=str(task.id), until="tomorrow 9am")
    async with maker() as s:
        stored = (await s.exec(select(Task).where(col(Task.id) == task.id))).first()
        assert stored is not None
        stored.snoozed_until = datetime.now(UTC) - timedelta(minutes=1)
        s.add(stored)
        await s.commit()
        assert [t.id for t in await svc.view_tasks(s, view="inbox")] == [task.id]


async def test_overdue_snoozed_work_is_discoverable(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="late and hidden", due_at="2020-01-01T09:00:00Z")
        await svc.snooze_task(s, task_id=str(task.id), until="tomorrow 9am")
        assert [t.id for t in await svc.view_tasks(s, view="overdue")] == [task.id]
        assert [t.id for t in await svc.view_tasks(s, view="today")] == []


async def test_today_memberships(maker) -> None:
    async with maker() as s:
        planned = await svc.create_task(s, title="planned today")
        await svc.plan_task(s, task_id=str(planned.id), date="today")
        due_date = await svc.create_task(s, title="due by date", due_date=dates.today_date().isoformat())
        due_instant = await svc.create_task(s, title="due by time")
        due_instant.due_at = _mid_today()
        s.add(due_instant)
        await s.commit()
        marker = await svc.create_task(s, title="legacy marker", delegate_to="hermes-1")
        assert marker.status == "today"  # pre-v0.0.2 planning marker

        today_ids = {t.id for t in await svc.view_tasks(s, view="today")}
        assert {planned.id, due_date.id, due_instant.id, marker.id} <= today_ids
        # No duplicate membership: each task appears once.
        listed = await svc.view_tasks(s, view="today")
        assert len(listed) == len({t.id for t in listed})
        # None of these are in inbox (they are planned or have deadlines).
        assert [t.id for t in await svc.view_tasks(s, view="inbox")] == []


async def test_missed_plans_are_separate_from_overdue(maker) -> None:
    async with maker() as s:
        missed = await svc.create_task(s, title="planned yesterday, no deadline")
        await svc.plan_task(s, task_id=str(missed.id), date="yesterday")
        overdue = await svc.create_task(s, title="deadline yesterday", due_date="2026-11-01")
        missed_ids = [t.id for t in await svc.view_tasks(s, view="missed")]
        overdue_ids = [t.id for t in await svc.view_tasks(s, view="overdue")]
        assert missed_ids == [missed.id]
        assert overdue_ids == [overdue.id]  # no deadline -> never overdue


async def test_completed_uses_completion_time_not_the_deadline(maker) -> None:
    """Acceptance: a task due next week but finished today counts as completed
    today (F06.3)."""
    async with maker() as s:
        task = await svc.create_task(s, title="finished early", due_date="2026-12-24")
        await svc.complete_task(s, task_id=str(task.id))
        assert [t.id for t in await svc.view_tasks(s, view="completed")] == [task.id]
        assert [t.id for t in await svc.view_tasks(s, view="overdue")] == []


async def test_completed_window_spans_days(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="recently done")
        await svc.complete_task(s, task_id=str(task.id))
        assert [t.id for t in await svc.view_tasks(s, view="completed", completed_days=3)] == [task.id]


async def test_unknown_view_rejects(maker) -> None:
    async with maker() as s:
        with pytest.raises(ValueError, match="Unknown view"):
            await svc.view_tasks(s, view="somewhere")


async def test_deadline_representations_are_mutually_exclusive(maker) -> None:
    async with maker() as s:
        with pytest.raises(ValueError, match="not both"):
            await svc.create_task(s, title="both", due_at="2026-12-01T09:00:00Z", due_date="2026-12-01")
        task = await svc.create_task(s, title="instant only", due_at="2026-12-01T09:00:00Z")
        with pytest.raises(ValueError, match="not both"):
            await svc.update_task(s, task_id=str(task.id), due_date="2026-12-01", due_date_provided=True)
        # switching representation requires clearing the other first
        switched = await svc.update_task(
            s, task_id=str(task.id), due_at=None, due_at_provided=True, due_date="2026-12-01",
            due_date_provided=True,
        )
        assert switched.due_at is None and switched.due_date is not None


async def test_summary_lists_are_separate_and_overlap_is_fine(maker) -> None:
    async with maker() as s:
        remaining = await svc.create_task(s, title="still to do")
        await svc.plan_task(s, task_id=str(remaining.id), date="today")
        finished = await svc.create_task(s, title="done today")
        await svc.complete_task(s, task_id=str(finished.id))
        summary = await svc.today_summary(s)
        assert summary["date"] == dates.today_date().isoformat()
        assert [t.id for t in summary["today"]] == [remaining.id]
        assert summary["overdue"] == []
        assert summary["missed_plans"] == []
        assert [t.id for t in summary["completed_today"]] == [finished.id]
