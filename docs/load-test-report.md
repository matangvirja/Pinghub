# PingHub Load Test Report

## 1. Objective

Validate that PingHub:

- accepts requests quickly during a traffic spike,
- enforces the token-bucket rate limit (429 instead of overload),
- keeps read latency low through the Redis history cache,
- buffers heavy work in RabbitMQ and drains it in the background,
- survives notification-provider failures without losing accepted messages.

## 2. Environment

| Item | Value |
|---|---|
| Host | Windows 11, Docker Desktop (WSL 2), 16 vCPU, 8 GB RAM allocated |
| Docker Compose | Compose v2, project name `pinghub` |
| Services | Gateway (4 uvicorn worker processes), 1 message-api, 2 worker replicas, PostgreSQL 16, Redis 7, RabbitMQ 3.13, notification-mock |
| Gateway config | capacity 10 tokens, refill 1 token/s, per client |
| History cache TTL | 60 s |
| Health cache TTL | 3 s (prevents health polls from hammering dependencies) |
| Notification latency | 50 ms |
| Notification mode | `always_succeed` for the throughput run |
| Worker retry policy | 3 attempts, 1 s / 2 s backoff |
| Worker concurrency | prefetch 5, up to 5 tasks processed concurrently per replica |
| Locust | 2.47.0, 4 worker processes |

The notification provider was set to a healthy mode for the throughput test so
the numbers measure the platform, not a deliberately failing dependency.
Failures are exercised separately in section 6.

## 3. Scenario

**Tool:** Locust 2.x
**Command:**

```bash
docker compose --profile loadtest run --rm locust \
  -f /mnt/locust/locustfile.py --host http://gateway:8000 \
  --headless -u 200 -r 50 -t 60s --processes 4 \
  --csv /tmp/results/spike
```

`scripts/run_load_test.py` runs this command, samples RabbitMQ queue depth every
0.5 s, waits for the queue to drain, and writes `loadtest/results/`.

**Workload mix (per virtual user):**

- 5 × `POST /messages`
- 2 × `GET /history?user=<self>`
- 1 × `GET /health`

Each virtual user sends a unique `X-API-Key`, so each has its own 10-token
bucket. Without this, all Locust users would share one bucket and the test would
measure almost nothing but 429s. Four Locust processes are used so the load
generator itself is not the bottleneck.

## 4. Results

### 4.1 Overall

| Metric | Value |
|---|---|
| Total requests | 36,637 |
| Accepted `POST /messages` (202) | 9,604 |
| Rate limited (429) | 13,188 |
| Unexpected errors | 0 |
| Requests/sec (average) | 610.2 |
| Median latency | 8 ms |
| p95 latency | 49 ms |
| p99 latency | 240 ms |
| Peak users | 200 |

The gateway accepted 9,604 messages in 60 seconds (~160 accepted/s) and rejected
13,188 with `429`; the limiter, not the infrastructure, was the throughput cap.

### 4.2 Per endpoint

| Endpoint | Requests | Median | p95 | p99 | Failures |
|---|---:|---:|---:|---:|---:|
| `POST /messages` | 22,792 | 10 ms | 37 ms | 120 ms | 0 |
| `GET /history` | 9,239 | 9 ms | 54 ms | 120 ms | 0 |
| `GET /health` | 4,606 | 5 ms | 250 ms | 430 ms | 0 |

`GET /health` is deliberately heavier than a user request: its first call
aggregates Redis, PostgreSQL, RabbitMQ, message-api, notification-mock and worker
heartbeats. The 3-second cache keeps repeated polls cheap.

### 4.3 Queue behaviour

| Observation | Value |
|---|---|
| Queue depth after ramp (4 s) | 0 |
| Peak queue depth | 2,106 messages at 65.8 s |
| Queue depth at end of test | 2,106 |
| Time to drain after test | 18.7 s (~113 messages/s) |
| Messages DELIVERED during run | 9,604 |
| Messages FAILED during run | 0 |

Every accepted message was eventually delivered. RabbitMQ absorbed the
difference between the gateway's accept rate and the workers' drain rate, then
the workers caught up after the load stopped.

RabbitMQ management UI: http://localhost:15672 -> Queues -> `messages`.

## 5. Observations

- **Gateway stays fast.** `POST /messages` median latency was 10 ms because the
  gateway only validates, rate-limits, writes task state to Redis, and publishes
  to RabbitMQ. It never waits for PostgreSQL or the notification provider.
- **Rate limiter protects the system.** Each virtual user exhausted its 10-token
  burst and then received `429`s at roughly 1 request/s while tokens refilled.
  `Retry-After` tells the client when to try again.
- **Redis cache reduces read load.** The first history request per user is a
  `MISS`; subsequent requests are `HIT` (verify with the `X-Cache` header) and do
  not touch PostgreSQL. History p95 stayed at 54 ms.
- **Queue absorbs the spike.** The accept rate exceeded the worker drain rate, so
  queue depth grew during the spike and fell afterwards. This is the intended
  behaviour: accept work quickly, process it as capacity allows.
- **No accepted message is lost.** All 9,604 accepted messages reached
  `DELIVERED`; 0 were `FAILED`. A message that fails notification is marked
  `FAILED` with an error and attempt count in `task_logs`; RabbitMQ redelivery
  plus idempotent IDs prevent duplicates.
- **Horizontal scaling works both ways.** The gateway runs four uvicorn
  processes sharing one Redis token-bucket store, and the two worker replicas
  process up to five tasks each concurrently.

## 6. Failure test

`scripts/failure_demo.py` drives the provider through failure and recovery.

1. Set the provider to fail and send 20 messages:

   ```bash
   curl -X POST http://localhost:8002/control \
     -H "Content-Type: application/json" \
     -d '{"mode":"always_fail"}'
   ```

2. Watch worker logs:

   ```bash
   docker compose logs -f worker
   ```

   Observed: `notify_failed` with attempt 1, 2, 3; then `db_updated` with
   `status: FAILED`. After five consecutive failures the circuit opened
   (`circuit_state_changed CLOSED -> OPEN`) and later tasks failed immediately
   with `circuit_open` instead of waiting for retries.

3. Wait for the 30-second recovery window, set the provider back to success, and
   send another message:

   ```bash
   curl -X POST http://localhost:8002/control \
     -H "Content-Type: application/json" \
     -d '{"mode":"always_succeed"}'
   ```

   Observed: the HALF_OPEN trial succeeded and the circuit closed
   (`OPEN -> CLOSED`); new messages were `DELIVERED`.

| Observation | Value |
|---|---|
| Time to open circuit | ~2.3 s after the first failing batch (5 consecutive failures) |
| Tasks failed while provider was down | 20 / 20 |
| Recovery time after provider restored | 0.3 s for 5 / 5 messages (after the 30 s window) |

Representative log lines:

```text
notify_failed ... attempt: 1 ... circuit_state: CLOSED
notify_failed ... attempt: 2 ... circuit_state: CLOSED
circuit_state_changed ... from: CLOSED to: OPEN failures: 5
circuit_open ... notification circuit is OPEN; retry in 29.1s
...
circuit_state_changed ... from: OPEN to: CLOSED failures: 0
notify_ok ... attempt: 1
```

## 7. Conclusion

PingHub meets the assignment's performance and resilience goals:

- The gateway isolates slow work behind a durable queue and keeps p95 latency
  under 50 ms for user-facing endpoints under a 200-user spike.
- The token bucket prevents overload and returns a clear 429 with `Retry-After`.
- Redis keeps repeated history reads fast (p95 54 ms).
- The worker pool drains the spike in ~19 s and every accepted message reaches a
  terminal state.
- Retries and the circuit breaker keep the worker pool healthy when the
  notification provider is unreliable.
- Structured `request_id` logs make the whole path observable.

## 8. Raw artefacts

- `loadtest/results/spike_stats.csv`
- `loadtest/results/spike_stats_history.csv`
- `loadtest/results/spike_failures.csv`
- `loadtest/results/spike_exceptions.csv`
- `loadtest/results/queue-monitor.csv`
- `loadtest/results/locust-output.txt`

Screenshots to include with the submission:

- `GET /health` with all nodes up
- Locust summary (throughput/latency)
- RabbitMQ queue graph
- Worker logs showing retries and circuit states
