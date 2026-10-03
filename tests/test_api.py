"""REST and HTML contracts using isolated SQLite; PG locking is tested separately."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from meldx.app import bus, db
from meldx.app.main import create_app
from meldx.app.services import tasks as svc


@pytest.fixture
async def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    monkeypatch.setenv("DATABASE_URL", url)
    db.reset_engine()
    bus.init_bus()
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://localhost"
    ) as instance:
        yield instance
    await db.get_engine().dispose()
    db.reset_engine()
    bus.reset_bus()


async def test_rest_roundtrip_filters_and_events(client: AsyncClient) -> None:
    event_bus = bus.get_bus()
    assert event_bus is not None
    with event_bus.subscribe() as queue:
        response = await client.post(
            "/api/tasks",
            json={
                "title": "REST task",
                "priority": "high",
                "due_at": datetime.now(UTC).isoformat(),
            },
        )
        assert response.status_code == 201
        task = response.json()
        tid = task["id"]
        assert task["status"] == "inbox"
        assert len((await client.get("/api/tasks?today_only=true")).json()) == 1
        assert (await client.get("/api/tasks?status=doing")).json() == []
        claimed = await client.post(f"/api/tasks/{tid}/claim", json={"agent_id": "codex-1"})
        assert claimed.status_code == 200
        assert claimed.json()["lease_expires_at"] is not None
        assert claimed.json()["agent_id"] == "codex-1"
        assert len((await client.get("/api/tasks?status=doing")).json()) == 1
        done = await client.post(f"/api/tasks/{tid}/complete")
        assert done.json()["status"] == "done"
        assert done.json()["agent_id"] is None
        assert done.json()["lease_expires_at"] is None
        assert done.json()["completed_at"] is not None
        assert (await client.get("/api/tasks?today_only=true")).json() == []
        assert [queue.get_nowait()["event"] for _ in range(3)] == [
            "task.created",
            "task.claimed",
            "task.completed",
        ]


async def test_missing_and_leased_messages(client: AsyncClient, caplog) -> None:
    tid = (await client.post("/api/tasks", json={"title": "shared"})).json()["id"]
    await client.post(f"/api/tasks/{tid}/claim", json={"agent_id": "codex-1"})
    response = await client.post(f"/api/tasks/{tid}/claim", json={"agent_id": "hermes-1"})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("Task leased to codex-1 until ")
    assert any(r.task_id == tid and r.agent_id == "hermes-1" for r in caplog.records)
    for missing in ["not-a-uuid", "00000000-0000-0000-0000-000000000000"]:
        for action, payload in [("claim", {"agent_id": "codex-1"}), ("complete", {})]:
            response = await client.post(f"/api/tasks/{missing}/{action}", json=payload)
            assert response.status_code == 404
            assert response.json() == {"detail": "Task locked or not found"}


async def test_locked_maps_to_conflict(client: AsyncClient, monkeypatch) -> None:
    async def locked(*args, **kwargs):
        raise svc.TaskLocked(svc.LOCKED_MSG)

    monkeypatch.setattr(svc, "claim_task", locked)
    response = await client.post("/api/tasks/existing/claim", json={"agent_id": "codex-1"})
    assert response.status_code == 409
    assert response.json() == {"detail": "Task locked or not found"}


async def test_validation_and_agent_upsert(client: AsyncClient) -> None:
    for payload in [{"title": ""}, {"title": "x" * 301}, {"title": "x", "priority": "urgent"}]:
        assert (await client.post("/api/tasks", json=payload)).status_code == 422
    assert (await client.get("/api/tasks?status=bad")).status_code == 422
    assert (
        await client.post("/api/tasks", json={"title": "x", "due_at": "garbage"})
    ).status_code == 400
    agent = {"id": "codex-1", "type": "codex"}
    assert (await client.post("/api/agents", json=agent)).status_code == 200
    agent["status"] = "working"
    assert (await client.post("/api/agents", json=agent)).json()["status"] == "working"
    assert len((await client.get("/api/agents")).json()) == 1


async def test_update_handoff_snooze(client: AsyncClient) -> None:
    tid = (await client.post("/api/tasks", json={"title": "draft"})).json()["id"]
    updated = await client.patch(f"/api/tasks/{tid}", json={"title": "draft v2"})
    assert updated.json()["title"] == "draft v2"
    handoff = await client.post(
        f"/api/tasks/{tid}/handoff",
        json={
            "to_agent": "hermes-1",
            "note": "take over",
        },
    )
    assert handoff.json()["status"] == "today"
    assert "[Handoff to hermes-1]: take over" in handoff.json()["description"]
    snoozed = await client.post(f"/api/tasks/{tid}/snooze", json={"until": "tomorrow 9am"})
    assert snoozed.json()["status"] == "snoozed"
    assert snoozed.json()["agent_id"] is None


async def test_today_groups_actions_and_escaping(client: AsyncClient) -> None:
    first = (await client.post("/api/tasks", json={"title": "<script>alert(1)</script>"})).json()
    delegated = (
        await client.post(
            "/api/tasks",
            json={
                "title": "delegated",
                "delegate_to": "hermes-1",
                "description": "Details",
                "priority": "high",
            },
        )
    ).json()
    completed = (await client.post("/api/tasks", json={"title": "finished task"})).json()
    snoozed = (await client.post("/api/tasks", json={"title": "later task"})).json()
    await client.post(f"/api/tasks/{completed['id']}/complete")
    await client.post(f"/api/tasks/{snoozed['id']}/snooze", json={"until": "tomorrow"})
    page = await client.get("/")
    assert page.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "finished task" not in page.text and "later task" not in page.text
    assert 'data-group="inbox"' in page.text and 'data-group="today"' in page.text
    assert 'data-group="doing"' in page.text
    assert "hermes-1" in page.text and "Details" in page.text
    assert 'sse-connect="/api/events"' in page.text
    assert "sse:task.updated" in page.text
    claimed = await client.post(f"/tasks/{first['id']}/claim", data={"agent_id": "codex-1"})
    assert claimed.status_code == 200
    assert "data-lease=" in claimed.text
    conflict = await client.post(f"/tasks/{first['id']}/claim", data={"agent_id": "hermes-1"})
    assert conflict.status_code == 409
    done = await client.post(f"/tasks/{delegated['id']}/complete")
    assert "delegated" not in done.text
    added = await client.post("/tasks", data={"title": "from web"})
    assert added.status_code == 200 and "from web" in added.text
    fragment = await client.get("/partials/tasks")
    assert fragment.status_code == 200 and "<html" not in fragment.text
