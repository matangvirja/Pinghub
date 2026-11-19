# PingHub — Gateway-Based Messaging and Notification Platform

PingHub is a **scalable messaging backend with a complete end-user web app**.
Every employee signs in with a username and password, shares a short unique ID,
and sends messages **or files of any type**. The message is accepted instantly
(`202`), then processed and delivered in the background. The sender sees
**Delivered** and **Read** ticks, the recipient sees unread badges and history,
either side can delete a whole chat, and an in-app **Load Lab** proves whether
the system stays robust under load.

PingHub is designed for a **company LAN**: employees open the same web app from
their own phones and laptops on the office Wi-Fi. A new device or a new network
never creates a profile by itself — it shows the sign-in screen, and only a
valid employee account can enter. Creating a brand-new account requires the
company access code.

No terminal commands are needed to use the product. The terminal is only for
starting the stack and for the optional engineering tools.

> **One-line viva answer:** “PingHub is a scalable messaging platform. The UI
> sends a message or a file through a real gateway, which queues it and replies
> instantly. Workers deliver it in the background, retry failures, cache
> history, and stay responsive under load — and the UI shows delivery and read
> receipts.”

---

## 1. Open the app

> **`http://localhost:3000` is the web app.** `http://localhost:8000` is the
> raw JSON API, not the UI. If you see JSON, you opened the wrong port.

### Start everything (Windows, recommended)

Double-click **`start.cmd`** in the project folder. It starts Docker Compose,
waits until the backend is actually healthy, then prints two addresses:

```text
On this computer:     http://localhost:3000
On other devices:     http://192.168.1.25:3000
```

The first build takes a few minutes. Later starts are much faster. The script
also saves the detected LAN address to `.env` so the in-app **“Use on another
device”** panel shows the exact phone/laptop URL.

### Start everything (terminal)

```powershell
# Windows PowerShell (recommended: prints the LAN URL and waits for health)
powershell -ExecutionPolicy Bypass -File scripts\start.ps1

# Any platform, plain Compose
docker compose up -d --build
```

When the stack is healthy, open:

| What | URL |
|---|---|
| **PingHub web app** | **http://localhost:3000** |
| **PingHub from a phone / other laptop** | **http://\<this-computer-ip\>:3000** |
| Gateway health | http://localhost:8000/health |
| Gateway API docs | http://localhost:8000/docs |
| Ops API docs | http://localhost:8003/docs |
| RabbitMQ management | http://localhost:15672 (pinghub / pinghub) |
| Message API health | http://localhost:8001/health |
| Notification mock | http://localhost:8002/health |

If a phone or another laptop cannot open the LAN address, allow the port once
from an **Administrator** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1
```

This adds a Windows Firewall inbound rule for TCP 3000 on Private networks. See
[Section 15](#15-troubleshooting) if it still fails (guest Wi-Fi / AP isolation
blocks device-to-device traffic).

### Sign in or create your employee account

1. Open http://localhost:3000. You always land on the **Sign in** screen.
2. If you already have an account, enter your **username** and **password** and
   click **Sign in**.
3. If this is your first time, open the **Activate / create account** tab, type
   the **company access code**, choose a username and password, and click
   **Create account**. The default demo code is `pinghub-demo`; the person who
   runs the stack can change it with `PINGHUB_SIGNUP_CODE` in `.env`.
4. Tick **Remember me on this device** to stay signed in for 30 days on that
   device. Leave it unticked to keep the session in this browser tab only.
5. Your **unique ID** appears in the left sidebar, for example `7F3A9C21`.
   Share it with the other person.

Rules and safety:

- Usernames are 2–32 letters, numbers, dots, dashes, or underscores.
- Passwords must be at least 8 characters and are stored as salted
  PBKDF2-SHA256 hashes — never in plain text.
- **No profile is created automatically.** Opening the app on a new phone or a
  new network just shows the sign-in form. Account creation needs the company
  code, so a random device on the Wi-Fi cannot invent an employee.
- Existing passwordless profiles from the old demo are **activated** the first
  time their owner uses **Activate / create account**; their history and unique
  ID are kept.

### Demo two people with two tabs

Read receipts need two identities. Each tab keeps its own session
(`sessionStorage`), so you can sign in as two different employees in the same
browser.

1. Sign in (or create) **Alice** in tab 1.
2. Open a second tab at http://localhost:3000 and sign in as **Bob**. Each tab
   has its own session, so the second tab does not replace Alice.
3. In Alice's tab, click **New chat**, choose **By unique ID**, and paste Bob's
   ID. Click **Open chat**.
4. Send a message.
5. In Bob's tab, the conversation shows an unread badge. Open it.
6. Alice's message changes from **Delivered** to **Read** (double tick), and
   Alice gets a read-receipt notification.
7. In either tab, use the **trash** icon at the top of the conversation to
   delete that chat from that employee's view only.

> Tip: keep both tabs in the same browser window and switch between them. The
> app polls the server every few seconds; the tab you are viewing refreshes
> immediately.

### Demo two people on two devices (same Wi-Fi)

The backend still runs only on this computer. Other devices just need a browser.

1. Run **`start.cmd`** (or `scripts\start.ps1`). Note the **“On other devices”**
   address it prints, for example `http://192.168.1.25:3000`.
2. Connect the phone/laptop to the **same Wi-Fi network**.
3. On the other device, open that address in any browser. Nothing is installed
   on the phone.
4. Sign in with the **same employee account** you use on this computer, or
   create a separate account using the company access code. A new device never
   creates a profile automatically — it only offers sign-in and account
   activation.
5. On this computer, click **Use on another device** below your ID to see the
   address, your unique ID, and a ready-made invite message. Send that invite.
6. The other person signs in, clicks **New chat**, pastes your 8-character ID,
   and sends.
7. You see the unread badge; open the chat and the sender sees **Read**.

`localhost` never works from another device — it means “this device”. Use the
detected LAN address instead. The in-app panel deliberately swaps `localhost`
for the LAN URL.

The same account can be signed in on several devices, each with its own session.
Changing your password signs out every other device but keeps the current one.

> Security note: keep the app on the company Wi-Fi. Change the default
> `PINGHUB_SIGNUP_CODE` and `PINGHUB_INTERNAL_TOKEN` before sharing, and do not
> expose port 3000 to the public internet.

### What each screen does

| Screen | What you can do |
|---|---|
| **Chats** | Search conversations, see unread badges, open a chat by unique ID or username, send messages **and files**, watch status ticks, resend failed messages, copy your own ID, and **delete a whole chat** from your own view. |
| **History** | See your own sent/received messages with status, filters and search, the Redis cache **HIT/MISS** badge, and JSON export. Other employees' history is private. |
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

1. The UI sends `Authorization: Bearer <session>`, plus `sender`, `receiver`,
   and `body`. `sender` must match the signed-in employee; `receiver` may be a
   username or an 8-character public ID of an existing account.
2. Gateway validates the session with message-api, resolves public IDs (cached
   in Redis), assigns a `request_id`, and checks the token bucket. Empty bucket
   -> `429` with `Retry-After`.
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
| `gateway` | 8000 | Request ID, employee-session gate + ownership checks, public-ID resolution, routing, token-bucket limiter (429), `/health`, queue publish, auth/read/conversation proxies. Runs 4 uvicorn workers. |
| `message-api` | 8001 | Accounts, passwords (PBKDF2-SHA256), bearer sessions, profiles, conversations, threads, read receipts, per-user chat delete, message history (Redis cache-aside), task status. |
| `ops-api` | 8003 | System dashboard aggregation and the in-app Load Lab. Kept separate from the message path. |
| `worker` (2 replicas) | — | Consume queue, write DB, call notifier, retry + circuit breaker. Up to 5 tasks concurrently per replica. |
| `notification-mock` | 8002 | Fake email provider; controllable failures and latency. |
| `postgres` | 5432 | `users`, `messages`, `task_logs`, `auth_sessions`, `conversation_hides`. |
| `redis` | 6379 | History cache, task state, rate-limit buckets, worker heartbeats, public-ID lookup cache. |
| `rabbitmq` | 5672 / 15672 | Durable task queue + management UI. |
| `migrate` | — | One-shot job that applies every SQL migration on every `compose up`, then exits. |
| `locust` | 8089 | Optional formal load-test UI (profile `loadtest`). |

---

## 4. Repository layout

```text
PingHub/
├── docker-compose.yml
├── start.cmd                     # double-click launcher for Windows
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
│   └── main.py                   # auth, sessions, profiles, read, delete, history,
│                                 # file upload + download (BYTEA)
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
│   ├── index.html                # sign-in/activation, chats, history, system, modals, attach button
│   ├── app.js                    # sessions, polling, read receipts, delete, upload/download, Load Lab
│   ├── styles.css                # responsive dark theme + attachment bubbles
│   ├── nginx.conf                # /api -> gateway (16m uploads), /ops -> ops-api
│   └── Dockerfile
├── db/migrations/
│   ├── 001_create_users.sql
│   ├── 002_create_messages.sql
│   ├── 003_create_task_logs.sql
│   ├── 004_ui_features.sql       # public IDs, display names, READ, timestamps
│   ├── 005_auth_and_chat_delete.sql  # passwords, sessions, per-user chat delete
│   └── 006_file_attachments.sql  # attachments table, messages.attachment_id
├── loadtest/
│   ├── locustfile.py
│   └── results/
├── scripts/
│   ├── start.ps1                 # start, wait for health, print local + LAN URL
│   ├── allow-lan.ps1             # one-time Windows Firewall rule for TCP 3000
│   ├── smoke_test.py             # 7-check end-to-end test (text-only)
│   ├── attachment_test.py        # 23-check file attachment end-to-end test
│   ├── failure_demo.py           # retries + circuit breaker demo
│   └── run_load_test.py          # Locust + queue-depth monitor
└── docs/
    ├── architecture.md
    ├── architecture.png
    ├── ui-guide.md               # non-technical walkthrough
    └── load-test-report.md
```

---

## 5. Data model additions for the UI and authentication

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

Migration `005_auth_and_chat_delete.sql` adds:

| Column / table | Purpose |
|---|---|
| `users.password_hash` | `pbkdf2_sha256$<iterations>$<salt>$<digest>`. `NULL` means a legacy profile that still needs activation. |
| `users.is_active` | Lets an operator disable an account without deleting its history. |
| `auth_sessions` | One row per signed-in device/tab: `user_id`, SHA-256 `token_hash`, `expires_at`, `created_at`. The raw bearer token is never stored. |
| `conversation_hides` | `user_id`, `peer_id`, `hidden_at`. A per-user soft delete; the other participant keeps their copy. |

Migration `006_file_attachments.sql` adds:

| Column / table | Purpose |
|---|---|
| `attachments` | `id`, `uploader_id`, `filename`, `content_type`, `size_bytes`, `content BYTEA`, `created_at`. The file bytes live in PostgreSQL, so no volume or object store is needed. |
| `messages.attachment_id` | Optional link from a message to its file. A message may be text-only, file-only (empty body), or text + file. |

Orphan drafts (uploaded but never linked to a message) older than 24 hours are
deleted automatically whenever a new upload happens.

Password hashing uses only the Python standard library (`hashlib.pbkdf2_hmac`,
`secrets`, `hmac`). No extra dependency is required. Session tokens are 32-byte
URL-safe random strings; the database stores only their SHA-256 hash.

---

## 6. Using the app (non-technical guide)

### Send a message

1. Click **New chat** (top-right of the conversation list or the empty state).
2. Pick **By unique ID** and paste the other person's ID, or choose
   **Directory** and click a profile.
3. Type in the composer. Press **Enter** to send, **Shift+Enter** for a new
   line.
4. Watch the tick: clock -> spinning clock -> single check -> blue double check.

The sender is always the signed-in employee, taken from the session token. The
UI cannot send a message as somebody else, and the recipient must be an existing
account.

### Send a file

1. Open a chat and click the **paperclip** button in the composer (or just use
   the file picker).
2. Choose **any file type** — documents, images, PDFs, archives, spreadsheets.
3. The file is uploaded right away. A small chip shows its name and size while
   you decide; press **Remove** to drop it, or type a note and press **Send**.
4. The file arrives as a card in the chat. Click the card to download it with
   its original filename.

Notes:

- A message can be **file-only** (no text needed) or **text + file**.
- Files follow the same path as text: gateway -> queue -> worker -> delivery.
- The **max size is 10 MB** per file by default (`PINGHUB_MAX_UPLOAD_MB`). nginx
  allows a 16 MB request body so the limit is enforced cleanly by the app.
- Only the two people in the conversation can download a file. Nobody else on
  the Wi-Fi can read it, and downloads are forced (never rendered inline).
- Deleting a chat hides its files from **your** view; the other person keeps
  theirs. A failed file message can be **resent** with the same attachment.
- Files are stored in PostgreSQL. Synthetic Load Lab / Locust traffic cannot
  upload — attachments always require a real employee session.

### Read receipts

When the recipient opens the chat while the tab is visible, the app marks every
delivered incoming message as `READ`. The sender sees:
- a blue double check and the read time,
- a **Read receipt** notification,
- a desktop notification if alerts were enabled.

Read receipts are best-effort. If the network drops, the next poll retries.

### Delete a whole chat

1. Open the conversation you want to remove.
2. Click the **trash** icon at the top of the thread (next to the person's name).
3. Confirm **Delete chat**.

What happens:

- The chat disappears from **your** Chats list and your History. The other
  person keeps their copy.
- It is a soft delete, not a database wipe. The row is recorded in
  `conversation_hides`.
- If the other person sends a **new** message afterwards, the chat comes back
  automatically with the new message. Messages from before the delete stay
  hidden for you.
- Deleting the chat you are currently reading clears the thread immediately.

### Unique IDs and history

- Your ID is in the sidebar and in the profile modal, with a copy button.
- **History** shows only your own sent and received messages; other employees'
  history is private. Use the filters to narrow it down.
- History supports text search, status filter, direction filter, and JSON
  export.

### Unread badges and notifications

The sidebar shows a count of unread incoming messages. Click **Enable desktop
alerts** to receive a browser notification when a new message arrives in a
background tab. The app polls; it does not need WebSockets.

### Rate limiting in the UI

The UI sends `Authorization: Bearer <session token>`, and the gateway keys the
bucket by the signed-in username. Each employee gets a 10-token burst and 1
token/second refill shared across their devices. If you send very quickly, you
will see a friendly “Rate limit reached” hint and a `429` — this is the
protection working, not a bug.

### Sessions in two tabs

The bearer token lives in `sessionStorage`, so each tab keeps its own employee.
Open a second tab and sign in as a second person to demo read receipts. Ticking
**Remember me on this device** moves the token to `localStorage` for 30 days, so
you stay signed in even after closing the tab. **Sign out** removes only the
current device's session.

### Same Wi-Fi, different devices

Click **Use on another device** under your unique ID (or from the profile
modal). The panel shows:

- the address to open on the other device (the LAN URL, not `localhost`),
- your 8-character unique ID,
- a ready-made invite message with both, ready to copy into WhatsApp or SMS.

An employee can sign in to the same account on several devices. A brand-new
device shows the sign-in screen and never creates a profile on its own. The
backend, database, queue, and workers all stay on the host computer.

Keep port 3000 on your private network, and change the default company access
code before sharing the app outside your team.

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

- the generator sends the internal tooling token plus a per-virtual-user
  `X-API-Key`, so each virtual user has its own token bucket;
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

### Authentication

The UI normally handles this; these calls show what happens underneath. Every
user-facing data route (messages, conversations, history, directory) needs a
bearer token. Only `/health`, `/auth/config`, `/auth/register`, and `/auth/login`
are open.

```powershell
# 1. Create an employee account (needs the company access code)
$registered = Invoke-RestMethod -Method Post -Uri http://localhost:8000/auth/register `
  -ContentType "application/json" `
  -Body '{"username":"alice","password":"correct-horse-9","signup_code":"pinghub-demo","display_name":"Alice"}'

# 2. Or sign in to an existing account
$session = Invoke-RestMethod -Method Post -Uri http://localhost:8000/auth/login `
  -ContentType "application/json" `
  -Body '{"username":"alice","password":"correct-horse-9","remember":false}'

$token = $session.token
$headers = @{ Authorization = "Bearer $token" }

# 3. Who am I?
Invoke-RestMethod -Uri http://localhost:8000/auth/me -Headers $headers

# 4. Change password (keeps this device signed in, revokes all others)
Invoke-RestMethod -Method Post -Uri http://localhost:8000/auth/password `
  -Headers $headers -ContentType "application/json" `
  -Body '{"current_password":"correct-horse-9","new_password":"another-good-1"}'

# 5. Sign out this device
Invoke-RestMethod -Method Post -Uri http://localhost:8000/auth/logout -Headers $headers
```

`POST /auth/register` also activates a legacy passwordless profile with the same
username, keeping its unique ID and history. `POST /users` is **not** public: it
exists only for the internal tooling token.

### Profiles and directory

```powershell
# Directory (load-test users are hidden)
Invoke-RestMethod -Uri "http://localhost:8000/users?limit=20" -Headers $headers

# Look up by username, public ID, or UUID
Invoke-RestMethod -Uri "http://localhost:8000/users/7F3A9C21" -Headers $headers
```

### Conversations and read receipts

```powershell
# Chat list with unread counts (only your own username/ID is allowed)
Invoke-RestMethod -Uri "http://localhost:8000/users/alice/conversations" -Headers $headers

# Full thread (accepts username or public ID for both sides, including your own)
Invoke-RestMethod -Uri "http://localhost:8000/users/alice/conversations/bob" -Headers $headers

# Mark all delivered messages from bob to alice as READ
Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/users/alice/conversations/bob/read" -Headers $headers

# Delete the whole chat from alice's view (bob keeps his copy)
Invoke-RestMethod -Method Delete `
  -Uri "http://localhost:8000/users/alice/conversations/bob" -Headers $headers
```

A request for another employee's conversations returns `403`. The delete is
per-user and soft: a newer message from `bob` makes the chat reappear.

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
  -Headers $headers `
  -Body $body

$taskId = $response.task_id
```

`sender` must match the signed-in employee (username, public ID, or UUID);
otherwise the gateway returns `403`. `receiver` must be an existing account, so
a typo cannot silently create a new user.

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

### File attachments

Upload first, then link the returned `attachment_id` to a message. Both calls
use the employee bearer token; synthetic internal-token traffic is rejected.

```powershell
# 1. Upload any file (multipart/form-data)
$upload = Invoke-RestMethod -Method Post -Uri http://localhost:8000/attachments `
  -Headers $headers -Form @{ file = Get-Item '.\report.pdf' }
$attachmentId = $upload.id

# 2a. File-only message (empty body is allowed)
$send = @{
  sender = "alice"; receiver = "bob"; body = ""; attachment_id = $attachmentId
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/messages `
  -Headers $headers -ContentType "application/json" -Body $send

# 2b. Or text + file: put a normal string in "body" as well.

# Metadata, bytes, and cleanup
Invoke-RestMethod -Uri "http://localhost:8000/attachments/$attachmentId" -Headers $headers
Invoke-WebRequest -Uri "http://localhost:8000/attachments/$attachmentId/content" `
  -Headers $headers -OutFile 'report.pdf'
Invoke-RestMethod -Method Delete -Uri "http://localhost:8000/attachments/$attachmentId" `
  -Headers $headers
```

- `POST /attachments` -> `201` with `id`, `filename`, `content_type`,
  `size_bytes`. Files over the limit get `413`.
- `GET /attachments/{id}/content` returns the original bytes with
  `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.
- Metadata and bytes are readable **only** by the two conversation participants;
  an outsider or a deleted chat returns `403` / `404`.
- A file already linked to a delivered message cannot be reused (`409`); a
  `FAILED` file message may be resent with the same `attachment_id`.

### Check status, history, and cache

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/messages/$taskId" -Headers $headers

$first = Invoke-WebRequest -Uri "http://localhost:8000/history?user=alice" -Headers $headers
$first.Headers["X-Cache"]     # MISS

$second = Invoke-WebRequest -Uri "http://localhost:8000/history?user=alice" -Headers $headers
$second.Headers["X-Cache"]    # HIT
```

`/history?user=` accepts only your own username/ID. Asking for a colleague's
history returns `403`.

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

### Smoke test (uses the internal tooling token)

```bash
python scripts/smoke_test.py
```

It signs in with `PINGHUB_INTERNAL_TOKEN` (not an employee password), then
checks `/health`, a `202` send, terminal delivery, history MISS then HIT, and
the token bucket returning `429`. Locust and the in-app Load Lab use the same
bypass, so synthetic usernames never need real accounts.

### Attachment end-to-end test

```bash
python scripts/attachment_test.py
```

This creates three real employee accounts and checks the whole file lifecycle:
unauthenticated and internal-token uploads are rejected, upload metadata, file-
only and text+file sends, delivery, byte-identical downloads by both
participants with safe headers, outsider denial, rejection of a delivered-file
reuse, resend of a failed file, and chat-delete hiding. It prints a
`23/23 checks passed` summary on success.

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
| `PINGHUB_SIGNUP_CODE` | `pinghub-demo` | message-api (account activation) |
| `PINGHUB_INTERNAL_TOKEN` | `pinghub-internal-dev` | gateway, message-api, ops-api, smoke test, Locust |
| `SESSION_TTL_HOURS` | `12` | message-api (normal session) |
| `REMEMBER_TTL_DAYS` | `30` | message-api (“Remember me” session) |
| `PASSWORD_ITERATIONS` | `210000` | message-api (PBKDF2 work factor) |
| `PINGHUB_MAX_UPLOAD_MB` | `10` | gateway, message-api (max attachment size) |
| `UPLOAD_TIMEOUT_SECONDS` | `60` | gateway (upload/download proxy timeout) |
| `PINGHUB_LAN_IP` | auto-detected | ops-api (shown by **Use on another device**) |
| `PINGHUB_UI_PORT` | `3000` | ui (host port) + ops-api (share URL) |

**Change these two before sharing the app:**

```powershell
# .env (or your shell) — pick your own values
PINGHUB_SIGNUP_CODE=acme-internal-2026
PINGHUB_INTERNAL_TOKEN=long-random-internal-secret
```

`PINGHUB_SIGNUP_CODE` is what employees type to create their account.
`PINGHUB_INTERNAL_TOKEN` is the shared bypass used by the smoke test, Locust and
the in-app Load Lab for synthetic users; employees never see it. If you change
the internal token, the test scripts and `ops-api` read it from the same
environment automatically.

`scripts/start.ps1` writes `PINGHUB_LAN_IP` and `PINGHUB_UI_PORT` to `.env`
automatically. The UI has no other runtime configuration: nginx routes `/api/`
and `/ops/` by service name inside the Compose network.

To use a different host port (for example when 3000 is taken):

```powershell
$env:PINGHUB_UI_PORT = "3100"
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -NoBuild
```

Then open `http://localhost:3100` (and `http://<lan-ip>:3100`).

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
still protects each replica. In the UI, each signed-in employee is its own
bucket, shared across that employee's devices.

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

**Why username + password instead of “just open the app”?**
The app runs on a company LAN, so every employee must have an identity that
follows them across devices. A passwordless profile that appears automatically
on any new phone is convenient for a demo but wrong for an office: anyone on the
Wi-Fi could become anyone. Passwords are hashed with PBKDF2-SHA256 and a unique
salt; sessions are opaque random tokens stored only as SHA-256 hashes, so a
database leak does not expose usable passwords or live tokens. Account creation
is gated by a company access code, which keeps a random device from inventing an
employee while still letting the team onboard itself.

**Why a per-user soft delete for chats?**
Messaging history is shared by two people. If deleting a chat removed the rows,
it would also erase the other person's copy. `conversation_hides` records “this
employee hid this conversation at time T”, so the list and history are filtered
per user. A newer message has a timestamp after T, so the chat reappears
naturally without a background job. The other participant is unaffected.

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
| Unique IDs + directory | `users.public_id`, `GET /users` |
| Employee authentication | `005_auth_and_chat_delete.sql`, `/auth/*`, PBKDF2 passwords, bearer sessions |
| Delivered / read receipts | `004_ui_features.sql`, conversations/read endpoints, UI ticks |
| Delete a whole chat | `conversation_hides`, `DELETE /users/{id}/conversations/{peer}`, trash button |
| Chat file attachments | `006_file_attachments.sql`, `POST /attachments`, `/attachments/{id}/content`, paperclip button |
| History + cache visibility | History screen (own history only), `X-Cache` HIT/MISS badge |
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
| 1–2 | Run `start.cmd`; open `http://localhost:3000`; optionally show a phone on the printed LAN URL |
| 2–4 | Sign in as Alice and Bob (two tabs or a phone); Alice sends by Bob's unique ID; Bob sees the unread badge; open it; Alice sees **Read** |
| 4–5 | Alice clicks the **paperclip**, attaches a file (any type), and sends it; Bob downloads it from the attachment card; show the conversation preview `📎 filename` |
| 5–6 | Delete the chat as Alice (trash icon + confirm); show that Bob still has it and his file; send a new message and show it reappear for Alice |
| 6–7 | History: own messages, filters, cache **MISS** then **HIT**; show that another user's history is refused |
| 7–8 | System -> Load Lab: 50 users / 10 s; show PASS, latency, queue chart, drain time |
| 8–9 | Provider -> Always fail; send a message; show retries and **Failed** with Resend; restore Healthy |
| 9–10 | Worker logs: retries, circuit breaker, request_id trace; summarize the formal Locust numbers from `docs/load-test-report.md` |

---

## 15. Troubleshooting

**I opened `:8000` and see JSON, not the app**
`:8000` is the gateway API (and `/docs` is its Swagger page). The web app is at
**`http://localhost:3000`**.

**The sign-in form says “message-api unavailable”**
The UI is up but the backend is still starting. Wait 15–20 seconds and submit
again. Check with:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:3000/api/health
```

All nodes should be `up`. `scripts\start.ps1` and `start.cmd` now wait for this
health check before declaring success.

**I opened the app on a new phone and it asks me to sign in**
That is correct. The app never creates a profile automatically. Sign in, or use
**Activate / create account** with the company access code. See
[Sign in or create your employee account](#sign-in-or-create-your-employee-account).

**The company access code is rejected**
It must match `PINGHUB_SIGNUP_CODE` exactly. The default is `pinghub-demo`.
Change it in `.env` and restart the stack:

```powershell
# edit .env, then
docker compose up -d
```

**An old passwordless username cannot sign in**
Legacy profiles have no password yet. Open **Activate / create account**, enter
the same username, the company code, and a new password. The unique ID and all
history are kept.

**I forgot my password**
There is no public reset email in this LAN demo. While signed in, open the
profile menu -> **Change password**. Otherwise an administrator can clear the
hash for one account and let its owner reactivate it with the company code
(history is preserved):

```powershell
docker compose exec postgres psql -U pinghub -d pinghub -c `
  "UPDATE users SET password_hash = NULL WHERE lower(username) = 'alice';"
docker compose exec postgres psql -U pinghub -d pinghub -c `
  "DELETE FROM auth_sessions WHERE user_id = (SELECT id FROM users WHERE lower(username) = 'alice');"
```

**I was signed out unexpectedly**
Normal sessions last `SESSION_TTL_HOURS` (12 by default). “Remember me” sessions
last `REMEMBER_TTL_DAYS` (30). Changing your password signs out every other
device. Sign in again; your history is unchanged.

**A phone or another laptop cannot open the LAN URL**
1. Confirm both devices are on the **same Wi-Fi** (not one on Wi-Fi and one on
   mobile data, and not a “guest” network — guest networks often block
   device-to-device traffic).
2. Use the address printed under **“On other devices”**, not `localhost`.
3. Allow the port once as Administrator:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1
   ```

4. Allow Docker Desktop through Windows Firewall if prompted.
5. As a test from the other device, open `http://<lan-ip>:8000/health`; if that
   also fails, the network/firewall is blocking the host, not PingHub.

**The page looks stale after an update**
The app shell and assets are served with `Cache-Control: no-store`. If an old
tab still shows the previous version, press **Ctrl+F5** once. Then create/open
a chat and retry.

**The System page does not scroll**
Fixed in the current UI: the app grid is locked to the viewport and the System
view has its own internal scrollbar. If you still see the old behaviour, press
**Ctrl+F5** to drop cached `styles.css`.

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

**New columns, `READ`, sign-in, or attachments are missing on an old database**
Migrations `004_ui_features.sql`, `005_auth_and_chat_delete.sql`, and
`006_file_attachments.sql` are applied by the `migrate` service on every start.
Check `docker compose logs migrate`; it should list all six files and end with
the `ALTER TABLE` and `CREATE INDEX` lines. You do not need to delete the volume.

**A file upload says “Files must be at most 10 MB”**
That is the `PINGHUB_MAX_UPLOAD_MB` limit working. Raise it in `.env` and also
increase `client_max_body_size` in `ui/nginx.conf` to a larger value (for example
`32m`), then rebuild with `docker compose up -d --build`.

**An attachment card will not download**
Check that you are one of the two people in the conversation and that you did
not delete the chat — both cases are denied with `403` / `404`. If the file was
part of a message, `docker compose logs message-api gateway` will show the
reason. Files are stored in PostgreSQL, so `docker compose down -v` removes them.

**Port already in use**
For the UI, set `PINGHUB_UI_PORT` (see [Section 11](#11-configuration)) instead
of editing `docker-compose.yml`. For the backend ports (`8000`, `8001`, `8002`,
`8003`), stop the other process or change the host side of the mapping.

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
