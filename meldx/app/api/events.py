"""SSE stream of task events via sse-starlette. No websockets in v1."""
import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from meldx.app import bus

router = APIRouter()

PING_SECONDS = 15


async def _gen(request: Request, take: int | None) -> AsyncIterator[dict[str, str]]:
    q = bus.get_bus() or bus.init_bus()
    sent = 0
    while True:
        if await request.is_disconnected():
            break
        try:
            item = await asyncio.wait_for(q.get(), timeout=PING_SECONDS)
            yield {"event": item["event"], "data": json.dumps(item)}
            sent += 1
            if take is not None and sent >= take:
                break
        except TimeoutError:
            yield {"event": "ping", "data": "{}"}


@router.get("/events")
async def events(request: Request, take: int | None = Query(default=None, ge=1)) -> EventSourceResponse:
    """Infinite SSE stream. take=N closes after N task events (bounded catch-up)."""
    return EventSourceResponse(_gen(request, take))
