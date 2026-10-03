"""Async Postgres engine + session factory.

URL comes from DATABASE_URL env (Neon in prod, local docker PG in dev).
Tests never touch it — they monkeypatch to sqlite (see tests/).
"""
import os
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

DEFAULT_URL = "postgresql+asyncpg://meldx:meldx@localhost:5432/meldx"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(database_url(), pool_pre_ping=True)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Test-only hook for URL monkeypatching."""
    global _engine
    if _engine is not None:
        _engine.sync_engine.dispose()
    _engine = None


async def get_session() -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


async def check_db() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
