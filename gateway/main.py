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
import hmac
import json
import math
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote

import aio_pika
import httpx
import redis.asyncio as redis
from fastapi import Body, FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator

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

# A shareable public id looks like 7F3A9C21.
PUBLIC_ID_RE = re.compile(r"^[A-Fa-f0-9]{8}$")


class MessageIn(BaseModel):
    sender: str = Field(..., min_length=1, max_length=64)
    receiver: str = Field(..., min_length=1, max_length=64)
    # A file message may have an empty body; the filename becomes the notifier
    # text. Text-only messages (smoke test, Locust, Load Lab) are unchanged.
    body: str = Field(default="", max_length=4000)
    attachment_id: str | None = Field(default=None, max_length=64)

    @field_validator("sender", "receiver")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def body_or_attachment_required(self):
        if not self.body and not self.attachment_id:
            raise ValueError("body or attachment_id is required")
        return self


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


def _is_internal_request(request: Request) -> bool:
    supplied = request.headers.get("x-internal-token")
    return bool(supplied) and hmac.compare_digest(supplied, settings.internal_token)


def _auth_headers(request: Request) -> dict[str, str]:
    """Headers for an internal message-api call.

    The employee's own bearer token is forwarded, so message-api remains the
    single source of truth for session validation and ownership.
    """

    headers = {"X-Request-ID": request.state.request_id}
    authorization = request.headers.get("authorization")
    if authorization:
        headers["Authorization"] = authorization
    if _is_internal_request(request):
        headers["X-Internal-Token"] = settings.internal_token
    return headers


async def _require_auth(request: Request) -> tuple[dict | None, Response | None]:
    """Return the employee session, or a ready-made 401/403/503 response."""

    if _is_internal_request(request):
        return {"internal": True, "user": None}, None

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None, JSONResponse(
            status_code=401,
            content={
                "detail": "Sign in required",
                "request_id": request.state.request_id,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        upstream = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}/auth/me",
            headers={
                "Authorization": authorization,
                "X-Request-ID": request.state.request_id,
            },
        )
    except httpx.HTTPError as exc:
        log.error("auth_api_unavailable", extra={"error": str(exc)})
        return None, JSONResponse(
            status_code=503,
            content={
                "detail": "Authentication service unavailable",
                "request_id": request.state.request_id,
            },
        )

    if upstream.status_code in (401, 403):
        try:
            detail = upstream.json().get("detail") or "Sign in required"
        except Exception:
            detail = "Sign in required"
        return None, JSONResponse(
            status_code=upstream.status_code,
            content={"detail": detail, "request_id": request.state.request_id},
            headers={"WWW-Authenticate": "Bearer"} if upstream.status_code == 401 else None,
        )
    if upstream.status_code != 200:
        return None, JSONResponse(
            status_code=503,
            content={
                "detail": "Authentication service unavailable",
                "request_id": request.state.request_id,
            },
        )

    try:
        user = upstream.json().get("user")
    except Exception:
        user = None
    if not user:
        return None, JSONResponse(
            status_code=401,
            content={
                "detail": "Sign in required",
                "request_id": request.state.request_id,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"internal": False, "user": user}, None


def _claims_match(session: dict, identifier: str) -> bool:
    if session.get("internal"):
        return True
    user = session.get("user") or {}
    value = (identifier or "").strip().lower()
    return value in {
        (user.get("username") or "").lower(),
        (user.get("public_id") or "").lower(),
        (user.get("id") or "").lower(),
    }


def _limit_key(request: Request, session: dict | None, fallback: str | None) -> str:
    """Authenticated employees get one bucket; synthetic clients use X-API-Key."""

    if session is not None and not session.get("internal"):
        user = session.get("user") or {}
        return f"user:{(user.get('username') or fallback or 'unknown').lower()}"
    return _client_key(request, fallback)


async def _guard(
    request: Request, identifier: str | None = None
) -> tuple[dict | None, Response | None]:
    """Require a session and, when given, that it owns ``identifier``."""

    session, error = await _require_auth(request)
    if error is not None:
        return None, error
    if identifier is not None and not _claims_match(session, identifier):
        return None, JSONResponse(
            status_code=403,
            content={
                "detail": "You can only access your own conversations.",
                "request_id": request.state.request_id,
            },
        )
    return session, None


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

    # 0. Every send needs a signed-in employee. The internal token is reserved
    #    for the smoke test, Locust and the in-app Load Lab.
    session, auth_error = await _require_auth(request)
    if auth_error is not None:
        return auth_error

    # Synthetic traffic never uploads files; attachments require a real session.
    if payload.attachment_id and session.get("internal"):
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Attachments require an employee session.",
                "request_id": request_id,
            },
        )

    if session["internal"]:
        # Synthetic load traffic keeps the old behaviour: the worker creates the
        # fake users. A caller may also address someone by public id.
        sender, sender_found = await _canonical_identifier(request, payload.sender)
        if not sender_found:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Sender {payload.sender} not found",
                    "request_id": request_id,
                },
            )
        receiver, receiver_found = await _canonical_identifier(request, payload.receiver)
        if not receiver_found:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Recipient {payload.receiver} not found",
                    "request_id": request_id,
                },
            )
    else:
        if not _claims_match(session, payload.sender):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "You can only send messages as yourself.",
                    "request_id": request_id,
                },
            )
        # The bearer token is the source of truth: never publish a raw public id
        # or UUID as the sender username.
        sender = session["user"]["username"]

        # Real employees must address an account that already exists. This is
        # what stops a new device from silently inventing a profile.
        receiver, lookup = await _resolve_recipient(request, payload.receiver)
        if lookup == "not_found":
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Recipient {payload.receiver} not found",
                    "request_id": request_id,
                },
            )
        if lookup == "unavailable":
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Directory unavailable, please retry",
                    "request_id": request_id,
                },
            )

    # A file message is only queued after message-api confirms the uploader owns
    # the file (and that a reused file belongs to a FAILED message).
    attachment_meta = None
    if payload.attachment_id:
        attachment_meta, attachment_error = await _validate_attachment(
            request, payload.attachment_id, session, request_id
        )
        if attachment_error is not None:
            return attachment_error

    client_key = _limit_key(request, session, sender)

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
        "sender": sender,
        "receiver": receiver,
        "body": payload.body,
        "attachment_id": payload.attachment_id,
        "attachment_filename": (
            attachment_meta.get("filename") if attachment_meta else None
        ),
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
        "sender": sender,
        "receiver": receiver,
        "body": payload.body,
        "attachment_id": payload.attachment_id,
        # The worker uses the filename as notifier text when body is empty.
        "attachment_filename": (
            attachment_meta.get("filename") if attachment_meta else None
        ),
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
            "attachment_id": payload.attachment_id,
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


async def _proxy_get(
    request: Request,
    path: str,
    *,
    params: dict | None = None,
) -> Response:
    """Proxy a light read request to message-api."""

    try:
        upstream = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}{path}",
            params=params,
            headers=_auth_headers(request),
        )
    except httpx.HTTPError as exc:
        log.error("message_api_unavailable", extra={"error": str(exc), "path": path})
        return JSONResponse(
            status_code=503,
            content={
                "detail": "message-api unavailable",
                "request_id": request.state.request_id,
            },
        )
    return await _forward_response(request, upstream)


async def _proxy_json(
    request: Request,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
) -> Response:
    """Proxy a small write request to message-api."""

    try:
        upstream = await request.app.state.http.request(
            method,
            f"{settings.message_api_url}{path}",
            json=payload,
            headers=_auth_headers(request),
        )
    except httpx.HTTPError as exc:
        log.error("message_api_unavailable", extra={"error": str(exc), "path": path})
        return JSONResponse(
            status_code=503,
            content={
                "detail": "message-api unavailable",
                "request_id": request.state.request_id,
            },
        )
    return await _forward_response(request, upstream)


async def _canonical_identifier(request: Request, value: str) -> tuple[str, bool]:
    """Turn a public id into the canonical username.

    Plain usernames are returned untouched, so the normal send path does not
    add an internal HTTP call. Only values shaped like a public id are looked
    up, and the result is cached in Redis for five minutes.
    """

    if not PUBLIC_ID_RE.fullmatch(value):
        return value, True

    cache_key = f"pinghub:userresolve:{value.upper()}"
    try:
        cached = await request.app.state.redis.get(cache_key)
        if cached:
            return cached, True
    except Exception:
        pass

    try:
        response = await request.app.state.http.get(
            f"{settings.message_api_url}/users/{value}",
            headers=_auth_headers(request),
        )
    except httpx.HTTPError:
        # message-api is down; degrade to the old behaviour instead of blocking
        # message publishing. The worker will upsert the name it receives.
        return value, True

    if response.status_code == 404:
        return value, False
    if response.status_code != 200:
        return value, True

    try:
        username = response.json().get("username") or value
    except Exception:
        return value, True

    try:
        await request.app.state.redis.set(cache_key, username, ex=300)
    except Exception:
        pass
    return username, True


async def _resolve_recipient(request: Request, value: str) -> tuple[str, str]:
    """Look up a real account for an authenticated send.

    Returns ``(username, "ok" | "not_found" | "unavailable")``. The result is
    cached for five minutes so a chatty conversation does not hammer the
    directory. Unknown recipients are rejected instead of being auto-created.
    """

    cache_key = f"pinghub:userlookup:{value.lower()}"
    try:
        cached = await request.app.state.redis.get(cache_key)
        if cached == "!missing":
            return value, "not_found"
        if cached:
            return cached, "ok"
    except Exception:
        pass

    try:
        response = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}/users/{quote(value, safe='')}",
            headers=_auth_headers(request),
        )
    except httpx.HTTPError as exc:
        log.error("recipient_lookup_failed", extra={"error": str(exc)})
        return value, "unavailable"

    if response.status_code == 404:
        try:
            await request.app.state.redis.set(cache_key, "!missing", ex=60)
        except Exception:
            pass
        return value, "not_found"
    if response.status_code != 200:
        return value, "unavailable"

    try:
        username = response.json().get("username") or value
    except Exception:
        return value, "unavailable"

    try:
        await request.app.state.redis.set(cache_key, username, ex=300)
    except Exception:
        pass
    return username, "ok"


async def _validate_attachment(
    request: Request,
    attachment_id: str,
    session: dict,
    request_id: str,
) -> tuple[dict | None, Response | None]:
    """Confirm an attachment may be attached to a new message.

    message-api is the source of truth for ownership; the gateway adds the
    "resend only a FAILED file" rule before anything is put on the queue.
    """

    try:
        upstream = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}/attachments/{quote(attachment_id, safe='')}",
            headers=_auth_headers(request),
        )
    except httpx.HTTPError as exc:
        log.error("attachment_lookup_failed", extra={"error": str(exc)})
        return None, JSONResponse(
            status_code=503,
            content={
                "detail": "Attachment service unavailable",
                "request_id": request_id,
            },
        )

    if upstream.status_code == 404:
        return None, JSONResponse(
            status_code=404,
            content={"detail": "Attachment not found", "request_id": request_id},
        )
    if upstream.status_code == 403:
        return None, JSONResponse(
            status_code=403,
            content={"detail": "Not your attachment", "request_id": request_id},
        )
    if upstream.status_code != 200:
        return None, JSONResponse(
            status_code=503,
            content={
                "detail": "Attachment service unavailable",
                "request_id": request_id,
            },
        )

    try:
        meta = upstream.json()
    except Exception:
        return None, JSONResponse(
            status_code=503,
            content={
                "detail": "Attachment service unavailable",
                "request_id": request_id,
            },
        )

    if meta.get("message_id"):
        if meta.get("message_status") != "FAILED":
            return None, JSONResponse(
                status_code=409,
                content={
                    "detail": "This file is already part of a message.",
                    "request_id": request_id,
                },
            )
        # Only the original uploader (the sender) may resend a failed file.
        uploader = (meta.get("uploader") or "").lower()
        me = ((session.get("user") or {}).get("username") or "").lower()
        if uploader and me and uploader != me:
            return None, JSONResponse(
                status_code=403,
                content={"detail": "Only the sender can resend this file.", "request_id": request_id},
            )

    return meta, None


# ---------------------------------------------------------------------------
# Authentication proxies. message-api owns password checks and sessions.
# ---------------------------------------------------------------------------
@app.get("/auth/config")
async def auth_config_proxy(request: Request):
    return await _proxy_get(request, "/auth/config")


@app.post("/auth/register")
async def auth_register_proxy(request: Request, payload: dict = Body(...)):
    return await _proxy_json(request, "POST", "/auth/register", payload=payload)


@app.post("/auth/login")
async def auth_login_proxy(request: Request, payload: dict = Body(...)):
    return await _proxy_json(request, "POST", "/auth/login", payload=payload)


@app.post("/auth/logout")
async def auth_logout_proxy(request: Request):
    return await _proxy_json(request, "POST", "/auth/logout")


@app.get("/auth/me")
async def auth_me_proxy(request: Request):
    return await _proxy_get(request, "/auth/me")


@app.post("/auth/password")
async def auth_password_proxy(request: Request, payload: dict = Body(...)):
    return await _proxy_json(request, "POST", "/auth/password", payload=payload)


@app.get("/users")
async def list_users_proxy(
    request: Request,
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
):
    _, error = await _guard(request)
    if error is not None:
        return error
    params: dict = {"limit": limit}
    if q:
        params["q"] = q
    return await _proxy_get(request, "/users", params=params)


@app.get("/users/{identifier}")
async def get_user_proxy(identifier: str, request: Request):
    # Any signed-in employee may look up a colleague (New chat picker and
    # recipient resolution need this).
    _, error = await _guard(request)
    if error is not None:
        return error
    return await _proxy_get(request, f"/users/{quote(identifier, safe='')}")


@app.get("/users/{identifier}/conversations")
async def list_conversations_proxy(
    identifier: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
):
    _, error = await _guard(request, identifier)
    if error is not None:
        return error
    return await _proxy_get(
        request,
        f"/users/{quote(identifier, safe='')}/conversations",
        params={"limit": limit},
    )


@app.get("/users/{identifier}/conversations/{peer}")
async def get_thread_proxy(
    identifier: str,
    peer: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=300),
):
    _, error = await _guard(request, identifier)
    if error is not None:
        return error
    return await _proxy_get(
        request,
        f"/users/{quote(identifier, safe='')}/conversations/{quote(peer, safe='')}",
        params={"limit": limit},
    )


@app.post("/users/{identifier}/conversations/{peer}/read")
async def mark_read_proxy(identifier: str, peer: str, request: Request):
    _, error = await _guard(request, identifier)
    if error is not None:
        return error
    return await _proxy_json(
        request,
        "POST",
        f"/users/{quote(identifier, safe='')}/conversations/{quote(peer, safe='')}/read",
    )


@app.delete("/users/{identifier}/conversations/{peer}")
async def delete_conversation_proxy(identifier: str, peer: str, request: Request):
    _, error = await _guard(request, identifier)
    if error is not None:
        return error
    return await _proxy_json(
        request,
        "DELETE",
        f"/users/{quote(identifier, safe='')}/conversations/{quote(peer, safe='')}",
    )


@app.get("/messages/{task_id}")
async def get_message_status(task_id: str, request: Request):
    """Light request: proxy status lookup to message-api (no rate limit)."""

    _, error = await _guard(request)
    if error is not None:
        return error

    try:
        upstream = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}/messages/{task_id}",
            headers=_auth_headers(request),
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

    session, error = await _guard(request, user)
    if error is not None:
        return error

    client_key = _limit_key(request, session, user)
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
            headers=_auth_headers(request),
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


# ---------------------------------------------------------------------------
# Chat file attachments. message-api stores the bytes and enforces who may
# read them; the gateway forwards the authenticated request and its headers.
# ---------------------------------------------------------------------------
@app.post("/attachments", status_code=201)
async def upload_attachment_proxy(request: Request, file: UploadFile = File(...)):
    session, error = await _require_auth(request)
    if error is not None:
        return error
    if session.get("internal"):
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Attachments require an employee session.",
                "request_id": request.state.request_id,
            },
        )

    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        return JSONResponse(
            status_code=413,
            content={
                "detail": (
                    f"Files must be at most "
                    f"{settings.max_upload_bytes // (1024 * 1024)} MB."
                ),
                "request_id": request.state.request_id,
            },
        )

    filename = file.filename or "attachment"
    content_type = file.content_type or "application/octet-stream"
    try:
        upstream = await request.app.state.http.post(
            f"{settings.message_api_url}/attachments",
            files={"file": (filename, content, content_type)},
            headers=_auth_headers(request),
            timeout=settings.upload_timeout,
        )
    except httpx.HTTPError as exc:
        log.error("attachment_upload_failed", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Attachment service unavailable",
                "request_id": request.state.request_id,
            },
        )
    return await _forward_response(request, upstream)


@app.get("/attachments/{attachment_id}/content")
async def attachment_content_proxy(attachment_id: str, request: Request):
    _, error = await _guard(request)
    if error is not None:
        return error
    try:
        upstream = await _upstream_get(
            request.app.state.http,
            f"{settings.message_api_url}/attachments/{quote(attachment_id, safe='')}/content",
            headers=_auth_headers(request),
            timeout=settings.upload_timeout,
        )
    except httpx.HTTPError as exc:
        log.error("attachment_download_failed", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Attachment service unavailable",
                "request_id": request.state.request_id,
            },
        )
    return await _forward_response(request, upstream)


@app.get("/attachments/{attachment_id}")
async def attachment_metadata_proxy(attachment_id: str, request: Request):
    _, error = await _guard(request)
    if error is not None:
        return error
    return await _proxy_get(
        request,
        f"/attachments/{quote(attachment_id, safe='')}",
    )


@app.delete("/attachments/{attachment_id}")
async def attachment_delete_proxy(attachment_id: str, request: Request):
    _, error = await _guard(request)
    if error is not None:
        return error
    return await _proxy_json(
        request,
        "DELETE",
        f"/attachments/{quote(attachment_id, safe='')}",
    )
