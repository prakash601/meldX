"""v0.0.3 export/import (product plan F10): transactional, repeatable,
identity-by-id, and verified as a round trip across two separate databases.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.models import Task, TaskActivity
from meldx.app.services import tasks as svc


@pytest.fixture
async def source(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/source.db")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def target(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A SEPARATE database — the round trip must not share state (F10.5)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/target.db")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker: async_sessionmaker[AsyncSession]) -> list[Task]:
    async with maker() as s:
        planned = await svc.create_task(s, title="planned work", priority="high")
        await svc.plan_task(s, task_id=str(planned.id), date="today")
        finished = await svc.create_task(s, title="finished work", due_date="2026-12-01")
        await svc.complete_task(s, task_id=str(finished.id))
        archived = await svc.create_task(s, title="old work")
        await svc.archive_task(s, task_id=str(archived.id))
        return [planned, finished, archived]


async def test_round_trip_preserves_ids_scheduling_and_history(
    source: async_sessionmaker[AsyncSession], target: async_sessionmaker[AsyncSession]
) -> None:
    seeded = await _seed(source)
    async with source() as s:
        exported = await svc.export_data(s)
    assert exported["schema_version"] == 1
    assert len(exported["tasks"]) == 3  # archived included

    async with target() as s:
        preview = await svc.import_preview(s, exported)
        assert preview["valid"] is True
        assert preview["counts"]["new"] == 3
        assert preview["counts"]["duplicates"] == 0
        applied = await svc.import_data(s, exported)
        assert applied == {"policy": "skip", "created": 3, "replaced": 0, "skipped": 0}

        tasks_in = list((await s.exec(select(Task))).all())
        assert {t.id for t in tasks_in} == {t.id for t in seeded}  # stable IDs
        by_title = {t.title: t for t in tasks_in}
        assert by_title["planned work"].planned_date == seeded[0].planned_date
        assert by_title["planned work"].priority.value == "high"
        assert by_title["finished work"].completed_at is not None
        assert by_title["finished work"].due_date == seeded[1].due_date
        assert by_title["old work"].archived_at is not None
        rows = list((await s.exec(select(TaskActivity))).all())
        assert len(rows) == sum(len(t["activity"]) for t in exported["tasks"])

        # repeatable: importing the same file again skips everything
        again = await svc.import_data(s, exported)
        assert again == {"policy": "skip", "created": 0, "replaced": 0, "skipped": 3}
        result = await s.exec(select(Task))
        assert len(list(result.all())) == 3


async def test_replace_policy_overwrites_with_revision_check(
    source: async_sessionmaker[AsyncSession], target: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(source)
    async with source() as s:
        exported = await svc.export_data(s)
    async with target() as s:
        await svc.import_data(s, exported)
        # mutate one exported task to a NEWER title+revision, then replace
        for row in exported["tasks"]:
            if row["title"] == "planned work":
                row["title"] = "planned work v2"
                row["revision"] = row["revision"] + 1
        applied = await svc.import_data(s, exported, policy="replace")
        assert applied["replaced"] == 3
        titles = {t.title for t in (await s.exec(select(Task))).all()}
        assert "planned work v2" in titles

        # a STALE revision refuses to replace and writes nothing
        for row in exported["tasks"]:
            if row["title"] == "planned work v2":
                row["revision"] = 1
        with pytest.raises(ValueError, match="newer than the imported"):
            await svc.import_data(s, exported, policy="replace")


async def test_invalid_import_writes_nothing(
    target: async_sessionmaker[AsyncSession]
) -> None:
    bad = {
        "schema_version": 1,
        "tasks": [{"id": "not-a-uuid", "title": "x"}, {"title": ""}],
    }
    async with target() as s:
        preview = await svc.import_preview(s, bad)
        assert preview["valid"] is False
        assert preview["counts"]["invalid"] == 2
        with pytest.raises(ValueError, match="nothing was written"):
            await svc.import_data(s, bad)
        assert list((await s.exec(select(Task))).all()) == []


async def test_unsupported_schema_version_rejected(
    target: async_sessionmaker[AsyncSession],
) -> None:
    async with target() as s:
        with pytest.raises(ValueError, match="Unsupported export schema version"):
            await svc.import_preview(s, {"schema_version": 99, "tasks": []})
        with pytest.raises(ValueError, match="no tasks list"):
            await svc.import_preview(s, {"schema_version": 1})


async def test_missing_parent_is_reported_not_written(
    target: async_sessionmaker[AsyncSession],
) -> None:
    orphan = {
        "schema_version": 1,
        "tasks": [
            {
                "id": "00000000-0000-0000-0000-0000000000aa",
                "title": "orphan",
                "parent_id": "00000000-0000-0000-0000-0000000000ff",
            }
        ],
    }
    async with target() as s:
        preview = await svc.import_preview(s, orphan)
        assert preview["valid"] is False
        assert preview["missing_parents"] == [
            {
                "task_id": "00000000-0000-0000-0000-0000000000aa",
                "parent_id": "00000000-0000-0000-0000-0000000000ff",
            }
        ]
        with pytest.raises(ValueError, match="nothing was written"):
            await svc.import_data(s, orphan)
        assert list((await s.exec(select(Task))).all()) == []


async def test_agent_references_get_stub_rows(
    source: async_sessionmaker[AsyncSession], target: async_sessionmaker[AsyncSession]
) -> None:
    async with source() as s:
        await svc.create_task(s, title="delegated", delegate_to="hermes-9")
        exported = await svc.export_data(s)
    async with target() as s:
        applied = await svc.import_data(s, exported)
        assert applied["created"] == 1
        task = (await s.exec(select(Task))).first()
        assert task is not None and task.agent_id == "hermes-9"
        from meldx.app.models import Agent

        agent = await s.get(Agent, "hermes-9")
        assert agent is not None  # stub keeps the FK honest
