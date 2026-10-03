"""P3: 7 MCP tools behavior + HTTP mount. sqlite file DB — never touches prod."""
import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from starlette.testclient import TestClient

from meldx.app import db
from meldx.app.main import create_app
from meldx.app.mcp import tools as t


@pytest.fixture
def db_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("DATABASE_URL", url)
    db.reset_engine()

    async def prep() -> None:
        e = create_async_engine(url)
        async with e.begin() as c:
            await c.run_sync(SQLModel.metadata.create_all)
        await e.dispose()

    asyncio.run(prep())
    yield url
    db.reset_engine()


@pytest.mark.asyncio
async def test_roundtrip_create_claim_complete(db_file: str) -> None:
    created = await t.todo_create("write report", description="q3", priority="high")
    assert created["status"] == "inbox"
    assert created["priority"] == "high"
    tid = created["id"]

    listed = await t.todo_list()
    assert [x["id"] for x in listed] == [tid]
    assert await t.todo_list(status="today") == []

    claimed = await t.todo_claim(tid, "opencode-1")
    assert claimed["status"] == "doing"
    assert claimed["agent_id"] == "opencode-1"
    lease = datetime.fromisoformat(claimed["lease_expires_at"])
    delta = (lease - datetime.now(UTC)).total_seconds()
    assert 25 * 60 < delta <= 30 * 60 + 60

    done = await t.todo_complete(tid)
    assert done["status"] == "done"
    assert done["agent_id"] is None
    assert done["lease_expires_at"] is None
    assert done["completed_at"] is not None


@pytest.mark.asyncio
async def test_double_claim_locked(db_file: str) -> None:
    tid = (await t.todo_create("shared"))["id"]
    await t.todo_claim(tid, "opencode-1")
    with pytest.raises(ValueError, match="Task leased to opencode-1 until"):
        await t.todo_claim(tid, "hermes-1")
    # same agent re-claim is idempotent
    again = await t.todo_claim(tid, "opencode-1")
    assert again["agent_id"] == "opencode-1"


@pytest.mark.asyncio
async def test_missing_and_bad_id(db_file: str) -> None:
    with pytest.raises(ValueError, match="Task locked or not found"):
        await t.todo_claim("00000000-0000-0000-0000-000000000000", "opencode-1")
    with pytest.raises(ValueError, match="Task locked or not found"):
        await t.todo_claim("not-a-uuid", "opencode-1")
    with pytest.raises(ValueError, match="Task locked or not found"):
        await t.todo_complete("00000000-0000-0000-0000-000000000000")


@pytest.mark.asyncio
async def test_update_handoff_snooze(db_file: str) -> None:
    tid = (await t.todo_create("draft", delegate_to="hermes-1"))["id"]
    assert (await t.todo_list(status="today"))[0]["agent_id"] == "hermes-1"

    upd = await t.todo_update(tid, title="draft v2", priority="low")
    assert upd["title"] == "draft v2" and upd["priority"] == "low"

    hand = await t.todo_handoff(tid, "codex-1", note="needs deploy")
    assert hand["agent_id"] == "codex-1" and hand["status"] == "today"
    assert "[Handoff to codex-1]: needs deploy" in (hand["description"] or "")

    snoozed = await t.todo_snooze(tid, "tomorrow 9am")
    assert snoozed["status"] == "snoozed"
    assert snoozed["agent_id"] is None
    assert snoozed["due_at"] is not None


@pytest.mark.asyncio
async def test_seven_tools_registered() -> None:
    tools = await t.mcp.list_tools()
    assert sorted(x.name for x in tools) == [
        "todo_claim",
        "todo_complete",
        "todo_create",
        "todo_handoff",
        "todo_list",
        "todo_snooze",
        "todo_update",
    ]


def test_http_mount_lists_tools(db_file: str) -> None:
    """End-to-end Streamable HTTP: initialize -> tools/list -> tools/call."""
    app = create_app()
    transport = ASGITransport(app=app)  # route registered (lifespan covered below)
    assert any(getattr(r, "path", "") in ("/", "") for r in app.routes)

    def sse_names(body: str) -> list[str]:
        for line in body.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                return sorted(x["name"] for x in payload["result"]["tools"])
        raise AssertionError("no tools in SSE body")

    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app, base_url="http://localhost:8000") as c:
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
            headers=headers,
        )
        assert r.status_code == 200
        sid = r.headers["mcp-session-id"]
        h2 = dict(headers, **{"mcp-session-id": sid})
        assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2).status_code == 202

        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h2)
        assert r.status_code == 200
        assert sse_names(r.text) == [
            "todo_claim",
            "todo_complete",
            "todo_create",
            "todo_handoff",
            "todo_list",
            "todo_snooze",
            "todo_update",
        ]

        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "todo_create", "arguments": {"title": "via http"}},
            },
            headers=h2,
        )
        assert r.status_code == 200
        assert "via http" in r.text

    async def check() -> None:
        async with AsyncClient(transport=transport, base_url="http://localhost") as ac:
            assert (await ac.get("/health")).status_code == 200

    asyncio.run(check())
