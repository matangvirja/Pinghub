"""Aggregated ``GET /health`` for the whole platform.

The gateway is the only public entry point, so it is the natural place to
report the state of every node.  Each dependency is checked in parallel with a
short timeout; one slow dependency cannot make ``/health`` hang.
"""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlparse

import httpx
import psycopg
from redis.asyncio import Redis

from common.logger import get_logger

log = get_logger("health")

# A health check is a diagnostic, not a user request. Caching the aggregate for
# a few seconds stops a monitoring system (or a load test) from opening a new
# PostgreSQL/RabbitMQ connection dozens of times per second. Each gateway
# process keeps its own short cache; staleness is bounded by this value.
HEALTH_CACHE_SECONDS = float(os.getenv("HEALTH_CACHE_SECONDS", "3"))

_cache: tuple[float, dict, int] | None = None
_cache_lock = asyncio.Lock()


async def _http_up(client: httpx.AsyncClient, url: str) -> bool:
    try:
        response = await client.get(url, timeout=2.0)
        # 5xx means the service is up but unhealthy.
        return response.status_code < 500
    except Exception:
        return False


async def _tcp_up(url: str, default_port: int) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or default_port
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2.0
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _postgres_up(dsn: str) -> bool:
    connection = None
    try:
        connection = await asyncio.wait_for(
            psycopg.AsyncConnection.connect(dsn, connect_timeout=2), timeout=3.0
        )
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            await cursor.fetchone()
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


async def _redis_up(redis_client: Redis | None) -> bool:
    if redis_client is None:
        return False
    try:
        await asyncio.wait_for(redis_client.ping(), timeout=2.0)
        return True
    except Exception:
        return False


async def _worker_replicas(redis_client: Redis | None) -> int:
    if redis_client is None:
        return 0
    count = 0
    try:
        async for _key in redis_client.scan_iter(
            match="pinghub:worker:heartbeat:*", count=100
        ):
            count += 1
    except Exception:
        return 0
    return count


async def build_health(
    *,
    redis_url: str,
    rabbitmq_url: str,
    database_url: str,
    message_api_url: str,
    notification_url: str,
    redis_client: Redis | None,
    http_client: httpx.AsyncClient,
) -> tuple[dict, int]:
    global _cache

    if _cache is not None and time.monotonic() - _cache[0] < HEALTH_CACHE_SECONDS:
        return _cache[1], _cache[2]

    async with _cache_lock:
        # Another request may have refreshed the cache while we waited.
        if _cache is not None and time.monotonic() - _cache[0] < HEALTH_CACHE_SECONDS:
            return _cache[1], _cache[2]

        payload, status = await _collect_health(
            redis_url=redis_url,
            rabbitmq_url=rabbitmq_url,
            database_url=database_url,
            message_api_url=message_api_url,
            notification_url=notification_url,
            redis_client=redis_client,
            http_client=http_client,
        )
        _cache = (time.monotonic(), payload, status)
        return payload, status


async def _collect_health(
    *,
    redis_url: str,
    rabbitmq_url: str,
    database_url: str,
    message_api_url: str,
    notification_url: str,
    redis_client: Redis | None,
    http_client: httpx.AsyncClient,
) -> tuple[dict, int]:
    rabbit_default_port = 5671 if rabbitmq_url.startswith("amqps") else 5672

    postgres_ok, redis_ok, rabbit_ok, message_api_ok, notification_ok = (
        await asyncio.gather(
            _postgres_up(database_url),
            _redis_up(redis_client),
            _tcp_up(rabbitmq_url, rabbit_default_port),
            _http_up(http_client, f"{message_api_url}/health"),
            _http_up(http_client, f"{notification_url}/health"),
        )
    )
    workers = await _worker_replicas(redis_client)

    nodes = {
        "gateway": {"status": "up"},
        "redis": {"status": "up" if redis_ok else "down"},
        "postgres": {"status": "up" if postgres_ok else "down"},
        "rabbitmq": {"status": "up" if rabbit_ok else "down"},
        "message-api": {"status": "up" if message_api_ok else "down"},
        "notification-mock": {"status": "up" if notification_ok else "down"},
        "worker": {
            "status": "up" if workers > 0 else "down",
            "replicas": workers,
        },
    }

    healthy = all(node["status"] == "up" for node in nodes.values())
    payload = {
        "status": "ok" if healthy else "degraded",
        "service": "gateway",
        "nodes": nodes,
    }
    return payload, (200 if healthy else 503)
