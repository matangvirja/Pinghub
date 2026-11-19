# PingHub Web App — End-User Guide

This guide is for someone who has never used a terminal. You only need a web
browser and your employee username and password. Someone technical starts the
stack once by **double-clicking `start.cmd`** (or running
`docker compose up -d --build`). The launcher prints:

```text
On this computer:     http://localhost:3000
On other devices:     http://192.168.1.25:3000
```

Open the first address on the computer that runs PingHub. Open the second
address on a phone or another laptop connected to the **same Wi-Fi**.

> `:8000` is the API, not the app. If you see JSON, use `:3000` instead.

---

## 1. Sign in to your account

Every employee has a username and a password. Opening PingHub never creates a
profile by itself — on a new phone or a new network you will always see the
sign-in screen first.

### If you already have an account

1. Type your **username** (for example `alice`).
2. Type your **password**.
3. Optionally tick **Remember me on this device** to stay signed in for 30 days
   on this device.
4. Click **Sign in**.

### If this is your first time

1. Open the **Activate / create account** tab.
2. Type the **company access code** given to you by whoever runs PingHub.
3. Choose a **username** (2–32 letters, numbers, dots, dashes, or underscores)
   and an optional **display name** (for example `Alice Sharma`).
4. Choose a password of at least 8 characters, then click **Create account**.

The company access code is what stops a random device on the Wi-Fi from
inventing an employee. If you do not know it, ask the person who started the
stack. (The demo default is `pinghub-demo`.)

After signing in you land in the app. Your **unique ID** appears at the
bottom-left of the conversation sidebar, for example:

```text
YOUR UNIQUE ID
7F3A9C21
@alice
```

The copy button next to it puts the ID on your clipboard.

### Coming back later

- Untick **Remember me** and your session lives only in that browser tab.
  Closing the tab signs you out.
- Tick **Remember me** and this device stays signed in for 30 days.
- The same account can be signed in on several devices at once.
- Open the profile menu (your avatar at the bottom of the left rail) to
  **Change password** or **Sign out**. Changing the password signs out every
  other device but keeps this one signed in.

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

You always send as the employee you signed in as. PingHub will not let you send
a message as someone else, and the recipient must already have an account.

### Send a file

You can attach a file of **any type** — a document, photo, PDF, spreadsheet, or
archive. It goes through the same gateway and queue as a text message.

1. In the chat, click the **paperclip** button at the bottom (left of the text
   box).
2. Pick the file from your computer. It uploads immediately; a small chip shows
   its name and size.
3. You can type a note alongside it, or send the file on its own. Press **Send**
   (or **Enter**).
4. The file appears as a card in the conversation with its name and size. Click
   the card to download it with its original filename.

Good to know:

- One file per message. Text is optional when a file is attached.
- The conversation list shows `📎 filename` for the latest file message.
- Each file can be up to **10 MB** by default. A larger file shows a clear
  “Files must be at most 10 MB” message.
- Only the two people in the conversation can open a file. Nobody else can see
  it, and deleting a chat hides its files from your view only.
- If delivery fails, use **Resend** on the message — the file is reused, so you
  do not have to upload it again.

### If you are sending too fast

Every employee has a small burst allowance (10 requests) and then refills at one
request per second. If you exceed it, the app shows a friendly notice and the
message is not sent. Wait a second and try again. This is the system protecting
itself, not an error.

### Delete a whole chat

1. Open the conversation you no longer want.
2. Click the **trash** icon at the top of the conversation, next to the person's
   name.
3. Confirm **Delete chat**.

The chat disappears from **your** list and your History. This does not delete it
for the other person — they keep their copy. It is also not permanent forever:
if the other person sends a **new** message, the chat comes back automatically
with that new message. Messages from before the delete stay hidden for you.

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

1. Sign in (or create) **Alice** in tab 1.
2. Open a second tab at http://localhost:3000 and sign in as **Bob**. Each tab
   keeps its own session, so the second tab does not replace Alice.
3. In Alice's tab, start a chat with Bob's ID and send a message.
4. Switch to Bob's tab. The conversation shows an unread badge (for example
   `1`).
5. Open the conversation. Bob's app marks Alice's message as read.
6. Switch back to Alice's tab. The message now shows **Read**.

Each tab keeps its own signed-in session, so this works even in the same browser.

### Two-person demo on two devices (phones/laptops)

1. Put both devices on the **same Wi-Fi network**.
2. On the device that runs PingHub, double-click **`start.cmd`** and read the
   **“On other devices”** address (for example `http://192.168.1.25:3000`).
3. Open that address in the browser on the other device. Nothing is installed
   on the phone — the browser is the app.
4. Sign in on each device. You can use the same employee account on both, or
   create a separate account with the company access code. A new device never
   creates a profile by itself.
5. Click **Use on another device** below your unique ID. Copy the invite; it
   contains the address and your 8-character ID.
6. The other person signs in, clicks **New chat**, pastes the ID, and sends.
7. The recipient sees an unread badge; opening the chat turns the sender's tick
   into **Read**.

`localhost` only means “this device”, so never use it on the phone. If the
address does not open, the technical operator may need to allow TCP 3000 through
the Windows firewall once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1
```

Guest Wi-Fi networks sometimes block device-to-device traffic; use a normal home
or hotspot network in that case.

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

### Whose history is this?

History always belongs to the employee who is signed in. Other employees'
history is private: PingHub refuses a request for someone else's messages. If
you delete a chat, its messages stop appearing in your History too (until a new
message makes the chat come back).

---

## 7. System dashboard

Click **System** in the left rail. It updates every 2 seconds. The page scrolls
inside the content area, so scroll down to reach the **Load Lab** below the
provider panel.

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

- The generator uses PingHub's internal tooling token and gives each virtual
  user its own `X-API-Key`, so each gets an independent token bucket (the same
  model as the formal Locust test). Employees never see this token.
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
Yes. Every employee signs in with a username and password. Creating a new account
also needs the company access code. This is a company LAN app, so treat your
password like any other work password.

**I opened PingHub on a new phone and it asks me to sign in. Is that a bug?**
No, that is exactly what should happen. A new device or a new network never
creates a profile. Sign in, or use **Activate / create account** with the company
access code.

**What is the company access code?**
The shared code that allows an employee to create a new account. Whoever runs
PingHub chooses it (`PINGHUB_SIGNUP_CODE`; the demo default is `pinghub-demo`).
Ask them if you do not know it.

**I forgot my password.**
While signed in, open the profile menu -> **Change password**. If you cannot sign
in at all, ask the technical operator: they can clear that one account's password
so you can activate it again with the company code. Your history is preserved.

**Why did I get signed out?**
A normal session lasts 12 hours; **Remember me** lasts 30 days. Changing your
password signs out every other device. Sign in again — nothing in your history is
lost.

**Why does my message say Failed?**
The notification provider is intentionally unreliable by default (30% failures).
The worker retries three times with backoff; if all attempts fail, the message
is marked `FAILED`. Use **Resend**, or switch the provider to **Healthy** on the
System page.

**Why did a send show “Rate limit reached”?**
You sent faster than your bucket refills (1 request/second after the first 10).
Wait a moment and retry. This is expected and is reported separately in the
Load Lab as `429`.

**I deleted a chat and it came back. Why?**
Deleting a chat hides it for you only. If the other person sends a new message,
the chat reappears with that new message. Messages from before the delete stay
hidden for you. The other person's copy was never removed.

**Can I send files, and what types?**
Yes — any file type up to 10 MB by default. Click the paperclip in the chat,
pick a file, and send it with or without a note. The recipient clicks the card
to download it.

**Why can't I open an attachment?**
Only the two people in the conversation can download a file. If you deleted the
chat, its files are hidden from your view (the other person keeps theirs). Ask
the other person to resend it if you need it again.

**What happens to a file when a message fails?**
It stays attached to the failed message. Switch the provider to **Healthy** on
the System page and use **Resend** — the same file is reused, so you do not need
to upload it twice.

**Is history lost when I close the browser?**
No. History is stored in PostgreSQL. Your session may end when you close the tab
(unless you ticked **Remember me**), but signing in again shows the same
messages.

**Why do two tabs show different people?**
Each tab keeps its own session in `sessionStorage`, so you can sign in as two
employees at once. **Remember me** stores that device's token in
`localStorage`; signing out removes it.

**A phone cannot open the address**
Use the **“On other devices”** address, not `localhost`, and join the same
Wi-Fi. On Windows, run `scripts\allow-lan.ps1` once as Administrator to allow
TCP 3000. Guest Wi-Fi often blocks device-to-device traffic.

**The sign-in form says “message-api unavailable”**
The backend is still starting. Wait 15–20 seconds and try again. The launcher
now waits for the full health check before it reports ready.

**The page looks old after an update**
Press **Ctrl+F5** once. The app is served with `Cache-Control: no-store`, so
later updates load normally.

**The System page did not scroll before**
It does now: the content area has its own scrollbar. Hard-refresh once if an old
tab still behaves differently.

**How do I start over?**
Profile menu -> **Sign out**. To create another employee, use **Activate / create
account** with the company code. To wipe the whole database, the technical
operator can run `docker compose down -v` and start again.
