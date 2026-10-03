"""Async Alembic env. DB URL comes from DATABASE_URL env (Neon prod, docker local)."""
import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from alembic import context
from meldx.app.models import Agent, Task  # noqa: F401 — register tables

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

section = config.get_section(config.config_ini_section, {})
section["sqlalchemy.url"] = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://meldx:meldx@localhost:5432/meldx"
)
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=section["sqlalchemy.url"],
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
