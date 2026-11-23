"""v0.0.2 planned date date-only deadline snoozed until

Revision ID: e3f3a2d36137
Revises: d88a912adfbd
Create Date: 2026-11-23 23:57:30.800041

Hand-adjusted (ADR-004):
- Alembic autogenerate does not detect CheckConstraints, so the
  due_at/due_date mutual exclusion is added explicitly.
- Legacy backfill: pre-v0.0.2 snooze stored its wake-up time in `due_at`,
  destroying the deadline. Those values move into `snoozed_until` once, and
  `due_at` is cleared — the original deadline is NOT recoverable and must
  not be claimed (provenance uncertain).
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = 'e3f3a2d36137'
down_revision: str | Sequence[str] | None = 'd88a912adfbd'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('planned_date', sa.Date(), nullable=True))
    op.add_column('tasks', sa.Column('due_date', sa.Date(), nullable=True))
    op.add_column('tasks', sa.Column('snoozed_until', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True))
    op.create_index('ix_tasks_completed_at', 'tasks', ['completed_at'], unique=False)
    op.create_index('ix_tasks_due_date', 'tasks', ['due_date'], unique=False)
    op.create_index('ix_tasks_planned_date', 'tasks', ['planned_date'], unique=False)
    op.create_index('ix_tasks_snoozed_until', 'tasks', ['snoozed_until'], unique=False)
    op.create_check_constraint(
        'ck_tasks_single_deadline',
        'tasks',
        'NOT (due_at IS NOT NULL AND due_date IS NOT NULL)',
    )

    # ADR-004 legacy snooze backfill, provenance uncertain by design.
    op.execute("UPDATE tasks SET snoozed_until = due_at, due_at = NULL WHERE status = 'snoozed'")


def downgrade() -> None:
    # Best-effort reverse of the legacy backfill: a still-snoozed task returns
    # its wake-up value to due_at, matching the pre-v0.0.2 representation.
    op.execute("UPDATE tasks SET due_at = snoozed_until WHERE status = 'snoozed' AND snoozed_until IS NOT NULL")
    op.drop_constraint('ck_tasks_single_deadline', 'tasks', type_='check')
    op.drop_index('ix_tasks_snoozed_until', table_name='tasks')
    op.drop_index('ix_tasks_planned_date', table_name='tasks')
    op.drop_index('ix_tasks_due_date', table_name='tasks')
    op.drop_index('ix_tasks_completed_at', table_name='tasks')
    op.drop_column('tasks', 'snoozed_until')
    op.drop_column('tasks', 'due_date')
    op.drop_column('tasks', 'planned_date')
