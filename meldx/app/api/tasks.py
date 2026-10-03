"""REST mirror of MCP tools; task rules and SQL live in services/tasks.py."""

from typing import Annotated

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
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def list_tasks(
    session: Session, status: TaskStatus | None = None, today_only: bool = False
) -> list[Task]:
    return await svc.list_tasks(session, status=status, today_only=today_only)


@router.post("", status_code=201)
async def create_task(request: Request, payload: TaskCreate, session: Session) -> Task:
    request.state.agent_id = payload.delegate_to
    return await svc.create_task(session, **payload.model_dump())


@router.post("/{task_id}/claim")
async def claim_task(request: Request, task_id: str, payload: TaskClaim, session: Session) -> Task:
    request.state.agent_id = payload.agent_id
    return await svc.claim_task(session, task_id=task_id, agent_id=payload.agent_id)


@router.post("/{task_id}/complete")
async def complete_task(task_id: str, session: Session) -> Task:
    return await svc.complete_task(session, task_id=task_id)


@router.patch("/{task_id}")
async def update_task(task_id: str, payload: TaskUpdate, session: Session) -> Task:
    return await svc.update_task(session, task_id=task_id, **payload.model_dump())


@router.post("/{task_id}/handoff")
async def handoff_task(
    request: Request, task_id: str, payload: TaskHandoff, session: Session
) -> Task:
    request.state.agent_id = payload.to_agent
    return await svc.handoff_task(session, task_id=task_id, **payload.model_dump())


@router.post("/{task_id}/snooze")
async def snooze_task(task_id: str, payload: TaskSnooze, session: Session) -> Task:
    return await svc.snooze_task(session, task_id=task_id, until=payload.until)
