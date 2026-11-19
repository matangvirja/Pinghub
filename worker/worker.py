"""PingHub worker: consumes RabbitMQ tasks and finishes the message pipeline.

Loop for one task:
  1. save users + message + task_log with status PROCESSING (PostgreSQL)
  2. call notification-mock with retries and a circuit breaker
  3. update message/task_log to DELIVERED or FAILED
  4. refresh the Redis task state and invalidate the two history keys

The worker also writes a heartbeat key to Redis every 10 seconds. The gateway's
/health uses those keys to count live worker replicas.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from contextlib import suppress
from urllib.parse import urlparse

import aio_pika
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from common.logger import configure_logging, get_logger, request_id_var
from db import finalize, save_processing
from notifier import Notifier
from resilience import CircuitBreaker, CircuitOpenError, RetryError

SERVICE_NAME = "worker"
configure_logging(SERVICE_NAME)
log = get_logger("worker")

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://pinghub:pinghub@postgres:5432/pinghub"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
RABBITMQ_URL = os.getenv(
    "RABBITMQ_URL", "amqp://pinghub:pinghub@rabbitmq:5672/"
)
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "http://notification-mock:8002")
QUEUE_NAME = os.getenv("QUEUE_NAME", "messages")

RETRY_ATTEMPTS = int(os.getenv("NOTIFY_RETRY_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("NOTIFY_RETRY_BASE_DELAY", "1.0"))
CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "5"))
CIRCUIT_RECOVERY_SECONDS = float(os.getenv("CIRCUIT_RECOVERY_SECONDS", "30"))
PREFETCH_COUNT = int(os.getenv("WORKER_PREFETCH", "5"))

WORKER_ID = os.getenv("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
HEARTBEAT_KEY = f"pinghub:worker:heartbeat:{WORKER_ID}"
HEARTBEAT_TTL_SECONDS = 30
TASK_PREFIX = "pinghub:task"
HISTORY_PREFIX = "pinghub:history:v1"


async def wait_for_rabbitmq(url: str, timeout: float = 120.0) -> None:
    """Wait until the broker's AMQP port is actually accepting connections.

    Docker's healthcheck can report RabbitMQ healthy while the Erlang node is up
    but the AMQP listener is still starting. Connecting in that window can leave
    a robust-connection retry loop stuck, so we wait for the TCP port first.
    """

    parsed = urlparse(url)
    host = parsed.hostname or "rabbitmq"
    port = parsed.port or 5672
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return
        except Exception:
            log.info("waiting_for_rabbitmq", extra={"host": host, "port": port})
            await asyncio.sleep(2.0)

    raise RuntimeError(f"RabbitMQ did not become reachable at {host}:{port}")


async def heartbeat(redis_client: redis.Redis) -> None:
    while True:
        try:
            await redis_client.set(
                HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS
            )
        except Exception as exc:
            log.warning("heartbeat_failed", extra={"error": str(exc)})
        await asyncio.sleep(10)


async def set_task_state(redis_client: redis.Redis, task_id: str, **fields) -> None:
    """Merge new fields into the Redis copy of the task state."""

    key = f"{TASK_PREFIX}:{task_id}"
    try:
        current = await redis_client.get(key)
        state = json.loads(current) if current else {"task_id": task_id}
        state.update(fields)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await redis_client.set(key, json.dumps(state), ex=86400)
    except Exception as exc:
        log.warning("task_state_update_failed", extra={"task_id": task_id, "error": str(exc)})


async def invalidate_history(redis_client: redis.Redis, *usernames: str) -> None:
    keys = [f"{HISTORY_PREFIX}:{name}" for name in usernames if name]
    if not keys:
        return
    try:
        await redis_client.delete(*keys)
    except Exception as exc:
        log.warning("history_invalidate_failed", extra={"error": str(exc)})


async def handle_task(
    body: bytes,
    *,
    pool: AsyncConnectionPool,
    notifier: Notifier,
    redis_client: redis.Redis,
) -> None:
    try:
        payload = json.loads(body)
    except Exception:
        log.error("invalid_task_payload", extra={"reason": "not json"})
        return

    required = ("task_id", "message_id", "sender", "receiver", "body")
    if any(key not in payload for key in required):
        log.error("invalid_task_payload", extra={"payload_keys": sorted(payload)})
        return

    task_id = payload["task_id"]
    message_id = payload["message_id"]
    request_id = payload.get("request_id", "-")
    sender = payload["sender"]
    receiver = payload["receiver"]
    text = payload["body"]
    attachment_id = payload.get("attachment_id")
    # A file-only message has an empty body; the provider sees the filename.
    notifier_text = text or payload.get("attachment_filename") or "File attachment"

    token = request_id_var.set(request_id)
    try:
        log.info("task_received", extra={"task_id": task_id, "message_id": message_id})

        should_notify = await save_processing(
            pool,
            task_id=task_id,
            message_id=message_id,
            request_id=request_id,
            sender=sender,
            receiver=receiver,
            body=text,
            attachment_id=attachment_id,
        )

        if not should_notify:
            log.info(
                "already_delivered",
                extra={"task_id": task_id, "message_id": message_id},
            )
            await set_task_state(
                redis_client,
                task_id,
                status="DELIVERED",
                message_id=message_id,
                attempts=0,
                error=None,
            )
            return

        await set_task_state(
            redis_client,
            task_id,
            status="PROCESSING",
            message_id=message_id,
            attempts=0,
            error=None,
        )
        log.info(
            "db_saved",
            extra={"task_id": task_id, "message_id": message_id, "status": "PROCESSING"},
        )

        attempts = 0
        try:
            _result, attempts = await notifier.send(
                to=receiver,
                body=notifier_text,
                request_id=request_id,
                task_id=task_id,
            )
            status = "DELIVERED"
            error = None
        except CircuitOpenError as exc:
            status = "FAILED"
            error = f"circuit_open: {exc}"
            attempts = 0
        except RetryError as exc:
            status = "FAILED"
            error = str(exc)
            attempts = exc.attempts
        except Exception as exc:
            status = "FAILED"
            error = str(exc)
            attempts = attempts or 0

        await finalize(
            pool,
            task_id=task_id,
            message_id=message_id,
            status=status,
            attempts=attempts,
            error=error,
        )
        await set_task_state(
            redis_client,
            task_id,
            status=status,
            message_id=message_id,
            attempts=attempts,
            error=error,
        )
        await invalidate_history(redis_client, sender, receiver)

        log.info(
            "db_updated",
            extra={
                "task_id": task_id,
                "message_id": message_id,
                "status": status,
                "attempts": attempts,
                "error": error,
            },
        )
    finally:
        request_id_var.reset(token)


async def main() -> None:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)
    await pool.open()

    breaker = CircuitBreaker(
        failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
        recovery_timeout=CIRCUIT_RECOVERY_SECONDS,
        name="notification",
    )
    notifier = Notifier(
        NOTIFICATION_URL,
        breaker,
        max_attempts=RETRY_ATTEMPTS,
        base_delay=RETRY_BASE_DELAY,
    )

    # Wait for the broker before opening the robust connection, then start the
    # heartbeat only after the consumer is ready. /health should not report a
    # worker as "up" while it is still unable to consume tasks.
    await wait_for_rabbitmq(RABBITMQ_URL)

    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=PREFETCH_COUNT)
    queue = await channel.declare_queue(QUEUE_NAME, durable=True)

    heartbeat_task = asyncio.create_task(heartbeat(redis_client))

    log.info(
        "worker_started",
        extra={
            "worker_id": WORKER_ID,
            "queue": QUEUE_NAME,
            "prefetch": PREFETCH_COUNT,
            "retry_attempts": RETRY_ATTEMPTS,
            "circuit_failure_threshold": CIRCUIT_FAILURE_THRESHOLD,
            "circuit_recovery_seconds": CIRCUIT_RECOVERY_SECONDS,
        },
    )

    in_flight: set[asyncio.Task] = set()

    async def process(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        # process(requeue=True): ack on success, requeue if handle_task raises
        # (for example a transient database error).  Catching the exception
        # outside the context manager lets it requeue while still marking the
        # asyncio task as handled for logging purposes.
        try:
            async with message.process(requeue=True):
                await handle_task(
                    message.body,
                    pool=pool,
                    notifier=notifier,
                    redis_client=redis_client,
                )
        except Exception:
            log.exception(
                "task_failed_requeue",
                extra={"reason": "unhandled worker error"},
            )

    try:
        async with queue.iterator() as iterator:
            async for message in iterator:
                # Run up to `prefetch_count` tasks at once per worker.  Retry
                # backoff then waits in the background instead of blocking the
                # whole consumer, so one slow provider call cannot stall the
                # queue.  RabbitMQ's prefetch limit bounds the number of
                # unacknowledged messages, so this cannot grow without limit.
                task = asyncio.create_task(process(message))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
    finally:
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await notifier.close()
        await connection.close()
        await pool.close()
        await redis_client.aclose()
        log.info("worker_stopped", extra={"worker_id": WORKER_ID})


if __name__ == "__main__":
    asyncio.run(main())
