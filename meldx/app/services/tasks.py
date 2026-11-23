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
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app import bus, dates
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
    """Owner-local instant (product plan §4.2): ISO 8601 or natural language;
    a naive value means the OWNER's timezone, not the server's."""
    if value is None:
        return None
    return dates.parse_instant(value)


def parse_due_date(value: str | None) -> date | None:
    if value is None:
        return None
    return dates.parse_date(value)


def _guard_single_deadline(*, due_at: datetime | None, due_date: date | None) -> None:
    """A deadline is an exact instant OR a calendar date, never both."""
    if due_at is not None and due_date is not None:
        raise ValueError(
            "A deadline is either an exact time or a date, not both; clear one first"
        )


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


def _guard_not_archived(task: Task) -> None:
    """Archived rows are read-only until restored (plan §4.3, F09.2)."""
    if task.archived_at is not None:
        raise TaskStateConflict("Task is archived; restore it first")


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
    due_date: str | None = None,
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
    parsed_due_at = parse_due(due_at)
    parsed_due_date = parse_due_date(due_date)
    _guard_single_deadline(due_at=parsed_due_at, due_date=parsed_due_date)
    request_hash: str | None = None
    if idempotency_key is not None:
        request_hash = _request_hash(
            title=title,
            description=description,
            due_at=due_at,
            due_date=due_date,
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
        due_at=parsed_due_at,
        due_date=parsed_due_date,
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
            changed=("title", "description", "priority", "due_at", "due_date"),
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
    q: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    limit: int | None = 50,
    offset: int = 0,
) -> list[Task]:
    """Stable order (created_at, id) with bounded pagination; None = unbounded.

    `q` searches title/description case-insensitively with SQL wildcards
    escaped — user input is never a pattern. Archived rows are excluded
    unless explicitly requested (the archive view is the only ordinary
    consumer that asks for them).
    """
    stmt = select(Task).order_by(col(Task.created_at), col(Task.id))
    if not include_archived:
        stmt = stmt.where(col(Task.archived_at).is_(None))
    if status is not None:
        stmt = stmt.where(col(Task.status) == TaskStatus(status))
    if priority is not None:
        stmt = stmt.where(col(Task.priority) == Priority(priority))
    if q:
        needle = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(
            or_(
                col(Task.title).ilike(f"%{needle}%", escape="\\"),
                col(Task.description).ilike(f"%{needle}%", escape="\\"),
            )
        )
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


def _matches_q(task: Task, q: str) -> bool:
    """In-memory twin of the SQL search for the view paths (personal scale)."""
    needle = q.casefold()
    return needle in (task.title or "").casefold() or needle in (
        task.description or ""
    ).casefold()


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
    _guard_not_archived(task)
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
        .where(col(Task.archived_at).is_(None))
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
        _guard_not_archived(task)
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
    _guard_not_archived(task)
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
    _guard_not_archived(task)
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
    _guard_not_archived(task)
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
    due_at: str | None = None,
    due_date: str | None = None,
    description_provided: bool = False,
    due_at_provided: bool = False,
    due_date_provided: bool = False,
    expected_revision: int | None = None,
) -> Task:
    """Edit with optimistic concurrency and explicit-null handling.

    `*_provided` distinguishes an omitted field (leave unchanged) from an
    explicit null (clear). Deadlines keep their mutual exclusion: an exact
    instant OR a calendar date, never both. `expected_revision` mismatch ->
    409 carrying the current task so the client can reload.
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    _guard_not_archived(task)
    if expected_revision is not None and expected_revision != task.revision:
        raise StaleRevision(task)
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("Title cannot be empty")
    target_due_at = task.due_at
    target_due_date = task.due_date
    if due_at_provided:
        target_due_at = parse_due(due_at)  # explicit null clears
    if due_date_provided:
        target_due_date = parse_due_date(due_date)
    _guard_single_deadline(due_at=target_due_at, due_date=target_due_date)
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
    if target_due_at != task.due_at:
        task.due_at = target_due_at
        changed.append("due_at")
    if target_due_date != task.due_date:
        task.due_date = target_due_date
        changed.append("due_date")
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
    _guard_not_archived(task)
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
    """Hide until an instant. The deadline is PRESERVED (product plan §4.1).

    The pre-v0.0.2 behavior stored the wake-up time in `due_at`, destroying
    the deadline; the migration copies those legacy values into
    `snoozed_until` once, with uncertain provenance (ADR-004). Snoozing into
    the past is rejected.
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    _guard_not_archived(task)
    instant = dates.parse_instant(until)
    if instant <= _now():
        raise ValueError(f"Cannot snooze into the past: {until!r}")
    task.status = TaskStatus.snoozed
    task.snoozed_until = instant
    task.agent_id = None
    task.lease_expires_at = None
    _touch(task)
    session.add(task)
    session.add(
        _record(
            task.id, action="snoozed", changed=("status", "snoozed_until")
        )
    )
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def plan_task(session: AsyncSession, *, task_id: str, date: str | None) -> Task:
    """Set the owner-local planned date, or clear it with None (plan §4.1).

    Planning is independent of deadlines: a planned task needs no due date.
    While the task is open and ready, the legacy status follows the plan —
    planned for today reads as the legacy `today` marker; any other date (or
    none) reads as `inbox`.
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    _guard_not_archived(task)
    planned = parse_due_date(date)
    task.planned_date = planned
    if task.lifecycle is TaskLifecycle.open and task.progress is TaskProgress.ready:
        if planned is not None and planned == dates.today_date():
            task.status = TaskStatus.today
        else:
            task.status = TaskStatus.inbox
    _touch(task)
    session.add(task)
    session.add(_record(task.id, action="planned", changed=("planned_date",)))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def unsnooze_task(session: AsyncSession, *, task_id: str) -> Task:
    """Show a snoozed task again. Idempotent for tasks that are visible.

    The legacy status returns to `today` when the plan says today, else
    `inbox`; the owner replans rather than the service guessing (plan §4.3).
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    _guard_not_archived(task)
    if task.snoozed_until is None:
        return task
    task.snoozed_until = None
    if task.lifecycle is TaskLifecycle.open and task.progress is TaskProgress.ready:
        if task.planned_date is not None and task.planned_date == dates.today_date():
            task.status = TaskStatus.today
        else:
            task.status = TaskStatus.inbox
    _touch(task)
    session.add(task)
    session.add(_record(task.id, action="unsnoozed"))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def archive_task(session: AsyncSession, *, task_id: str) -> Task:
    """Reversible removal from ordinary views (plan §4.3, F09).

    Lifecycle, scheduling, and completion metadata are all preserved; an
    archived row is read-only until restored. Idempotent.
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    if task.archived_at is not None:
        return task
    task.archived_at = _now()
    _touch(task)
    session.add(task)
    session.add(_record(task.id, action="archived"))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


async def restore_task(session: AsyncSession, *, task_id: str) -> Task:
    """Return an archived task to ordinary views, dates untouched (F09.3).

    A restored overdue task shows as overdue; nothing is silently replanned.
    Idempotent for tasks that were never archived.
    """
    task = await _get_or_locked(session, _coerce_id(task_id))
    if task.archived_at is None:
        return task
    task.archived_at = None
    _touch(task)
    session.add(task)
    session.add(_record(task.id, action="restored"))
    await session.commit()
    await session.refresh(task)
    bus.publish("task.updated", task)
    return task


VIEW_NAMES = (
    "today",
    "inbox",
    "upcoming",
    "open",
    "overdue",
    "missed",
    "completed",
    "archive",
)


def _is_live(task: Task) -> bool:
    """Ordinary views never show archived rows (plan §4.3)."""
    return task.archived_at is None


def _snoozed_active(task: Task, now: datetime) -> bool:
    until = _aware(task.snoozed_until)
    return until is not None and until > now


def _planned_today(task: Task, today: date) -> bool:
    return task.planned_date is not None and task.planned_date == today


def _due_today(task: Task, today: date) -> bool:
    return dates.is_due_today(_aware(task.due_at), task.due_date, today)


def _legacy_today_marker(task: Task) -> bool:
    """Pre-v0.0.2 `today` rows carry no calendar date: keep them visible in
    Today for owner review instead of inventing a planned date (ADR-004)."""
    return task.status is TaskStatus.today and task.planned_date is None


def _view_filter(view: str, tasks: list[Task], *, completed_days: int = 1) -> list[Task]:
    """View membership per product plan §4.3. Ordinary views exclude archived
    rows. Snoozed tasks stay hidden until their instant passes — expiry is
    derived at query time, no scheduler needed."""
    now = _now()
    today = dates.today_date()
    _, day_end = dates.local_day_bounds(today)

    if view == "inbox":
        # No planned date and no deadline; the legacy today marker keeps
        # delegated/legacy rows out. Visibility is derived from
        # snoozed_until, so an EXPIRED snooze lands back in Inbox even
        # though the legacy status column still says `snoozed` (plan §4.3).
        return [
            t
            for t in tasks
            if _is_live(t)
            and t.lifecycle is TaskLifecycle.open
            and t.progress is TaskProgress.ready
            and not _snoozed_active(t, now)
            and not _legacy_today_marker(t)
            and t.planned_date is None
            and t.due_date is None
            and _aware(t.due_at) is None
        ]
    if view == "today":
        return [
            t
            for t in tasks
            if _is_live(t)
            and t.lifecycle is TaskLifecycle.open
            and not _snoozed_active(t, now)
            and (_planned_today(t, today) or _due_today(t, today) or _legacy_today_marker(t))
        ]
    if view == "overdue":
        # Snoozed stays discoverable here with its badge (plan §4.3).
        return [
            t
            for t in tasks
            if _is_live(t)
            and t.lifecycle is TaskLifecycle.open
            and dates.is_overdue(_aware(t.due_at), t.due_date)
        ]
    if view == "missed":
        return [
            t
            for t in tasks
            if _is_live(t)
            and t.lifecycle is TaskLifecycle.open
            and t.planned_date is not None
            and t.planned_date < today
        ]
    if view == "upcoming":
        return [
            t
            for t in tasks
            if _is_live(t)
            and t.lifecycle is TaskLifecycle.open
            and (
                (t.planned_date is not None and t.planned_date > today)
                or (t.due_date is not None and t.due_date > today)
                or (_aware(t.due_at) is not None and _aware(t.due_at) >= day_end)  # type: ignore[operator]
            )
        ]
    if view == "open":
        return [t for t in tasks if _is_live(t) and t.lifecycle is TaskLifecycle.open]
    if view == "completed":
        start = dates.local_day_bounds(today - timedelta(days=max(completed_days, 1) - 1))[0]

        def completed_in_range(task: Task) -> bool:
            completed = _aware(task.completed_at)
            return completed is not None and start <= completed

        return [
            t
            for t in tasks
            if _is_live(t) and t.lifecycle is TaskLifecycle.done and completed_in_range(t)
        ]
    if view == "archive":
        return [t for t in tasks if t.archived_at is not None]
    raise ValueError(f"Unknown view: {view!r}")


async def view_tasks(
    session: AsyncSession,
    *,
    view: str,
    completed_days: int = 1,
    q: str | None = None,
    priority: str | None = None,
    limit: int | None = 200,
) -> list[Task]:
    """Explicit view queries with the §4.3 membership rules, optionally
    narrowed by search text and priority. Bounded scan of the personal list;
    the legacy `status`/`today_only` filters keep their old UTC semantics for
    existing consumers (ADR-004 compatibility #7)."""
    if view not in VIEW_NAMES:
        raise ValueError(f"Unknown view: {view!r}")
    tasks = await list_tasks(session, limit=1000, include_archived=True)
    if q:
        tasks = [t for t in tasks if _matches_q(t, q)]
    if priority is not None:
        wanted = Priority(priority)
        tasks = [t for t in tasks if t.priority is wanted]
    filtered = _view_filter(view, tasks, completed_days=completed_days)
    if limit is not None:
        filtered = filtered[:limit]
    return filtered


async def task_activity(
    session: AsyncSession, *, task_id: str, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """Durable history for one task (v0.0.3 exposure of the v0.0.1
    foundation). Stable order; missing ids keep the contract message."""
    tid = _coerce_id(task_id)
    await _get_or_locked(session, tid)  # 404 for unknown tasks, not empty list
    stmt = (
        select(TaskActivity)
        .where(col(TaskActivity.task_id) == tid)
        .order_by(col(TaskActivity.occurred_at), col(TaskActivity.id))
    )
    rows = list((await session.exec(stmt)).all())
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    return {
        "task_id": str(tid),
        "activity": [
            {
                "id": str(row.id),
                "action": row.action,
                "actor_type": row.actor_type.value,
                "actor_id": row.actor_id,
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                "request_id": row.request_id,
                "changed_fields": row.changed_fields,
            }
            for row in rows
        ],
    }


async def today_summary(session: AsyncSession) -> dict[str, Any]:
    """What remains today, and what was finished today (product plan F06).

    Memberships overlap by design; counts are per-view and must not be
    summed as a unique total (F06.4).
    """
    tasks = await list_tasks(session, limit=1000)
    return {
        "date": dates.today_date().isoformat(),
        "today": _view_filter("today", tasks),
        "overdue": _view_filter("overdue", tasks),
        "missed_plans": _view_filter("missed", tasks),
        "completed_today": _view_filter("completed", tasks),
    }


EXPORT_SCHEMA_VERSION = 1

# Fields a `replace`-policy import overwrites on an existing row. Identity
# (id), history, and the audit trail columns stay managed by the service.
_REPLACE_FIELDS = (
    "title",
    "description",
    "priority",
    "status",
    "lifecycle",
    "progress",
    "revision",
    "planned_date",
    "due_date",
    "due_at",
    "snoozed_until",
    "archived_at",
    "agent_id",
    "lease_expires_at",
    "parent_id",
    "created_at",
    "updated_at",
    "completed_at",
)


def _task_from_import(row: dict[str, Any]) -> Task:
    payload = {key: value for key, value in row.items() if key != "activity"}
    return Task.model_validate(payload)


def _activity_from_import(row: dict[str, Any], task_id: UUID) -> TaskActivity:
    payload = dict(row)
    payload["task_id"] = task_id
    payload["actor_type"] = ActorType(payload.get("actor_type", ActorType.owner.value))
    return TaskActivity.model_validate(payload)


async def export_data(session: AsyncSession) -> dict[str, Any]:
    """Full data portability snapshot (plan F10.1): every task — archived
    included — with its durable history and stable IDs. No credentials exist
    in task data; agent identity is a plain id."""
    tasks = await list_tasks(session, limit=1000, include_archived=True)
    exported: list[dict[str, Any]] = []
    for task in tasks:
        history = await task_activity(session, task_id=str(task.id), limit=1000)
        dump = task.model_dump(mode="json")
        dump["activity"] = history["activity"]
        exported.append(dump)
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": _now().isoformat(),
        "tasks": exported,
    }


async def _analyze_import(
    session: AsyncSession, data: dict[str, Any]
) -> dict[str, Any]:
    """Validate an import payload against the database WITHOUT writing.

    Identity is only ever the task id — never a title guess (plan F10.3).
    """
    version = data.get("schema_version")
    if version != EXPORT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported export schema version: {version!r}; expected {EXPORT_SCHEMA_VERSION}"
        )
    rows = data.get("tasks")
    if not isinstance(rows, list):
        # ValueError on purpose: the 400 contract reports payload-shape
        # problems to the client like every other import error.
        raise ValueError("Import payload has no tasks list")  # noqa: TRY004

    prepared: list[tuple[Task, list[TaskActivity]]] = []
    invalid_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            invalid_rows.append({"index": index, "reason": "task entry is not an object"})
            continue
        try:
            task = _task_from_import(row)
            if not (task.title or "").strip():
                raise ValueError("title is empty")
            activity = [
                _activity_from_import(entry, task.id)
                for entry in row.get("activity", [])
                if isinstance(entry, dict)
            ]
        except Exception as exc:  # noqa: BLE001 - reported per row, not raised
            invalid_rows.append({"index": index, "reason": str(exc)})
            continue
        if str(task.id) in seen_ids:
            invalid_rows.append(
                {"index": index, "task_id": str(task.id), "reason": "duplicate id inside the file"}
            )
            continue
        seen_ids.add(str(task.id))
        prepared.append((task, activity))

    new_entries: list[tuple[Task, list[TaskActivity]]] = []
    duplicate_entries: list[tuple[Task, list[TaskActivity], Task]] = []
    for task, activity in prepared:
        existing = await session.get(Task, task.id)
        if existing is None:
            new_entries.append((task, activity))
        else:
            duplicate_entries.append((task, activity, existing))

    missing_parents: list[dict[str, str]] = []
    for task, _ in prepared:
        if task.parent_id is None or str(task.parent_id) in seen_ids:
            continue
        found = await session.get(Task, task.parent_id)
        if found is None:
            missing_parents.append(
                {"task_id": str(task.id), "parent_id": str(task.parent_id)}
            )

    return {
        "valid": not invalid_rows and not missing_parents,
        "schema_version": EXPORT_SCHEMA_VERSION,
        "counts": {
            "tasks_in_file": len(rows),
            "new": len(new_entries),
            "duplicates": len(duplicate_entries),
            "invalid": len(invalid_rows),
            "missing_parents": len(missing_parents),
            "activity_rows": sum(len(activity) for _, activity in prepared),
        },
        "invalid_rows": invalid_rows,
        "missing_parents": missing_parents,
        "duplicates": [
            {
                "task_id": str(task.id),
                "existing_revision": existing.revision,
                "incoming_revision": task.revision,
            }
            for task, _, existing in duplicate_entries
        ],
        "_new": new_entries,
        "_duplicates": duplicate_entries,
    }


async def import_preview(session: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    """Validate and report; writes nothing (plan F10.2)."""
    report = await _analyze_import(session, data)
    return {key: value for key, value in report.items() if not key.startswith("_")}


async def import_data(
    session: AsyncSession, data: dict[str, Any], *, policy: str = "skip"
) -> dict[str, Any]:
    """Apply an import transactionally (plan F10.2-10.4).

    Default duplicate policy is skip; replace is an explicit choice and
    respects revision checks. Identity is the task id only. On any problem
    the whole import writes nothing.
    """
    if policy not in ("skip", "replace"):
        raise ValueError("policy must be 'skip' or 'replace'")
    analysis = await _analyze_import(session, data)
    if not analysis["valid"]:
        raise ValueError(
            "Import file is invalid; nothing was written. Run import preview for the report"
        )

    # Stub agents without committing — one transaction for the whole import.
    imported_tasks = [task for task, _ in analysis["_new"]]
    imported_tasks += [task for task, _, _ in analysis["_duplicates"]]
    agent_ids = sorted({task.agent_id for task in imported_tasks if task.agent_id})
    for agent_id in agent_ids:
        if await session.get(Agent, agent_id) is None:
            prefix = agent_id.split("-")[0] if "-" in agent_id else agent_id
            try:
                kind = AgentType(prefix)
            except ValueError:
                kind = AgentType.chatgpt
            session.add(Agent(id=agent_id, type=kind))

    created = replaced = skipped = 0
    for task, activity in analysis["_new"]:
        session.add(task)
        for row in activity:
            session.add(row)
        created += 1

    for task, activity, existing in analysis["_duplicates"]:
        if policy == "skip":
            skipped += 1
            continue
        if task.revision < existing.revision:
            raise ValueError(
                f"Import aborted: task {task.id} is at revision {existing.revision}, "
                f"newer than the imported {task.revision}; nothing was written"
            )
        for field in _REPLACE_FIELDS:
            setattr(existing, field, getattr(task, field))
        for row in await session.exec(
            select(TaskActivity).where(col(TaskActivity.task_id) == existing.id)
        ):
            await session.delete(row)
        for row in activity:
            session.add(row)
        session.add(existing)
        replaced += 1

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ValueError("Import failed a database constraint; nothing was written") from None
    return {"policy": policy, "created": created, "replaced": replaced, "skipped": skipped}
