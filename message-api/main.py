"""PingHub Message API.

Owns the light read paths and the conversation data used by the web UI:
  * POST /auth/register             -> create/activate an employee account
  * POST /auth/login                -> password sign-in, returns a bearer token
  * POST /auth/logout               -> revoke this device's session
  * GET  /auth/me                   -> the signed-in employee
  * POST /auth/password             -> change password, revoke other sessions
  * GET  /history?user=alice        -> cache-aside over Redis + PostgreSQL
  * GET  /messages/{task_id}        -> task status from task_logs/messages
  * GET  /users                     -> directory of profiles
  * GET  /users/{identifier}        -> look up by username or public id
  * GET  /users/{id}/conversations  -> chat list with unread counts
  * GET  /users/{id}/conversations/{peer} -> full thread with delivery state
  * POST /users/{id}/conversations/{peer}/read -> mark incoming as READ
  * DELETE /users/{id}/conversations/{peer} -> hide the chat for this user

Every user-facing route requires an employee session (``Authorization:
Bearer <token>``). ``X-Internal-Token`` is accepted for smoke tests, Locust and
the in-app Load Lab, which use synthetic users instead of real accounts.

The history cache is intentionally simple:
    1. try Redis
    2. on miss, query PostgreSQL
    3. write the top-50 result to Redis with a TTL
The worker deletes the two affected history keys whenever a message changes
state, and mark-read does the same, so the cache never shows a stale status for
long.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from common.logger import configure_logging, get_logger, request_id_var

SERVICE_NAME = os.getenv("SERVICE_NAME", "message-api")
configure_logging(SERVICE_NAME)
log = get_logger("message-api")

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://pinghub:pinghub@postgres:5432/pinghub"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
HISTORY_CACHE_TTL = int(os.getenv("HISTORY_CACHE_TTL", "60"))
CACHE_PREFIX = "pinghub:history:v1"
TASK_PREFIX = "pinghub:task"

# Employee authentication. Change these in .env for a real deployment.
SIGNUP_CODE = os.getenv("PINGHUB_SIGNUP_CODE", "pinghub-demo")
INTERNAL_TOKEN = os.getenv("PINGHUB_INTERNAL_TOKEN", "pinghub-internal-dev")
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "12"))
REMEMBER_TTL_DAYS = int(os.getenv("REMEMBER_TTL_DAYS", "30"))
PASSWORD_ITERATIONS = int(os.getenv("PASSWORD_ITERATIONS", "210000"))
MIN_PASSWORD_LENGTH = 8

# Public ids are 8 uppercase hex characters, e.g. 7F3A9C21.
PUBLIC_ID_RE = re.compile(r"^[A-Fa-f0-9]{8}$")
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{2,32}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(REDIS_URL, decode_responses=True)
    app.state.pool = AsyncConnectionPool(
        DATABASE_URL, min_size=1, max_size=10, open=False
    )
    await app.state.pool.open()
    log.info("message_api_started", extra={"cache_ttl_seconds": HISTORY_CACHE_TTL})
    try:
        yield
    finally:
        await app.state.pool.close()
        await app.state.redis.aclose()
        log.info("message_api_stopped")


app = FastAPI(
    title="PingHub Message API",
    version="2.0.0",
    description="Conversations, read receipts, cached history and task status",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Cache", "X-Request-ID"],
)


class CreateUserIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    display_name: str | None = Field(default=None, max_length=64)


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=128)
    signup_code: str = Field(..., min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)
    remember: bool = False


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    remember: bool = False


class PasswordChangeIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=128)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _serialize_user(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "public_id": row["public_id"],
        "display_name": row["display_name"] or row["username"],
        "created_at": _iso(row["created_at"]),
    }


# ---------------------------------------------------------------------------
# Passwords and employee sessions
# ---------------------------------------------------------------------------
def _hash_password(password: str) -> str:
    """PBKDF2-SHA256 with a fresh per-user salt (stdlib, no extra dependency)."""

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_expiry(remember: bool) -> datetime:
    if remember:
        return datetime.now(timezone.utc) + timedelta(days=REMEMBER_TTL_DAYS)
    return datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)


def _is_internal_request(request: Request) -> bool:
    supplied = request.headers.get("x-internal-token")
    return bool(supplied) and hmac.compare_digest(supplied, INTERNAL_TOKEN)


async def require_session(request: Request) -> dict:
    """Resolve the bearer token to an employee, or allow the internal bypass.

    ``X-Internal-Token`` exists for the smoke test, Locust and the in-app Load
    Lab. They send synthetic usernames that do not belong to a real employee.
    """

    if _is_internal_request(request):
        return {
            "internal": True,
            "session_id": None,
            "user": None,
            "expires_at": None,
        }

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Sign in required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(SESSION_SQL, (_token_hash(token),))
            row = await cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=401,
            detail="Your session expired. Sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {
        "internal": False,
        "session_id": row["session_id"],
        "user": _serialize_user(row),
        "expires_at": row["expires_at"],
    }


def _assert_self(session: dict, user: dict) -> None:
    if session["internal"]:
        return
    if str(user["id"]) != session["user"]["id"]:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own conversations.",
        )


async def _create_session(cursor, user_id, remember: bool) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = _session_expiry(remember)
    await cursor.execute(
        """
        INSERT INTO auth_sessions (user_id, token_hash, expires_at)
        VALUES (%s, %s, %s)
        """,
        (user_id, _token_hash(token), expires_at),
    )
    return token, expires_at


def _serialize_row(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "sender": row["sender"],
        "receiver": row["receiver"],
        "body": row["body"],
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "delivered_at": _iso(row.get("delivered_at")),
        "read_at": _iso(row.get("read_at")),
    }


def _serialize_thread_row(row: dict, me_username: str) -> dict:
    return {
        "id": str(row["id"]),
        "task_id": str(row["task_id"]) if row["task_id"] else None,
        "sender": row["sender"],
        "sender_public_id": row["sender_public_id"],
        "receiver": row["receiver"],
        "receiver_public_id": row["receiver_public_id"],
        "body": row["body"],
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "delivered_at": _iso(row["delivered_at"]),
        "read_at": _iso(row["read_at"]),
        "attempts": row["attempts"] or 0,
        "error": row["error"],
        "is_mine": row["sender"] == me_username,
    }


SESSION_SQL = """
SELECT s.id AS session_id,
       s.expires_at,
       u.id,
       u.username,
       u.public_id,
       u.display_name,
       u.created_at
FROM auth_sessions s
JOIN users u ON u.id = s.user_id
WHERE s.token_hash = %s
  AND s.expires_at > now()
  AND u.is_active = TRUE
LIMIT 1
"""

AUTH_USER_SQL = """
SELECT id, username, public_id, display_name, created_at,
       password_hash, is_active
FROM users
"""

HISTORY_SQL = """
SELECT m.id,
       s.username AS sender,
       r.username AS receiver,
       m.body,
       m.status,
       m.created_at,
       m.updated_at,
       m.delivered_at,
       m.read_at
FROM messages m
JOIN users s ON s.id = m.sender_id
JOIN users r ON r.id = m.receiver_id
WHERE (m.sender_id = %(me)s OR m.receiver_id = %(me)s)
  AND m.created_at > COALESCE(
      (
          SELECT h.hidden_at
          FROM conversation_hides h
          WHERE h.user_id = %(me)s
            AND h.peer_id = CASE
                WHEN m.sender_id = %(me)s THEN m.receiver_id
                ELSE m.sender_id
            END
      ),
      '-infinity'::timestamptz
  )
ORDER BY m.created_at DESC
LIMIT 50
"""

TASK_SQL = """
SELECT t.task_id,
       t.message_id,
       t.status,
       t.attempts,
       t.error,
       t.created_at,
       t.updated_at,
       s.username AS sender,
       r.username AS receiver,
       m.body
FROM task_logs t
LEFT JOIN messages m ON m.id = t.message_id
LEFT JOIN users s ON s.id = m.sender_id
LEFT JOIN users r ON r.id = m.receiver_id
WHERE t.task_id = %s
"""

USER_SQL = """
SELECT id, username, public_id, display_name, created_at
FROM users
"""

RESOLVE_SQL = USER_SQL + """
WHERE lower(username) = lower(%(identifier)s)
   OR upper(public_id) = upper(%(identifier)s)
   OR id::text = %(identifier)s
LIMIT 1
"""

CONVERSATIONS_SQL = """
WITH visible AS (
    SELECT m.id,
           m.sender_id,
           m.receiver_id,
           m.body,
           m.status,
           m.created_at,
           CASE WHEN m.sender_id = %(me)s THEN m.receiver_id ELSE m.sender_id END AS peer_id
    FROM messages m
    LEFT JOIN conversation_hides h
           ON h.user_id = %(me)s
          AND h.peer_id = CASE
              WHEN m.sender_id = %(me)s THEN m.receiver_id
              ELSE m.sender_id
          END
    WHERE (m.sender_id = %(me)s OR m.receiver_id = %(me)s)
      AND (h.hidden_at IS NULL OR m.created_at > h.hidden_at)
),
latest AS (
    SELECT DISTINCT ON (peer_id) *
    FROM visible
    ORDER BY peer_id, created_at DESC
)
SELECT u.id,
       u.username,
       u.public_id,
       u.display_name,
       l.id AS last_id,
       l.body AS last_body,
       l.status AS last_status,
       l.created_at AS last_at,
       (l.sender_id = %(me)s) AS last_from_me,
       (
           SELECT COUNT(*)::int
           FROM visible v
           WHERE v.receiver_id = %(me)s
             AND v.sender_id = u.id
             AND v.status IN ('QUEUED', 'PROCESSING', 'DELIVERED')
       ) AS unread_count
FROM latest l
JOIN users u ON u.id = l.peer_id
ORDER BY l.created_at DESC
LIMIT %(limit)s
"""

THREAD_SQL = """
SELECT m.id,
       t.task_id,
       s.username AS sender,
       s.public_id AS sender_public_id,
       r.username AS receiver,
       r.public_id AS receiver_public_id,
       m.body,
       m.status,
       m.created_at,
       m.updated_at,
       m.delivered_at,
       m.read_at,
       t.attempts,
       t.error
FROM messages m
JOIN users s ON s.id = m.sender_id
JOIN users r ON r.id = m.receiver_id
LEFT JOIN task_logs t ON t.message_id = m.id
WHERE ((m.sender_id = %(me)s AND m.receiver_id = %(peer)s)
    OR (m.sender_id = %(peer)s AND m.receiver_id = %(me)s))
  AND m.created_at > COALESCE(
      (
          SELECT h.hidden_at
          FROM conversation_hides h
          WHERE h.user_id = %(me)s
            AND h.peer_id = %(peer)s
      ),
      '-infinity'::timestamptz
  )
ORDER BY m.created_at DESC
LIMIT %(limit)s
"""


async def _resolve_user(cursor, identifier: str) -> dict | None:
    await cursor.execute(RESOLVE_SQL, {"identifier": identifier})
    return await cursor.fetchone()


async def _invalidate_history(*usernames: str) -> None:
    keys = [f"{CACHE_PREFIX}:{name}" for name in usernames if name]
    if not keys:
        return
    try:
        await app.state.redis.delete(*keys)
    except Exception as exc:
        log.warning("history_invalidate_failed", extra={"error": str(exc)})


@app.get("/live")
async def live() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health")
async def health() -> JSONResponse:
    db_ok = False
    redis_ok = False

    try:
        async with app.state.pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
        db_ok = True
    except Exception:
        pass

    try:
        await app.state.redis.ping()
        redis_ok = True
    except Exception:
        pass

    healthy = db_ok and redis_ok
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "service": SERVICE_NAME,
            "nodes": {
                "message-api": {"status": "up"},
                "postgres": {"status": "up" if db_ok else "down"},
                "redis": {"status": "up" if redis_ok else "down"},
            },
        },
    )


# ---------------------------------------------------------------------------
# Employee authentication
# ---------------------------------------------------------------------------
@app.get("/auth/config")
async def auth_config() -> dict:
    """Public policy the sign-in screen needs before anyone logs in."""

    return {
        "signup_enabled": bool(SIGNUP_CODE),
        "min_password_length": MIN_PASSWORD_LENGTH,
        "session_hours": SESSION_TTL_HOURS,
        "remember_days": REMEMBER_TTL_DAYS,
    }


@app.post("/auth/register")
async def register(payload: RegisterIn):
    """Create a new employee account, or activate a legacy passwordless one.

    The company access code is the only way to create an account, so a device
    on the Wi-Fi cannot simply invent a new identity.
    """

    if not SIGNUP_CODE:
        raise HTTPException(
            status_code=403,
            detail="Account creation is disabled. Ask your administrator.",
        )
    if not hmac.compare_digest(payload.signup_code.strip(), SIGNUP_CODE):
        raise HTTPException(status_code=403, detail="The company access code is incorrect.")

    username = payload.username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=422,
            detail="Use 2-32 letters, numbers, dots, dashes or underscores.",
        )
    if len(payload.password.strip()) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.",
        )

    display_name = (payload.display_name or "").strip() or None
    password_hash = _hash_password(payload.password)

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                AUTH_USER_SQL + "WHERE lower(username) = lower(%s) FOR UPDATE",
                (username,),
            )
            existing = await cursor.fetchone()

            created = existing is None
            activated = False

            if existing is not None:
                if existing["password_hash"]:
                    raise HTTPException(
                        status_code=409,
                        detail="That username is already registered. Sign in instead.",
                    )
                # An old passwordless profile keeps its history and gets a
                # password. This is the migration path for existing users.
                await cursor.execute(
                    """
                    UPDATE users
                    SET password_hash = %s,
                        is_active = TRUE,
                        display_name = COALESCE(%s, display_name)
                    WHERE id = %s
                    RETURNING id, username, public_id, display_name, created_at
                    """,
                    (password_hash, display_name, existing["id"]),
                )
                row = await cursor.fetchone()
                activated = True
            else:
                await cursor.execute(
                    """
                    INSERT INTO users (username, display_name, password_hash)
                    VALUES (%s, %s, %s)
                    RETURNING id, username, public_id, display_name, created_at
                    """,
                    (username, display_name, password_hash),
                )
                row = await cursor.fetchone()

            token, expires_at = await _create_session(
                cursor, row["id"], payload.remember
            )

    log.info(
        "employee_registered",
        extra={
            "username": row["username"],
            "is_new_account": created,
            "activated": activated,
        },
    )
    return {
        "token": token,
        "token_type": "bearer",
        "expires_at": _iso(expires_at),
        "created": created,
        "activated": activated,
        "user": _serialize_user(row),
    }


@app.post("/auth/login")
async def login(payload: LoginIn):
    username = payload.username.strip()
    generic_error = HTTPException(
        status_code=401,
        detail="Incorrect username or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not username:
        raise generic_error

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                AUTH_USER_SQL + "WHERE lower(username) = lower(%s) LIMIT 1",
                (username,),
            )
            row = await cursor.fetchone()

            if row is None:
                raise generic_error
            if not row["is_active"]:
                raise HTTPException(
                    status_code=403,
                    detail="This account is disabled. Ask your administrator.",
                )
            if row["password_hash"] is None:
                raise HTTPException(
                    status_code=403,
                    detail="This account has not been activated yet. "
                    "Use 'Activate / create account' with the company code.",
                )
            if not _verify_password(payload.password, row["password_hash"]):
                raise generic_error

            token, expires_at = await _create_session(
                cursor, row["id"], payload.remember
            )
            # Housekeeping: expired rows are never usable again.
            await cursor.execute("DELETE FROM auth_sessions WHERE expires_at < now()")

    log.info("employee_login", extra={"username": row["username"]})
    return {
        "token": token,
        "token_type": "bearer",
        "expires_at": _iso(expires_at),
        "user": _serialize_user(row),
    }


@app.post("/auth/logout")
async def logout(session: dict = Depends(require_session)):
    if session["internal"]:
        return {"ok": True, "sessions_removed": 0}

    async with app.state.pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM auth_sessions WHERE id = %s", (session["session_id"],)
            )

    log.info("employee_logout", extra={"username": session["user"]["username"]})
    return {"ok": True, "sessions_removed": 1}


@app.get("/auth/me")
async def auth_me(session: dict = Depends(require_session)):
    if session["internal"]:
        raise HTTPException(status_code=401, detail="Sign in required")
    return {
        "user": session["user"],
        "expires_at": _iso(session["expires_at"]),
    }


@app.post("/auth/password")
async def change_password(
    payload: PasswordChangeIn,
    session: dict = Depends(require_session),
):
    if session["internal"]:
        raise HTTPException(status_code=401, detail="Sign in required")
    if len(payload.new_password.strip()) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.",
        )

    user_id = session["user"]["id"]
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT password_hash FROM users WHERE id = %s", (user_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="User not found")
            if not _verify_password(payload.current_password, row["password_hash"]):
                raise HTTPException(status_code=400, detail="Current password is incorrect.")
            if _verify_password(payload.new_password, row["password_hash"]):
                raise HTTPException(
                    status_code=400,
                    detail="Choose a password different from the current one.",
                )

            await cursor.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (_hash_password(payload.new_password), user_id),
            )
            # Keep this device signed in, but sign out every other device.
            await cursor.execute(
                "DELETE FROM auth_sessions WHERE user_id = %s AND id <> %s",
                (user_id, session["session_id"]),
            )

    log.info("employee_password_changed", extra={"username": session["user"]["username"]})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
@app.post("/users")
async def create_user(payload: CreateUserIn, session: dict = Depends(require_session)):
    """Internal-only profile creation for tooling and synthetic load users.

    Real employees create an account through ``POST /auth/register``.
    """

    if not session["internal"]:
        raise HTTPException(
            status_code=403,
            detail="Create an account with the company access code.",
        )

    username = payload.username.strip()
    display_name = (payload.display_name or "").strip() or None

    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=422,
            detail="Use 2-32 letters, numbers, dots, dashes or underscores.",
        )

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                USER_SQL + "WHERE lower(username) = lower(%s) LIMIT 1", (username,)
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if display_name and not existing["display_name"]:
                    await cursor.execute(
                        "UPDATE users SET display_name = %s WHERE id = %s RETURNING "
                        "id, username, public_id, display_name, created_at",
                        (display_name, existing["id"]),
                    )
                    existing = await cursor.fetchone()
                return {"created": False, "user": _serialize_user(existing)}

            await cursor.execute(
                """
                INSERT INTO users (username, display_name)
                VALUES (%s, %s)
                RETURNING id, username, public_id, display_name, created_at
                """,
                (username, display_name),
            )
            row = await cursor.fetchone()

    log.info("user_created", extra={"username": row["username"], "public_id": row["public_id"]})
    return {"created": True, "user": _serialize_user(row)}


@app.get("/users")
async def list_users(
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    session: dict = Depends(require_session),
):
    """Directory used by the "New chat" picker.

    Load-test users are hidden so a demo does not get flooded with fake names.
    """

    sql = USER_SQL + "WHERE is_active = TRUE AND username NOT LIKE 'load\\_%'"
    params: list = []

    if q:
        sql += " AND (username ILIKE %s OR public_id ILIKE %s OR display_name ILIKE %s)"
        like = f"%{q.strip()}%"
        params.extend([like, like, like])

    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            rows = await cursor.fetchall()

    return {"count": len(rows), "users": [_serialize_user(row) for row in rows]}


@app.get("/users/{identifier}")
async def get_user(identifier: str, session: dict = Depends(require_session)):
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            row = await _resolve_user(cursor, identifier)

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return _serialize_user(row)


# ---------------------------------------------------------------------------
# Conversations and threads
# ---------------------------------------------------------------------------
@app.get("/users/{identifier}/conversations")
async def list_conversations(
    identifier: str,
    limit: int = Query(default=50, ge=1, le=100),
    session: dict = Depends(require_session),
):
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            me = await _resolve_user(cursor, identifier)
            if me is None:
                raise HTTPException(status_code=404, detail="User not found")
            _assert_self(session, me)

            await cursor.execute(
                CONVERSATIONS_SQL, {"me": me["id"], "limit": limit}
            )
            rows = await cursor.fetchall()

    conversations = [
        {
            "peer": {
                "id": str(row["id"]),
                "username": row["username"],
                "public_id": row["public_id"],
                "display_name": row["display_name"] or row["username"],
            },
            "last_message": {
                "id": str(row["last_id"]),
                "body": row["last_body"],
                "status": row["last_status"],
                "created_at": _iso(row["last_at"]),
                "from_me": row["last_from_me"],
            },
            "unread_count": row["unread_count"],
        }
        for row in rows
    ]
    return {"user": _serialize_user(me), "count": len(conversations), "conversations": conversations}


@app.get("/users/{identifier}/conversations/{peer}")
async def get_thread(
    identifier: str,
    peer: str,
    limit: int = Query(default=200, ge=1, le=300),
    session: dict = Depends(require_session),
):
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            me = await _resolve_user(cursor, identifier)
            if me is None:
                raise HTTPException(status_code=404, detail="User not found")
            _assert_self(session, me)

            other = await _resolve_user(cursor, peer)
            if other is None:
                raise HTTPException(status_code=404, detail="Peer not found")

            await cursor.execute(
                THREAD_SQL, {"me": me["id"], "peer": other["id"], "limit": limit}
            )
            rows = await cursor.fetchall()

    # The query returns newest-first so LIMIT keeps the latest messages; the UI
    # renders oldest-first.
    messages = [
        _serialize_thread_row(row, me["username"]) for row in reversed(rows)
    ]
    return {
        "me": _serialize_user(me),
        "peer": _serialize_user(other),
        "count": len(messages),
        "messages": messages,
    }


@app.post("/users/{identifier}/conversations/{peer}/read")
async def mark_conversation_read(
    identifier: str,
    peer: str,
    session: dict = Depends(require_session),
):
    """Mark every delivered message from ``peer`` to ``identifier`` as READ."""

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            me = await _resolve_user(cursor, identifier)
            if me is None:
                raise HTTPException(status_code=404, detail="User not found")
            _assert_self(session, me)

            other = await _resolve_user(cursor, peer)
            if other is None:
                raise HTTPException(status_code=404, detail="Peer not found")

            await cursor.execute(
                """
                UPDATE messages m
                SET status = 'READ', read_at = now(), updated_at = now()
                WHERE m.sender_id = %s
                  AND m.receiver_id = %s
                  AND m.status IN ('DELIVERED', 'READ')
                  AND m.read_at IS NULL
                RETURNING m.id
                """,
                (other["id"], me["id"]),
            )
            rows = await cursor.fetchall()

    await _invalidate_history(me["username"], other["username"])

    log.info(
        "conversation_read",
        extra={
            "user": me["username"],
            "peer": other["username"],
            "marked": len(rows),
        },
    )
    return {
        "marked": len(rows),
        "read_at": _iso(datetime.now().astimezone()),
        "user": _serialize_user(me),
        "peer": _serialize_user(other),
    }


@app.delete("/users/{identifier}/conversations/{peer}")
async def delete_conversation(
    identifier: str,
    peer: str,
    session: dict = Depends(require_session),
):
    """Delete the whole chat from this employee's view.

    This is a soft, per-user delete: the other person keeps their copy. A newer
    message (after ``hidden_at``) makes the chat reappear automatically.
    """

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            me = await _resolve_user(cursor, identifier)
            if me is None:
                raise HTTPException(status_code=404, detail="User not found")
            _assert_self(session, me)

            other = await _resolve_user(cursor, peer)
            if other is None:
                raise HTTPException(status_code=404, detail="Peer not found")

            await cursor.execute(
                """
                INSERT INTO conversation_hides (user_id, peer_id, hidden_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_id, peer_id)
                DO UPDATE SET hidden_at = now()
                RETURNING hidden_at
                """,
                (me["id"], other["id"]),
            )
            row = await cursor.fetchone()

    await _invalidate_history(me["username"])

    log.info(
        "conversation_deleted",
        extra={"user": me["username"], "peer": other["username"]},
    )
    return {
        "deleted": True,
        "hidden_at": _iso(row["hidden_at"]),
        "user": _serialize_user(me),
        "peer": _serialize_user(other),
    }


# ---------------------------------------------------------------------------
# History and task status
# ---------------------------------------------------------------------------
@app.get("/history")
async def history(
    user: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=50),
    session: dict = Depends(require_session),
):
    # An employee may only read their own history. The internal bypass keeps the
    # smoke test, Locust and the Load Lab working with synthetic users.
    canonical = user
    me_id: str | None = None

    if session["internal"]:
        if PUBLIC_ID_RE.fullmatch(user):
            try:
                async with app.state.pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cursor:
                        row = await _resolve_user(cursor, user)
                if row is not None:
                    canonical = row["username"]
            except Exception as exc:
                log.warning(
                    "history_user_resolve_failed",
                    extra={"user": user, "error": str(exc)},
                )
    else:
        me_user = session["user"]
        if PUBLIC_ID_RE.fullmatch(user):
            if user.upper() != (me_user["public_id"] or "").upper():
                raise HTTPException(
                    status_code=403, detail="You can only read your own history."
                )
            canonical = me_user["username"]
        elif user.lower() != me_user["username"].lower():
            raise HTTPException(
                status_code=403, detail="You can only read your own history."
            )
        else:
            canonical = me_user["username"]
        me_id = me_user["id"]

    cache_key = f"{CACHE_PREFIX}:{canonical}"

    # 1. Cache-aside read.
    cached_messages: list[dict] | None = None
    try:
        raw = await app.state.redis.get(cache_key)
        if raw:
            cached_messages = json.loads(raw)
    except Exception as exc:
        log.warning("cache_read_failed", extra={"user": canonical, "error": str(exc)})

    if cached_messages is not None:
        messages = cached_messages[:limit]
        log.info(
            "history_cache_hit",
            extra={"user": canonical, "count": len(messages), "cache": "HIT"},
        )
        return JSONResponse(
            status_code=200,
            content={
                "user": canonical,
                "count": len(messages),
                "cache": "HIT",
                "messages": messages,
            },
            headers={"X-Cache": "HIT"},
        )

    # 2. Cache miss -> PostgreSQL.
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            if me_id is None:
                resolved = await _resolve_user(cursor, canonical)
                if resolved is None:
                    raise HTTPException(status_code=404, detail="User not found")
                me_id = str(resolved["id"])

            await cursor.execute(HISTORY_SQL, {"me": me_id})
            rows = await cursor.fetchall()

    messages = [_serialize_row(row) for row in rows]

    # 3. Store the top-50 list. The endpoint slices it for smaller limits.
    try:
        await app.state.redis.set(
            cache_key, json.dumps(messages), ex=HISTORY_CACHE_TTL
        )
    except Exception as exc:
        log.warning("cache_write_failed", extra={"user": canonical, "error": str(exc)})

    sliced = messages[:limit]
    log.info(
        "history_cache_miss",
        extra={"user": canonical, "count": len(sliced), "cache": "MISS"},
    )
    return JSONResponse(
        status_code=200,
        content={
            "user": canonical,
            "count": len(sliced),
            "cache": "MISS",
            "messages": sliced,
        },
        headers={"X-Cache": "MISS"},
    )


@app.get("/messages/{task_id}")
async def message_status(task_id: str, session: dict = Depends(require_session)):
    # Validate before hitting PostgreSQL so a malformed id is a clean 404.
    try:
        uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(TASK_SQL, (task_id,))
            row = await cursor.fetchone()

    if row is not None:
        if not session["internal"] and session["user"]["username"] not in (
            row["sender"],
            row["receiver"],
        ):
            raise HTTPException(status_code=403, detail="This task belongs to someone else.")
        return {
            "task_id": str(row["task_id"]),
            "message_id": str(row["message_id"]) if row["message_id"] else None,
            "status": row["status"],
            "attempts": row["attempts"],
            "error": row["error"],
            "sender": row["sender"],
            "receiver": row["receiver"],
            "body": row["body"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "source": "database",
        }

    # The worker may not have created the task_log row yet. The gateway writes
    # the initial QUEUED state to Redis, so we can still answer immediately.
    state = None
    try:
        raw = await app.state.redis.get(f"{TASK_PREFIX}:{task_id}")
        if raw:
            state = json.loads(raw)
    except Exception as exc:
        log.warning("task_cache_read_failed", extra={"task_id": task_id, "error": str(exc)})

    if state is not None:
        if not session["internal"] and session["user"]["username"] not in (
            state.get("sender"),
            state.get("receiver"),
        ):
            raise HTTPException(
                status_code=403, detail="This task belongs to someone else."
            )
        state["source"] = "redis"
        return state

    raise HTTPException(status_code=404, detail="Task not found")
