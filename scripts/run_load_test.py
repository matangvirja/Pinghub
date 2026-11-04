"""Run the 200-user Locust spike while sampling RabbitMQ queue depth.

Usage:
    python scripts/run_load_test.py

Writes Locust CSV files and ``loadtest/results/queue-monitor.csv``, then prints
the numbers needed by docs/load-test-report.md. Requires the stack to be up and
the notification mock in ``always_succeed`` mode (the script sets it).
"""

from __future__ import annotations

import base64
import csv
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "loadtest" / "results"
QUEUE_URL = "http://localhost:15672/api/queues/%2F/messages"
MOCK_URL = "http://localhost:8002/control"
DURATION = 60
RAMP_SECONDS = 4


def http_json(url: str, body: dict | None = None, auth: tuple[str, str] | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read() or b"{}")


def queue_depth() -> tuple[int, int, int]:
    _, payload = http_json(QUEUE_URL, auth=("pinghub", "pinghub"))
    return payload["messages"], payload["messages_ready"], payload["messages_unacknowledged"]


def db_counts() -> dict[str, int]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "pinghub",
            "-d",
            "pinghub",
            "-t",
            "-A",
            "-F",
            ",",
            "-c",
            "SELECT status, count(*) FROM messages GROUP BY status",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status, count = line.strip().split(",")
        counts[status] = int(count)
    return counts


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    http_json(MOCK_URL, {"mode": "always_succeed"})

    before = db_counts()
    samples: list[tuple[float, int, int, int]] = []
    started = time.monotonic()
    stop = threading.Event()

    def monitor() -> None:
        while not stop.is_set():
            try:
                messages, ready, unacked = queue_depth()
                samples.append((time.monotonic() - started, messages, ready, unacked))
            except Exception:
                pass
            stop.wait(0.5)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

    command = [
        "docker",
        "compose",
        "--profile",
        "loadtest",
        "run",
        "--rm",
        "locust",
        "-f",
        "/mnt/locust/locustfile.py",
        "--host",
        "http://gateway:8000",
        "--headless",
        "-u",
        "200",
        "-r",
        "50",
        "-t",
        f"{DURATION}s",
        "--processes",
        "4",
        "--csv",
        "/tmp/results/spike",
        "--only-summary",
    ]
    print("running:", " ".join(command))
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    test_ended = time.monotonic() - started
    (RESULTS / "locust-output.txt").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )

    # Keep sampling until the queue is empty (or 120s safety limit).
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            messages, _ready, _unacked = queue_depth()
        except Exception:
            messages = -1
        if messages <= 0:
            break
        time.sleep(0.5)
    drain_finished = time.monotonic() - started
    stop.set()
    thread.join(timeout=3)

    if not samples:
        print("queue monitor collected no samples", file=sys.stderr)
        return 1

    peak = max(samples, key=lambda row: row[1])
    at_ramp = min(samples, key=lambda row: abs(row[0] - RAMP_SECONDS))
    at_end = min(samples, key=lambda row: abs(row[0] - test_ended))
    final = samples[-1]

    with (RESULTS / "queue-monitor.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed_s", "messages", "ready", "unacknowledged"])
        for elapsed, messages, ready, unacked in samples:
            writer.writerow([round(elapsed, 1), messages, ready, unacked])

    after = db_counts()
    delivered = after.get("DELIVERED", 0) - before.get("DELIVERED", 0)
    failed = after.get("FAILED", 0) - before.get("FAILED", 0)

    print()
    print("=== Queue + delivery summary ===")
    print(f"queue after ramp ({at_ramp[0]:.0f}s):      {at_ramp[1]}")
    print(f"peak queue depth:                {peak[1]} at {peak[0]:.1f}s")
    print(f"queue at end of test:            {at_end[1]}")
    print(f"queue at final sample:           {final[1]}")
    print(f"drain time after locust stopped: {drain_finished - test_ended:.1f}s")
    print(f"messages DELIVERED during run:   {delivered}")
    print(f"messages FAILED during run:      {failed}")
    print(f"locust exit code:                {completed.returncode}")
    print(f"artefacts:                       {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
