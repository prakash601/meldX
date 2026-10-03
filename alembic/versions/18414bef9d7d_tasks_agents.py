"""tasks agents

Revision ID: 18414bef9d7d
Revises:
Create Date: 2026-10-03 16:39:55.311223

Hand-adjusted: PG enum types are created/dropped explicitly with
checkfirst so downgrade -> upgrade round-trips cleanly (autogenerate
leaves orphaned types behind).
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '18414bef9d7d'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENTTYPE = postgresql.ENUM('opencode', 'hermes', 'codex', 'chatgpt', name='agenttype')
AGENTSTATUS = postgresql.ENUM('idle', 'working', name='agentstatus')
TASKSTATUS = postgresql.ENUM('inbox', 'today', 'doing', 'done', 'snoozed', name='taskstatus')
PRIORITY = postgresql.ENUM('low', 'med', 'high', name='priority')
ENUMS = (AGENTTYPE, AGENTSTATUS, TASKSTATUS, PRIORITY)


def _col(enum: postgresql.ENUM) -> postgresql.ENUM:
    """Column copy that never issues CREATE TYPE itself (upgrade() owns that)."""
    return postgresql.ENUM(*enum.enums, name=enum.name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)
    op.create_table('agents',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('type', _col(AGENTTYPE), nullable=False),
    sa.Column('status', _col(AGENTSTATUS), nullable=False),
    sa.Column('last_seen', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('title', sqlmodel.sql.sqltypes.AutoString(length=300), nullable=False),
    sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('status', _col(TASKSTATUS), nullable=False),
    sa.Column('priority', _col(PRIORITY), nullable=False),
    sa.Column('agent_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('lease_expires_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.Column('due_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('updated_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('completed_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_tasks_lease', 'tasks', ['lease_expires_at'], unique=False)
    op.create_index('ix_tasks_status_due', 'tasks', ['status', 'due_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_tasks_status_due', table_name='tasks')
    op.drop_index('ix_tasks_lease', table_name='tasks')
    op.drop_table('tasks')
    op.drop_table('agents')
    bind = op.get_bind()
    for enum in ENUMS:
        enum.drop(bind, checkfirst=True)
