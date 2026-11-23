"""REST mirror of MCP tools; task rules and SQL live in services/tasks.py."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.db import get_session
from meldx.app.models import (
    Task,
    TaskClaim,
    TaskCreate,
    TaskHandoff,
    TaskPlan,
    TaskSnooze,
    TaskStatus,
    TaskUpdate,
)
from meldx.app.services import tasks as svc

router = APIRouter(prefix="/tasks", tags=["tasks"])
# Agent-execution surface (claim/handoff): registered only when
# ENABLE_AGENT_EXECUTION is set — see main.create_app (product plan §6.1).
execution_router = APIRouter(prefix="/tasks", tags=["tasks"])
Session = Annotated[AsyncSession, Depends(get_session)]

# Documented once and attached to every task route. These mirror the MCP tool
# messages exactly; clients rely on the strings, not just the status codes.
CLIENT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Unparseable due or snooze datetime."},
    404: {"description": "No task with that id. `detail` is `Task locked or not found`."},
    409: {
        "description": (
            "Conflict. `detail` is `Task locked or not found` (row locked by a "
            "concurrent claim), `Task leased to {agent} until {ts}`, `Stale revision: "
            "task is at revision {n}` (with `current_task` to reload), `Task is done; "
            "reopen it first`, or an idempotency-key payload mismatch."
        )
    },
}


@router.get(
    "",
    summary="List tasks",
    description="Stable order (oldest first). `status` filters by exact status and "
    "`today_only` keeps the LEGACY UTC-date semantics for existing consumers. "
    "For the owner-local view semantics use `view`: `inbox`, `today`, `upcoming`, "
    "`open`, `overdue`, `missed`, or `completed` (with `completed_days` window, "
    "default today). `view` takes precedence over `status`/`today_only`. Bounded: "
    "`limit` default 50, max 200, with `offset` paging.",
)
async def list_tasks(
    session: Session,
    status: TaskStatus | None = None,
    today_only: bool = False,
    view: str | None = None,
    completed_days: Annotated[int, Query(ge=1, le=366)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Task]:
    if view is not None:
        return await svc.view_tasks(
            session, view=view, completed_days=completed_days, limit=limit
        )
    return await svc.list_tasks(
        session, status=status, today_only=today_only, limit=limit, offset=offset
    )


@router.post(
    "",
    status_code=201,
    summary="Create a task",
    description="Creates an `inbox` task. Supplying `delegate_to` assigns the agent "
    "and starts the task in `today`. `due_at` accepts ISO 8601 or natural language "
    "such as `tomorrow 9am` (owner-local when naive); `due_date` is a date-only "
    "deadline with no invented time. A task has one deadline representation, "
    "never both. Send an `Idempotency-Key` header to retry safely: the same key "
    "and payload replay the same task; the same key with a different payload "
    "returns 409.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def create_task(request: Request, payload: TaskCreate, session: Session) -> Task:
    request.state.agent_id = payload.delegate_to
    key = request.headers.get("idempotency-key") or None
    return await svc.create_task(session, **payload.model_dump(), idempotency_key=key)


@router.get(
    "/summary",
    summary="Today summary",
    description="What remains and what was finished, in the owner's timezone: "
    "separate `today`, `overdue`, `missed_plans`, and `completed_today` task "
    "lists with the owner date. Memberships overlap by design; counts are "
    "per-view and must not be summed as a unique total (plan F06).",
)
async def today_summary(session: Session) -> dict[str, Any]:
    return await svc.today_summary(session)


@router.get(
    "/{task_id}",
    summary="Fetch a task",
    description="The exact task including `revision`, `lifecycle`, and `progress`. "
    "Missing or invalid ids use the shared task contract message.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def get_task(task_id: str, session: Session) -> Task:
    return await svc.get_task(session, task_id=task_id)


@router.post(
    "/{task_id}/start",
    summary="Start a task",
    description="Personal doing: sets `progress=doing` with no agent identity and no "
    "lease. Idempotent. A done task returns 409 with `Task is done; reopen it first`.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def start_task(task_id: str, session: Session) -> Task:
    return await svc.start_task(session, task_id=task_id)


@router.post(
    "/{task_id}/plan",
    summary="Plan a task",
    description="Set the owner-local planned date (`date`: ISO or natural language "
    "such as `today`, `tomorrow`, `friday`), or send `null` to clear. Planning is "
    "independent of deadlines — a planned task needs no due date. Planning for "
    "today moves an inbox task into Today.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def plan_task(task_id: str, payload: TaskPlan, session: Session) -> Task:
    return await svc.plan_task(session, task_id=task_id, date=payload.date)


@router.post(
    "/{task_id}/unsnooze",
    summary="Show a snoozed task again",
    description="Clears `snoozed_until`. The task returns to `today` when its plan "
    "says today, else `inbox`. Idempotent for tasks that are already visible.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def unsnooze_task(task_id: str, session: Session) -> Task:
    return await svc.unsnooze_task(session, task_id=task_id)


@router.post(
    "/{task_id}/reopen",
    summary="Reopen a task",
    description="Returns a done task to open/ready and clears `completed_at`; the "
    "activity history is preserved. Idempotent for tasks that are already open.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def reopen_task(task_id: str, session: Session) -> Task:
    return await svc.reopen_task(session, task_id=task_id)


@execution_router.post(
    "/{task_id}/claim",
    summary="Claim a task",
    description="Takes an exclusive lease for `agent_id` and sets `status=doing`. "
    "Returns 409 if another agent holds an unexpired lease, or if a concurrent "
    "claim holds the row lock. Re-claiming an expired lease succeeds.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def claim_task(request: Request, task_id: str, payload: TaskClaim, session: Session) -> Task:
    request.state.agent_id = payload.agent_id
    return await svc.claim_task(session, task_id=task_id, agent_id=payload.agent_id)


@router.post(
    "/{task_id}/complete",
    summary="Complete a task",
    description="Sets `status=done`, stamps `completed_at`, and clears `agent_id` and the lease.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def complete_task(task_id: str, session: Session) -> Task:
    return await svc.complete_task(session, task_id=task_id)


@router.patch(
    "/{task_id}",
    summary="Edit a task",
    description="Updates `title`, `description`, `priority`, or the deadline. An "
    "explicit `null` clears a nullable field. `due_at` (exact instant) and "
    "`due_date` (date-only) are mutually exclusive — clear one before setting the "
    "other. Send `expected_revision` to detect stale edits: a mismatch returns 409 "
    "with the current task as `current_task` so the client can reload and reapply.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def update_task(task_id: str, payload: TaskUpdate, session: Session) -> Task:
    fields = payload.model_fields_set
    if "title" in fields and payload.title is None:
        raise HTTPException(status_code=422, detail="title cannot be null")
    if "priority" in fields and payload.priority is None:
        raise HTTPException(status_code=422, detail="priority cannot be null")
    return await svc.update_task(
        session,
        task_id=task_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        due_at=payload.due_at,
        due_date=payload.due_date,
        description_provided="description" in fields,
        due_at_provided="due_at" in fields,
        due_date_provided="due_date" in fields,
        expected_revision=payload.expected_revision,
    )


@execution_router.post(
    "/{task_id}/handoff",
    summary="Hand off a task",
    description="Reassigns `agent_id` to `to_agent`, returns the task to `today`, "
    "drops the lease, and appends `[Handoff to {to_agent}]: {note}` to the "
    "description when `note` is given.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def handoff_task(
    request: Request, task_id: str, payload: TaskHandoff, session: Session
) -> Task:
    request.state.agent_id = payload.to_agent
    return await svc.handoff_task(session, task_id=task_id, **payload.model_dump())


@router.post(
    "/{task_id}/snooze",
    summary="Snooze a task",
    description="Sets `status=snoozed`, clears the owner and lease, and stores `until` "
    "as `due_at`. Accepts ISO 8601 or natural language such as `tomorrow 9am`.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def snooze_task(task_id: str, payload: TaskSnooze, session: Session) -> Task:
    return await svc.snooze_task(session, task_id=task_id, until=payload.until)
