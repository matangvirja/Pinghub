"""Mock email/push notification provider.

It is deliberately unreliable so the worker's retry and circuit-breaker logic
can be demonstrated.  You can control it at runtime:

    POST /control {"mode": "always_fail"}
    POST /control {"mode": "always_succeed"}
    POST /control {"mode": "random", "fail_rate": 0.3, "latency_ms": 50}

Modes:
    random          fail with probability ``fail_rate``
    always_fail     every call returns 503
    always_succeed  every call succeeds

The provider is still "healthy" in always_fail mode: /health reports that the
process is alive. The worker's circuit breaker is what protects the system.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.logger import configure_logging, get_logger, request_id_var

SERVICE_NAME = "notification-mock"
configure_logging(SERVICE_NAME)
log = get_logger("notification")

app = FastAPI(
    title="PingHub Mock Notification Provider",
    version="1.0.0",
    description="Fake email/push provider that can fail on demand",
)


class ProviderState:
    def __init__(self) -> None:
        self.mode: str = os.getenv("NOTIFY_MODE", "random")
        self.fail_rate: float = float(os.getenv("NOTIFY_FAIL_RATE", "0.3"))
        self.latency_ms: int = int(os.getenv("NOTIFY_LATENCY_MS", "50"))
        self.attempted = 0
        self.succeeded = 0
        self.failed = 0
        self.lock = asyncio.Lock()

    async def snapshot(self) -> dict:
        async with self.lock:
            return {
                "mode": self.mode,
                "fail_rate": self.fail_rate,
                "latency_ms": self.latency_ms,
                "attempted": self.attempted,
                "succeeded": self.succeeded,
                "failed": self.failed,
            }


state = ProviderState()


class NotifyIn(BaseModel):
    channel: str = Field(default="email", max_length=20)
    to: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1, max_length=4000)
    request_id: str | None = None


class ControlIn(BaseModel):
    mode: Literal["random", "always_fail", "always_succeed"] | None = None
    fail_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: int | None = Field(default=None, ge=0, le=5000)


@app.get("/live")
async def live() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "mode": state.mode,
        "fail_rate": state.fail_rate,
        "latency_ms": state.latency_ms,
    }


@app.get("/stats")
async def stats() -> dict:
    return await state.snapshot()


@app.get("/control")
async def get_control() -> dict:
    return await state.snapshot()


@app.post("/control")
async def set_control(body: ControlIn) -> dict:
    async with state.lock:
        if body.mode is not None:
            state.mode = body.mode
        if body.fail_rate is not None:
            state.fail_rate = body.fail_rate
        if body.latency_ms is not None:
            state.latency_ms = body.latency_ms
        snapshot = {
            "mode": state.mode,
            "fail_rate": state.fail_rate,
            "latency_ms": state.latency_ms,
            "attempted": state.attempted,
            "succeeded": state.succeeded,
            "failed": state.failed,
        }
    log.info("provider_control_changed", extra=snapshot)
    return snapshot


@app.post("/reset-stats")
async def reset_stats() -> dict:
    async with state.lock:
        state.attempted = 0
        state.succeeded = 0
        state.failed = 0
    return {"status": "reset"}


@app.post("/notify")
async def notify(body: NotifyIn, request: Request):
    request_id = body.request_id or request.headers.get("x-request-id") or "-"
    token = request_id_var.set(request_id)

    try:
        # Simulate provider latency with a little jitter.
        delay_ms = state.latency_ms
        if delay_ms > 0:
            await asyncio.sleep((delay_ms + random.uniform(-10, 10)) / 1000.0)

        should_fail = state.mode == "always_fail" or (
            state.mode == "random" and random.random() < state.fail_rate
        )

        async with state.lock:
            state.attempted += 1
            if should_fail:
                state.failed += 1
            else:
                state.succeeded += 1

        if should_fail:
            log.warning(
                "provider_call_failed",
                extra={"to": body.to, "channel": body.channel, "mode": state.mode},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "failed",
                    "provider": "mock-email",
                    "error": "provider_unavailable",
                    "request_id": request_id,
                },
            )

        provider_id = f"msg_{uuid.uuid4().hex[:12]}"
        log.info(
            "provider_call_succeeded",
            extra={"to": body.to, "channel": body.channel, "provider_id": provider_id},
        )
        return {
            "status": "sent",
            "provider": "mock-email",
            "provider_id": provider_id,
            "channel": body.channel,
            "to": body.to,
            "request_id": request_id,
        }
    finally:
        request_id_var.reset(token)
