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
        transport=ASGITransport(app=create_app(enable_agent_execution=True)),
        base_url="http://localhost",
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


async def test_detail_start_reopen_flow(client: AsyncClient) -> None:
    created = (await client.post("/api/tasks", json={"title": "focus work"})).json()
    tid = created["id"]
    detail = await client.get(f"/api/tasks/{tid}")
    assert detail.status_code == 200
    assert detail.json()["revision"] == 1
    assert detail.json()["lifecycle"] == "open"
    assert detail.json()["progress"] == "ready"
    missing = await client.get("/api/tasks/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Task locked or not found"}

    started = await client.post(f"/api/tasks/{tid}/start")
    assert started.json()["progress"] == "doing"
    assert started.json()["status"] == "doing"
    again = await client.post(f"/api/tasks/{tid}/start")
    assert again.json()["revision"] == started.json()["revision"]  # idempotent

    done = await client.post(f"/api/tasks/{tid}/complete")
    assert done.json()["lifecycle"] == "done"
    refused = await client.post(f"/api/tasks/{tid}/start")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Task is done; reopen it first"
    reopened = await client.post(f"/api/tasks/{tid}/reopen")
    assert reopened.json()["lifecycle"] == "open"
    assert reopened.json()["completed_at"] is None
    assert reopened.json()["revision"] > done.json()["revision"]


async def test_patch_revision_and_null_semantics(client: AsyncClient) -> None:
    created = await client.post("/api/tasks", json={"title": "doc", "description": "v1"})
    tid = created.json()["id"]
    cleared = await client.patch(f"/api/tasks/{tid}", json={"description": None})
    assert cleared.json()["description"] is None
    patched = await client.patch(f"/api/tasks/{tid}", json={"title": "doc v2"})
    assert patched.json()["description"] is None  # omitted keeps the cleared value
    assert patched.json()["revision"] == 3  # create=1, clear=2, rename=3

    assert (await client.patch(f"/api/tasks/{tid}", json={"title": None})).status_code == 422
    assert (await client.patch(f"/api/tasks/{tid}", json={"priority": None})).status_code == 422

    stale = await client.patch(
        f"/api/tasks/{tid}", json={"title": "stale edit", "expected_revision": 1}
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["detail"] == "Stale revision: task is at revision 3"
    assert body["current_task"]["title"] == "doc v2"
    # reapply with the fresh revision succeeds
    ok = await client.patch(
        f"/api/tasks/{tid}", json={"title": "stale edit", "expected_revision": 3}
    )
    assert ok.json()["title"] == "stale edit"


async def test_idempotency_key_replay_and_conflict(client: AsyncClient) -> None:
    headers = {"Idempotency-Key": "cli-retry-1"}
    first = await client.post("/api/tasks", json={"title": "retry me"}, headers=headers)
    assert first.status_code == 201
    replay = await client.post("/api/tasks", json={"title": "retry me"}, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    conflict = await client.post("/api/tasks", json={"title": "different"}, headers=headers)
    assert conflict.status_code == 409
    assert "already used with a different payload" in conflict.json()["detail"]
    listing = (await client.get("/api/tasks")).json()
    assert [t["title"] for t in listing] == ["retry me"]


async def test_list_is_bounded(client: AsyncClient) -> None:
    for i in range(3):
        await client.post("/api/tasks", json={"title": f"page {i}"})
    page = (await client.get("/api/tasks?limit=2&offset=1")).json()
    assert [t["title"] for t in page] == ["page 1", "page 2"]
    assert (await client.get("/api/tasks?limit=0")).status_code == 422
    assert (await client.get("/api/tasks?limit=201")).status_code == 422


async def test_title_is_trimmed_and_whitespace_only_fails(client: AsyncClient) -> None:
    blank = await client.post("/api/tasks", json={"title": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "Title cannot be empty"
    trimmed = await client.post("/api/tasks", json={"title": "  padded  "})
    assert trimmed.json()["title"] == "padded"
    tid = trimmed.json()["id"]
    blank_edit = await client.patch(f"/api/tasks/{tid}", json={"title": "   "})
    assert blank_edit.status_code == 400
    assert blank_edit.json()["detail"] == "Title cannot be empty"


async def test_plan_unsnooze_views_and_summary(client: AsyncClient) -> None:
    tid = (await client.post("/api/tasks", json={"title": "plan via REST"})).json()["id"]
    planned = await client.post(f"/api/tasks/{tid}/plan", json={"date": "today"})
    assert planned.status_code == 200
    assert planned.json()["planned_date"] is not None
    assert planned.json()["status"] == "today"
    cleared = await client.post(f"/api/tasks/{tid}/plan", json={"date": None})
    assert cleared.json()["planned_date"] is None and cleared.json()["status"] == "inbox"

    snoozed = await client.post(f"/api/tasks/{tid}/snooze", json={"until": "tomorrow 9am"})
    assert snoozed.json()["snoozed_until"] is not None
    assert snoozed.json()["due_at"] is None  # deadline preserved (was none)
    assert (await client.get("/api/tasks?view=inbox")).json() == []
    awake = await client.post(f"/api/tasks/{tid}/unsnooze")
    assert awake.json()["snoozed_until"] is None and awake.json()["status"] == "inbox"

    # Explicit views with the owner-local semantics.
    assert [t["id"] for t in (await client.get("/api/tasks?view=inbox")).json()] == [tid]
    assert (await client.get("/api/tasks?view=somewhere")).status_code == 400

    # Static path registered before /{task_id}: summary answers, not a 404.
    summary = await client.get("/api/tasks/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["date"]
    assert [t["id"] for t in body["today"]] == []
    assert body["overdue"] == [] and body["missed_plans"] == []
    assert [t["id"] for t in body["completed_today"]] == []


async def test_settings_reports_timezone_and_capabilities(client: AsyncClient) -> None:
    settings = await client.get("/api/settings")
    assert settings.status_code == 200
    body = settings.json()
    assert body["timezone"] == "Asia/Kolkata"
    assert body["lease_minutes"] == 30
    assert body["capabilities"] == {"mcp": False, "agent_execution": True}


async def test_deadline_editing_and_mutual_exclusion(client: AsyncClient) -> None:
    tid = (await client.post("/api/tasks", json={"title": "deadline edit"})).json()["id"]
    by_date = await client.patch(f"/api/tasks/{tid}", json={"due_date": "2026-12-01"})
    assert by_date.json()["due_date"] == "2026-12-01"
    assert by_date.json()["due_at"] is None
    conflict = await client.patch(f"/api/tasks/{tid}", json={"due_at": "2026-12-01T09:00:00Z"})
    assert conflict.status_code == 400
    assert "not both" in conflict.json()["detail"]
    # switch representation: clear first, then set
    cleared = await client.patch(f"/api/tasks/{tid}", json={"due_date": None})
    assert cleared.json()["due_date"] is None
    switched = await client.patch(f"/api/tasks/{tid}", json={"due_at": "tomorrow 9am"})
    assert switched.json()["due_at"] is not None
    # create rejects both at once
    both = await client.post(
        "/api/tasks", json={"title": "both", "due_at": "2026-12-01T09:00:00Z", "due_date": "2026-12-01"}
    )
    assert both.status_code == 400
    assert "not both" in both.json()["detail"]
    # date-only create works
    dated = await client.post("/api/tasks", json={"title": "dated", "due_date": "2026-12-24"})
    assert dated.json()["due_date"] == "2026-12-24"


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
    assert "later task" not in page.text  # snoozed stays hidden
    assert 'data-group="inbox"' in page.text and 'data-group="today"' in page.text
    assert 'data-group="doing"' in page.text
    assert "hermes-1" in page.text and "Details</p>" in page.text
    assert ">Start<" in page.text  # personal start on ready cards
    assert 'sse-connect="/api/events"' in page.text
    assert "sse:task.updated" in page.text
    # F05: recent completions are visible with Undo
    assert 'data-group="done"' in page.text
    assert "finished task" in page.text and ">Undo<" in page.text
    claimed = await client.post(f"/tasks/{first['id']}/claim", data={"agent_id": "codex-1"})
    assert claimed.status_code == 200
    assert "data-lease=" in claimed.text
    conflict = await client.post(f"/tasks/{first['id']}/claim", data={"agent_id": "hermes-1"})
    assert conflict.status_code == 409
    done = await client.post(f"/tasks/{delegated['id']}/complete")
    assert "delegated" in done.text and 'data-group="done"' in done.text
    added = await client.post("/tasks", data={"title": "from web"})
    assert added.status_code == 200 and "from web" in added.text
    fragment = await client.get("/partials/tasks")
    assert fragment.status_code == 200 and "<html" not in fragment.text


async def test_web_start_and_undo(client: AsyncClient) -> None:
    tid = (await client.post("/api/tasks", json={"title": "personal"})).json()["id"]
    started = await client.post(f"/tasks/{tid}/start")
    assert started.status_code == 200
    assert 'data-group="doing"' in started.text and "personal" in started.text
    done = await client.post(f"/tasks/{tid}/complete")
    assert ">Undo<" in done.text
    undone = await client.post(f"/tasks/{tid}/reopen")
    assert 'data-group="inbox"' in undone.text and "personal" in undone.text


async def test_web_detail_edit_and_stale_conflict(client: AsyncClient) -> None:
    tid = (await client.post("/api/tasks", json={"title": "web edit", "description": "first"})).json()["id"]
    page = await client.get(f"/tasks/{tid}")
    assert page.status_code == 200
    assert f'hx-post="/tasks/{tid}/edit"' in page.text
    assert 'name="expected_revision" value="1"' in page.text
    assert "Task not found" not in page.text

    saved = await client.post(
        f"/tasks/{tid}/edit",
        data={"title": "web edit v2", "description": "second", "priority": "high", "expected_revision": 1},
    )
    assert "web edit v2" in saved.text and "second" in saved.text

    # Stale revision: the draft is preserved with a conflict banner and the
    # fresh revision offered for reapply (F03).
    stale = await client.post(
        f"/tasks/{tid}/edit",
        data={"title": "draft title", "description": "draft", "priority": "low", "expected_revision": 1},
    )
    assert "changed elsewhere" in stale.text
    assert "draft title" in stale.text
    assert 'name="expected_revision" value="2"' in stale.text

    # Saving with the fresh revision succeeds.
    ok = await client.post(
        f"/tasks/{tid}/edit",
        data={"title": "draft title", "description": "draft", "priority": "low", "expected_revision": 2},
    )
    assert "draft title" in ok.text and "changed elsewhere" not in ok.text

    missing = await client.get("/tasks/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert "Task not found" in missing.text
    bad = await client.get("/tasks/not-a-uuid")
    assert bad.status_code == 404 and "Task not found" in bad.text
