"""HTML views and HTMX form actions over the shared task services.

v0.0.2: the board is view-driven (product plan §6.6) — Today (with In
progress, Overdue, and Missed plans sections), Inbox, Upcoming, All open, and
Completed — all derived from the same service view predicates as REST, in
the owner's timezone.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app import dates
from meldx.app.db import get_session
from meldx.app.models import Priority, Task
from meldx.app.services import tasks as svc

router = APIRouter()
# Agent-execution form action (claim): registered only when
# ENABLE_AGENT_EXECUTION is set — see main.create_app (product plan §6.1).
execution_router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]

NAV_VIEWS = ("today", "inbox", "upcoming", "open", "completed", "archive")


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _flag_ids(tasks: list[Task]) -> tuple[set[str], set[str]]:
    """Snoozed/overdue badges per card, computed once in the owner's terms."""
    now = datetime.now(UTC)
    snoozed: set[str] = set()
    overdue: set[str] = set()
    for task in tasks:
        until = _aware_utc(task.snoozed_until)
        if until is not None and until > now:
            snoozed.add(str(task.id))
        if dates.is_overdue(_aware_utc(task.due_at), task.due_date):
            overdue.add(str(task.id))
    return snoozed, overdue


async def board(
    request: Request,
    session: AsyncSession,
    template: str,
    *,
    view: str = "today",
    completed_days: int = 1,
) -> HTMLResponse:
    if view not in NAV_VIEWS:
        view = "today"
    sections: list[tuple[str, list[Task], str]] = []
    if view == "today":
        summary = await svc.today_summary(session)
        in_progress = [
            t for t in await svc.view_tasks(session, view="open") if t.progress.value == "doing"
        ]
        sections = [
            ("In progress", in_progress, "in-progress"),
            ("Today", list(summary["today"]), "today"),
            ("Overdue", list(summary["overdue"]), "overdue"),
            ("Missed plans", list(summary["missed_plans"]), "missed"),
        ]
    elif view == "completed":
        completed = await svc.view_tasks(
            session, view="completed", completed_days=completed_days
        )
        label = "Completed today" if completed_days == 1 else f"Completed (last {completed_days} days)"
        sections = [(label, completed, "completed")]
    elif view == "archive":
        archived = await svc.view_tasks(session, view="archive")
        sections = [("Archive", archived, "archive")]
    else:
        labels = {"inbox": "Inbox", "upcoming": "Upcoming", "open": "All open"}
        sections = [(labels[view], await svc.view_tasks(session, view=view), view)]

    shown = [t for _, tasks, _ in sections for t in tasks]
    snoozed_ids, overdue_ids = _flag_ids(shown)
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "view": view,
            "sections": sections,
            "today": dates.today_date(),
            "completed_days": completed_days,
            "snoozed_ids": snoozed_ids,
            "overdue_ids": overdue_ids,
            "agent_execution": getattr(request.app.state, "agent_execution", False),
        },
    )


@router.get(
    "/",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Today board",
    description="Today's remaining work plus In progress, Overdue, and Missed "
    "plans sections, owner-local dates, lease countdown, SSE wiring.",
)
async def today(request: Request, session: Session) -> HTMLResponse:
    return await board(request, session, "board.html", view="today")


@router.get(
    "/inbox",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Inbox board",
    description="Open, unsnoozed, unplanned, no deadline — the capture queue.",
)
async def inbox(request: Request, session: Session) -> HTMLResponse:
    return await board(request, session, "board.html", view="inbox")


@router.get(
    "/upcoming",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Upcoming board",
    description="Open tasks with future planned dates or deadlines.",
)
async def upcoming(request: Request, session: Session) -> HTMLResponse:
    return await board(request, session, "board.html", view="upcoming")


@router.get(
    "/open",
    response_class=HTMLResponse,
    tags=["web"],
    summary="All open board",
    description="Every open task, including actively snoozed ones (badged).",
)
async def all_open(request: Request, session: Session) -> HTMLResponse:
    return await board(request, session, "board.html", view="open")


@router.get(
    "/completed",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Completed board",
    description="Done tasks filtered by completion time (not deadline), with "
    "Undo. `days=1` is today (owner-local); larger windows reach back.",
)
async def completed(
    request: Request,
    session: Session,
    days: Annotated[int, Query(ge=1, le=366)] = 1,
) -> HTMLResponse:
    return await board(
        request, session, "board.html", view="completed", completed_days=days
    )


@router.get(
    "/archive",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Archive board",
    description="Archived tasks with Restore. Nothing is deleted; metadata is preserved.",
)
async def archive(request: Request, session: Session) -> HTMLResponse:
    return await board(request, session, "board.html", view="archive")


@router.get(
    "/partials/tasks",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Board fragment",
    description="HTML fragment for the requested view. Used by HTMX on load, "
    "refresh, SSE events, and day rollover.",
)
async def task_board(
    request: Request,
    session: Session,
    view: str = "today",
    days: Annotated[int, Query(ge=1, le=366)] = 1,
) -> HTMLResponse:
    return await board(
        request, session, "partials/board.html", view=view, completed_days=days
    )


@router.post(
    "/tasks",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Add task (form)",
    description="Form-encoded `title`. Returns the current view's fragment.",
)
async def add_task(
    request: Request,
    session: Session,
    title: Annotated[str, Form(min_length=1, max_length=300)],
    view: Annotated[str, Form()] = "today",
) -> HTMLResponse:
    await svc.create_task(session, title=title)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/start",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Start task (form)",
    description="Personal doing with no agent identity or lease.",
)
async def start_task(
    request: Request, task_id: str, session: Session, view: Annotated[str, Form()] = "today"
) -> HTMLResponse:
    await svc.start_task(session, task_id=task_id)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/reopen",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Undo completion (form)",
    description="Reopens a done task to open/ready; history is preserved.",
)
async def reopen_task(
    request: Request, task_id: str, session: Session, view: Annotated[str, Form()] = "today"
) -> HTMLResponse:
    await svc.reopen_task(session, task_id=task_id)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/plan",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Plan task (form)",
    description="Form-encoded `date` (`today`, `tomorrow`, or ISO). An empty "
    "date clears the plan. Planning never touches deadlines.",
)
async def plan_task(
    request: Request,
    task_id: str,
    session: Session,
    date: Annotated[str | None, Form()] = None,
    view: Annotated[str, Form()] = "today",
) -> HTMLResponse:
    await svc.plan_task(session, task_id=task_id, date=date or None)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/snooze",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Snooze task (form)",
    description="Form-encoded `until` (ISO or `YYYY-MM-DDTHH:MM`, owner-local "
    "when naive). Hides the task until that instant; the deadline is kept. "
    "Conflict responses carry the same `detail` strings as the REST API.",
)
async def snooze_task(
    request: Request,
    task_id: str,
    session: Session,
    until: Annotated[str, Form(min_length=1)],
    view: Annotated[str, Form()] = "today",
) -> HTMLResponse:
    await svc.snooze_task(session, task_id=task_id, until=until)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/unsnooze",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Unsnooze task (form)",
    description="Shows a snoozed task again.",
)
async def unsnooze_task(
    request: Request, task_id: str, session: Session, view: Annotated[str, Form()] = "today"
) -> HTMLResponse:
    await svc.unsnooze_task(session, task_id=task_id)
    return await board(request, session, "partials/board.html", view=view)


@router.get(
    "/tasks/{task_id}",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Task detail page",
    description="One task with an edit form (revision-guarded) and lifecycle metadata.",
)
async def task_detail(request: Request, task_id: str, session: Session) -> HTMLResponse:
    try:
        task = await svc.get_task(session, task_id=task_id)
    except ValueError:
        return templates.TemplateResponse(
            request=request, name="missing.html", status_code=404, context={}
        )
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "task": task,
            "view": "detail",
            "agent_execution": getattr(request.app.state, "agent_execution", False),
        },
    )


@router.post(
    "/tasks/{task_id}/edit",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Edit task (form)",
    description="Revision-guarded edit of title, description, priority, and the "
    "date-only deadline. A stale revision re-renders the form with the draft "
    "preserved and a conflict banner offering reload or reapply.",
)
async def edit_task(
    request: Request,
    task_id: str,
    session: Session,
    title: Annotated[str, Form(min_length=1, max_length=300)],
    description: Annotated[str | None, Form()] = None,
    priority: Annotated[Priority, Form()] = Priority.med,
    due_date: Annotated[str | None, Form()] = None,
    expected_revision: Annotated[int, Form(ge=1)] = 1,
) -> HTMLResponse:
    # Form encoding has no explicit-null: an empty field means clear.
    try:
        task = await svc.update_task(
            session,
            task_id=task_id,
            title=title,
            description=description if description else None,
            priority=priority.value,
            due_date=due_date if due_date else None,
            description_provided=True,
            due_date_provided=True,
            expected_revision=expected_revision,
        )
    except svc.StaleRevision as conflict:
        return templates.TemplateResponse(
            request=request,
            name="partials/task_detail.html",
            context={
                "task": conflict.task,
                "draft": {
                    "title": title,
                    "description": description,
                    "priority": priority.value,
                    "due_date": due_date,
                },
                "conflict": True,
                "view": "detail",
                "agent_execution": getattr(request.app.state, "agent_execution", False),
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/task_detail.html",
        context={
            "task": task,
            "view": "detail",
            "agent_execution": getattr(request.app.state, "agent_execution", False),
        },
    )


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
    view: Annotated[str, Form()] = "today",
) -> HTMLResponse:
    request.state.agent_id = agent_id
    await svc.claim_task(session, task_id=task_id, agent_id=agent_id)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/archive",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Archive task (form)",
    description="Hides the task from ordinary views; read-only until restored.",
)
async def archive_task(
    request: Request, task_id: str, session: Session, view: Annotated[str, Form()] = "today"
) -> HTMLResponse:
    await svc.archive_task(session, task_id=task_id)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/restore",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Restore task (form)",
    description="Returns an archived task to ordinary views, dates untouched.",
)
async def restore_task(
    request: Request, task_id: str, session: Session, view: Annotated[str, Form()] = "today"
) -> HTMLResponse:
    await svc.restore_task(session, task_id=task_id)
    return await board(request, session, "partials/board.html", view=view)


@router.post(
    "/tasks/{task_id}/complete",
    response_class=HTMLResponse,
    tags=["web"],
    summary="Complete task (form)",
    description="Marks the task done (idempotent) and returns the current view's fragment.",
)
async def complete_task(
    request: Request, task_id: str, session: Session, view: Annotated[str, Form()] = "today"
) -> HTMLResponse:
    await svc.complete_task(session, task_id=task_id)
    return await board(request, session, "partials/board.html", view=view)
