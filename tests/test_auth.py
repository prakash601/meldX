"""v0.0.4 owner authentication (product plan §6.5).

The service-first completion gate: unauthorized reads, writes, exports, and
SSE fail; the web login issues expiring revocable sessions; CLI bearer
credentials carry scopes; cookie writes are CSRF-protected; login is
rate-limited.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select

from meldx.app import auth as authz
from meldx.app import bus, db
from meldx.app.db import get_engine, session_scope
from meldx.app.main import create_app
from meldx.app.models import OwnerSession

SECRET = "correct-horse-battery-staple-42"


@pytest.fixture
async def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    url = f"sqlite+aiosqlite:///{tmp_path}/auth.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OWNER_BOOTSTRAP_SECRET", SECRET)
    monkeypatch.setenv("LOGIN_RATE_LIMIT_ATTEMPTS", "3")
    db.reset_engine()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()
    bus.init_bus()
    app = create_app(enable_auth=True, enable_agent_execution=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c
    await get_engine().dispose()
    db.reset_engine()
    bus.reset_bus()


async def _login(client: AsyncClient, secret: str = SECRET):
    return await client.post("/login", data={"secret": secret})


def _csrf(client: AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(authz.CSRF_COOKIE) or ""}


async def test_public_surfaces_stay_public(client: AsyncClient) -> None:
    assert (await client.get("/health")).status_code == 200
    settings = await client.get("/api/settings")
    assert settings.status_code == 200 and settings.json()["timezone"]
    assert (await client.get("/login")).status_code == 200


async def test_unauthorized_reads_writes_exports_and_sse_fail(client: AsyncClient) -> None:
    for method, path, kwargs in (
        ("GET", "/api/tasks", {}),
        ("POST", "/api/tasks", {"json": {"title": "nope"}}),
        ("GET", "/api/export", {}),
        ("POST", "/api/import/preview", {"json": {"policy": "skip", "data": {}}}),
        ("GET", "/api/events", {}),
        ("GET", "/api/tasks/00000000-0000-0000-0000-000000000000/activity", {}),
    ):
        response = await client.request(method, path, **kwargs)
        assert response.status_code == 401, (method, path, response.status_code)
        assert response.json() == {"detail": "Unauthorized"}
    # web pages bounce to login instead of leaking content
    page = await client.get("/")
    assert page.status_code == 303 and page.headers["location"] == "/login"


async def test_login_issues_session_and_web_works(client: AsyncClient) -> None:
    wrong = await _login(client, "wrong-secret")
    assert wrong.status_code == 401 and "not correct" in wrong.text
    ok = await _login(client)
    assert ok.status_code == 303
    page = await client.get("/")
    assert page.status_code == 200 and "Sign out" in page.text
    # the session cookie authorizes the API too
    assert (await client.get("/api/tasks")).status_code == 200


async def test_web_writes_require_csrf_double_submit(client: AsyncClient) -> None:
    await _login(client)
    response = await client.post("/tasks", data={"title": "csrf me", "view": "inbox"})
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token missing or invalid"
    with_csrf = await client.post(
        "/tasks", data={"title": "csrf me", "view": "inbox"}, headers=_csrf(client)
    )
    assert with_csrf.status_code == 200 and "csrf me" in with_csrf.text


async def test_login_rate_limit_locks_after_failures(client: AsyncClient) -> None:
    for _ in range(3):  # fixture sets LOGIN_RATE_LIMIT_ATTEMPTS=3
        assert (await _login(client, "bad")).status_code == 401
    locked = await _login(client)  # even the CORRECT secret is refused now
    assert locked.status_code == 429
    assert "Too many failed attempts" in locked.text


async def test_logout_revokes_the_session(client: AsyncClient) -> None:
    await _login(client)
    assert (await client.get("/")).status_code == 200
    response = await client.post("/logout", headers=_csrf(client))
    assert response.status_code == 303
    after = await client.get("/")
    assert after.status_code == 303 and after.headers["location"] == "/login"
    # the server-side row is gone: the old cookie is worthless
    async with session_scope() as s:
        rows = list((await s.exec(select(OwnerSession))).all())
        assert rows == []


async def test_expired_session_is_invalid(client: AsyncClient) -> None:
    await _login(client)
    async with session_scope() as s:
        row = (await s.exec(select(OwnerSession))).first()
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.add(row)
        await s.commit()
    expired = await client.get("/")
    assert expired.status_code == 303 and expired.headers["location"] == "/login"


async def test_bearer_credentials_and_scopes(client: AsyncClient) -> None:
    async with session_scope() as s:
        read_token, _ = await authz.create_credential(s, name="reader", scopes=["read"])
        write_token, _ = await authz.create_credential(s, name="writer", scopes=["read", "write"])

    read_headers = {"Authorization": f"Bearer {read_token}"}
    write_headers = {"Authorization": f"Bearer {write_token}"}

    assert (await client.get("/api/tasks", headers=read_headers)).status_code == 200
    refused = await client.post("/api/tasks", json={"title": "no"}, headers=read_headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Credential lacks the write scope"

    created = await client.post(
        "/api/tasks", json={"title": "via bearer"}, headers=write_headers
    )
    assert created.status_code == 201
    assert (await client.get("/api/export", headers=read_headers)).status_code == 200


async def test_revoked_bearer_fails_immediately(client: AsyncClient) -> None:
    async with session_scope() as s:
        token, row = await authz.create_credential(s, name="gone", scopes=["read", "write"])
        revoked = await authz.revoke_credential(s, str(row.id))
        assert revoked is not None and revoked.revoked_at is not None
    response = await client.get("/api/tasks", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_unknown_scopes_rejected(client: AsyncClient) -> None:
    async with session_scope() as s:
        with pytest.raises(ValueError, match="Unknown scopes"):
            await authz.create_credential(s, name="odd", scopes=["admin"])


async def test_auth_requires_bootstrap_secret(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("OWNER_BOOTSTRAP_SECRET", "")
    db.reset_engine()
    try:
        with pytest.raises(ValueError, match="OWNER_BOOTSTRAP_SECRET"):
            create_app(enable_auth=True)
    finally:
        db.reset_engine()


async def test_sse_recheck_closes_revoked_streams(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation affects an OPEN stream within one recheck interval."""
    import asyncio

    url = f"sqlite+aiosqlite:///{tmp_path}/sse.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OWNER_BOOTSTRAP_SECRET", SECRET)
    monkeypatch.setenv("SSE_AUTH_RECHECK_SECONDS", "0.2")  # close fast in-test
    db.reset_engine()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()
    bus.init_bus()
    app = create_app(enable_auth=True)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
            await _login(c)
            stream = asyncio.create_task(c.get("/api/events"))
            await asyncio.sleep(0.2)
            assert not stream.done()
            async with session_scope() as s:
                for row in await s.exec(select(OwnerSession)):
                    await s.delete(row)
                await s.commit()
            await asyncio.sleep(1.0)
            assert stream.done()  # closed after the session vanished
    finally:
        await get_engine().dispose()
        db.reset_engine()
        bus.reset_bus()
