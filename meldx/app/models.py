"""SQLModel table definitions.

Single source of truth for Postgres tables + API schemas + MCP schemas.
Spec: docs/data-model.md (local-only for now).
"""
import enum
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Index
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


class Task(SQLModel, table=True):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_due", "status", "due_at"),
        Index("ix_tasks_lease", "lease_expires_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    status: TaskStatus = Field(default=TaskStatus.inbox)
    priority: Priority = Field(default=Priority.med)
    agent_id: str | None = Field(default=None, foreign_key="agents.id")
    lease_expires_at: datetime | None = Field(default=None)
    due_at: datetime | None = None
    parent_id: UUID | None = Field(default=None, foreign_key="tasks.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    completed_at: datetime | None = None
