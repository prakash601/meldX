"""v0.0.4 backup/restore gate exercise (product plan acceptance): the
runbook scripts work end to end and the isolated restore preserves task and
history counts.

Runs only where a dedicated local Postgres AND the pg client tools exist —
CI's runner has no postgres clients, so it skips there; the local docker
container satisfies everything.
"""

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.services import tasks as svc

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not (
        shutil.which("pg_dump")
        and shutil.which("pg_restore")
        and shutil.which("psql")
        and shutil.which("createdb")
    ),
    reason="postgres client tools not installed",
)


def _createdb(url: str) -> subprocess.CompletedProcess[str]:
    """createdb/dropdb take flags, not libpq URIs (unlike psql/pg_dump)."""
    parsed = urlparse(url)
    return subprocess.run(
        ["createdb", "-h", parsed.hostname or "localhost", "-p", str(parsed.port or 5432),
         "-U", parsed.username or "meldx", parsed.path.lstrip("/")],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PGPASSWORD": parsed.password or ""},
    )


def _dropdb(url: str) -> subprocess.CompletedProcess[str]:
    parsed = urlparse(url)
    return subprocess.run(
        ["dropdb", "--if-exists", "-h", parsed.hostname or "localhost",
         "-p", str(parsed.port or 5432), "-U", parsed.username or "meldx",
         parsed.path.lstrip("/")],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PGPASSWORD": parsed.password or ""},
    )


def _urls(tag: str) -> tuple[str, str]:
    base = os.environ.get("DATABASE_URL_TEST") or (
        "postgresql+asyncpg://meldx:meldx@localhost:5432/meldx_test"
    )
    psql_base = base.replace("postgresql+asyncpg://", "postgresql://")
    # Swap only the database name so user:password@host:port is preserved.
    prefix, _, _ = psql_base.rpartition("/")
    return (f"{prefix}/meldx_backup_{tag}", f"{prefix}/meldx_restore_{tag}")


def test_backup_restore_round_trip_preserves_counts(tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:8]
    source_url, restore_url = _urls(tag)
    async_url = source_url.replace("postgresql://", "postgresql+asyncpg://")

    def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # fixed argv, no shell; callers assert returncodes
            args, capture_output=True, text=True, check=False, env={**os.environ, **(env or {})}
        )

    try:
        made = _createdb(source_url)
        assert made.returncode == 0, made.stderr
        assert (
            run("uv", "run", "alembic", "upgrade", "head", env={"DATABASE_URL": async_url}).returncode
            == 0
        )

        async def seed() -> None:
            engine = create_async_engine(async_url, pool_size=4)
            maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with maker() as s:
                planned = await svc.create_task(s, title="exercised task", priority="high")
                await svc.plan_task(s, task_id=str(planned.id), date="today")
                done = await svc.create_task(s, title="finished task")
                await svc.complete_task(s, task_id=str(done.id))
            await engine.dispose()

        asyncio.run(seed())

        backup = run(
            "sh", str(REPO / "scripts" / "backup.sh"),
            env={"DATABASE_URL": source_url, "BACKUP_DIR": str(tmp_path)},
        )
        assert backup.returncode == 0, backup.stderr
        dump_files = sorted((tmp_path).glob("meldx-*.dump"))
        assert len(dump_files) == 1

        made = _createdb(restore_url)
        assert made.returncode == 0, made.stderr
        restore = run(
            "sh", str(REPO / "scripts" / "restore.sh"), str(dump_files[0]),
            env={"TARGET_URL": restore_url, "DATABASE_URL": source_url},
        )
        assert restore.returncode == 0, restore.stderr
        assert "counts match the source database" in restore.stdout

        for query in ("SELECT count(*) FROM tasks", "SELECT count(*) FROM task_activity"):
            source_count = run("psql", source_url, "-tAc", query)
            restored_count = run("psql", restore_url, "-tAc", query)
            assert restored_count.stdout == source_count.stdout
            assert int(restored_count.stdout) > 0
    finally:
        _dropdb(source_url)
        _dropdb(restore_url)
