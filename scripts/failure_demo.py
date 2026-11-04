"""Exercise the retry + circuit-breaker path and print a compact summary.

Usage:
    python scripts/failure_demo.py

It drives the notification mock through three phases:
    1. always_fail  -> retries, then the circuit opens
    2. wait         -> recovery timeout elapses (OPEN -> HALF_OPEN on next call)
    3. always_succeed -> one trial call closes the circuit again
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

GATEWAY = "http://localhost:8000"
MOCK = "http://localhost:8002"


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read() or b"{}")


def send(key: str) -> str:
    status, payload = call(
        "POST",
        f"{GATEWAY}/messages",
        {"sender": "fault-demo", "receiver": "fault-demo", "body": "fault test"},
        {"X-API-Key": key},
    )
    assert status == 202, (status, payload)
    return payload["task_id"]


def status_of(task_id: str) -> str:
    _, payload = call("GET", f"{GATEWAY}/messages/{task_id}")
    return payload.get("status", "?")


def wait_terminal(task_ids: list[str], timeout: float = 30.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    final: dict[str, str] = {}
    while time.monotonic() < deadline:
        for task_id in task_ids:
            if task_id in final:
                continue
            state = status_of(task_id)
            if state in {"DELIVERED", "FAILED"}:
                final[task_id] = state
        if len(final) == len(task_ids):
            break
        time.sleep(0.5)
    return final


def main() -> None:
    call("POST", f"{MOCK}/control", {"mode": "always_fail"})
    call("POST", f"{MOCK}/reset-stats")
    started = time.monotonic()
    tasks = [send(f"fault-{uuid.uuid4()}") for _ in range(20)]
    print(f"sent 20 tasks in {time.monotonic() - started:.1f}s")

    final = wait_terminal(tasks, timeout=40)
    failed = sum(1 for state in final.values() if state == "FAILED")
    delivered = sum(1 for state in final.values() if state == "DELIVERED")
    print(
        f"phase 1 (always_fail): {len(final)}/20 terminal, "
        f"{delivered} DELIVERED, {failed} FAILED in "
        f"{time.monotonic() - started:.1f}s"
    )

    print("waiting 32s for the OPEN -> HALF_OPEN recovery window...")
    time.sleep(32)

    call("POST", f"{MOCK}/control", {"mode": "always_succeed"})
    recovery_started = time.monotonic()
    recovered = [send(f"recovered-{uuid.uuid4()}") for _ in range(5)]
    final = wait_terminal(recovered, timeout=30)
    print(
        f"phase 2 (provider restored): "
        f"{sum(1 for s in final.values() if s == 'DELIVERED')}/5 DELIVERED in "
        f"{time.monotonic() - recovery_started:.1f}s"
    )
    print("task ids:", " ".join(recovered))


if __name__ == "__main__":
    main()
