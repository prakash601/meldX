"""v0.0.3 search, filters, and activity exposure (product plan F08)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.services import tasks as svc


@pytest.fixture
async def maker(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/search.db")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_search_matches_title_and_description(maker) -> None:
    async with maker() as s:
        await svc.create_task(s, title="Write API docs", description="covers REST")
        await svc.create_task(s, title="Buy groceries")
        await svc.create_task(s, title="Draft REST proposal", description="for review")
        hits = await svc.list_tasks(s, q="rest")
        assert [t.title for t in hits] == ["Write API docs", "Draft REST proposal"]
        # case-insensitive, and title matches too
        assert [t.title for t in await svc.list_tasks(s, q="GROCERIES")] == ["Buy groceries"]


async def test_search_input_is_never_a_pattern(maker) -> None:
    """% and _ are literal user text, not SQL wildcards (plan F08.3)."""
    async with maker() as s:
        await svc.create_task(s, title="100% done review")
        await svc.create_task(s, title="draft_v2 notes")
        assert [t.title for t in await svc.list_tasks(s, q="100%")] == ["100% done review"]
        # A wildcard would also match "draft v2" style text; literal must not.
        assert [t.title for t in await svc.list_tasks(s, q="draft_")] == ["draft_v2 notes"]


async def test_priority_filter_combines_with_search(maker) -> None:
    async with maker() as s:
        await svc.create_task(s, title="urgent rest work", priority="high")
        await svc.create_task(s, title="rest backlog", priority="low")
        hits = await svc.list_tasks(s, q="rest", priority="high")
        assert [t.title for t in hits] == ["urgent rest work"]


async def test_view_queries_accept_search_and_priority(maker) -> None:
    async with maker() as s:
        first = await svc.create_task(s, title="findable report", priority="high")
        await svc.plan_task(s, task_id=str(first.id), date="today")
        second = await svc.create_task(s, title="other report")
        await svc.plan_task(s, task_id=str(second.id), date="today")
        hits = await svc.view_tasks(s, view="today", q="findable", priority="high")
        assert [t.id for t in hits] == [first.id]


async def test_legacy_list_excludes_archived(maker) -> None:
    async with maker() as s:
        live = await svc.create_task(s, title="live")
        gone = await svc.create_task(s, title="gone")
        await svc.archive_task(s, task_id=str(gone.id))
        assert [t.id for t in await svc.list_tasks(s)] == [live.id]
        assert len(await svc.list_tasks(s, include_archived=True)) == 2


async def test_activity_timeline_is_stable_and_ordered(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="story", description="v1")
        await svc.update_task(s, task_id=str(task.id), title="story v2", description_provided=False)
        await svc.start_task(s, task_id=str(task.id))
        await svc.complete_task(s, task_id=str(task.id))
        history = await svc.task_activity(s, task_id=str(task.id))
        assert history["task_id"] == str(task.id)
        assert [row["action"] for row in history["activity"]] == [
            "created",
            "updated",
            "started",
            "completed",
        ]
        # changed-field names only: no titles/descriptions leak into history
        updated = history["activity"][1]
        assert updated["changed_fields"] == ["title"]
        assert "story" not in str(updated)


async def test_activity_paging_and_missing_task(maker) -> None:
    async with maker() as s:
        task = await svc.create_task(s, title="paged")
        await svc.start_task(s, task_id=str(task.id))
        page = await svc.task_activity(s, task_id=str(task.id), limit=1, offset=1)
        assert [row["action"] for row in page["activity"]] == ["started"]
    async with maker() as s:
        with pytest.raises(svc.TaskNotFound, match="Task locked or not found"):
            await svc.task_activity(s, task_id="00000000-0000-0000-0000-000000000000")
