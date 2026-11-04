"""Retries with exponential backoff + a three-state circuit breaker.

This is implemented from scratch (no tenacity/pybreaker) so every line can be
explained in the viva.

Circuit breaker states:
    CLOSED     normal; calls are allowed
    OPEN       too many failures; calls are rejected immediately
    HALF_OPEN  recovery window elapsed; exactly one trial call is allowed

Transitions:
    CLOSED --(failure_threshold consecutive failures)--> OPEN
    OPEN --(recovery_timeout seconds)--> HALF_OPEN
    HALF_OPEN --(success)--> CLOSED
    HALF_OPEN --(failure)--> OPEN
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    """Raised when the breaker refuses a call because it is OPEN."""


class RetryError(RuntimeError):
    """Raised after every retry attempt failed."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"failed after {attempts} attempts: {last_error}")


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        name: str = "notification",
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.name = name

        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def before_call(self) -> None:
        now = time.monotonic()

        if self._state == OPEN:
            elapsed = now - self._opened_at
            if elapsed < self.recovery_timeout:
                remaining = self.recovery_timeout - elapsed
                raise CircuitOpenError(
                    f"{self.name} circuit is OPEN; retry in {remaining:.1f}s"
                )
            # Recovery window elapsed: allow one trial call.
            self._state = HALF_OPEN
            self._half_open_in_flight = False

        if self._state == HALF_OPEN:
            if self._half_open_in_flight:
                raise CircuitOpenError(
                    f"{self.name} circuit is HALF_OPEN; trial already in flight"
                )
            self._half_open_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._state = CLOSED
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._half_open_in_flight = False

        if self._state == HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = OPEN
            self._opened_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self._state,
            "failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }


async def retry_call(
    func: Callable[[], Awaitable[T]],
    *,
    breaker: CircuitBreaker,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    on_failure: Callable[[int, Exception], None] | None = None,
) -> tuple[T, int]:
    """Run ``func`` with exponential backoff (1s, 2s, 4s, ...).

    Returns ``(result, attempts_used)`` on success.
    Raises ``CircuitOpenError`` if the breaker blocks a call.
    Raises ``RetryError`` when every attempt fails.
    """

    delay = base_delay
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        breaker.before_call()
        try:
            result = await func()
        except CircuitOpenError:
            raise
        except Exception as exc:
            last_error = exc
            breaker.record_failure()
            if on_failure is not None:
                on_failure(attempt, exc)

            if attempt >= max_attempts:
                break

            await asyncio.sleep(delay)
            delay *= 2
        else:
            breaker.record_success()
            return result, attempt

    assert last_error is not None
    raise RetryError(max_attempts, last_error)
