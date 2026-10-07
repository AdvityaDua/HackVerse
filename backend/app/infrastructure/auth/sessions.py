"""Redis-backed denylist so sessions can actually be revoked.

Session tokens are stateless HMAC blobs, which means logout and ban would
otherwise leave a usable credential until natural expiry. We keep only the
revoked ``jti`` values, with a TTL matching the token's remaining lifetime, so
the denylist stays small.
"""

from redis.asyncio import Redis

_KEY_PREFIX = "session:revoked:"


class SessionRevocationStore:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    def _key(self, jti: str) -> str:
        return f"{_KEY_PREFIX}{jti}"

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        if not jti or ttl_seconds <= 0:
            return
        await self.redis.set(self._key(jti), "1", ex=ttl_seconds)

    async def is_revoked(self, jti: str) -> bool:
        if not jti:
            return False
        return bool(await self.redis.exists(self._key(jti)))
