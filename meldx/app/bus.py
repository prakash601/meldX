"""Bounded, process-local broadcast bus; each SSE connection gets its own queue."""
import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from meldx.app.models import Task

logger = logging.getLogger(__name__)
Event = dict[str, Any]


class EventBus:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[Event]] = set()

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=128)
        self.subscribers.add(queue)
        try:
            yield queue
        finally:
            self.subscribers.discard(queue)

    def publish(self, item: Event) -> None:
        for queue in self.subscribers:
            # Events invalidate the board, so the latest event is sufficient
            # for slow clients. Never block a mutation or grow memory unbounded.
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)


_bus: EventBus | None = None


def init_bus() -> EventBus:
    global _bus
    _bus = EventBus()
    return _bus


def reset_bus() -> None:
    global _bus
    _bus = None


def get_bus() -> EventBus | None:
    return _bus


def publish(event: str, task: Task) -> None:
    item = {
        "event": event,
        "task_id": str(task.id),
        "status": task.status.value,
        "agent_id": task.agent_id,
    }
    logger.info("task mutation", extra=item)
    if _bus is not None:
        _bus.publish(item)
