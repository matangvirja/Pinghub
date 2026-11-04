"""PingHub API Gateway.

Responsibilities:
  * create/propagate a request id (X-Request-ID)
  * apply the token-bucket rate limiter and return 429
  * publish heavy requests (POST /messages) to RabbitMQ and immediately reply 202
  * proxy light read requests (/history, /messages/{task_id}) to message-api
  * aggregate /health across every node
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aio_pika
import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from common.logger import configure_logging, get_logger, request_id_var
from config import settings
from health import build_health
from rate_limiter import TokenBucketRateLimiter

configure_logging(settings.service_name)
log = get_logger("gateway")

# These headers must not be copied when proxying a response.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class MessageIn(BaseModel):
    sender: str = Field(..., min_length=1, max_length=64)
    receiver: str = Field(..., min_length=1, max_length=64)
    body: str = Field(..., min_length=1, max_length=4000)

    @field_validator("sender", "receiver", "body")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class Publisher:
    """Lazy, reconnecting RabbitMQ publisher.

    ``connect_robust`` reconnects the TCP connection automatically.  We still
    check ``is_closed`` before every publish so a fresh channel is created if
    the broker restarted while the gateway was idle.
    """

    def __init__(self, url: str, queue_name: str) -> None:
        self.url = url
        self.queue_name = queue_name
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractRobustChannel | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.connection = await aio_pika.connect_robust(self.url)
        self.channel = await self.connection.channel()
        # Durable queue: survives a RabbitMQ restart.
        await self.channel.declare_queue(self.queue_name, durable=True)

    def _is_ready(self) -> bool:
        return bool(
            self.connection is not None
            and not self.connection.is_closed
            and self.channel is not None
            and not self.channel.is_closed
        )

    async def ensure(self) -> None:
        """Reconnect if needed.

        The lock only guards (re)connecting.  Publishing must happen outside
        the lock: holding a global lock while waiting for the broker's
        publisher-confirm serialises every request and adds queueing delay
        under load.  aio_pika multiplexes concurrent publishes on one channel.
        """

        if self._is_ready():
            return
        async with self._lock:
            if not self._is_ready():
                await self.connect()

    async def publish(self, payload: dict) -> None:
        message = aio_pika.Message(
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        for attempt in range(2):
            await self.ensure()
            channel = self.channel
            assert channel is not None
            try:
                await channel.default_exchange.publish(
                    message, routing_key=self.queue_name
                )
                return
            except Exception:
                # A reconnect can close the channel between the check above and
                # the publish.  Retry once with a fresh channel; the caller
                # still decides what to do if the broker is really down.
                if attempt == 1:
                    raise
                async with self._lock:
                    self.connection = None
                    self.channel = None

    async def close(self) -> None:
        if self.connection is not None and not self.connection.is_closed:
            await self.connection.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    app.state.limiter = TokenBucketRateLimiter(
        app.state.redis,
        capacity=settings.rate_limit_capacity,
        refill_per_second=settings.rate_limit_refill,
    )
    app.state.http = httpx.AsyncClient(
        timeout=settings.proxy_timeout,
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=50,
            keepalive_expiry=30,
        ),
    )
    app.state.publisher = Publisher(settings.rabbitmq_url, settings.queue_name)

    try:
        await app.state.publisher.connect()
        log.info("rabbitmq_connected", extra={"queue": settings.queue_name})
    except Exception as exc:
        # The gateway can still serve reads and /health; publishing will retry
        # on the first request that needs the queue.
        log.error("rabbitmq_connect_failed", extra={"error": str(exc)})

    log.info(
        "gateway_started",
        extra={
            "rate_limit_capacity": settings.rate_limit_capacity,
            "rate_limit_refill_per_sec": settings.rate_limit_refill,
        },
    )
    try:
        yield
    finally:
        await app.state.publisher.close()
        await app.state.http.aclose()
        await app.state.redis.aclose()
        log.info("gateway_stopped")


app = FastAPI(
    title="PingHub API Gateway",
    version="1.0.0",
    description="Gateway-based messaging and notification platform",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-*", "X-Cache", "Retry-After"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Assign a request id, log the completed request, and echo the id back."""

    request_id = request.headers.get("x-request-id") or f"r-{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        log.exception(
            "request_failed",
            extra={"method": request.method, "path": request.url.path},
        )
        raise
    else:
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response
    finally:
        request_id_var.reset(token)


def _client_key(request: Request, fallback: str | None) -> str:
    """Identify the bucket owner.

    Priority: explicit API key, then the user in the payload/query, then IP.
    For a load test each simulated user sends its own X-API-Key, which gives
    every virtual user its own 10-token bucket (a realistic "per client" limit).
    """

    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"key:{api_key}"
    if fallback:
        return f"user:{fallback}"
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


def _rate_limit_headers(result) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(settings.rate_limit_capacity),
        "X-RateLimit-Remaining": str(max(0, int(result.remaining))),
        "X-RateLimit-Backend": result.backend,
    }


@app.get("/")
async def root() -> dict:
    return {
        "service": "PingHub API Gateway",
        "version": "1.0.0",
        "endpoints": {
            "POST /messages": "send a message (async, returns 202 + task_id)",
            "GET /messages/{task_id}": "check task/message status",
            "GET /history?user=alice": "message history (Redis cached)",
            "GET /health": "health of every node",
        },
        "docs": "/docs",
    }


@app.get("/live")
async def live() -> dict:
    """Cheap liveness probe used by Docker (never depends on downstreams)."""

    return {"status": "ok", "service": "gateway"}


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    payload, status_code = await build_health(
        redis_url=settings.redis_url,
        rabbitmq_url=settings.rabbitmq_url,
        database_url=settings.database_url,
        message_api_url=settings.message_api_url,
        notification_url=settings.notification_url,
        redis_client=request.app.state.redis,
        http_client=request.app.state.http,
    )
    return JSONResponse(status_code=status_code, content=payload)


@app.post("/messages", status_code=202)
async def create_message(payload: MessageIn, request: Request):
    request_id = request.state.request_id
    client_key = _client_key(request, payload.sender)

    # 1. Rate limit first: reject excess traffic before doing any work.
    limit = await request.app.state.limiter.check(client_key)
    headers = _rate_limit_headers(limit)

    if not limit.allowed:
        retry_after = max(1, math.ceil(limit.retry_after))
        log.warning(
            "rate_limited",
            extra={
                "client": client_key,
                "tokens_left": round(limit.remaining, 2),
                "backend": limit.backend,
            },
        )
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Too Many Requests",
                "retry_after_seconds": retry_after,
                "request_id": request_id,
            },
            headers={**headers, "Retry-After": str(retry_after)},
        )

    # 2. Generate IDs up front so the worker is idempotent: if the same task is
    #    redelivered it reuses the same message_id and does not duplicate rows.
    task_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    task_state = {
        "task_id": task_id,
        "message_id": message_id,
        "status": "QUEUED",
        "sender": payload.sender,
        "receiver": payload.receiver,
        "body": payload.body,
        "attempts": 0,
        "error": None,
        "request_id": request_id,
        "created_at": created_at,
        "updated_at": created_at,
    }

    # Put the initial state in Redis so GET /messages/{task_id} can answer
    # immediately, even before a worker has inserted the database row.
    try:
        await request.app.state.redis.set(
            f"pinghub:task:{task_id}", json.dumps(task_state), ex=86400
        )
    except Exception as exc:
        log.warning("task_state_cache_failed", extra={"task_id": task_id, "error": str(exc)})

    queue_payload = {
        "task_id": task_id,
        "message_id": message_id,
        "sender": payload.sender,
        "receiver": payload.receiver,
        "body": payload.body,
        "request_id": request_id,
        "created_at": created_at,
    }

    # 3. Hand the work to RabbitMQ and return immediately.
    try:
        await request.app.state.publisher.publish(queue_payload)
    except Exception as exc:
        log.error("queue_publish_failed", extra={"task_id": task_id, "error": str(exc)})
        try:
            await request.app.state.redis.delete(f"pinghub:task:{task_id}")
        except Exception:
            pass
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Queue unavailable, please retry",
                "request_id": request_id,
            },
            headers=headers,
        )

    log.info(
        "accepted",
        extra={
            "task_id": task_id,
            "message_id": message_id,
            "client": client_key,
            "tokens_left": round(limit.remaining, 2),
            "backend": limit.backend,
        },
    )
    log.info("queued", extra={"task_id": task_id, "message_id": message_id})

    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id,
            "message_id": message_id,
            "status": "QUEUED",
            "message": "Accepted for processing",
            "request_id": request_id,
        },
        headers=headers,
    )


async def _forward_response(request: Request, upstream: httpx.Response) -> Response:
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    headers["X-Request-ID"] = request.state.request_id
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )


async def _upstream_get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET an internal service, retrying once on a stale keep-alive connection.

    A server can close an idle keep-alive socket at the same moment the client
    reuses it.  Under load that shows up as "Server disconnected without
    sending a response".  The request is idempotent, so one retry is safe.
    """

    try:
        return await client.get(url, **kwargs)
    except httpx.TransportError:
        return await client.get(url, **kwargs)


@app.get("/messages/{task_id}")
async def get_message_status(task_id: str, request: Request):
    """Light request: proxy status lookup to message-api (no rate limit)."""

    try:
        upstream = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}/messages/{task_id}",
            headers={"X-Request-ID": request.state.request_id},
        )
    except httpx.HTTPError as exc:
        log.error("message_api_unavailable", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            content={
                "detail": "message-api unavailable",
                "request_id": request.state.request_id,
            },
        )
    return await _forward_response(request, upstream)


@app.get("/history")
async def get_history(
    request: Request,
    user: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=50),
):
    """Light request: proxy to message-api, which owns the Redis cache."""

    client_key = _client_key(request, user)
    limit_result = await request.app.state.limiter.check(client_key)
    headers = _rate_limit_headers(limit_result)

    if not limit_result.allowed:
        retry_after = max(1, math.ceil(limit_result.retry_after))
        log.warning(
            "rate_limited",
            extra={
                "client": client_key,
                "tokens_left": round(limit_result.remaining, 2),
                "backend": limit_result.backend,
                "path": "/history",
            },
        )
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Too Many Requests",
                "retry_after_seconds": retry_after,
                "request_id": request.state.request_id,
            },
            headers={**headers, "Retry-After": str(retry_after)},
        )

    try:
        upstream = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}/history",
            params={"user": user, "limit": limit},
            headers={"X-Request-ID": request.state.request_id},
        )
    except httpx.HTTPError as exc:
        log.error("message_api_unavailable", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            content={
                "detail": "message-api unavailable",
                "request_id": request.state.request_id,
            },
            headers=headers,
        )

    response = await _forward_response(request, upstream)
    for key, value in headers.items():
        response.headers[key] = value
    return response
