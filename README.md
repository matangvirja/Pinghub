# PingHub — Gateway-Based Messaging and Notification Platform

PingHub is a **scalable messaging backend**. A user sends a message and gets an
instant `202 Accepted` reply. The system then processes and delivers the message
in the background, retries when the notification provider fails, caches message
history, and protects itself with a token-bucket rate limiter.

It stays responsive under load because the gateway does only three things:
validate, rate-limit, and push work into a queue. Everything slow happens in
workers.

> **One-line viva answer:** “PingHub is a scalable messaging backend. The user
> sends a message and gets an instant response. The system processes and
> delivers it in the background, and stays reliable under heavy load and
> failures.”

---

## 1. Architecture

```text
                       +--------------------------------------+
                       |        API GATEWAY  :8000            |
 Client / Locust ----> | request_id · routing · token bucket  |
                       | 429 when bucket empty · /health      |
                       +------+----------------------+--------+
                              |                      |
                    light     |                      | heavy: POST /messages
                    requests  |                      v
                              |              +---------------+
                              |              |   RabbitMQ    |
                              |              |  queue: msgs  |
                              |              +-------+-------+
                              v                      |
                    +------------------+             v
                    |   Message API    |     +----------------------+
                    |      :8001       |     | Worker pool (2-3)    |
                    | history + status |     | save · notify · retry|
                    +--------+---------+     | circuit breaker      |
                             |               +---+--------------+---+
                             v                   |              |
                       +-----------+             v              v
                       |   Redis   |      +-----------+  +----------------+
                       | cache +   |      | PostgreSQL|  | notification-  |
                       | counters  |      | users     |  | mock :8002     |
                       +-----------+      | messages  |  | can fail       |
                                          | task_logs |  +----------------+
                                          +-----------+
```

Full diagrams and the request lifecycle are in
[`docs/architecture.md`](docs/architecture.md).

### Message status lifecycle

```text
QUEUED -> PROCESSING -> DELIVERED
                     \-> FAILED   (notification retries exhausted / circuit open)
```

### Request flow for `POST /messages`

1. Client sends `POST /messages` with `sender`, `receiver`, `body`.
2. Gateway assigns a `request_id` and checks the token bucket.
   Empty bucket -> `429 Too Many Requests` with `Retry-After`.
3. Gateway generates `task_id` + `message_id`, writes the initial `QUEUED`
   state to Redis, publishes a persistent task to RabbitMQ, and immediately
   returns `202 Accepted` with the `task_id`.
4. A worker consumes the task, creates the users/message/task-log rows with
   status `PROCESSING`, and calls the notification mock.
5. The call is wrapped in **retries with exponential backoff** (3 attempts;
   waits of 1s then 2s) and a **circuit breaker** (CLOSED -> OPEN after 5
   failures -> HALF_OPEN after 30s).
6. The worker sets the final status (`DELIVERED` or `FAILED`) in PostgreSQL and
   Redis, then deletes the two affected history cache keys.

### Read flow for `GET /history?user=alice`

Gateway -> Message API -> Redis hit? return immediately
                                  miss? query PostgreSQL, store result in Redis
                                  with a 60-second TTL, return it.

---

## 2. Services

| Service | Port | Responsibility |
|---|---|---|
| `gateway` | 8000 | Request ID, routing, token-bucket limiter (429), `/health`, queue publish. Runs 4 uvicorn worker processes behind one port. |
| `message-api` | 8001 | Message history (Redis cache-aside), task status |
| `worker` (2 replicas) | — | Consume queue, write DB, call notifier, retry + circuit breaker. Up to 5 tasks concurrently per replica. |
| `notification-mock` | 8002 | Fake email provider; controllable failures and latency |
| `postgres` | 5432 | `users`, `messages`, `task_logs` |
| `redis` | 6379 | History cache, task state, rate-limit buckets, worker heartbeats |
| `rabbitmq` | 5672 / 15672 | Durable task queue + management UI |
| `locust` | 8089 | Optional load-test UI (profile `loadtest`) |

---

## 3. Repository layout

```text
PingHub/
├── docker-compose.yml
├── README.md
├── common/
│   └── logger.py                 # shared JSON logging + request_id context
├── gateway/
│   ├── main.py                   # routing, request ID, queue publish, proxy
│   ├── rate_limiter.py           # Redis Lua token bucket + memory fallback
│   ├── health.py                 # aggregated /health
│   ├── config.py
│   └── Dockerfile
├── message-api/
│   └── main.py                   # history cache-aside + task status
├── worker/
│   ├── worker.py                 # RabbitMQ consumer loop, heartbeat
│   ├── db.py                     # idempotent PostgreSQL writes
│   ├── resilience.py             # exponential backoff + circuit breaker
│   ├── notifier.py               # calls notification-mock
│   └── Dockerfile
├── notification-mock/
│   └── main.py                   # randomly failing / controllable provider
├── db/migrations/                # 001 users, 002 messages, 003 task_logs
├── loadtest/
│   ├── locustfile.py
│   └── results/                  # generated CSV + queue monitor output
├── scripts/
│   ├── smoke_test.py             # 7-check end-to-end test
│   ├── failure_demo.py           # retries + circuit breaker demo
│   └── run_load_test.py          # Locust + queue-depth monitor
└── docs/
    ├── architecture.md
    ├── architecture.png
    └── load-test-report.md
```

---

## 4. Run it

### Prerequisites

- Docker Desktop (with Compose v2) running.

### Start everything

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, RabbitMQ, the notification mock, message API,
two workers, and the gateway. First build takes a few minutes because images
and Python packages are downloaded; later starts are fast.

Check the stack:

```bash
docker compose ps
```

Open:

| What | URL |
|---|---|
| Gateway health | http://localhost:8000/health |
| Interactive API docs | http://localhost:8000/docs |
| RabbitMQ management | http://localhost:15672 (pinghub / pinghub) |
| Message API health | http://localhost:8001/health |
| Notification mock | http://localhost:8002/health |

Run the end-to-end smoke test from the host:

```bash
python scripts/smoke_test.py
```

Stop everything:

```bash
docker compose down
```

Delete the database volume too (fresh migrations next start):

```bash
docker compose down -v
```

### Use three workers

```bash
docker compose up --build --scale worker=3
```

---

## 5. API examples (Windows PowerShell)

### Send a message

```powershell
$body = @{
  sender   = "alice"
  receiver = "bob"
  body     = "Hello from PingHub"
} | ConvertTo-Json

$response = Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/messages `
  -ContentType "application/json" `
  -Headers @{ "X-API-Key" = "demo-alice" } `
  -Body $body

$response
$taskId = $response.task_id
```

Response:

```json
{
  "task_id": "0f0b0c4e-...",
  "message_id": "6a1f...",
  "status": "QUEUED",
  "message": "Accepted for processing",
  "request_id": "r-1a2b3c4d5e6f"
}
```

### Check status

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/messages/$taskId"
```

Possible statuses: `QUEUED`, `PROCESSING`, `DELIVERED`, `FAILED`.

### History (watch the cache)

```powershell
$first = Invoke-WebRequest -Uri "http://localhost:8000/history?user=alice"
$first.Headers["X-Cache"]     # MISS

$second = Invoke-WebRequest -Uri "http://localhost:8000/history?user=alice"
$second.Headers["X-Cache"]    # HIT
```

### Trigger the rate limiter

```powershell
1..15 | ForEach-Object {
  try {
    Invoke-WebRequest -Method Post `
      -Uri http://localhost:8000/messages `
      -ContentType "application/json" `
      -Headers @{ "X-API-Key" = "burst-demo" } `
      -Body '{"sender":"alice","receiver":"bob","body":"burst"}' |
      Select-Object -ExpandProperty StatusCode
  } catch {
    $_.Exception.Response.StatusCode.value__
  }
}
```

The first 10 requests use the burst tokens; the rest return `429`.

---

## 6. Demonstrating fault tolerance

The notification mock exposes a control endpoint:

```powershell
# Make every notification fail
Invoke-RestMethod -Method Post -Uri http://localhost:8002/control `
  -ContentType "application/json" `
  -Body '{"mode":"always_fail"}'

# Back to random failures (30%)
Invoke-RestMethod -Method Post -Uri http://localhost:8002/control `
  -ContentType "application/json" `
  -Body '{"mode":"random","fail_rate":0.3}'

# Always succeed
Invoke-RestMethod -Method Post -Uri http://localhost:8002/control `
  -ContentType "application/json" `
  -Body '{"mode":"always_succeed"}'

# Provider counters
Invoke-RestMethod -Uri http://localhost:8002/stats
```

Watch the worker logs:

```bash
docker compose logs -f worker
```

You will see `notify_failed` with `attempt: 1`, `attempt: 2`, then either
`notify_ok` or `db_updated` with `FAILED`. After five failures the circuit
opens (`circuit_open`), and after 30 seconds the next task is a HALF_OPEN
trial; one success closes it again.

### Trace one request across services

Every response includes `X-Request-ID`. Search all logs for it:

```bash
docker compose logs | Select-String "r-abc123"
```

Expected sequence:

```text
gateway  request_completed
gateway  accepted
gateway  queued
worker   task_received
worker   db_saved
worker   notify_failed   (only if the provider fails)
worker   notify_ok
worker   db_updated
```

---

## 7. Load test

### Option A — Locust UI

```bash
docker compose --profile loadtest up locust
```

Open http://localhost:8089 and use 200 users, spawn rate 50.

### Option B — Headless (recommended for the report)

```bash
docker compose --profile loadtest run --rm locust `
  -f /mnt/locust/locustfile.py --host http://gateway:8000 `
  --headless -u 200 -r 50 -t 60s --processes 4 `
  --csv /tmp/results/spike
```

`--processes 4` makes Locust use four worker processes, so the load generator
itself does not become the bottleneck.

Or run the whole experiment (Locust + RabbitMQ queue-depth sampling) with:

```bash
python scripts/run_load_test.py
```

Or locally:

```bash
pip install -r loadtest/requirements.txt
locust -f loadtest/locustfile.py --host http://localhost:8000 `
  --headless -u 200 -r 50 -t 60s --csv loadtest/results/spike
```

Results are written to `loadtest/results/`. The measured run is written up in
[`docs/load-test-report.md`](docs/load-test-report.md): 200 users, 60 s,
36,637 requests, 0 errors, `POST /messages` p95 = 37 ms.

What to look for:

- `202` responses: accepted immediately by the gateway.
- `429` responses: the per-client token bucket kicked in.
- Stable gateway latency even while the queue grows.
- Workers drain the queue in the background.

---

## 8. Configuration

All settings are environment variables with sane defaults. See `.env.example`.

| Variable | Default | Used by |
|---|---|---|
| `RATE_LIMIT_CAPACITY` | `10` | gateway |
| `RATE_LIMIT_REFILL_PER_SEC` | `1` | gateway |
| `HISTORY_CACHE_TTL` | `60` | message-api |
| `NOTIFY_MODE` | `random` | notification-mock |
| `NOTIFY_FAIL_RATE` | `0.3` | notification-mock |
| `NOTIFY_LATENCY_MS` | `50` | notification-mock |
| `NOTIFY_RETRY_ATTEMPTS` | `3` | worker |
| `NOTIFY_RETRY_BASE_DELAY` | `1.0` | worker |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | worker |
| `CIRCUIT_RECOVERY_SECONDS` | `30` | worker |
| `WORKER_PREFETCH` | `5` | worker (concurrent tasks per replica) |
| `HEALTH_CACHE_SECONDS` | `3` | gateway |
| `PROXY_TIMEOUT_SECONDS` | `5` | gateway |

---

## 9. Key design decisions (viva notes)

**Why a queue?**
The gateway must answer in milliseconds. Saving to the database and calling an
external provider takes hundreds of milliseconds and can fail. A queue
decouples “accept the request” from “finish the work”, so traffic spikes become
a longer queue instead of slow responses or crashes.

**Why a token bucket?**
It allows a short burst (10 requests) and then smooths traffic to 1 request per
second per client. Redis makes the bucket shared across gateway replicas, so
adding replicas cannot bypass the limit. If Redis dies, an in-process fallback
still protects each replica.

**Why Redis for history?**
History is read far more often than it changes. Cache-aside gives an immediate
response on the second and later reads. The worker invalidates the affected
keys when a message changes state, and a 60-second TTL bounds staleness.

**Why retries AND a circuit breaker?**
Retries handle a brief provider blip. A circuit breaker stops hammering a
provider that is clearly down, which protects the worker pool and lets queued
work fail fast instead of consuming all worker time. HALF_OPEN probes recovery
with a single call before fully closing.

**Why is the worker idempotent?**
RabbitMQ can redeliver a task if a worker crashes before acknowledging it. The
gateway generates `task_id` and `message_id`, and the worker uses `ON CONFLICT`
plus a “already DELIVERED” check, so a redelivery does not duplicate data or
resend a notification.

**Why several gateway processes?**
A single Python process is still limited to one CPU core. The gateway keeps no
request state of its own, so uvicorn can run four worker processes behind the
same port. Rate limiting stays correct because every process uses the same Redis
Lua token bucket.

**Why does each worker process several tasks at once?**
RabbitMQ's prefetch limit (5) caps unacknowledged deliveries. The worker starts
one asyncio task per delivery, so a 1-second retry backoff on one message does
not block the other four. This is what lets two replicas drain ~113 messages/s.

**Why cache `/health`?**
It aggregates every node and is polled by monitoring. Creating a new PostgreSQL
and RabbitMQ connection on every poll is wasteful; a 3-second cache keeps the
endpoint cheap without hiding a failure for long.

**Rate-limit identity:** the bucket key is `X-API-Key` if present, otherwise the
`sender`/`user` value, otherwise the client IP. This is why the smoke test and
Locust send a unique API key per simulated client.

---

## 10. Assignment checklist

| Requirement | Where |
|---|---|
| Shared repo + base Compose | this repository, `docker-compose.yml` |
| API gateway with routing | `gateway/main.py` |
| Rate limiter returning 429 | `gateway/rate_limiter.py` |
| Health endpoint per node | `gateway/health.py`, `GET /health` |
| Architecture + data flow | `docs/architecture.md`, `docs/architecture.png` |
| PostgreSQL + migrations | `db/migrations/` |
| Redis cache + rate-limit counters | `message-api/main.py`, `gateway/rate_limiter.py` |
| Async queue + worker pool | `worker/`, RabbitMQ, `--scale worker=3` |
| Retries + circuit breaker | `worker/resilience.py` |
| Load test | `loadtest/locustfile.py` |
| Structured logs with `request_id` | `common/logger.py`, all services |
| README + load-test report | `README.md`, `docs/load-test-report.md` |
| 10-minute demo | Section 11 below |

---

## 11. 10-minute demo script

| Minute | Show |
|---|---|
| 0–1 | `docs/architecture.png`; explain gateway -> queue -> worker -> DB |
| 1–2 | `docker compose ps`; open `http://localhost:8000/health` (all nodes up) |
| 2–4 | Send a message with Postman/PowerShell; show 202, worker logs, DB row |
| 4–5 | Call `/history` twice; show `X-Cache: MISS` then `HIT` |
| 5–7 | Run Locust; show 429s and stable gateway latency |
| 7–9 | Set mock to `always_fail`; show retries, circuit opening, recovery |
| 9–10 | Search logs by one `request_id`; summarize load-test numbers |

---

## 12. Troubleshooting

**`/health` shows worker down**
Give the workers a few seconds to write their first Redis heartbeat. If it
stays down, check `docker compose logs worker`.

**Everything returns 503 after startup**
PostgreSQL may still be running migrations. `docker compose ps` should show
`healthy` for `postgres`, `redis`, and `rabbitmq`.

**Port already in use**
Stop the other process or change the host side of the port mapping in
`docker-compose.yml`.

**Migrations did not run**
PostgreSQL init scripts run only on an empty data volume. Use
`docker compose down -v` and start again.

**Want a fresh database**
```bash
docker compose down -v
docker compose up --build
```
