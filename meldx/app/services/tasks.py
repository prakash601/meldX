"""Shared task logic. MCP tools AND REST call these — never duplicate SQL.

Error strings are a client contract (see AGENTS.md):
- missing/locked  -> ValueError("Task locked or not found")
- claimed by other -> ValueError("Task leased to {agent} until {ts}")
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import dateparser
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.models import Agent, AgentStatus, AgentType, Priority, Task, TaskStatus

LOCKED_MSG = "Task locked or not found"


def lease_minutes() -> int:
    try:
        return int(os.environ.get("LEASE_MINUTES", "30"))
    except ValueError:
        return 30


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """PG returns tz-aware; sqlite returns naive. Normalize to aware UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def parse_due(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        parsed = dateparser.parse(value)
        if parsed is None:
            raise ValueError(f"Cannot parse due date: {value!r}") from None
        dt = parsed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _coerce_id(task_id: str) -> UUID:
    try:
        return UUID(task_id)
    except (ValueError, AttributeError):
        raise ValueError(LOCKED_MSG) from None


async def _get_or_locked(session: AsyncSession, task_id: UUID) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise ValueError(LOCKED_MSG)
    return task


async def ensure_agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        prefix = agent_id.split("-")[0] if "-" in agent_id else agent_id
        try:
            kind = AgentType(prefix)
        except ValueError:
            kind = AgentType.chatgpt
        agent = Agent(id=agent_id, type=kind, status=AgentStatus.idle)
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
    else:
        agent.last_seen = _now()
        session.add(agent)
        await session.commit()
    return agent


async def create_task(
    session: AsyncSession,
    *,
    title: str,
    description: str | None = None,
    due_at: str | None = None,
    priority: str = "med",
    delegate_to: str | None = None,
) -> Task:
    task = Task(
        title=title,
        description=description,
        priority=Priority(priority),
        due_at=parse_due(due_at),
    )
    if delegate_to is not None:
        await ensure_agent(session, delegate_to)
        task.status = TaskStatus.today
        task.agent_id = delegate_to
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def list_tasks(
    session: AsyncSession, *, status: str | None = None, today_only: bool = False
) -> list[Task]:
    stmt = select(Task).order_by(col(Task.created_at))
    if status is not None:
        stmt = stmt.where(col(Task.status) == TaskStatus(status))
    tasks = list((await session.exec(stmt)).all())
    if today_only:
        today = _now().date()
        tasks = [
            t
            for t in tasks
            if t.status != TaskStatus.done
            and _aware(t.due_at) is not None
            and _aware(t.due_at).date() == today  # type: ignore[union-attr]
        ]
    return tasks


async def claim_task(session: AsyncSession, *, task_id: str, agent_id: str) -> Task:
    """Exclusive claim. FOR UPDATE SKIP LOCKED wins races on PG (no-op on sqlite)."""
    tid = _coerce_id(task_id)
    stmt = select(Task).where(col(Task.id) == tid).with_for_update(skip_locked=True)
    task = (await session.exec(stmt)).first()
    if task is None:
        raise ValueError(LOCKED_MSG)
    now = _now()
    lease = _aware(task.lease_expires_at)
    if lease is not None and lease > now and task.agent_id != agent_id:
        raise ValueError(f"Task leased to {task.agent_id} until {lease.isoformat()}")
    await ensure_agent(session, agent_id)
    task.status = TaskStatus.doing
    task.agent_id = agent_id
    task.lease_expires_at = now + timedelta(minutes=lease_minutes())
    task.updated_at = now
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def complete_task(session: AsyncSession, *, task_id: str) -> Task:
    task = await _get_or_locked(session, _coerce_id(task_id))
    now = _now()
    task.status = TaskStatus.done
    task.agent_id = None
    task.lease_expires_at = None
    task.updated_at = now
    task.completed_at = now
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def update_task(
    session: AsyncSession,
    *,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
) -> Task:
    task = await _get_or_locked(session, _coerce_id(task_id))
    if title is not None:
        task.title = title
    if description is not None:
        task.description = description
    if priority is not None:
        task.priority = Priority(priority)
    task.updated_at = _now()
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def handoff_task(
    session: AsyncSession, *, task_id: str, to_agent: str, note: str | None = None
) -> Task:
    task = await _get_or_locked(session, _coerce_id(task_id))
    await ensure_agent(session, to_agent)
    task.agent_id = to_agent
    task.status = TaskStatus.today
    task.lease_expires_at = None
    if note:
        task.description = (task.description or "") + f"\n\n[Handoff to {to_agent}]: {note}"
    task.updated_at = _now()
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def snooze_task(session: AsyncSession, *, task_id: str, until: str) -> Task:
    task = await _get_or_locked(session, _coerce_id(task_id))
    task.status = TaskStatus.snoozed
    task.due_at = parse_due(until)
    task.agent_id = None
    task.lease_expires_at = None
    task.updated_at = _now()
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task
