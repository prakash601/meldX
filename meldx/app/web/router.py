"""HTML views and HTMX form actions over the shared task services."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.db import get_session
from meldx.app.models import TaskStatus
from meldx.app.services import tasks as svc

router = APIRouter()
# Agent-execution form action (claim): registered only when
# ENABLE_AGENT_EXECUTION is set — see main.create_app (product plan §6.1).
execution_router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]


async def board(request: Request, session: AsyncSession, template: str) -> HTMLResponse:
    tasks = await svc.list_tasks(session)
    groups = {
        status.value: [t for t in tasks if t.status == status]
        for status in (TaskStatus.inbox, TaskStatus.today, TaskStatus.doing)
    }
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "groups": groups,
            "agent_execution": getattr(request.app.state, "agent_execution", False),
        },
    )


@router.get(
    "/",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Today board",
    description="Full HTML page: inbox/today/doing columns, lease countdown, SSE wiring.",
)
async def today(request: Request, session: Session) -> HTMLResponse:
    return await board(request, session, "today.html")


@router.get(
    "/partials/tasks",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Board fragment",
    description="HTML fragment for the board. Used by HTMX on load, refresh, and SSE events.",
)
async def task_board(request: Request, session: Session) -> HTMLResponse:
    return await board(request, session, "partials/task_groups.html")


@router.post(
    "/tasks",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Add task (form)",
    description="Form-encoded `title`. Returns the refreshed board fragment.",
)
async def add_task(
    request: Request, session: Session, title: Annotated[str, Form(min_length=1, max_length=300)]
) -> HTMLResponse:
    await svc.create_task(session, title=title)
    return await board(request, session, "partials/task_groups.html")


@execution_router.post(
    "/tasks/{task_id}/claim",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Claim task (form)",
    description="Form-encoded `agent_id`. Conflict responses carry the same `detail` "
    "strings as the REST API.",
)
async def claim_task(
    request: Request,
    task_id: str,
    session: Session,
    agent_id: Annotated[str, Form(min_length=1)],
) -> HTMLResponse:
    request.state.agent_id = agent_id
    await svc.claim_task(session, task_id=task_id, agent_id=agent_id)
    return await board(request, session, "partials/task_groups.html")


@router.post(
    "/tasks/{task_id}/complete",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Complete task (form)",
    description="Marks the task done and returns the refreshed board fragment.",
)
async def complete_task(request: Request, task_id: str, session: Session) -> HTMLResponse:
    await svc.complete_task(session, task_id=task_id)
    return await board(request, session, "partials/task_groups.html")
