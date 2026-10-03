"""Broadcast, subscriber cleanup, bounded queues, and the HTTP SSE contract."""

import asyncio
import json
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from meldx.app import bus
from meldx.app.main import create_app
from meldx.app.models import Task


@pytest.fixture(autouse=True)
def event_bus() -> Iterator[bus.EventBus]:
    instance = bus.init_bus()
    yield instance
    bus.reset_bus()


def test_publish_payload_and_broadcast(event_bus: bus.EventBus) -> None:
    task = Task(title="broadcast")
    with event_bus.subscribe() as first, event_bus.subscribe() as second:
        bus.publish("task.created", task)
        item = first.get_nowait()
        assert second.get_nowait() == item
        assert item == {
            "event": "task.created",
            "task_id": str(task.id),
            "status": "inbox",
            "agent_id": None,
        }
    assert not event_bus.subscribers


def test_slow_subscriber_is_bounded(event_bus: bus.EventBus) -> None:
    with event_bus.subscribe() as slow, event_bus.subscribe() as fast:
        for i in range(200):
            event_bus.publish({"event": "task.updated", "task_id": str(i)})
            assert fast.get_nowait()["task_id"] == str(i)
        assert slow.qsize() == 128
        items = [slow.get_nowait() for _ in range(128)]
        assert items[-1]["task_id"] == "199"
    bus.publish("task.created", Task(title="no subscribers"))
    assert not event_bus.subscribers


@pytest.mark.asyncio
async def test_events_stream(event_bus: bus.EventBus) -> None:
    async def publish_when_connected() -> None:
        while not event_bus.subscribers:
            await asyncio.sleep(0)
        task = Task(title="stream me")
        bus.publish("task.created", task)
        bus.publish("task.claimed", task)

    producer = asyncio.create_task(publish_when_connected())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://localhost"
        ) as client:
            response = await asyncio.wait_for(client.get("/api/events?take=2"), timeout=5)
            assert (await client.get("/api/events?take=0")).status_code == 422
        await producer
    finally:
        producer.cancel()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [item["event"] for item in payloads] == ["task.created", "task.claimed"]
    assert not event_bus.subscribers


@pytest.mark.asyncio
async def test_cancelled_stream_unsubscribes(event_bus: bus.EventBus) -> None:
    from starlette.requests import Request

    from meldx.app.api.events import _gen

    async def receive() -> dict[str, object]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    stream = _gen(Request({"type": "http"}, receive), None)
    pending = asyncio.create_task(anext(stream))
    while not event_bus.subscribers:
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()
    assert not event_bus.subscribers
