"""Locust load test for PingHub.

Run locally:
    pip install -r loadtest/requirements.txt
    locust -f loadtest/locustfile.py --host http://localhost:8000

Run headless (matches the assignment's "200 users for 60 seconds"):
    locust -f loadtest/locustfile.py --host http://localhost:8000 \
        --headless -u 200 -r 50 -t 60s --csv loadtest/results/spike

Or through Docker Compose:
    docker compose --profile loadtest run --rm locust \
        -f /mnt/locust/locustfile.py --host http://gateway:8000 \
        --headless -u 200 -r 50 -t 60s --csv /tmp/results/spike

Each virtual user gets its own X-API-Key, so the token bucket (10 tokens,
1 token/second) is applied per simulated client instead of putting every
Locust user in one shared bucket.
"""

from __future__ import annotations

import uuid

from locust import HttpUser, between, events, task

COUNTERS = {"accepted": 0, "rate_limited": 0, "errors": 0}


class PingHubUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        self.api_key = f"locust-{uuid.uuid4()}"
        self.sender = f"load-{uuid.uuid4().hex[:10]}"
        self.receiver = "loadtest-receiver"
        self.client.headers.update({"X-API-Key": self.api_key})

    @task(5)
    def send_message(self) -> None:
        payload = {
            "sender": self.sender,
            "receiver": self.receiver,
            "body": "load test message",
        }
        with self.client.post(
            "/messages", json=payload, name="POST /messages", catch_response=True
        ) as response:
            if response.status_code == 202:
                COUNTERS["accepted"] += 1
                response.success()
            elif response.status_code == 429:
                # Expected once the 10-token bucket is empty. It is still a
                # successful demonstration of the rate limiter, so do not count
                # it as a Locust failure.
                COUNTERS["rate_limited"] += 1
                response.success()
            else:
                COUNTERS["errors"] += 1
                response.failure(f"unexpected status {response.status_code}")

    @task(2)
    def read_history(self) -> None:
        with self.client.get(
            f"/history?user={self.sender}",
            name="GET /history",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 429):
                response.success()
            else:
                COUNTERS["errors"] += 1
                response.failure(f"unexpected status {response.status_code}")

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="GET /health")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    print("\n=== PingHub load test counters ===")
    print(f"accepted (202):      {COUNTERS['accepted']}")
    print(f"rate limited (429):  {COUNTERS['rate_limited']}")
    print(f"unexpected errors:   {COUNTERS['errors']}")
    print("==================================\n")
