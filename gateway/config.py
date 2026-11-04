"""Gateway configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    service_name: str = os.getenv("SERVICE_NAME", "gateway")

    # Infrastructure addresses (Docker service names by default).
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    rabbitmq_url: str = os.getenv(
        "RABBITMQ_URL", "amqp://pinghub:pinghub@rabbitmq:5672/"
    )
    queue_name: str = os.getenv("QUEUE_NAME", "messages")
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://pinghub:pinghub@postgres:5432/pinghub"
    )

    # Internal services.
    message_api_url: str = os.getenv("MESSAGE_API_URL", "http://message-api:8001")
    notification_url: str = os.getenv(
        "NOTIFICATION_URL", "http://notification-mock:8002"
    )

    # Token bucket: capacity 10, refill 1 token/second by default.
    rate_limit_capacity: int = _int("RATE_LIMIT_CAPACITY", 10)
    rate_limit_refill: float = _float("RATE_LIMIT_REFILL_PER_SEC", 1.0)

    # How long the gateway waits for message-api before returning 503.
    proxy_timeout: float = _float("PROXY_TIMEOUT_SECONDS", 5.0)


settings = Settings()
