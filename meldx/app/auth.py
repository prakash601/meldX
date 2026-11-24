"""Owner authentication (product plan §6.5, v0.0.4).

The owner is the only human user. Authentication protects this one owner's
data; it does not imply teams, signup, or billing.

Design, per the accepted plan defaults:
- The owner logs in through a web form with an environment-provisioned
  high-entropy bootstrap secret (OWNER_BOOTSTRAP_SECRET), compared
  constant-time. No public registration, no secrets in URLs.
- Web sessions are server-side rows: the cookie carries a raw token, the
  database stores only its hash; sessions expire and are revocable.
- CLI/programmatic clients use separately revocable bearer credentials with
  scopes. Scopes start at read/write; execution and administration are
  reserved for later explicit scopes.
- Only hashes are stored; comparisons are constant-time.
- Login attempts are rate-limited in-process (one uvicorn worker is the
  documented deployment shape).
"""
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from meldx.app.models import Credential, OwnerSession

SESSION_COOKIE = "meldx_session"
CSRF_COOKIE = "meldx_csrf"
READ_SCOPE = "read"
WRITE_SCOPE = "write"
OWNER_SCOPES = (READ_SCOPE, WRITE_SCOPE)


def bootstrap_secret() -> str | None:
    return os.environ.get("OWNER_BOOTSTRAP_SECRET") or None


def _hash(raw: str) -> str:
    """High-entropy tokens need no slow KDF: sha256 + constant-time compare."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def session_days() -> int:
    try:
        return max(1, int(os.environ.get("SESSION_DAYS", "7")))
    except ValueError:
        return 7


def verify_bootstrap_secret(candidate: str | None) -> bool:
    """Constant-time comparison against the environment-provisioned secret."""
    expected = bootstrap_secret()
    if expected is None or not candidate:
        return False
    return hmac.compare_digest(_hash(candidate), _hash(expected))


async def create_session(db: AsyncSession) -> tuple[str, datetime]:
    """Issue a web session. Returns the RAW cookie token (shown once here
    only) and the expiry instant."""
    raw = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=session_days())
    db.add(OwnerSession(token_hash=_hash(raw), expires_at=expires))
    await db.commit()
    return raw, expires


async def verify_session_token(db: AsyncSession, raw: str | None) -> OwnerSession | None:
    if not raw:
        return None
    stmt = select(OwnerSession).where(col(OwnerSession.token_hash) == _hash(raw))
    row = (await db.exec(stmt)).first()
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires <= _now():
        await db.delete(row)
        await db.commit()
        return None
    return row


async def revoke_session_token(db: AsyncSession, raw: str | None) -> None:
    if not raw:
        return
    row = await verify_session_token(db, raw)
    if row is not None:
        await db.delete(row)
        await db.commit()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(request_cookie: str | None, header_token: str | None) -> bool:
    """Double-submit: the header must repeat the csrf cookie value."""
    if not request_cookie or not header_token:
        return False
    return hmac.compare_digest(request_cookie, header_token)


async def create_credential(
    db: AsyncSession, *, name: str, scopes: list[str]
) -> tuple[str, Credential]:
    """Issue a CLI credential. Returns the RAW token exactly once."""
    if not name.strip():
        raise ValueError("Credential name cannot be empty")
    unknown = sorted(set(scopes) - {READ_SCOPE, WRITE_SCOPE})
    if unknown:
        raise ValueError(f"Unknown scopes: {', '.join(unknown)}")
    raw = secrets.token_urlsafe(32)
    row = Credential(name=name.strip(), token_hash=_hash(raw), scopes=sorted(set(scopes)))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return raw, row


@dataclass
class BearerIdentity:
    credential: Credential


async def verify_bearer_token(
    db: AsyncSession, raw: str | None
) -> Credential | None:
    if not raw:
        return None
    stmt = select(Credential).where(col(Credential.token_hash) == _hash(raw))
    row = (await db.exec(stmt)).first()
    if row is None or row.revoked_at is not None:
        return None
    return row


async def revoke_credential(db: AsyncSession, credential_id: str) -> Credential | None:
    from uuid import UUID

    try:
        row = await db.get(Credential, UUID(credential_id))
    except ValueError:
        return None
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = _now()
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


@dataclass
class LoginRateLimiter:
    """In-process failed-attempt tracker. One worker is the documented
    deployment shape; the window and attempt count are env-tunable so tests
    can exercise the lockout deterministically."""

    window_seconds: int = 900
    max_attempts: int = 5
    _failures: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    @classmethod
    def from_env(cls) -> "LoginRateLimiter":
        def read_int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, str(default))))
            except ValueError:
                return default

        return cls(
            window_seconds=read_int("LOGIN_RATE_LIMIT_SECONDS", 900),
            max_attempts=read_int("LOGIN_RATE_LIMIT_ATTEMPTS", 5),
        )

    def _prune(self, key: str, now: float) -> None:
        bucket = self._failures[key]
        while bucket and bucket[0] <= now - self.window_seconds:
            bucket.popleft()
        if not bucket:
            self._failures.pop(key, None)

    def locked(self, key: str) -> bool:
        now = time.monotonic()
        self._prune(key, now)
        return len(self._failures.get(key, ())) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._failures[key].append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
