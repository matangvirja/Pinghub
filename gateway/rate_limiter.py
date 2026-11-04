"""Token-bucket rate limiter.

Why a token bucket?
    Each client gets a bucket that holds ``capacity`` tokens.  Tokens refill at
    ``refill_per_second``.  Every request costs one token.  A client may burst
    up to ``capacity`` requests immediately, then is limited to the refill rate.

Why Redis?
    If the gateway runs as several replicas, an in-memory counter would be
    per-replica and easy to bypass.  A Redis Lua script makes the read +
    refill + take operation atomic across all gateway instances.

Fallback:
    If Redis is unavailable the limiter falls back to per-process memory so the
    gateway keeps protecting itself instead of failing completely.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from common.logger import get_logger

log = get_logger("rate_limiter")

# KEYS[1] = bucket key
# ARGV[1] = capacity, ARGV[2] = refill per second,
# ARGV[3] = current time in milliseconds, ARGV[4] = token cost
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    ts = now_ms
end

local elapsed = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now_ms))
-- Keep the bucket around long enough to refill completely.
local ttl_ms = math.ceil((capacity / refill) * 2000)
redis.call('PEXPIRE', key, ttl_ms)

return {allowed, tostring(tokens)}
"""


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: float
    retry_after: float
    backend: str


class TokenBucketRateLimiter:
    def __init__(
        self,
        redis_client: Redis | None,
        capacity: int = 10,
        refill_per_second: float = 1.0,
    ) -> None:
        self.redis = redis_client
        self.capacity = max(1, capacity)
        self.refill = max(0.001, refill_per_second)

        # Fallback bucket state: client -> (tokens, last_refill_monotonic)
        self._memory: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(client_key: str) -> str:
        # Hash the key so usernames/IPs with odd characters cannot break Redis.
        digest = hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:32]
        return f"pinghub:ratelimit:{digest}"

    async def check(self, client_key: str, cost: int = 1) -> RateLimitResult:
        if self.redis is not None:
            try:
                return await self._check_redis(client_key, cost)
            except Exception as exc:  # Redis down -> local fallback
                log.warning(
                    "rate_limiter_redis_failed",
                    extra={"error": str(exc), "fallback": "memory"},
                )
        return await self._check_memory(client_key, cost)

    async def _check_redis(self, client_key: str, cost: int) -> RateLimitResult:
        now_ms = int(time.time() * 1000)
        allowed, tokens = await self.redis.eval(
            _LUA_TOKEN_BUCKET,
            1,
            self._key(client_key),
            self.capacity,
            self.refill,
            now_ms,
            cost,
        )
        allowed_int = int(allowed)
        remaining = float(tokens)
        retry_after = 0.0
        if not allowed_int:
            retry_after = max(0.0, (cost - remaining) / self.refill)

        return RateLimitResult(
            allowed=bool(allowed_int),
            remaining=remaining,
            retry_after=retry_after,
            backend="redis",
        )

    async def _check_memory(self, client_key: str, cost: int) -> RateLimitResult:
        async with self._lock:
            now = time.monotonic()
            tokens, last = self._memory.get(client_key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)

            if tokens >= cost:
                tokens -= cost
                allowed = True
            else:
                allowed = False

            self._memory[client_key] = (tokens, now)

        retry_after = 0.0 if allowed else max(0.0, (cost - tokens) / self.refill)
        return RateLimitResult(
            allowed=allowed,
            remaining=tokens,
            retry_after=retry_after,
            backend="memory",
        )
