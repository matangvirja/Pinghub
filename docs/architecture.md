# PingHub Architecture and Data Flow

## 1. Component diagram

```mermaid
flowchart TD
    C[Client / Locust] -->|POST /messages<br/>GET /history<br/>GET /messages/:id| G[API Gateway<br/>:8000<br/>4 uvicorn processes]
    G -->|token bucket 429<br/>request_id| RL[(Redis<br/>rate buckets)]
    G -->|heavy: publish task| Q[(RabbitMQ<br/>queue: messages)]
    G -->|light: proxy| M[Message API<br/>:8001]
    M -->|cache-aside| R[(Redis<br/>history + task state)]
    M -->|cache miss| DB[(PostgreSQL<br/>users / messages / task_logs)]
    Q --> W1[Worker 1]
    Q --> W2[Worker 2]
    Q --> W3[Worker 3 optional]
    W1 --> DB
    W2 --> DB
    W3 --> DB
    W1 --> N[Notification Mock<br/>:8002]
    W2 --> N
    W3 --> N
    W1 -->|invalidate history<br/>heartbeat| R
    W2 -->|invalidate history<br/>heartbeat| R
    W3 -->|invalidate history<br/>heartbeat| R
    G -->|health check| M
    G -->|health check| N
    G -->|PING / TCP| RL
    G -->|PING / TCP| Q
    G -->|SELECT 1| DB
```

## 2. Heavy request: `POST /messages`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Gateway
    participant Redis as Redis
    participant MQ as RabbitMQ
    participant W as Worker
    participant DB as PostgreSQL
    participant N as Notification Mock

    C->>G: POST /messages {sender, receiver, body}
    G->>G: request_id = X-Request-ID or generated
    G->>Redis: atomic token bucket check (Lua)
    alt bucket empty
        G-->>C: 429 Too Many Requests + Retry-After
    else token available
        G->>G: task_id = uuid, message_id = uuid
        G->>Redis: SET task:<id> = QUEUED (TTL 24h)
        G->>MQ: publish persistent task
        G-->>C: 202 Accepted {task_id, status: QUEUED}
        MQ->>W: deliver task (request_id preserved)
        W->>DB: upsert users, message PROCESSING, task_log PROCESSING
        W->>Redis: task:<id> = PROCESSING
        W->>N: POST /notify (attempt 1)
        alt provider fails
            N-->>W: 503
            W->>W: backoff 1s, then 2s
            W->>N: retry (up to 3 attempts)
        end
        alt success
            N-->>W: 200 sent
            W->>DB: status DELIVERED
        else retries exhausted / circuit open
            W->>DB: status FAILED + error
        end
        W->>Redis: task:<id> = final status
        W->>Redis: DEL history:<sender>, history:<receiver>
        W-->>MQ: ack
    end
```

## 3. Light request: `GET /history?user=alice`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Gateway
    participant M as Message API
    participant R as Redis
    participant DB as PostgreSQL

    C->>G: GET /history?user=alice
    G->>G: token bucket check
    alt bucket empty
        G-->>C: 429 + Retry-After
    else allowed
        G->>M: proxy GET /history (X-Request-ID)
        M->>R: GET history:v1:alice
        alt cache hit
            R-->>M: JSON list
            M-->>G: 200 + X-Cache: HIT
        else cache miss
            M->>DB: SELECT ... WHERE sender=alice OR receiver=alice LIMIT 50
            DB-->>M: rows
            M->>R: SET history:v1:alice (TTL 60s)
            M-->>G: 200 + X-Cache: MISS
        end
        G-->>C: 200 (X-Cache forwarded)
    end
```

## 4. Message status state machine

```mermaid
stateDiagram-v2
    [*] --> QUEUED: gateway 202
    QUEUED --> PROCESSING: worker saves DB row
    PROCESSING --> DELIVERED: notification success
    PROCESSING --> FAILED: retries exhausted or circuit open
    DELIVERED --> [*]
    FAILED --> [*]
```

## 5. Circuit breaker state machine

```mermaid
stateDiagram-v2
    CLOSED --> OPEN: 5 consecutive failures
    OPEN --> HALF_OPEN: 30s recovery timeout
    HALF_OPEN --> CLOSED: trial call succeeds
    HALF_OPEN --> OPEN: trial call fails
```

## 6. Failure and recovery behaviour

| Failure | Behaviour |
|---|---|
| Client sends too fast | Gateway returns 429; bucket refills at 1 token/s |
| Redis unavailable | Rate limiter falls back to in-process memory; history reads skip cache; `/health` reports Redis down |
| RabbitMQ unavailable | Gateway returns 503 for `POST /messages`; reads still work |
| Worker crash before ack | RabbitMQ redelivers; worker is idempotent (`ON CONFLICT`) |
| Notification provider flaky | Up to 3 attempts with 1s then 2s backoff |
| Notification provider down | Circuit opens after 5 failures; tasks fail fast; HALF_OPEN probes after 30s |
| Database unavailable | Worker requeues the task; gateway `/health` reports PostgreSQL down |

## 7. Logging and traceability

The gateway creates `request_id` and returns it as `X-Request-ID`. It is placed
in the RabbitMQ task payload, so the worker logs the same id. A single request
can therefore be followed through:

```text
gateway → cache → queue → worker → database
```

Example:

```json
{"service":"gateway","request_id":"r-101","event":"accepted","tokens_left":7}
{"service":"gateway","request_id":"r-101","event":"queued","task_id":"t-55"}
{"service":"worker","request_id":"r-101","event":"db_saved","status":"PROCESSING"}
{"service":"worker","request_id":"r-101","event":"notify_failed","attempt":1}
{"service":"worker","request_id":"r-101","event":"notify_ok","attempt":2}
{"service":"worker","request_id":"r-101","event":"db_updated","status":"DELIVERED"}
```

## 8. Why these choices?

| Decision | Reason |
|---|---|
| FastAPI | Async I/O fits a gateway and queue worker; automatic OpenAPI docs |
| RabbitMQ | Durable queue, acknowledgements, redelivery, management UI |
| PostgreSQL | Strong consistency for message/task state and history queries |
| Redis | Sub-millisecond cache, atomic Lua token bucket, shared across replicas |
| Separate message-api | Keeps read traffic away from the write path; independently scalable |
| Two worker replicas | Shows horizontal scaling and survives one worker dying |
| Four gateway processes | Uses multiple CPU cores while Redis keeps the limiter globally consistent |
| Concurrent worker tasks | Prefetch bounds in-flight work; a retry sleep cannot stall the whole consumer |
| Short `/health` cache | Monitoring can poll without opening a database connection per check |
