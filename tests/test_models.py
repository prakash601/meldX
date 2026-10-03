"""P2 table/index smoke. Runs on sqlite — never touches DATABASE_URL (no prod writes)."""
import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from meldx.app.models import Agent, Task, TaskStatus


@pytest.mark.asyncio
async def test_tables_columns_indexes() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    def check(sync_conn: object) -> None:
        from sqlalchemy.engine import Connection

        assert isinstance(sync_conn, Connection)
        insp = inspect(sync_conn)
        assert set(insp.get_table_names()) >= {"tasks", "agents"}

        task_cols = {c["name"] for c in insp.get_columns("tasks")}
        assert task_cols >= {
            "id", "title", "description", "status", "priority", "agent_id",
            "lease_expires_at", "due_at", "parent_id",
            "created_at", "updated_at", "completed_at",
        }
        idx_names = {i["name"] for i in insp.get_indexes("tasks")}
        assert "ix_tasks_status_due" in idx_names
        assert "ix_tasks_lease" in idx_names

        fks = {
            (fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in insp.get_foreign_keys("tasks")
        }
        assert ("agents", ("id",)) in fks
        assert ("tasks", ("id",)) in fks

    async with engine.connect() as conn:
        await conn.run_sync(check)
    await engine.dispose()


def test_task_defaults() -> None:
    t = Task(title="smoke")
    assert t.status == TaskStatus.inbox
    assert t.priority == "med"
    assert t.agent_id is None
    assert t.lease_expires_at is None
    assert t.id is not None

    a = Agent(id="opencode-1", type="opencode")
    assert a.status == "idle"
