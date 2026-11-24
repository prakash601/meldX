"""SSE broadcast of committed task mutations. No replay; clients refresh on reconnect.

v0.0.4 (plan §6.5): revocation affects EXISTING streams within a bounded
interval — each subscriber re-validates its credential on every ping cycle
(SSE_AUTH_RECHECK_SECONDS, default 15s) and the stream closes when the
session or bearer token is no longer valid.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from meldx.app import auth as authz
from meldx.app import bus

router = APIRouter()
PING_SECONDS = 15


def _recheck_seconds() -> float:
    """Auth re-check cadence for OPEN streams; the keep-alive ping stays on
    its own 15s rhythm so short recheck intervals do not flood pings."""
    try:
        return max(0.05, float(os.environ.get("SSE_AUTH_RECHECK_SECONDS", str(PING_SECONDS))))
    except ValueError:
        return PING_SECONDS


async def _credentials_still_valid(request: Request) -> bool:
    """Revocation/intent check for an OPEN stream (one short DB session)."""
    from meldx.app.db import session_scope

    header = request.headers.get("authorization", "")
    async with session_scope() as db:
        if header.startswith("Bearer "):
            return await authz.verify_bearer_token(db, header[7:].strip()) is not None
        cookie = request.cookies.get(authz.SESSION_COOKIE)
        return await authz.verify_session_token(db, cookie) is not None


async def _gen(request: Request, take: int | None) -> AsyncIterator[dict[str, str]]:
    event_bus = bus.get_bus() or bus.init_bus()
    # Scope-safe: raw-dict Requests (and tests) carry no "app" entry.
    auth_enabled = bool(request.scope.get("app")) and getattr(
        request.app.state, "auth", False
    )
    recheck_every = _recheck_seconds()
    last_ping = time.monotonic()
    with event_bus.subscribe() as queue:
        sent = 0
        while not await request.is_disconnected():
            wait = PING_SECONDS if not auth_enabled else min(PING_SECONDS, recheck_every)
            try:
                item = await asyncio.wait_for(queue.get(), timeout=wait)
            except TimeoutError:
                if time.monotonic() - last_ping >= PING_SECONDS:
                    yield {"event": "ping", "data": "{}"}
                    last_ping = time.monotonic()
                if auth_enabled and not await _credentials_still_valid(request):
                    return  # revoked mid-stream: close within one recheck
                continue
            yield {"event": item["event"], "data": json.dumps(item)}
            sent += 1
            if take is not None and sent >= take:
                return


@router.get(
    "/events",
    tags=["events"],
    summary="Stream task events",
    description=(
        "Server-Sent Events for committed mutations: `task.created`, `task.claimed`, "
        "`task.completed`, `task.updated`. Each `data` payload is a JSON object with "
        "`event`, `task_id`, `status`, and `agent_id`. A `ping` event is sent every "
        "15s to keep the connection alive; with authentication enabled, each ping "
        "also re-validates the session or token, so a revoked credential closes the "
        "stream within one ping cycle. There is no replay, so reconnecting clients "
        "should refetch current state. `take=N` closes the stream after N task events."
    ),
)
async def events(
    request: Request, take: int | None = Query(default=None, ge=1)
) -> EventSourceResponse:
    """Stream future events to each subscriber; take=N closes after N task events."""
    return EventSourceResponse(_gen(request, take))
