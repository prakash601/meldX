"""SSE broadcast of committed task mutations. No replay; clients refresh on reconnect."""
import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from meldx.app import bus

router = APIRouter()
PING_SECONDS = 15


async def _gen(request: Request, take: int | None) -> AsyncIterator[dict[str, str]]:
    event_bus = bus.get_bus() or bus.init_bus()
    with event_bus.subscribe() as queue:
        sent = 0
        while not await request.is_disconnected():
            try:
                item = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
            except TimeoutError:
                yield {"event": "ping", "data": "{}"}
                continue
            yield {"event": item["event"], "data": json.dumps(item)}
            sent += 1
            if take is not None and sent >= take:
                return


@router.get("/events")
async def events(request: Request, take: int | None = Query(default=None, ge=1)) -> EventSourceResponse:
    """Stream future events to each subscriber; take=N closes after N task events."""
    return EventSourceResponse(_gen(request, take))
