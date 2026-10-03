"""P4 events: bus unit + HTTP SSE stream. sqlite file DB — never touches prod."""
import asyncio
import json
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from meldx.app import bus, db
from meldx.app.db import session_scope
from meldx.app.main import create_app
from meldx.app.models import Task
from meldx.app.services import tasks as svc


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
    bus.reset_bus()


def test_publish_payload() -> None:
    q = bus.init_bus()
    t = Task(title="x")
    t.status = t.status  # TaskStatus.inbox
    bus.publish("task.created", t)
    item = q.get_nowait()
    assert item["event"] == "task.created"
    assert item["status"] == "inbox"
    assert item["agent_id"] is None
    assert item["task_id"] == str(t.id)
    bus.reset_bus()


@pytest.mark.asyncio
async def test_events_stream(db_file: str) -> None:
    """Bounded catch-up (take=2): the bus buffers, so publish first, then read.

    Full infinite streams can't be asserted over httpx's ASGI transport (it
    buffers the whole body); live-server ping flow was verified manually.
    """
    bus.init_bus()
    async with session_scope() as s:
        created = await svc.create_task(s, title="stream me")
        await svc.claim_task(s, task_id=str(created.id), agent_id="opencode-1")

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as ac:
        r = await asyncio.wait_for(ac.get("/api/events", params={"take": 2}), timeout=20)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    payloads = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")
    ]
    payloads = [p for p in payloads if isinstance(p, dict) and "event" in p]
    assert [p["event"] for p in payloads] == ["task.created", "task.claimed"]
    assert all(p["task_id"] == str(created.id) for p in payloads)
    assert payloads[1]["agent_id"] == "opencode-1"
