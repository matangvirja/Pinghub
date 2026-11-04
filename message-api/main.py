"""PingHub Message API.

Owns the two light read paths:
  * GET /history?user=alice  -> cache-aside over Redis + PostgreSQL
  * GET /messages/{task_id}  -> task status from task_logs/messages

The history cache is intentionally simple:
    1. try Redis
    2. on miss, query PostgreSQL
    3. write the top-50 result to Redis with a TTL
The worker deletes the two affected history keys whenever a message changes
state, so the cache never shows a stale status for long.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

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
    version="1.0.0",
    description="Message history (cached) and task status",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Cache", "X-Request-ID"],
)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _serialize_row(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "sender": row["sender"],
        "receiver": row["receiver"],
        "body": row["body"],
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


HISTORY_SQL = """
SELECT m.id,
       s.username AS sender,
       r.username AS receiver,
       m.body,
       m.status,
       m.created_at,
       m.updated_at
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


@app.get("/history")
async def history(
    user: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=50),
):
    cache_key = f"{CACHE_PREFIX}:{user}"

    # 1. Cache-aside read.
    cached_messages: list[dict] | None = None
    try:
        raw = await app.state.redis.get(cache_key)
        if raw:
            cached_messages = json.loads(raw)
    except Exception as exc:
        log.warning("cache_read_failed", extra={"user": user, "error": str(exc)})

    if cached_messages is not None:
        messages = cached_messages[:limit]
        log.info(
            "history_cache_hit",
            extra={"user": user, "count": len(messages), "cache": "HIT"},
        )
        return JSONResponse(
            status_code=200,
            content={
                "user": user,
                "count": len(messages),
                "cache": "HIT",
                "messages": messages,
            },
            headers={"X-Cache": "HIT"},
        )

    # 2. Cache miss -> PostgreSQL.
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(HISTORY_SQL, (user, user))
            rows = await cursor.fetchall()

    messages = [_serialize_row(row) for row in rows]

    # 3. Store the top-50 list. The endpoint slices it for smaller limits.
    try:
        await app.state.redis.set(
            cache_key, json.dumps(messages), ex=HISTORY_CACHE_TTL
        )
    except Exception as exc:
        log.warning("cache_write_failed", extra={"user": user, "error": str(exc)})

    sliced = messages[:limit]
    log.info(
        "history_cache_miss",
        extra={"user": user, "count": len(sliced), "cache": "MISS"},
    )
    return JSONResponse(
        status_code=200,
        content={
            "user": user,
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
