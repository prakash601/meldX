"""In-process event bus. Services publish; /api/events streams. No broker in v1."""
import asyncio
from typing import Any

from meldx.app.models import Task

_bus: asyncio.Queue[dict[str, Any]] | None = None


def init_bus() -> asyncio.Queue[dict[str, Any]]:
    global _bus
    _bus = asyncio.Queue()
    return _bus


def reset_bus() -> None:
    global _bus
    _bus = None


def get_bus() -> asyncio.Queue[dict[str, Any]] | None:
    return _bus


def publish(event: str, task: Task) -> None:
    if _bus is not None:
        _bus.put_nowait(
            {
                "event": event,
                "task_id": str(task.id),
                "status": task.status.value,
                "agent_id": task.agent_id,
            }
        )
