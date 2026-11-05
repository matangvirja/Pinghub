"""PingHub Ops API.

Powers the UI's "System" page and the in-app Load Lab.

It is deliberately a separate service so operations code cannot slow down the
message path. It reads:
  * /health from the gateway (all nodes)
  * queue depth from the RabbitMQ management API
  * message/task counters from PostgreSQL
  * provider state from notification-mock

The Load Lab runs a bounded, in-process HTTP load test against the gateway. Each
virtual user gets its own X-API-Key, so every user has an independent 10-token
bucket (the same model as the Locust test). It records p50/p95/p99 latency,
status-code counts, queue depth over time, and how long the queue takes to
drain. That is the evidence that the system is robust under load.
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from common.logger import configure_logging, get_logger

SERVICE_NAME = os.getenv("SERVICE_NAME", "ops-api")
configure_logging(SERVICE_NAME)
log = get_logger("ops-api")

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://pinghub:pinghub@postgres:5432/pinghub"
)
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "http://notification-mock:8002")
RABBITMQ_API_URL = os.getenv("RABBITMQ_API_URL", "http://rabbitmq:15672")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "pinghub")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "pinghub")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LoadRequest(BaseModel):
    users: int = Field(default=100, ge=1, le=500)
    duration_seconds: int = Field(default=20, ge=5, le=120)


class MockRequest(BaseModel):
    mode: str = Field(pattern="^(random|always_fail|always_succeed)$")
    fail_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: int | None = Field(default=None, ge=0, le=5000)


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * (p / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2
    )


class LoadRun:
    """Mutable state for at most one load test at a time."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.status = "idle"
        self.run_id: str | None = None
        self.params: dict = {}
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.started_monotonic: float | None = None
        self.total_requests = 0
        self.status_counts: Counter = Counter()
        self.endpoint_counts: Counter = Counter()
        self.endpoint_errors: Counter = Counter()
        self.latencies: list[float] = []
        self.send_latencies: list[float] = []
        self.read_latencies: list[float] = []
        self.samples: list[dict] = []
        self.queue_peak = 0
        self.queue_end = 0
        self.drain_seconds: float | None = None
        self.db: dict = {}
        self.verdict: str | None = None
        self.verdict_reason: str | None = None
        self.stopped = False
        self.error: str | None = None

    def record(self, endpoint: str, status_code: int, latency_ms: float) -> None:
        self.total_requests += 1
        self.endpoint_counts[endpoint] += 1
        self.latencies.append(latency_ms)
        if endpoint == "send":
            self.send_latencies.append(latency_ms)
        else:
            self.read_latencies.append(latency_ms)

        if status_code == 0:
            self.status_counts["network_error"] += 1
            self.endpoint_errors[endpoint] += 1
        else:
            self.status_counts[str(status_code)] += 1

    @property
    def server_errors(self) -> int:
        return sum(
            count
            for status, count in self.status_counts.items()
            if status.isdigit() and int(status) >= 500
        )

    def snapshot(self) -> dict:
        elapsed = 0.0
        if self.started_monotonic is not None:
            elapsed = max(0.0, time.monotonic() - self.started_monotonic)

        status_counts = dict(self.status_counts)
        accepted = status_counts.get("202", 0)
        rate_limited = status_counts.get("429", 0)

        return {
            "status": self.status,
            "run_id": self.run_id,
            "params": self.params,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(elapsed, 1),
            "total_requests": self.total_requests,
            "rps": round(self.total_requests / elapsed, 1) if elapsed > 0 else 0,
            "status_counts": status_counts,
            "accepted": accepted,
            "rate_limited": rate_limited,
            "server_errors": self.server_errors,
            "endpoints": dict(self.endpoint_counts),
            "endpoint_errors": dict(self.endpoint_errors),
            "latency_ms": {
                "p50": _percentile(self.latencies, 50),
                "p95": _percentile(self.latencies, 95),
                "p99": _percentile(self.latencies, 99),
            },
            "send_latency_ms": {
                "p50": _percentile(self.send_latencies, 50),
                "p95": _percentile(self.send_latencies, 95),
                "p99": _percentile(self.send_latencies, 99),
            },
            "read_latency_ms": {
                "p50": _percentile(self.read_latencies, 50),
                "p95": _percentile(self.read_latencies, 95),
                "p99": _percentile(self.read_latencies, 99),
            },
            "queue_peak": self.queue_peak,
            "queue_end": self.queue_end,
            "drain_seconds": (
                round(self.drain_seconds, 1)
                if self.drain_seconds is not None
                else None
            ),
            "db": self.db,
            "samples": self.samples[-120:],
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "stopped": self.stopped,
            "error": self.error,
        }


RUN = LoadRun()
RUN_TASK: asyncio.Task | None = None
STOP_EVENT: asyncio.Event | None = None
RUN_LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        limits=httpx.Limits(max_connections=1200, max_keepalive_connections=600),
    )
    app.state.pool = AsyncConnectionPool(
        DATABASE_URL, min_size=1, max_size=6, open=False
    )
    await app.state.pool.open()
    log.info("ops_api_started", extra={"gateway": GATEWAY_URL})
    try:
        yield
    finally:
        global RUN_TASK
        if STOP_EVENT is not None:
            STOP_EVENT.set()
        if RUN_TASK is not None:
            RUN_TASK.cancel()
            try:
                await RUN_TASK
            except (asyncio.CancelledError, Exception):
                pass
        await app.state.http.aclose()
        await app.state.pool.close()
        log.info("ops_api_stopped")


app = FastAPI(
    title="PingHub Ops API",
    version="1.0.0",
    description="System counters and the in-app load test",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return await cursor.fetchall()


async def _fetchone(sql: str, params: tuple = ()) -> dict | None:
    async with app.state.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return await cursor.fetchone()


async def _health() -> dict:
    try:
        response = await app.state.http.get(f"{GATEWAY_URL}/health", timeout=3.0)
        return response.json()
    except Exception as exc:
        return {"status": "down", "nodes": {}, "error": str(exc)}


async def _queue_stats() -> dict:
    token = base64.b64encode(
        f"{RABBITMQ_USER}:{RABBITMQ_PASSWORD}".encode("utf-8")
    ).decode("ascii")
    try:
        response = await app.state.http.get(
            f"{RABBITMQ_API_URL}/api/queues/%2F/messages",
            headers={"Authorization": f"Basic {token}"},
            timeout=2.0,
        )
        if response.status_code != 200:
            return {"up": False, "messages": 0, "ready": 0, "unacked": 0, "consumers": 0}
        data = response.json()
        return {
            "up": True,
            "messages": int(data.get("messages", 0) or 0),
            "ready": int(data.get("messages_ready", 0) or 0),
            "unacked": int(data.get("messages_unacknowledged", 0) or 0),
            "consumers": int(data.get("consumers", 0) or 0),
            "publish_rate": round(float(data.get("message_stats", {}).get("publish_details", {}).get("rate", 0) or 0), 1),
            "deliver_rate": round(float(data.get("message_stats", {}).get("deliver_get_details", {}).get("rate", 0) or 0), 1),
        }
    except Exception:
        return {"up": False, "messages": 0, "ready": 0, "unacked": 0, "consumers": 0}


async def _provider_state() -> dict:
    try:
        response = await app.state.http.get(f"{NOTIFICATION_URL}/control", timeout=2.0)
        return response.json()
    except Exception:
        return {"mode": "unknown"}


async def _db_overview() -> dict:
    counts = {"QUEUED": 0, "PROCESSING": 0, "DELIVERED": 0, "READ": 0, "FAILED": 0}
    try:
        rows = await _fetchall(
            "SELECT status, COUNT(*)::int AS count FROM messages GROUP BY status"
        )
        for row in rows:
            counts[row["status"]] = row["count"]

        users = await _fetchone("SELECT COUNT(*)::int AS count FROM users")
        tasks = await _fetchone(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'PROCESSING')::int AS processing,
                   COUNT(*) FILTER (WHERE status = 'FAILED')::int AS failed,
                   COUNT(*)::int AS total
            FROM task_logs
            """
        )
        recent = await _fetchall(
            """
            SELECT t.task_id, t.status, t.attempts, t.error, t.updated_at,
                   s.username AS sender, r.username AS receiver, m.body
            FROM task_logs t
            LEFT JOIN messages m ON m.id = t.message_id
            LEFT JOIN users s ON s.id = m.sender_id
            LEFT JOIN users r ON r.id = m.receiver_id
            ORDER BY t.updated_at DESC
            LIMIT 8
            """
        )
        return {
            "messages": counts,
            "total_messages": sum(counts.values()),
            "users": users["count"] if users else 0,
            "tasks": tasks or {"processing": 0, "failed": 0, "total": 0},
            "recent": [
                {
                    "task_id": str(row["task_id"]),
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "error": row["error"],
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                    "sender": row["sender"],
                    "receiver": row["receiver"],
                    "body": (row["body"] or "")[:90],
                }
                for row in recent
            ],
        }
    except Exception as exc:
        return {
            "messages": counts,
            "total_messages": 0,
            "users": 0,
            "tasks": {"processing": 0, "failed": 0, "total": 0},
            "recent": [],
            "error": str(exc),
        }


@app.get("/live")
async def live() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/overview")
async def overview() -> dict:
    health, queue, db, provider = await asyncio.gather(
        _health(), _queue_stats(), _db_overview(), _provider_state()
    )
    return {
        "timestamp": _now_iso(),
        "health": health,
        "queue": queue,
        "database": db,
        "provider": provider,
        "loadtest": RUN.snapshot(),
    }


@app.get("/queue")
async def queue() -> dict:
    return await _queue_stats()


@app.get("/provider")
async def provider() -> dict:
    return await _provider_state()


@app.post("/provider")
async def set_provider(body: MockRequest) -> dict:
    payload = {"mode": body.mode}
    if body.fail_rate is not None:
        payload["fail_rate"] = body.fail_rate
    if body.latency_ms is not None:
        payload["latency_ms"] = body.latency_ms
    try:
        response = await app.state.http.post(
            f"{NOTIFICATION_URL}/control", json=payload, timeout=3.0
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Provider unavailable: {exc}")


@app.post("/provider/reset")
async def reset_provider() -> dict:
    try:
        response = await app.state.http.post(
            f"{NOTIFICATION_URL}/reset-stats", timeout=3.0
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Provider unavailable: {exc}")


# ---------------------------------------------------------------------------
# Load Lab
# ---------------------------------------------------------------------------
async def _db_counts_since(started_at) -> dict:
    try:
        rows = await _fetchall(
            """
            SELECT status, COUNT(*)::int AS count
            FROM messages
            WHERE created_at >= %s
            GROUP BY status
            """,
            (started_at,),
        )
        counts = {"QUEUED": 0, "PROCESSING": 0, "DELIVERED": 0, "READ": 0, "FAILED": 0}
        for row in rows:
            counts[row["status"]] = row["count"]
        return counts
    except Exception as exc:
        return {"error": str(exc)}


async def _sample_queue(started_monotonic: float, last: list) -> None:
    queue = await _queue_stats()
    now = time.monotonic()
    requests = RUN.total_requests
    rps = (requests - last[0]) / max(0.001, now - last[1])
    last[0] = requests
    last[1] = now

    depth = queue.get("messages", 0)
    RUN.queue_peak = max(RUN.queue_peak, depth)
    RUN.queue_end = depth
    RUN.samples.append(
        {
            "t": round(now - started_monotonic, 1),
            "rps": round(rps, 1),
            "queue": depth,
            "unacked": queue.get("unacked", 0),
            "consumers": queue.get("consumers", 0),
            "errors": RUN.server_errors,
        }
    )


async def _run_loadtest(users: int, duration_seconds: int, stop_event: asyncio.Event) -> None:
    client = app.state.http
    run_id = uuid.uuid4().hex[:6]
    receiver = f"load_receiver_{run_id}"

    RUN.reset()
    RUN.status = "running"
    RUN.run_id = run_id
    RUN.params = {"users": users, "duration_seconds": duration_seconds}
    RUN.started_at = _now_iso()
    RUN.started_monotonic = time.monotonic()

    provider_before: dict | None = None
    sampler_task: asyncio.Task | None = None
    try:
        # A throughput test should measure the platform, not the mock's random
        # failures. Preserve the operator's setting and restore it afterwards.
        try:
            response = await client.get(f"{NOTIFICATION_URL}/control", timeout=3.0)
            provider_before = response.json()
            await client.post(
                f"{NOTIFICATION_URL}/control",
                json={"mode": "always_succeed"},
                timeout=3.0,
            )
        except Exception:
            provider_before = None

        started_db = await _fetchone("SELECT now() AS ts")
        started_db_ts = started_db["ts"] if started_db else None

        end = RUN.started_monotonic + duration_seconds
        last = [0, RUN.started_monotonic]

        async def sampler() -> None:
            while RUN.status in ("running", "draining"):
                await asyncio.sleep(1.0)
                try:
                    await _sample_queue(RUN.started_monotonic, last)
                except Exception:
                    pass

        sampler_task = asyncio.create_task(sampler())

        async def virtual_user(index: int) -> None:
            api_key = f"load-{run_id}-{index}"
            sender = f"load_{run_id}_{index}"
            counter = 0
            while time.monotonic() < end and not stop_event.is_set():
                counter += 1
                endpoint = "send" if counter % 10 != 0 else "history"
                started = time.perf_counter()
                try:
                    if endpoint == "send":
                        response = await client.post(
                            f"{GATEWAY_URL}/messages",
                            json={
                                "sender": sender,
                                "receiver": receiver,
                                "body": f"Load #{counter} from virtual user {index}",
                            },
                            headers={"X-API-Key": api_key},
                        )
                    else:
                        response = await client.get(
                            f"{GATEWAY_URL}/history",
                            params={"user": sender, "limit": 10},
                            headers={"X-API-Key": api_key},
                        )
                    latency = (time.perf_counter() - started) * 1000
                    RUN.record(endpoint, response.status_code, latency)
                except Exception:
                    latency = (time.perf_counter() - started) * 1000
                    RUN.record(endpoint, 0, latency)

        await asyncio.gather(*(virtual_user(i) for i in range(users)))

        RUN.status = "draining"
        drain_started = time.monotonic()
        drained = False

        # Wait for the queue and in-flight messages to settle. The queue can be
        # empty for a moment before a worker inserts the row, so also check the
        # database's QUEUED/PROCESSING count.
        for _ in range(90):
            queue = await _queue_stats()
            counts = await _db_counts_since(started_db_ts) if started_db_ts else {}
            pending = counts.get("QUEUED", 0) + counts.get("PROCESSING", 0)
            if queue.get("messages", 0) == 0 and queue.get("unacked", 0) == 0 and pending == 0:
                drained = True
                break
            await asyncio.sleep(1.0)

        RUN.drain_seconds = time.monotonic() - drain_started
        RUN.status = "completed"
        RUN.finished_at = _now_iso()

        db_counts = await _db_counts_since(started_db_ts) if started_db_ts else {}
        RUN.db = db_counts
        delivered = db_counts.get("DELIVERED", 0) + db_counts.get("READ", 0)
        failed = db_counts.get("FAILED", 0)
        pending = db_counts.get("QUEUED", 0) + db_counts.get("PROCESSING", 0)
        accepted = RUN.status_counts.get("202", 0)
        rate_limited = RUN.status_counts.get("429", 0)

        if RUN.server_errors > 0:
            RUN.verdict = "FAIL"
            RUN.verdict_reason = (
                f"{RUN.server_errors} server/network errors occurred. "
                "At least one request never reached a structured response."
            )
        elif pending > 0:
            RUN.verdict = "WARN"
            RUN.verdict_reason = (
                f"{pending} messages were still QUEUED/PROCESSING when the drain "
                "window closed. The queue is not keeping up with the arrival rate."
            )
        elif not drained:
            RUN.verdict = "WARN"
            RUN.verdict_reason = (
                "The queue did not fully drain within 90 seconds. Increase worker "
                "capacity or reduce the arrival rate."
            )
        elif delivered + failed < accepted:
            RUN.verdict = "WARN"
            RUN.verdict_reason = (
                f"{accepted - delivered - failed} accepted messages could not be "
                "accounted for after draining. Check worker logs for lost tasks."
            )
        else:
            RUN.verdict = "PASS"
            RUN.verdict_reason = (
                f"{accepted} messages were accepted, "
                f"{delivered} delivered and {failed} permanently failed with 0 server "
                f"errors. {rate_limited} requests were rejected by the rate limiter as "
                "designed, and the queue drained in "
                f"{RUN.drain_seconds:.1f}s."
            )

    except asyncio.CancelledError:
        RUN.status = "completed"
        RUN.error = "Load test cancelled"
        RUN.finished_at = _now_iso()
        raise
    except Exception as exc:
        RUN.status = "completed"
        RUN.error = str(exc)
        RUN.finished_at = _now_iso()
        log.exception("loadtest_failed", extra={"error": str(exc)})
    finally:
        if sampler_task is not None:
            sampler_task.cancel()
            try:
                await sampler_task
            except (asyncio.CancelledError, Exception):
                pass
        if provider_before and provider_before.get("mode"):
            try:
                await client.post(
                    f"{NOTIFICATION_URL}/control",
                    json={
                        "mode": provider_before["mode"],
                        "fail_rate": provider_before.get("fail_rate", 0.3),
                        "latency_ms": provider_before.get("latency_ms", 50),
                    },
                    timeout=3.0,
                )
            except Exception:
                pass


@app.post("/loadtest")
async def start_loadtest(body: LoadRequest):
    global RUN_TASK, STOP_EVENT

    async with RUN_LOCK:
        if RUN.status in ("running", "draining"):
            raise HTTPException(status_code=409, detail="A load test is already running.")

        STOP_EVENT = asyncio.Event()
        RUN_TASK = asyncio.create_task(
            _run_loadtest(body.users, body.duration_seconds, STOP_EVENT)
        )
        await asyncio.sleep(0.05)

    return RUN.snapshot()


@app.post("/loadtest/stop")
async def stop_loadtest():
    if STOP_EVENT is not None:
        STOP_EVENT.set()
    RUN.stopped = True
    return RUN.snapshot()


@app.get("/loadtest")
async def loadtest_status():
    return RUN.snapshot()
