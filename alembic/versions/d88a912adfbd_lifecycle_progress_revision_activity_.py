"""lifecycle progress revision activity idempotency

Revision ID: d88a912adfbd
Revises: 18414bef9d7d
Create Date: 2026-11-23 23:40:34.502237

Hand-adjusted (ADR-004, following the 18414bef9d7d enum pattern):
- PG enum types are created/dropped explicitly with checkfirst so a
  downgrade -> upgrade round-trip leaves no orphaned types.
- New task columns carry server defaults so the upgrade also runs on a
  populated database; the backfill then maps the legacy status enum per
  ADR-004: done -> lifecycle=done, doing -> progress=doing, and
  inbox|today|snoozed keep the open/ready defaults (legacy planning
  markers are preserved, never invented).
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = 'd88a912adfbd'
down_revision: str | Sequence[str] | None = '18414bef9d7d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASKLIFECYCLE = postgresql.ENUM('open', 'done', name='tasklifecycle')
TASKPROGRESS = postgresql.ENUM('ready', 'doing', name='taskprogress')
ACTORTYPE = postgresql.ENUM('owner', 'agent', 'system', name='actortype')
ENUMS = (TASKLIFECYCLE, TASKPROGRESS, ACTORTYPE)


def _col(enum: postgresql.ENUM) -> postgresql.ENUM:
    """Column copy that never issues CREATE TYPE itself (upgrade() owns that)."""
    return postgresql.ENUM(*enum.enums, name=enum.name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table('idempotency_key',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('client', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
    sa.Column('key', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('request_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('response', sa.JSON(), nullable=True),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client', 'key')
    )
    op.create_index('ix_idempotency_client_key', 'idempotency_key', ['client', 'key'], unique=False)
    op.create_table('task_activity',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('actor_type', _col(ACTORTYPE), nullable=False),
    sa.Column('actor_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('occurred_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('request_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('changed_fields', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_task_activity_task_time', 'task_activity', ['task_id', 'occurred_at'], unique=False)
    op.add_column('tasks', sa.Column('lifecycle', _col(TASKLIFECYCLE), nullable=False, server_default='open'))
    op.add_column('tasks', sa.Column('progress', _col(TASKPROGRESS), nullable=False, server_default='ready'))
    op.add_column('tasks', sa.Column('revision', sa.Integer(), nullable=False, server_default='1'))
    op.create_index('ix_tasks_lifecycle_progress', 'tasks', ['lifecycle', 'progress'], unique=False)

    # ADR-004 backfill from the legacy status column.
    op.execute("UPDATE tasks SET lifecycle = 'done' WHERE status = 'done'")
    op.execute("UPDATE tasks SET progress = 'doing' WHERE status = 'doing'")


def downgrade() -> None:
    op.drop_index('ix_tasks_lifecycle_progress', table_name='tasks')
    op.drop_column('tasks', 'revision')
    op.drop_column('tasks', 'progress')
    op.drop_column('tasks', 'lifecycle')
    op.drop_index('ix_task_activity_task_time', table_name='task_activity')
    op.drop_table('task_activity')
    op.drop_index('ix_idempotency_client_key', table_name='idempotency_key')
    op.drop_table('idempotency_key')
    bind = op.get_bind()
    for enum in ENUMS:
        enum.drop(bind, checkfirst=True)
