"""SQLModel table definitions.

Single source of truth for Postgres tables + API schemas + MCP schemas.
Spec: docs/data-model.md (local-only for now).
"""

import enum
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, Column, Date, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(str, enum.Enum):
    inbox = "inbox"
    today = "today"
    doing = "doing"
    done = "done"
    snoozed = "snoozed"


class Priority(str, enum.Enum):
    low = "low"
    med = "med"
    high = "high"


class TaskLifecycle(str, enum.Enum):
    """Lifecycle: open or done (ADR-0004). Orthogonal to work progress."""

    open = "open"
    done = "done"


class TaskProgress(str, enum.Enum):
    """Work progress: ready or doing. Personal doing needs no agent or lease."""

    ready = "ready"
    doing = "doing"


class ActorType(str, enum.Enum):
    """Who performed a recorded activity (ADR-0004 task_activity)."""

    owner = "owner"
    agent = "agent"
    system = "system"


class AgentType(str, enum.Enum):
    opencode = "opencode"
    hermes = "hermes"
    codex = "codex"
    chatgpt = "chatgpt"


class AgentStatus(str, enum.Enum):
    idle = "idle"
    working = "working"


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: str = Field(primary_key=True)  # e.g. "opencode-1"
    type: AgentType
    status: AgentStatus = Field(default=AgentStatus.idle)
    last_seen: datetime = Field(default_factory=_utcnow)


class TaskFields(SQLModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    priority: Priority = Field(default=Priority.med)


class TaskCreate(TaskFields):
    due_at: str | None = None
    due_date: str | None = None
    delegate_to: str | None = Field(default=None, min_length=1)


class TaskUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    priority: Priority | None = None
    due_at: str | None = None
    due_date: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class TaskPlan(SQLModel):
    """Set the planned date, or null to clear it (product plan §4.1)."""

    date: str | None = Field(default=None, min_length=1)


class TaskImport(SQLModel):
    """Apply or preview an import (plan F10). `data` is an export document."""

    policy: str = Field(default="skip")  # skip | replace
    data: dict[str, Any]


class TaskClaim(SQLModel):
    agent_id: str = Field(min_length=1)


class TaskHandoff(SQLModel):
    to_agent: str = Field(min_length=1)
    note: str | None = None


class TaskSnooze(SQLModel):
    until: str = Field(min_length=1)


class AgentUpsert(SQLModel):
    id: str = Field(min_length=1)
    type: AgentType
    status: AgentStatus = AgentStatus.idle


class Task(TaskFields, table=True):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_due", "status", "due_at"),
        Index("ix_tasks_lease", "lease_expires_at"),
        Index("ix_tasks_lifecycle_progress", "lifecycle", "progress"),
        Index("ix_tasks_planned_date", "planned_date"),
        Index("ix_tasks_due_date", "due_date"),
        Index("ix_tasks_snoozed_until", "snoozed_until"),
        Index("ix_tasks_completed_at", "completed_at"),
        Index("ix_tasks_archived_at", "archived_at"),
        # A deadline is a date-only value OR an exact instant, never both
        # (product plan §4.2: never silently invent a time for a date).
        CheckConstraint(
            "NOT (due_at IS NOT NULL AND due_date IS NOT NULL)",
            name="ck_tasks_single_deadline",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    status: TaskStatus = Field(default=TaskStatus.inbox)
    # ADR-0004 dual representation: lifecycle/progress are authoritative from
    # v0.0.1; the legacy status column stays in sync via one adapter in
    # services/tasks.py until the aligned v0.0.5+ releases retire it.
    lifecycle: TaskLifecycle = Field(default=TaskLifecycle.open)
    progress: TaskProgress = Field(default=TaskProgress.ready)
    revision: int = Field(default=1)
    # v0.0.2 scheduling (ADR-0004): planned date and date-only deadline are
    # owner-local calendar dates; snooze hides until an instant without
    # touching either deadline representation.
    planned_date: date | None = Field(default=None, sa_column=Column(Date))
    due_date: date | None = Field(default=None, sa_column=Column(Date))
    snoozed_until: datetime | None = Field(default=None)
    # v0.0.3 reversible archive (ADR-004): ordinary views exclude archived
    # rows; restore returns them with all metadata intact.
    archived_at: datetime | None = Field(default=None)
    agent_id: str | None = Field(default=None, foreign_key="agents.id")
    lease_expires_at: datetime | None = Field(default=None)
    due_at: datetime | None = None
    parent_id: UUID | None = Field(default=None, foreign_key="tasks.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    completed_at: datetime | None = None


class TaskActivity(SQLModel, table=True):
    """Durable per-task history (ADR-0004). Committed with the task mutation.

    No titles, descriptions, or credentials — only which fields changed.
    """

    __tablename__ = "task_activity"
    __table_args__ = (Index("ix_task_activity_task_time", "task_id", "occurred_at"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    task_id: UUID = Field(foreign_key="tasks.id")
    action: str  # created|updated|started|completed|reopened|claimed|handed_off|snoozed
    actor_type: ActorType = Field(default=ActorType.owner)
    actor_id: str | None = None
    occurred_at: datetime = Field(default_factory=_utcnow)
    request_id: str | None = None
    changed_fields: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class IdempotencyKey(SQLModel, table=True):
    """Create-retry safety (ADR-0004): same client/key/payload replays the same
    stored response; same key with a different payload is a conflict."""

    __tablename__ = "idempotency_key"
    __table_args__ = (UniqueConstraint("client", "key"), Index("ix_idempotency_client_key", "client", "key"))

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    client: str = Field(default="local", max_length=100)
    key: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(max_length=64)
    response: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)
