"""Fixed-window rate limiting backed by Redis.

Applies to movement, chat, interactions, signalling, auth callbacks and invite
claims. Without it a single client can saturate the database pool or the
broadcast loop, which is the cheapest possible denial of service against a live
event.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.infrastructure.observability.logging import get_logger

logger = get_logger(__name__)


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        """Consume one token from ``key``. True when the call is within budget.

        Fails open: if Redis is unreachable we would rather serve traffic than
        hard-fail every message, but we log it so the degradation is visible.
        """
        if limit <= 0:
            return False
        try:
            pipe = self.redis.pipeline(transaction=False)
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            count, _ = await pipe.execute()
            return int(count) <= limit
        except Exception:
            logger.warning("rate_limit.unavailable", key=key, exc_info=True)
            return True

    async def remaining(self, key: str, limit: int) -> int:
        try:
            value = await self.redis.get(key)
            used = int(value) if value is not None else 0
        except Exception:
            return limit
        return max(0, limit - used)


def movement_key(event_id: object, user_id: object, second_bucket: int) -> str:
    return f"rl:move:{event_id}:{user_id}:{second_bucket}"


def chat_key(event_id: object, user_id: object, minute_bucket: int) -> str:
    return f"rl:chat:{event_id}:{user_id}:{minute_bucket}"


def interact_key(event_id: object, user_id: object, minute_bucket: int) -> str:
    return f"rl:interact:{event_id}:{user_id}:{minute_bucket}"


def signalling_key(event_id: object, user_id: object, minute_bucket: int) -> str:
    return f"rl:rtc:{event_id}:{user_id}:{minute_bucket}"


def auth_key(scope: str, identifier: str, minute_bucket: int) -> str:
    return f"rl:auth:{scope}:{identifier}:{minute_bucket}"
