"""Calls the mock notification provider through the retry + breaker policy."""

from __future__ import annotations

import httpx

from common.logger import get_logger, request_id_var
from resilience import CircuitBreaker, CircuitOpenError, retry_call

log = get_logger("notifier")


class Notifier:
    def __init__(
        self,
        base_url: str,
        breaker: CircuitBreaker,
        *,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.breaker = breaker
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.client = httpx.AsyncClient(timeout=timeout)

    async def send(
        self,
        *,
        to: str,
        body: str,
        request_id: str,
        task_id: str,
    ) -> tuple[dict, int]:
        async def _call() -> dict:
            response = await self.client.post(
                f"{self.base_url}/notify",
                json={
                    "channel": "email",
                    "to": to,
                    "message": body,
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"notification provider returned {response.status_code}: "
                    f"{response.text[:200]}"
                )
            return response.json()

        def _on_failure(attempt: int, error: Exception) -> None:
            log.warning(
                "notify_failed",
                extra={
                    "task_id": task_id,
                    "attempt": attempt,
                    "error": str(error),
                    "circuit_state": self.breaker.state,
                },
            )

        previous_state = self.breaker.state
        try:
            result, attempts = await retry_call(
                _call,
                breaker=self.breaker,
                max_attempts=self.max_attempts,
                base_delay=self.base_delay,
                on_failure=_on_failure,
            )
        except CircuitOpenError as exc:
            log.error(
                "circuit_open",
                extra={"task_id": task_id, "error": str(exc), "circuit_state": self.breaker.state},
            )
            raise
        finally:
            if self.breaker.state != previous_state:
                log.warning(
                    "circuit_state_changed",
                    extra={
                        "task_id": task_id,
                        "from": previous_state,
                        "to": self.breaker.state,
                        "failures": self.breaker.failures,
                    },
                )

        log.info(
            "notify_ok",
            extra={"task_id": task_id, "attempt": attempts, "request_id": request_id_var.get()},
        )
        return result, attempts

    async def close(self) -> None:
        await self.client.aclose()
