"""Shared task logic. MCP tools AND REST call these — never duplicate SQL.

Error strings are a client contract (see AGENTS.md):
- missing/locked  -> ValueError("Task locked or not found")
- claimed by other -> ValueError(f"Task leased to {agent} until {ts}")

ADR-004 (accepted): lifecycle/progress are authoritative from v0.0.1; the legacy
status column is kept in sync by ONE adapter, `_sync_legacy_status`. Every
mutation also bumps `revision` and commits a `task_activity` row in the same
transaction. SSE publishes only after commit.
"""
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import dateparser
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app import bus
from meldx.app.models import (
    ActorType,
    Agent,
    AgentStatus,
    AgentType,
    IdempotencyKey,
    Priority,
    Task,
    TaskActivity,
    TaskLifecycle,
    TaskProgress,
    TaskStatus,
)

LOCKED_MSG = "Task locked or not found"


class TaskNotFound(ValueError):
    """Missing or invalid task ID, retaining the MCP message contract."""


class TaskLocked(ValueError):
    """Existing task is currently locked by another transaction."""


class StaleRevision(ValueError):
    """PATCH expected_revision mismatch. Carries the current task for the 409 body."""

    def __init__(self, task: Task) -> None:
        self.task = task
        super().__init__(f"Stale revision: task is at revision {task.revision}")


class IdempotencyConflict(ValueError):
    """Same idempotency key reused with a different payload."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Idempotency key {key!r} was already used with a different payload")


class TaskStateConflict(ValueError):
    """Lifecycle forbids the action (e.g. claiming or starting a completed task)."""


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
        raise TaskNotFound(LOCKED_MSG) from None


async def _get_or_locked(session: AsyncSession, task_id: UUID) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise TaskNotFound(LOCKED_MSG)
    return task


async def get_task(session: AsyncSession, *, task_id: str) -> Task:
    """Detail lookup. Missing/invalid IDs keep the legacy contract message."""
    return await _get_or_locked(session, _coerce_id(task_id))


def _sync_legacy_status(task: Task) -> None:
    """ADR-004 adapter: the ONE place the legacy status column is derived.

    done/doing are dictated by lifecycle/progress. open+ready keeps whatever
    legacy planning value (inbox/today/snoozed) the planning paths set;
    `reopen` resets it to inbox explicitly because sync is a no-op there.
    """
    if task.lifecycle is TaskLifecycle.done:
        task.status = TaskStatus.done
    elif task.progress is TaskProgress.doing:
        task.status = TaskStatus.doing


def _touch(task: Task) -> None:
    """Every mutation bumps the revision for optimistic concurrency."""
    task.revision += 1
    task.updated_at = _now()


def _record(
    task_id: UUID,
    *,
    action: str,
    actor_type: ActorType = ActorType.owner,
    actor_id: str | None = None,
    changed: tuple[str, ...] = (),
) -> TaskActivity:
    """Activity row for the caller to session.add() in the SAME transaction."""
    return TaskActivity(
        task_id=task_id,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        changed_fields=list(changed),
    )


def _guard_not_done(task: Task) -> None:
    if task.lifecycle is TaskLifecycle.done:
        raise TaskStateConflict("Task is done; reopen it first")


def _request_hash(**fields: Any) -> str:
    blob = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


async def _replay_or_conflict(
    session: AsyncSession, client: str, key: str, request_hash: str
) -> Task | None:
    """Existing key: replay the stored snapshot, or 409 on payload mismatch."""
    stmt = select(IdempotencyKey).where(
        col(IdempotencyKey.client) == client, col(IdempotencyKey.key) == key
    )
    stored = (await session.exec(stmt)).first()
    if stored is None:
        return None
    if stored.request_hash != request_hash:
        raise IdempotencyConflict(key)
    return Task.model_validate(stored.response["task"])


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
    idempotency_client: str = "local",
    idempotency_key: str | None = None,
) -> Task:
    """Create with optional idempotency (ADR-004): same client/key/payload
    replays the stored snapshot; same key with a different payload is a 409.
    Key and task commit atomically."""
    title = title.strip()
    if not title:
        raise ValueError("Title cannot be empty")
    request_hash: str | None = None
    if idempotency_key is not None:
        request_hash = _request_hash(
            title=title,
            description=description,
            due_at=due_at,
            priority=priority,
            delegate_to=delegate_to,
        )
        replayed = await _replay_or_conflict(
            session, idempotency_client, idempotency_key, request_hash
        )
        if replayed is not None:
            return replayed

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
    _sync_legacy_status(task)
    session.add(task)
    session.add(
        _record(
            task.id,
            action="created",
            changed=("title", "description", "priority", "due_at"),
        )
    )
    if idempotency_key is not None and request_hash is not None:
        session.add(
            IdempotencyKey(
                client=idempotency_client,
                key=idempotency_key,
                request_hash=request_hash,
                response={"task": task.model_dump(mode="json")},
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent create with the same key: the loser replays the winner.
        if idempotency_key is None:
            raise
        await session.rollback()
        assert request_hash is not None
        replayed = await _replay_or_conflict(
            session, idempotency_client, idempotency_key, request_hash
        )
        if replayed is None:  # pragma: no cover - unique violation implies a row
            raise
        return replayed
    await session.refresh(task)
    bus.publish("task.created", task)
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    status: str | None = None,
    today_only: bool = False,
    limit: int | None = 50,
    offset: int = 0,
) -> list[Task]:
    """Stable order (created_at, id) with bounded pagination; None = unbounded."""
    stmt = select(Task).order_by(col(Task.created_at), col(Task.id))
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
    if offset:
        tasks = tasks[offset:]
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


async def claim_task(session: AsyncSession, *, task_id: str, agent_id: str) -> Task:
    """Exclusive claim, using whichever primitive the backend actually enforces.

    Agent row is ensured BEFORE the claim: ensure_agent commits, and any commit
    between SELECT FOR UPDATE and the final commit would release the row lock
    early and reopen the race. Completed tasks cannot be claimed (ADR-004).
    """
    tid = _coerce_id(task_id)
    await ensure_agent(session, agent_id)
    now = _now()
    if _dialect_name(session) == "sqlite":
        task = await _claim_atomic(session, tid, agent_id, now)
    else:
        task = await _claim_skip_locked(session, tid, agent_id, now)
    bus.publish("task.claimed", task)
    return task


def _dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


async def _claim_skip_locked(
    session: AsyncSession, tid: UUID, agent_id: str, now: datetime
) -> Task:
    """Postgres path: the row lock makes the read-then-write claim atomic, and
    SKIP LOCKED turns a contended row into an immediate failure instead of a
    wait."""
    stmt = select(Task).where(col(Task.id) == tid).with_for_update(skip_locked=True)
    task = (await session.exec(stmt)).first()
    if task is None:
        if await session.get(Task, tid) is None:
            raise TaskNotFound(LOCKED_MSG)
        raise TaskLocked(LOCKED_MSG)
    _guard_not_done(task)
    _guard_lease(task, agent_id, now)
    task.progress = TaskProgress.doing
    task.agent_id = agent_id
    task.lease_expires_at = now + timedelta(minutes=lease_minutes())
    _touch(task)
    _sync_legacy_status(task)
    session.add(task)
    session.add(_record(
            task.id, action="claimed", actor_type=ActorType.agent, actor_id=agent_id
        ))
    await session.commit()
    await session.refresh(task)
    return task


async def _claim_atomic(
    session: AsyncSession, tid: UUID, agent_id: str, now: datetime
) -> Task:
    """SQLite path.

    SQLite silently ignores SELECT ... FOR UPDATE, so the read-then-write claim
    above lets two claimants both pass the lease check and both win. A single
    conditional UPDATE is atomic there: SQLite serialises writers, so the loser
    re-evaluates the WHERE clause against the committed row and matches nothing.
    """
    stmt = (
        update(Task)
        .where(col(Task.id) == tid)
        .where(col(Task.lifecycle) != TaskLifecycle.done)
        .where(
            or_(
                col(Task.lease_expires_at).is_(None),
                col(Task.lease_expires_at) <= now,
                col(Task.agent_id) == agent_id,
            )
        )
        .values(
            status=TaskStatus.doing,
            progress=TaskProgress.doing,
            agent_id=agent_id,
            lease_expires_at=now + timedelta(minutes=lease_minutes()),
            revision=col(Task.revision) + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.exec(stmt)
    if result.rowcount != 1:
        task = await session.get(Task, tid)
        if task is None:
            raise TaskNotFound(LOCKED_MSG)
        _guard_not_done(task)
        _guard_lease(task, agent_id, now)
        raise TaskLocked(LOCKED_MSG)
    session.add(
        _record(
            tid,
            action="claimed",
            actor_type=ActorType.agent,
            actor_id=agent_id,
        )
    )
    await session.commit()
    session.expire_all()  # expire_on_commit is False, so reload explicitly
    claimed = await session.get(Task, tid)
    if claimed is None:  # pragma: no cover - row existed a statement ago
        raise TaskNotFound(LOCKED_MSG)
    return claimed


def _guard_lease(task: Task, agent_id: str, now: datetime) -> None:
    """Reject a claim when another agent holds an unexpired lease."""
    lease = _aware(task.lease_expires_at)
    if lease is not None and lease > now and task.agent_id != agent_id:
        raise ValueError(f"Task leased to {task.agent_id} until {lease.isoformat()}")


async def complete_task(session: AsyncSession, *, task_id: str) -> Task:
    """Idempotent completion: a repeated request keeps the original timestamp
    and records no duplicate activity or event."""
    task = await _get_or_locked(session, _coerce_id(task_id))
    if task.lifecycle is TaskLifecycle.done:
        return task
    now = _now()
    task.lifecycle = TaskLifecycle.done
    task.agent_id = None
    task.lease_expires_at = None
    task.completed_at = now
    _touch(task)
    _sync_legacy_status(task)
    session.add(task)
    session.add(_record(task.id, action="completed"))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.completed", task)
    return task


async def reopen_task(session: AsyncSession, *, task_id: str) -> Task:
    """Return a done task to open/ready. Idempotent; preserves history.

    The legacy status resets to inbox explicitly: sync is a no-op for open+ready,
    and planning markers are reintroduced as data in v0.0.2 (ADR-004).
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    if task.lifecycle is TaskLifecycle.open:
        return task
    task.lifecycle = TaskLifecycle.open
    task.progress = TaskProgress.ready
    task.completed_at = None
    task.status = TaskStatus.inbox
    _touch(task)
    session.add(task)
    session.add(_record(task.id, action="reopened"))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def start_task(session: AsyncSession, *, task_id: str) -> Task:
    """Personal doing: no agent identity, no lease. Idempotent."""
    task = await _get_or_locked(session, _coerce_id(task_id))
    _guard_not_done(task)
    if task.progress is TaskProgress.doing:
        return task
    task.progress = TaskProgress.doing
    _touch(task)
    _sync_legacy_status(task)
    session.add(task)
    session.add(_record(task.id, action="started"))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def update_task(
    session: AsyncSession,
    *,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    description_provided: bool = False,
    expected_revision: int | None = None,
) -> Task:
    """Edit with optimistic concurrency and explicit-null handling.

    `description_provided` distinguishes an omitted field (leave unchanged)
    from an explicit null (clear). `expected_revision` mismatch -> 409 carrying
    the current task so the client can reload.
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    if expected_revision is not None and expected_revision != task.revision:
        raise StaleRevision(task)
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("Title cannot be empty")
    changed: list[str] = []
    if title is not None and title != task.title:
        task.title = title
        changed.append("title")
    if description_provided and description != task.description:
        task.description = description
        changed.append("description")
    if priority is not None and Priority(priority) is not task.priority:
        task.priority = Priority(priority)
        changed.append("priority")
    if not changed:
        return task
    _touch(task)
    _sync_legacy_status(task)
    session.add(task)
    session.add(_record(task.id, action="updated", changed=tuple(changed)))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def handoff_task(
    session: AsyncSession, *, task_id: str, to_agent: str, note: str | None = None
) -> Task:
    task = await _get_or_locked(session, _coerce_id(task_id))
    await ensure_agent(session, to_agent)
    task.agent_id = to_agent
    task.status = TaskStatus.today
    task.progress = TaskProgress.ready
    task.lease_expires_at = None
    if note:
        task.description = (task.description or "") + f"\n\n[Handoff to {to_agent}]: {note}"
    _touch(task)
    session.add(task)
    session.add(
        _record(
            task.id, action="handed_off", actor_type=ActorType.agent, actor_id=to_agent
        )
    )
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def snooze_task(session: AsyncSession, *, task_id: str, until: str) -> Task:
    task = await _get_or_locked(session, _coerce_id(task_id))
    task.status = TaskStatus.snoozed
    task.due_at = parse_due(until)
    task.agent_id = None
    task.lease_expires_at = None
    _touch(task)
    session.add(task)
    session.add(_record(
            task.id, action="snoozed", changed=("status", "due_at")
        ))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task
