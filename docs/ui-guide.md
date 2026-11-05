# PingHub Web App — End-User Guide

This guide is for someone who has never used a terminal. You only need a web
browser. Someone technical starts the stack once with:

```bash
docker compose up --build
```

Then open **http://localhost:3000**.

---

## 1. Create your profile

1. Type a **username** (for example `alice`). It can contain letters, numbers,
   dots, dashes, and underscores, 2–32 characters.
2. Optionally type a **display name** (for example `Alice Sharma`).
3. Click **Create profile**.

You do not need an email, phone number, or password. This is a demo identity.

After creating a profile you land in the app. Your **unique ID** appears at the
bottom-left of the conversation sidebar, for example:

```text
YOUR UNIQUE ID
7F3A9C21
@alice
```

The copy button next to it puts the ID on your clipboard.

### Coming back later

Profiles are remembered in your browser. On the welcome screen, click a saved
profile under **or continue as**. You can also open the profile menu (your
avatar at the bottom of the left rail) to switch profiles or sign out of the
current tab.

---

## 2. Start a conversation

### Option A: use a unique ID

1. Click **New chat**.
2. Stay on the **By unique ID** tab.
3. Paste the other person's ID (for example `64F2BF0E`) or type their username.
4. Click **Open chat**.

If the ID does not exist, the app tells you clearly. If you paste your own ID,
it asks you to use someone else's.

### Option B: use the directory

1. Click **New chat**.
2. Open the **Directory** tab.
3. Search by name, username, or ID.
4. Click a person.

Load-test accounts are hidden from the directory so the list stays readable.

---

## 3. Send a message

1. Choose a conversation from the left.
2. Type in the box at the bottom.
3. Press **Enter** to send, or **Shift+Enter** to add a new line.
4. Watch the status under your message:

| What you see | What it means |
|---|---|
| Clock + **Queued** | The gateway accepted it and put it in the queue. |
| Spinning clock + **Sending** | A worker is delivering it to the notification provider. |
| Single check + **Delivered** | The provider accepted it. |
| Blue double check + **Read** | The other person opened the conversation. The read time is shown. |
| Red alert + **Failed** | All retries failed. A **Resend** button appears. |

Messages are queued first. That is why the send button responds immediately
even when the system is busy.

### If you are sending too fast

Every profile has a small burst allowance (10 requests) and then refills at one
request per second. If you exceed it, the app shows a friendly notice and the
message is not sent. Wait a second and try again. This is the system protecting
itself, not an error.

---

## 4. Read receipts and notifications

A read receipt is created when the **recipient** opens the conversation while
the tab is visible. The recipient's app then marks the delivered incoming
messages as read.

On the sender's side:

- the tick becomes a blue double check;
- the read time appears (for example “Read · 09:58 AM”);
- a **Read receipt** notification slides in;
- if desktop alerts are enabled and the tab is in the background, the browser
  also shows a notification.

To turn on desktop alerts, click **Enable desktop alerts** under your unique ID
and allow notifications when the browser asks.

### Two-person demo in two tabs

1. Create **Alice** in tab 1.
2. Open a second tab at http://localhost:3000 and create **Bob**.
3. In Alice's tab, start a chat with Bob's ID and send a message.
4. Switch to Bob's tab. The conversation shows an unread badge (for example
   `1`).
5. Open the conversation. Bob's app marks Alice's message as read.
6. Switch back to Alice's tab. The message now shows **Read**.

Each tab keeps its own active profile, so this works even in the same browser.

---

## 5. Unread badges

The conversation list shows a count of incoming messages you have not opened.
Opening the conversation clears the count. A new incoming message also produces
a notification in the app and, optionally, a desktop notification.

The app refreshes conversations every ~2.6 seconds while the tab is visible.

---

## 6. History

Click **History** in the left rail.

### What you see

- Four summary cards: messages, delivered, read, failed.
- A search box for message text or a person.
- Status filter: All, Queued, Processing, Delivered, Read, Failed.
- Direction filter: sent + received, sent only, received only.
- A table with time, direction, person, message, and status.
- **Open** next to each row jumps to that conversation.
- **Export JSON** downloads the current list.
- A **redis cache: HIT** or **MISS** badge:
  - **MISS** means the server read PostgreSQL and filled the cache.
  - **HIT** means the answer came from Redis. Refresh again and it should be a
    HIT.

### Look up another user's history

Below the filters there is **Look up another user**:

1. Enter their unique ID (`7F3A9C21`), username (`alice`), or UUID.
2. Click **View**.
3. Click **Me** to go back to your own history.

This is useful for demonstrating that history is stored per person, not just in
the browser.

---

## 7. System dashboard

Click **System** in the left rail. It updates every 2 seconds.

### Node health

A green banner means all nodes are healthy. A node card turns red when that
component is down. The worker card shows how many replicas are alive.

### Metrics

- **Queue depth** — messages waiting in RabbitMQ. A spike is normal during a
  burst; the queue is doing its job.
- **Consumers** — workers attached to the queue.
- **Messages / Delivered / Read / Failed** — database counters.
- **Recent tasks** — the last worker tasks, their attempts, and any error.

### Notification provider

Use the buttons to simulate the third-party notification service:

- **Healthy** — everything succeeds.
- **30% failures** — the normal demo behaviour.
- **Always fail** — every attempt fails, so you can watch retries and the
  circuit breaker.
- **Reset provider counters** — clears attempted/succeeded/failed.

Changing the provider affects new messages only. Already-delivered messages do
not change.

---

## 8. Load Lab — is the system robust?

The Load Lab is at the bottom of **System**. It runs virtual users against the
real gateway and gives you a verdict.

### Run it

1. Set **Users** (1–500). Start with **50**.
2. Set **Seconds** (5–120). Start with **10**.
3. Click **Start test**.
4. Watch the phase change: `running` -> `draining` -> `completed`.
5. Read the verdict and the numbers.

### Understand the numbers

| Number | Meaning |
|---|---|
| **requests** | Total HTTP requests the generator made. |
| **req/s** | Throughput while the test was running. |
| **accepted** | `202` responses — messages the gateway queued. |
| **rate limited** | `429` responses. Expected: the token bucket rejected excess requests. |
| **server errors** | `5xx` or network failures. Any value above zero is serious. |
| **p50 / p95 / p99 send** | Latency percentiles for sends. p95 means 95% of sends were faster than this. |
| **queue peak** | Largest number of messages buffered in RabbitMQ during the run. |
| **drain time** | How long the queue took to empty after traffic stopped. |
| **delivered / failed / pending** | Database counts for messages created during the run. |

### The verdict

- **PASS** — 0 server/network errors, every accepted message reached a terminal
  state, and the queue drained. The system absorbed the load.
- **WARN** — the queue did not drain within 90 seconds, or messages were still
  queued/processing, or accepted messages could not be accounted for. This is a
  capacity warning. Add workers or reduce the rate.
- **FAIL** — at least one 5xx or network failure. A real availability problem;
  check the service logs.

**429s do not make the test fail.** They show the rate limiter protecting the
system.

### What the Load Lab does behind the scenes

- Each virtual user has its own `X-API-Key`, so each gets an independent token
  bucket (the same model as the formal Locust test).
- Nine out of ten requests are sends; one is a cached history read.
- The notification provider is temporarily switched to **Healthy** so the test
  measures the platform instead of the mock's intentional 30% failures. The
  previous mode is restored automatically.
- The service samples queue depth once per second and waits up to 90 seconds
  for the queue and in-flight messages to settle.

For a formal, multi-process load test, use Locust as described in the main
[README](../README.md#10-testing-and-load-tools). The in-app lab is a quick,
explainable check, not a replacement for Locust.

---

## 9. Common questions

**Do I need a password?**
No. This is a demo identity. Do not use real personal data.

**Why does my message say Failed?**
The notification provider is intentionally unreliable by default (30% failures).
The worker retries three times with backoff; if all attempts fail, the message
is marked `FAILED`. Use **Resend**, or switch the provider to **Healthy** on the
System page.

**Why did a send show “Rate limit reached”?**
You sent faster than your bucket refills (1 request/second after the first 10).
Wait a moment and retry. This is expected and is reported separately in the
Load Lab as `429`.

**Is history lost when I close the browser?**
No. History is stored in PostgreSQL. Your saved profile list is in the browser,
but the messages are on the server and can be found by username or unique ID.

**Why do two tabs show different people?**
The active profile is stored per tab (`sessionStorage`) so you can demo two
people at once. The saved profile list is shared (`localStorage`).

**How do I start over?**
Profile menu -> **Sign out of this tab**, or create a new profile. To wipe the
database, the technical operator can run `docker compose down -v` and start
again.
