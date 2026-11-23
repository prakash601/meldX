"""REST mirror of MCP tools; task rules and SQL live in services/tasks.py."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.db import get_session
from meldx.app.models import (
    Task,
    TaskClaim,
    TaskCreate,
    TaskHandoff,
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
            "Locked by a concurrent claim, or leased to another agent. `detail` is "
            "`Task locked or not found` or `Task leased to {agent} until {ts}`."
        )
    },
}


@router.get(
    "",
    summary="List tasks",
    description="Newest last. `status` filters by exact status; `today_only` keeps "
    "unfinished tasks whose `due_at` falls on today's date (UTC).",
)
async def list_tasks(
    session: Session, status: TaskStatus | None = None, today_only: bool = False
) -> list[Task]:
    return await svc.list_tasks(session, status=status, today_only=today_only)


@router.post(
    "",
    status_code=201,
    summary="Create a task",
    description="Creates an `inbox` task. Supplying `delegate_to` assigns the agent "
    "and starts the task in `today`. `due_at` accepts ISO 8601 or natural language "
    "such as `tomorrow 9am`.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def create_task(request: Request, payload: TaskCreate, session: Session) -> Task:
    request.state.agent_id = payload.delegate_to
    return await svc.create_task(session, **payload.model_dump())


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
    summary="Update a task",
    description="Updates `title`, `description`, or `priority`. Omitted fields are "
    "left unchanged; status moves only through claim/complete/handoff/snooze.",
    responses=CLIENT_ERROR_RESPONSES,
)
async def update_task(task_id: str, payload: TaskUpdate, session: Session) -> Task:
    return await svc.update_task(session, task_id=task_id, **payload.model_dump())


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
