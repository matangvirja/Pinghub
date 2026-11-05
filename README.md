# PingHub — Gateway-Based Messaging and Notification Platform

PingHub is a **scalable messaging backend with a complete end-user web app**. A
user creates a profile, shares a short unique ID, and sends messages. The
message is accepted instantly (`202`), then processed and delivered in the
background. The sender sees **Delivered** and **Read** ticks, the recipient sees
unread badges and history, and an in-app **Load Lab** proves whether the system
stays robust under load.

No terminal commands are needed to use the product. The terminal is only for
starting the stack and for the optional engineering tools.

> **One-line viva answer:** “PingHub is a scalable messaging platform. The UI
> sends a message through a real gateway, which queues it and replies instantly.
> Workers deliver it in the background, retry failures, cache history, and stay
> responsive under load — and the UI shows delivery and read receipts.”

---

## 1. Open the app

### Start everything

```bash
docker compose up --build
```

First build takes a few minutes; later starts are fast. When Compose reports the
services as healthy, open:

| What | URL |
|---|---|
| **PingHub web app** | **http://localhost:3000** |
| Gateway health | http://localhost:8000/health |
| Gateway API docs | http://localhost:8000/docs |
| Ops API docs | http://localhost:8003/docs |
| RabbitMQ management | http://localhost:15672 (pinghub / pinghub) |
| Message API health | http://localhost:8001/health |
| Notification mock | http://localhost:8002/health |

### Create your profile

1. Open http://localhost:3000.
2. Enter a username (for example `alice`) and an optional display name.
3. Click **Create profile**.
4. Your **unique ID** appears in the left sidebar, for example `7F3A9C21`.
   Share it with the other person.

### Demo two people with two tabs

Read receipts need two identities. Each browser tab keeps its own active profile
(profiles are stored in `sessionStorage`; the saved profile list is in
`localStorage`).

1. Create **Alice** in tab 1.
2. Open a second tab at http://localhost:3000 and create **Bob**.
3. In Alice's tab, click **New chat**, choose **By unique ID**, and paste Bob's
   ID. Click **Open chat**.
4. Send a message.
5. In Bob's tab, the conversation shows an unread badge. Open it.
6. Alice's message changes from **Delivered** to **Read** (double tick), and
   Alice gets a read-receipt notification.

> Tip: keep both tabs in the same browser window and switch between them. The
> app polls the server every few seconds; the tab you are viewing refreshes
> immediately.

### What each screen does

| Screen | What you can do |
|---|---|
| **Chats** | Search conversations, see unread badges, open a chat by unique ID or username, send messages, watch status ticks, resend failed messages, copy your own ID. |
| **History** | See every sent/received message with its status, filters and search, the Redis cache **HIT/MISS** badge, export JSON, and look up another user's history by ID or username. |
| **System** | Live health of every node, queue depth, message counters, provider failure simulator, recent worker tasks, and the **Load Lab** with a PASS/WARN/FAIL verdict. |

### Status meanings

| Status | Meaning | UI |
|---|---|---|
| `QUEUED` | Gateway accepted the message; it is waiting in RabbitMQ. | Clock icon, “Queued” |
| `PROCESSING` | A worker is calling the notification provider. | Spinning clock, “Sending” |
| `DELIVERED` | The provider accepted it. | Single check, “Delivered” |
| `READ` | The recipient opened the conversation. | Blue double check, “Read” + time |
| `FAILED` | All retries / the circuit breaker gave up. | Red alert, “Failed” + **Resend** |

The message path is:

```text
UI -> gateway :8000 -> RabbitMQ -> worker -> notification-mock
                                   -> PostgreSQL (message + task log)
                                   -> Redis (task state, history invalidation)
```

A detailed walkthrough is in [`docs/ui-guide.md`](docs/ui-guide.md).

---

## 2. Architecture

```text
                        +------------------------------------------+
                        |            WEB UI  :3000                 |
                        |  chats · history · system · load lab     |
                        +--------------+-------------+-------------+
                                       | /api/       | /ops/
                                       v             v
                        +--------------------+  +------------------+
                        |  API GATEWAY :8000 |  |  OPS API :8003   |
                        | request_id · 429   |  | health · queue   |
                        | queue publish      |  | provider · tests |
                        +---+------------+---+  +--------+---------+
                            |            |               |
                  light     |            | heavy         | reads
                  requests  |            | POST /messages|
                            v            v               v
                  +------------------+  +---------+   (PostgreSQL,
                  |  Message API     |  |RabbitMQ |    RabbitMQ API,
                  |     :8001        |  | queue   |    gateway /health,
                  | conversations    |  +----+----+    notification-mock)
                  | history + status |       |
                  +--------+---------+       v
                           |          +----------------------+
                           v          | Worker pool (2-3)    |
                     +-----------+    | save · notify · retry|
                     |   Redis   |    | circuit breaker      |
                     | cache +   |    +---+--------------+---+
                     | counters  |        |              |
                     +-----------+        v              v
                                    +-----------+  +----------------+
                                    | PostgreSQL|  | notification-  |
                                    | users     |  | mock :8002     |
                                    | messages  |  | can fail       |
                                    | task_logs |  +----------------+
                                    +-----------+
```

Full diagrams and the request lifecycle are in
[`docs/architecture.md`](docs/architecture.md).

### Message status lifecycle

```text
QUEUED -> PROCESSING -> DELIVERED -> READ
                     \-> FAILED   (notification retries exhausted / circuit open)
```

`READ` is set by the recipient's UI when the conversation is opened and the tab
is visible. It is a product-level receipt, not a separate delivery attempt.

### Request flow for `POST /messages`

1. The UI sends `sender`, `receiver`, and `body`. `sender`/`receiver` may be a
   username or an 8-character public ID.
2. Gateway resolves public IDs (cached in Redis), assigns a `request_id`, and
   checks the token bucket. Empty bucket -> `429` with `Retry-After`.
3. Gateway generates `task_id` + `message_id`, writes the initial `QUEUED`
   state to Redis, publishes a persistent task to RabbitMQ, and immediately
   returns `202 Accepted`.
4. A worker consumes the task, creates the users/message/task-log rows with
   status `PROCESSING`, and calls the notification mock.
5. The call is protected by **retries with exponential backoff** (3 attempts;
   1s then 2s) and a **circuit breaker** (CLOSED -> OPEN after 5 failures ->
   HALF_OPEN after 30s).
6. The worker sets `DELIVERED` or `FAILED` in PostgreSQL and Redis, records
   `delivered_at`, and deletes the affected history cache keys.

### Read flow

```text
recipient opens chat
  -> POST /users/{me}/conversations/{peer}/read
  -> message-api: UPDATE messages SET status='READ', read_at=now()
                  WHERE sender=peer AND receiver=me AND status='DELIVERED'
  -> history cache keys for both users are deleted
  -> sender's next poll sees READ and renders the double tick
```

### History read flow (`GET /history?user=alice`)

```text
Gateway -> Message API -> Redis hit?  return immediately (X-Cache: HIT)
                                  miss? query PostgreSQL, store the top 50 in
                                        Redis for 60s, return (X-Cache: MISS)
```

The UI shows that header as the **redis cache: HIT/MISS** badge on History.

---

## 3. Services

| Service | Port | Responsibility |
|---|---|---|
| `ui` | **3000** | Static end-user web app served by nginx. Proxies `/api/` to the gateway and `/ops/` to ops-api, so the browser uses one origin. |
| `gateway` | 8000 | Request ID, public-ID resolution, routing, token-bucket limiter (429), `/health`, queue publish, read-API proxy. Runs 4 uvicorn workers. |
| `message-api` | 8001 | Profiles, conversations, threads, read receipts, message history (Redis cache-aside), task status. |
| `ops-api` | 8003 | System dashboard aggregation and the in-app Load Lab. Kept separate from the message path. |
| `worker` (2 replicas) | — | Consume queue, write DB, call notifier, retry + circuit breaker. Up to 5 tasks concurrently per replica. |
| `notification-mock` | 8002 | Fake email provider; controllable failures and latency. |
| `postgres` | 5432 | `users`, `messages`, `task_logs`. |
| `redis` | 6379 | History cache, task state, rate-limit buckets, worker heartbeats, public-ID lookup cache. |
| `rabbitmq` | 5672 / 15672 | Durable task queue + management UI. |
| `migrate` | — | One-shot job that applies every SQL migration on every `compose up`, then exits. |
| `locust` | 8089 | Optional formal load-test UI (profile `loadtest`). |

---

## 4. Repository layout

```text
PingHub/
├── docker-compose.yml
├── README.md
├── .env.example
├── common/
│   └── logger.py                 # shared JSON logging + request_id context
├── gateway/
│   ├── main.py                   # routing, request ID, queue publish, proxy
│   ├── rate_limiter.py           # Redis Lua token bucket + memory fallback
│   ├── health.py                 # aggregated /health
│   └── config.py
├── message-api/
│   └── main.py                   # profiles, conversations, read, history, status
├── worker/
│   ├── worker.py                 # RabbitMQ consumer loop, heartbeat
│   ├── db.py                     # idempotent PostgreSQL writes
│   ├── resilience.py             # exponential backoff + circuit breaker
│   └── notifier.py               # calls notification-mock
├── notification-mock/
│   └── main.py                   # randomly failing / controllable provider
├── ops-api/
│   └── main.py                   # dashboard metrics + in-app load generator
├── ui/
│   ├── index.html                # onboarding, chats, history, system, modals
│   ├── app.js                    # state, polling, read receipts, Load Lab
│   ├── styles.css                # responsive dark theme
│   ├── nginx.conf                # /api -> gateway, /ops -> ops-api
│   └── Dockerfile
├── db/migrations/
│   ├── 001_create_users.sql
│   ├── 002_create_messages.sql
│   ├── 003_create_task_logs.sql
│   └── 004_ui_features.sql       # public IDs, display names, READ, timestamps
├── loadtest/
│   ├── locustfile.py
│   └── results/
├── scripts/
│   ├── smoke_test.py             # 7-check end-to-end test
│   ├── failure_demo.py           # retries + circuit breaker demo
│   └── run_load_test.py          # Locust + queue-depth monitor
└── docs/
    ├── architecture.md
    ├── architecture.png
    ├── ui-guide.md               # non-technical walkthrough
    └── load-test-report.md
```

---

## 5. Data model additions for the UI

Migration `004_ui_features.sql` adds:

| Column | Purpose |
|---|---|
| `users.public_id` | 8-character uppercase hex ID, auto-generated by the database, unique. Example `7F3A9C21`. |
| `users.display_name` | Friendly name shown in the UI (optional). |
| `messages.delivered_at` | Timestamp set when the worker reaches `DELIVERED`. |
| `messages.read_at` | Timestamp set when the recipient opens the conversation. |
| `messages.status` | Constraint extended from four states to five: `QUEUED`, `PROCESSING`, `DELIVERED`, `READ`, `FAILED`. |

Why a separate `public_id` instead of using the UUID? A UUID is long and hard to
read aloud. An 8-character ID is easy to share, and lookups accept either form.
The migration is re-runnable: the `migrate` Compose service applies every file
on every start, so existing `pgdata` volumes are upgraded automatically.

---

## 6. Using the app (non-technical guide)

### Send a message

1. Click **New chat** (top-right of the conversation list or the empty state).
2. Pick **By unique ID** and paste the other person's ID, or choose
   **Directory** and click a profile.
3. Type in the composer. Press **Enter** to send, **Shift+Enter** for a new
   line.
4. Watch the tick: clock -> spinning clock -> single check -> blue double check.

### Read receipts

When the recipient opens the chat while the tab is visible, the app marks every
delivered incoming message as `READ`. The sender sees:
- a blue double check and the read time,
- a **Read receipt** notification,
- a desktop notification if alerts were enabled.

Read receipts are best-effort. If the network drops, the next poll retries.

### Unique IDs and history lookup

- Your ID is in the sidebar and in the profile modal, with a copy button.
- **History -> Look up another user** accepts an ID, username, or UUID and shows
  that person's latest 50 messages.
- History supports text search, status filter, direction filter, and JSON
  export.

### Unread badges and notifications

The sidebar shows a count of unread incoming messages. Click **Enable desktop
alerts** to receive a browser notification when a new message arrives in a
background tab. The app polls; it does not need WebSockets.

### Rate limiting in the UI

The UI sends `X-API-Key` equal to your public ID. Each identity gets a 10-token
burst and 1 token/second refill. If you send very quickly, you will see a
friendly “Rate limit reached” hint and a `429` — this is the protection working,
not a bug.

### Two profiles in two tabs

Profiles are saved in `localStorage`, but the **active** profile is in
`sessionStorage`. Open a second tab to be a second person, or use the profile
menu to switch. Signing out only affects the current tab.

---

## 7. System dashboard and robustness (Load Lab)

Open **System** in the left rail. The page refreshes every 2 seconds while
visible.

### What it shows

- **Node health:** gateway, postgres, redis, rabbitmq, message-api,
  notification-mock, worker replicas.
- **Queue depth:** messages buffered in RabbitMQ. A spike here is the queue
  absorbing load.
- **Message counters:** total, delivered, read, failed; profiles; recent tasks
  with attempts and errors.
- **Provider simulator:** Healthy, 30% failures, Always fail, and reset
  counters. This is the same control as the notification mock API.

### Run the Load Lab

1. Set **Users** (1–500) and **Seconds** (5–120). Start with 50 users / 10 s.
2. Click **Start test**.
3. Watch requests/sec and queue depth. The phase changes from `running` to
   `draining` to `completed`.
4. Read the verdict.

The Load Lab is a bounded, in-process HTTP generator inside `ops-api`:

- each virtual user gets its own `X-API-Key`, so each has its own token bucket;
- nine requests in ten are sends, one is a cached history read;
- the provider is temporarily set to `always_succeed` so the measurement is
  about the platform, not the mock’s random failures; the previous mode is
  restored afterwards;
- it records p50/p95/p99 latency, status counts, queue samples, drain time, and
  database delivery counts.

### How to read the verdict

| Verdict | Meaning |
|---|---|
| **PASS** | 0 server/network errors, every accepted message reached `DELIVERED` or `FAILED`, and the queue fully drained. The system absorbed the spike. |
| **WARN** | The queue did not drain within 90 seconds, or messages were still `QUEUED`/`PROCESSING`, or accepted messages could not be accounted for. Add workers or reduce the arrival rate. |
| **FAIL** | At least one request returned 5xx or never reached a structured response. This is a real availability failure. |

**429 responses are not failures.** They are the token bucket rejecting excess
requests by design. The Load Lab reports them separately.

### In-app Load Lab vs Locust

The in-app lab is for a quick, explainable check inside the product. For the
formal report, use Locust with multiple processes:

```bash
docker compose --profile loadtest run --rm locust `
  -f /mnt/locust/locustfile.py --host http://gateway:8000 `
  --headless -u 200 -r 50 -t 60s --processes 4 `
  --csv /tmp/results/spike
```

The measured run is in [`docs/load-test-report.md`](docs/load-test-report.md):
200 users, 60 s, 36,637 requests, 0 errors, `POST /messages` p95 = 37 ms.

---

## 8. API reference

The UI uses these through nginx. They are also available directly on the
gateway at `:8000`.

### Profiles

```powershell
# Create or sign in
Invoke-RestMethod -Method Post -Uri http://localhost:8000/users `
  -ContentType "application/json" `
  -Body '{"username":"alice","display_name":"Alice"}'

# Directory (load-test users are hidden)
Invoke-RestMethod -Uri "http://localhost:8000/users?limit=20"

# Look up by username, public ID, or UUID
Invoke-RestMethod -Uri "http://localhost:8000/users/7F3A9C21"
```

### Conversations and read receipts

```powershell
# Chat list with unread counts
Invoke-RestMethod -Uri "http://localhost:8000/users/alice/conversations"

# Full thread (accepts username or public ID for both sides)
Invoke-RestMethod -Uri "http://localhost:8000/users/alice/conversations/bob"

# Mark all delivered messages from bob to alice as READ
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/users/alice/conversations/bob/read"
```

### Send a message

```powershell
$body = @{
  sender   = "alice"
  receiver = "7F3A9C21"     # public ID or username
  body     = "Hello from PingHub"
} | ConvertTo-Json

$response = Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/messages `
  -ContentType "application/json" `
  -Headers @{ "X-API-Key" = "demo-alice" } `
  -Body $body

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

### Check status, history, and cache

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/messages/$taskId"

$first = Invoke-WebRequest -Uri "http://localhost:8000/history?user=alice"
$first.Headers["X-Cache"]     # MISS

$second = Invoke-WebRequest -Uri "http://localhost:8000/history?user=alice"
$second.Headers["X-Cache"]    # HIT
```

Possible statuses: `QUEUED`, `PROCESSING`, `DELIVERED`, `READ`, `FAILED`.

### Ops and Load Lab

```powershell
Invoke-RestMethod -Uri http://localhost:8003/overview
Invoke-RestMethod -Uri http://localhost:8003/queue
Invoke-RestMethod -Uri http://localhost:8003/provider

# Start a 50-user, 10-second test
Invoke-RestMethod -Method Post -Uri http://localhost:8003/loadtest `
  -ContentType "application/json" `
  -Body '{"users":50,"duration_seconds":10}'

Invoke-RestMethod -Uri http://localhost:8003/loadtest
Invoke-RestMethod -Method Post -Uri http://localhost:8003/loadtest/stop
```

---

## 9. Demonstrating fault tolerance

### Through the UI

**System -> Notification provider**:

- **Healthy** — every delivery succeeds.
- **30% failures** — the normal demo default.
- **Always fail** — forces retries; after 5 failures the circuit opens.
- **Reset provider counters** — clears the mock’s counters.

Send a few messages while the provider is failing. In Chats you will see
**Failed** bubbles with a **Resend** action. Switch back to **Healthy** and
resend.

### Through the API or scripts

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
```

Watch the worker logs:

```bash
docker compose logs -f worker
```

You will see `notify_failed` with `attempt: 1`, `attempt: 2`, then either
`notify_ok` or `db_updated` with `FAILED`. After five failures the circuit
opens (`circuit_open`), and after 30 seconds the next task is a HALF_OPEN
trial; one success closes it again.

Run the scripted version:

```bash
python scripts/failure_demo.py
```

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

## 10. Testing and load tools

### Smoke test (backward-compatible)

```bash
python scripts/smoke_test.py
```

It checks `/health`, a `202` send, terminal delivery, history MISS then HIT,
and the token bucket returning `429`.

### Formal Locust load test

```bash
docker compose --profile loadtest up locust
# http://localhost:8089, 200 users, spawn rate 50
```

Headless:

```bash
docker compose --profile loadtest run --rm locust `
  -f /mnt/locust/locustfile.py --host http://gateway:8000 `
  --headless -u 200 -r 50 -t 60s --processes 4 `
  --csv /tmp/results/spike
```

Or run the whole experiment (Locust + RabbitMQ queue-depth sampling):

```bash
python scripts/run_load_test.py
```

What to look for:

- `202` responses: accepted immediately by the gateway.
- `429` responses: the per-client token bucket kicked in.
- Stable gateway latency even while the queue grows.
- Workers drain the queue in the background.

---

## 11. Configuration

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
| `GATEWAY_URL` | `http://gateway:8000` | ops-api |
| `NOTIFICATION_URL` | `http://notification-mock:8002` | ops-api |
| `RABBITMQ_API_URL` | `http://rabbitmq:15672` | ops-api |
| `RABBITMQ_USER` / `RABBITMQ_PASSWORD` | `pinghub` / `pinghub` | ops-api |

The UI itself has no runtime configuration: nginx routes `/api/` and `/ops/` by
service name inside the Compose network.

---

## 12. Key design decisions (viva notes)

**Why a queue?**
The gateway must answer in milliseconds. Saving to the database and calling an
external provider takes hundreds of milliseconds and can fail. A queue
decouples “accept the request” from “finish the work”, so traffic spikes become
a longer queue instead of slow responses or crashes.

**Why a token bucket?**
It allows a short burst (10 requests) and then smooths traffic to 1 request per
second per client. Redis makes the bucket shared across gateway replicas, so
adding replicas cannot bypass the limit. If Redis dies, an in-process fallback
still protects each replica. In the UI, each profile is its own bucket.

**Why Redis for history?**
History is read far more often than it changes. Cache-aside gives an immediate
response on the second and later reads. The worker invalidates the affected
keys when a message changes state, and a 60-second TTL bounds staleness. The UI
shows whether the last read was a HIT or a MISS.

**Why retries AND a circuit breaker?**
Retries handle a brief provider blip. A circuit breaker stops hammering a
provider that is clearly down, which protects the worker pool and lets queued
work fail fast instead of consuming all worker time. HALF_OPEN probes recovery
with a single call before fully closing.

**Why is the worker idempotent?**
RabbitMQ can redeliver a task if a worker crashes before acknowledging it. The
gateway generates `task_id` and `message_id`, and the worker uses `ON CONFLICT`
plus an “already delivered/read” check, so a redelivery does not duplicate data
or resend a notification.

**Why a separate ops-api?**
The dashboard aggregates several services and the Load Lab is CPU- and
connection-heavy. Keeping that code in a separate service means an operations
query or a load test cannot slow down the message path.

**Why public IDs?**
A UUID is hard to share verbally. An 8-character uppercase hex ID is short,
copyable, and indexed. Lookups accept the public ID, username, or UUID, and the
gateway caches public-ID resolution in Redis so the normal username path adds no
extra call.

**Why polling instead of WebSockets?**
The read paths are cheap and cached. A 2–2.6 second poll is simpler, survives
proxies and reloads, and is easy to reason about for a demo. The correctness of
read receipts does not depend on a persistent socket.

**Why several gateway processes?**
A single Python process is still limited to one CPU core. The gateway keeps no
request state of its own, so uvicorn can run four worker processes behind the
same port. Rate limiting stays correct because every process uses the same Redis
Lua token bucket.

**Why does each worker process several tasks at once?**
RabbitMQ's prefetch limit (5) caps unacknowledged deliveries. The worker starts
one asyncio task per delivery, so a 1-second retry backoff on one message does
not block the other four. This is what lets two replicas drain ~113 messages/s.

---

## 13. Assignment checklist

| Requirement | Where |
|---|---|
| Shared repo + base Compose | this repository, `docker-compose.yml` |
| API gateway with routing | `gateway/main.py` |
| Rate limiter returning 429 | `gateway/rate_limiter.py` |
| Health endpoint per node | `gateway/health.py`, `GET /health` |
| Architecture + data flow | `docs/architecture.md`, `docs/architecture.png` |
| PostgreSQL + migrations | `db/migrations/`, `migrate` service |
| Redis cache + rate-limit counters | `message-api/main.py`, `gateway/rate_limiter.py` |
| Async queue + worker pool | `worker/`, RabbitMQ, `--scale worker=3` |
| Retries + circuit breaker | `worker/resilience.py` |
| End-user web UI | `ui/` at http://localhost:3000 |
| Unique IDs + directory | `users.public_id`, `POST/GET /users` |
| Delivered / read receipts | `004_ui_features.sql`, conversations/read endpoints, UI ticks |
| History + cache visibility | History screen, `X-Cache` HIT/MISS badge |
| In-app robustness test | Ops API `/loadtest`, System -> Load Lab |
| Load test | `loadtest/locustfile.py` |
| Structured logs with `request_id` | `common/logger.py`, all services |
| README + load-test report | `README.md`, `docs/load-test-report.md` |
| 10-minute demo | Section 14 below |

---

## 14. 10-minute demo script

| Minute | Show |
|---|---|
| 0–1 | `docs/architecture.png`; explain UI -> gateway -> queue -> worker -> DB |
| 1–2 | `docker compose ps`; open `http://localhost:3000`; create Alice and Bob |
| 2–4 | Alice sends by Bob's unique ID; Bob's tab shows the unread badge; open it; Alice sees **Read** |
| 4–5 | History: filters, cache **MISS** then **HIT**, lookup by another user's ID |
| 5–7 | System -> Load Lab: 50 users / 10 s; show PASS, latency, queue chart, drain time |
| 7–8 | Provider -> Always fail; send a message; show retries and **Failed** with Resend; restore Healthy |
| 8–9 | Worker logs: retries, circuit breaker, request_id trace |
| 9–10 | Summarize the formal Locust numbers from `docs/load-test-report.md` |

---

## 15. Troubleshooting

**The UI shows “Cannot reach the server”**
Check `docker compose ps`. The `gateway` and `ops-api` must be healthy; `ui`
must be running. If the page itself loads, nginx is fine and the problem is
downstream.

**`/health` shows worker down**
Give the workers a few seconds to write their first Redis heartbeat. If it
stays down, check `docker compose logs worker`.

**Everything returns 503 after startup**
PostgreSQL may still be applying migrations. `docker compose ps` should show
`healthy` for `postgres`, `redis`, and `rabbitmq`. `migrate` should have exited
with code 0.

**New columns or `READ` are missing on an old database**
Migration `004_ui_features.sql` is applied by the `migrate` service on every
start. Check `docker compose logs migrate`; it should end with the `ALTER TABLE`
and `CREATE INDEX` lines. You do not need to delete the volume.

**Port already in use**
Stop the other process or change the host side of the port mapping in
`docker-compose.yml` (`3000`, `8000`, `8001`, `8002`, `8003`).

**I want a completely fresh database**

```bash
docker compose down -v
docker compose up --build
```

**Load Lab says WARN**
The queue did not drain within 90 seconds or messages were still pending. Add
workers (`docker compose up --scale worker=3`) or reduce users/seconds. A WARN
is a capacity signal, not data loss.

**Load Lab says FAIL**
Look for 5xx or network errors. Check `docker compose logs gateway worker
ops-api`, and verify the provider is healthy. The Load Lab temporarily forces
the provider to `always_succeed`, then restores the prior mode.
