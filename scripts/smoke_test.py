#!/usr/bin/env python3
"""End-to-end smoke test for a running PingHub stack.

Start the stack first:
    docker compose up --build

Then run:
    python scripts/smoke_test.py

It checks:
  1. /health
  2. POST /messages -> 202 + task id
  3. task reaches a terminal status
  4. /history is a MISS then a HIT (Redis cache)
  5. the token bucket returns 429 after the burst
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

GATEWAY = os.getenv("PINGHUB_GATEWAY_URL", "http://localhost:8000")
NOTIFICATION = os.getenv("PINGHUB_NOTIFICATION_URL", "http://localhost:8002")
INTERNAL_TOKEN = os.getenv("PINGHUB_INTERNAL_TOKEN", "pinghub-internal-dev")

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[bool, str]] = []


def internal_headers(**extra: str) -> dict[str, str]:
    """The smoke test uses synthetic users, so it presents the internal token."""

    return {"X-Internal-Token": INTERNAL_TOKEN, **extra}


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def record(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"[{PASS if ok else FAIL}] {label}{(' -> ' + detail) if detail else ''}")


def main() -> int:
    run_id = uuid.uuid4().hex[:8]
    sender = f"smoke-sender-{run_id}"
    receiver = f"smoke-receiver-{run_id}"
    api_key = f"smoke-key-{run_id}"

    print(f"PingHub smoke test (run {run_id})\n")

    # Make the provider deterministic for this test.
    status, _, body = call(
        "POST", f"{NOTIFICATION}/control", {"mode": "always_succeed"}
    )
    record(status == 200, "notification-mock controlled", body[:120])

    # 1. Health
    status, _, body = call("GET", f"{GATEWAY}/health")
    health = json.loads(body) if body else {}
    record(status == 200, "GET /health returned 200", f"status={health.get('status')}")

    # 2. Send a message
    status, headers, body = call(
        "POST",
        f"{GATEWAY}/messages",
        {
            "sender": sender,
            "receiver": receiver,
            "body": "hello from the smoke test",
        },
        internal_headers(**{"X-API-Key": api_key}),
    )
    payload = json.loads(body) if body else {}
    task_id = payload.get("task_id")
    record(
        status == 202 and bool(task_id),
        "POST /messages returned 202 + task_id",
        f"status={status} task_id={task_id}",
    )
    if not task_id:
        return finish()

    # 3. Poll status until terminal
    final = None
    for _ in range(40):
        time.sleep(0.5)
        status, _, body = call(
            "GET", f"{GATEWAY}/messages/{task_id}", headers=internal_headers()
        )
        state = json.loads(body) if body else {}
        final = state.get("status")
        if final in {"DELIVERED", "FAILED"}:
            break
    record(
        final == "DELIVERED",
        "task reached DELIVERED",
        f"final_status={final} attempts={state.get('attempts')}",
    )

    # 4. History cache: first MISS, second HIT
    status1, headers1, body1 = call(
        "GET",
        f"{GATEWAY}/history?user={sender}",
        headers=internal_headers(**{"X-API-Key": api_key}),
    )
    cache1 = headers1.get("X-Cache") or headers1.get("x-cache")
    history = json.loads(body1) if body1 else {}
    record(
        status1 == 200 and cache1 == "MISS" and history.get("count", 0) >= 1,
        "first /history is a cache MISS",
        f"status={status1} cache={cache1} count={history.get('count')}",
    )

    status2, headers2, _ = call(
        "GET",
        f"{GATEWAY}/history?user={sender}",
        headers=internal_headers(**{"X-API-Key": api_key}),
    )
    cache2 = headers2.get("X-Cache") or headers2.get("x-cache")
    record(
        status2 == 200 and cache2 == "HIT",
        "second /history is a cache HIT",
        f"status={status2} cache={cache2}",
    )

    # 5. Rate limit: 15 rapid requests with a fresh key -> expect some 429s.
    burst_key = f"burst-{uuid.uuid4()}"
    accepted = 0
    limited = 0
    for index in range(15):
        status, _, _ = call(
            "POST",
            f"{GATEWAY}/messages",
            {
                "sender": sender,
                "receiver": receiver,
                "body": f"burst message {index}",
            },
            internal_headers(**{"X-API-Key": burst_key}),
        )
        if status == 202:
            accepted += 1
        elif status == 429:
            limited += 1
    record(
        limited > 0,
        "token bucket returned 429 under a burst",
        f"accepted={accepted} rate_limited={limited}",
    )

    # Restore random failures for the normal demo.
    call("POST", f"{NOTIFICATION}/control", {"mode": "random", "fail_rate": 0.3})
    return finish()


def finish() -> int:
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        print("Failed checks:")
        for label in failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
