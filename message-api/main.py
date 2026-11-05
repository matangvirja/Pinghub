"""PingHub Message API.

Owns the light read paths and the conversation data used by the web UI:
  * GET  /history?user=alice        -> cache-aside over Redis + PostgreSQL
  * GET  /messages/{task_id}        -> task status from task_logs/messages
  * POST /users                     -> create/sign in a profile
  * GET  /users                     -> directory of profiles
  * GET  /users/{identifier}        -> look up by username or public id
  * GET  /users/{id}/conversations  -> chat list with unread counts
  * GET  /users/{id}/conversations/{peer} -> full thread with delivery state
  * POST /users/{id}/conversations/{peer}/read -> mark incoming as READ

The history cache is intentionally simple:
    1. try Redis
    2. on miss, query PostgreSQL
    3. write the top-50 result to Redis with a TTL
The worker deletes the two affected history keys whenever a message changes
state, and mark-read does the same, so the cache never shows a stale status for
long.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import redis.asyncio as redis
from fastapi import Body, FastAPI, HTTPException, Query
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

# Public ids are 8 uppercase hex characters, e.g. 7F3A9C21.
PUBLIC_ID_RE = re.compile(r"^[A-Fa-f0-9]{8}$")


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
WHERE s.username = %s OR r.username = %s
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
WITH my_messages AS (
    SELECT m.id,
           m.sender_id,
           m.receiver_id,
           m.body,
           m.status,
           m.created_at,
           CASE WHEN m.sender_id = %(me)s THEN m.receiver_id ELSE m.sender_id END AS peer_id
    FROM messages m
    WHERE m.sender_id = %(me)s OR m.receiver_id = %(me)s
),
latest AS (
    SELECT DISTINCT ON (peer_id) *
    FROM my_messages
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
           FROM messages m2
           WHERE m2.receiver_id = %(me)s
             AND m2.sender_id = u.id
             AND m2.status IN ('QUEUED', 'PROCESSING', 'DELIVERED')
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
WHERE (m.sender_id = %(me)s AND m.receiver_id = %(peer)s)
   OR (m.sender_id = %(peer)s AND m.receiver_id = %(me)s)
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
# Profiles
# ---------------------------------------------------------------------------
@app.post("/users")
async def create_user(payload: CreateUserIn):
    """Create a profile, or return the existing one with the same username.

    This is intentionally password-less: it is a demo identity, not real auth.
    """

    username = payload.username.strip()
    display_name = (payload.display_name or "").strip() or None

    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", username):
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
):
    """Directory used by the "New chat" picker.

    Load-test users are hidden so a demo does not get flooded with fake names.
    """

    sql = USER_SQL + "WHERE username NOT LIKE 'load\\_%'"
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
async def get_user(identifier: str):
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
):
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            me = await _resolve_user(cursor, identifier)
            if me is None:
                raise HTTPException(status_code=404, detail="User not found")

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
):
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            me = await _resolve_user(cursor, identifier)
            if me is None:
                raise HTTPException(status_code=404, detail="User not found")

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
async def mark_conversation_read(identifier: str, peer: str):
    """Mark every delivered message from ``peer`` to ``identifier`` as READ."""

    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            me = await _resolve_user(cursor, identifier)
            if me is None:
                raise HTTPException(status_code=404, detail="User not found")

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


# ---------------------------------------------------------------------------
# History and task status
# ---------------------------------------------------------------------------
@app.get("/history")
async def history(
    user: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=50),
):
    # Resolve a public id to the canonical username. Plain usernames skip the
    # lookup entirely, so the common path stays a single Redis GET.
    canonical = user
    if PUBLIC_ID_RE.fullmatch(user):
        try:
            async with app.state.pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cursor:
                    row = await _resolve_user(cursor, user)
            if row is not None:
                canonical = row["username"]
        except Exception as exc:
            log.warning("history_user_resolve_failed", extra={"user": user, "error": str(exc)})

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
            await cursor.execute(HISTORY_SQL, (canonical, canonical))
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
async def message_status(task_id: str):
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
    try:
        raw = await app.state.redis.get(f"{TASK_PREFIX}:{task_id}")
        if raw:
            state = json.loads(raw)
            state["source"] = "redis"
            return state
    except Exception as exc:
        log.warning("task_cache_read_failed", extra={"task_id": task_id, "error": str(exc)})

    raise HTTPException(status_code=404, detail="Task not found")
