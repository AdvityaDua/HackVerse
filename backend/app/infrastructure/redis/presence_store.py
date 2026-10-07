"""Ephemeral presence.

Previous implementation kept one hash per event and set the TTL on the *hash*,
refreshed by any player's movement. That had two failure modes: players who
dropped without a clean close never expired (ghost avatars accumulating across
a 36-hour event), and a quiet event wiped everyone's presence at once.

Current layout, per event:

    ev:{event_id}:player:{user_id}   HASH  x, y, direction, status, room_id
                                          with its own TTL, refreshed per update
    ev:{event_id}:players           ZSET  member=user_id, score=last_seen epoch
    ev:{event_id}:room:{room_id}    SET   user_ids currently in the room

Position and status live in separate hash fields, so walking around no longer
clobbers a mentor's ``busy``/``dnd`` state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from redis.asyncio import Redis

DEFAULT_PRESENCE_TTL_SECONDS = 60


@dataclass(frozen=True)
class PresenceRecord:
    user_id: str
    x: float
    y: float
    direction: str
    status: str
    room_id: str | None
    last_seen: float

    def to_payload(self) -> dict[str, object]:
        return {
            "userId": self.user_id,
            "x": self.x,
            "y": self.y,
            "direction": self.direction,
            "status": self.status,
            "roomId": self.room_id,
        }


def _as_float(value: object, fallback: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


class RedisPresenceStore:
    def __init__(self, redis: Redis, ttl_seconds: int = DEFAULT_PRESENCE_TTL_SECONDS) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    # -- keys ---------------------------------------------------------------

    def _player_key(self, event_id: UUID, user_id: UUID) -> str:
        return f"ev:{event_id}:player:{user_id}"

    def _index_key(self, event_id: UUID) -> str:
        return f"ev:{event_id}:players"

    def _room_key(self, event_id: UUID, room_id: str) -> str:
        return f"ev:{event_id}:room:{room_id}"

    # -- writes -------------------------------------------------------------

    async def register_player(
        self,
        event_id: UUID,
        user_id: UUID,
        *,
        x: float,
        y: float,
        status: str,
        room_id: str | None = None,
    ) -> None:
        """Create or refresh the full player record on join/reconnect."""
        now = time.time()
        fields: dict[str, str] = {
            "x": str(x),
            "y": str(y),
            "direction": "down",
            "status": status,
            "room_id": room_id or "",
        }
        pipe = self.redis.pipeline(transaction=False)
        pipe.hset(self._player_key(event_id, user_id), mapping=fields)
        pipe.expire(self._player_key(event_id, user_id), self.ttl_seconds)
        pipe.zadd(self._index_key(event_id), {str(user_id): now})
        await pipe.execute()

    async def update_position(
        self,
        event_id: UUID,
        user_id: UUID,
        x: float,
        y: float,
        direction: str = "down",
    ) -> None:
        """Write position only. Deliberately does not touch status or room."""
        now = time.time()
        key = self._player_key(event_id, user_id)
        pipe = self.redis.pipeline(transaction=False)
        pipe.hset(key, mapping={"x": str(x), "y": str(y), "direction": direction})
        pipe.expire(key, self.ttl_seconds)
        pipe.zadd(self._index_key(event_id), {str(user_id): now})
        await pipe.execute()

    async def update_status(self, event_id: UUID, user_id: UUID, status: str) -> None:
        key = self._player_key(event_id, user_id)
        pipe = self.redis.pipeline(transaction=False)
        pipe.hset(key, "status", status)
        pipe.expire(key, self.ttl_seconds)
        pipe.zadd(self._index_key(event_id), {str(user_id): time.time()})
        await pipe.execute()

    async def set_room(self, event_id: UUID, user_id: UUID, room_id: str | None) -> None:
        key = self._player_key(event_id, user_id)
        previous = await self.redis.hget(key, "room_id")

        pipe = self.redis.pipeline(transaction=False)
        pipe.hset(key, "room_id", room_id or "")
        pipe.expire(key, self.ttl_seconds)
        if previous:
            pipe.srem(self._room_key(event_id, previous), str(user_id))
        if room_id:
            pipe.sadd(self._room_key(event_id, room_id), str(user_id))
            pipe.expire(self._room_key(event_id, room_id), self.ttl_seconds * 10)
        await pipe.execute()

    async def heartbeat(self, event_id: UUID, user_id: UUID) -> None:
        pipe = self.redis.pipeline(transaction=False)
        pipe.expire(self._player_key(event_id, user_id), self.ttl_seconds)
        pipe.zadd(self._index_key(event_id), {str(user_id): time.time()})
        await pipe.execute()

    async def remove_player(self, event_id: UUID, user_id: UUID) -> None:
        key = self._player_key(event_id, user_id)
        room_id = await self.redis.hget(key, "room_id")
        pipe = self.redis.pipeline(transaction=False)
        pipe.delete(key)
        pipe.zrem(self._index_key(event_id), str(user_id))
        if room_id:
            pipe.srem(self._room_key(event_id, room_id), str(user_id))
        await pipe.execute()

    # -- reads --------------------------------------------------------------

    async def get_player(self, event_id: UUID, user_id: UUID) -> PresenceRecord | None:
        raw = await self.redis.hgetall(self._player_key(event_id, user_id))
        if not raw:
            return None
        return self._record(str(user_id), raw, last_seen=time.time())

    async def get_event_presence(self, event_id: UUID) -> list[PresenceRecord]:
        """Return live players, sweeping anyone whose record has expired."""
        user_ids = await self._live_user_ids(event_id)
        if not user_ids:
            return []

        pipe = self.redis.pipeline(transaction=False)
        for user_id in user_ids:
            pipe.hgetall(f"ev:{event_id}:player:{user_id}")
        results = await pipe.execute()

        records: list[PresenceRecord] = []
        orphaned: list[str] = []
        now = time.time()
        for user_id, raw in zip(user_ids, results, strict=True):
            if not raw:
                orphaned.append(user_id)
                continue
            records.append(self._record(user_id, raw, last_seen=now))

        if orphaned:
            # Index entry outlived the TTL'd record: drop it so the ghost goes away.
            await self.redis.zrem(self._index_key(event_id), *orphaned)
        return records

    async def get_room_members(self, event_id: UUID, room_id: str) -> list[str]:
        members = await self.redis.smembers(self._room_key(event_id, room_id))
        return sorted(str(member) for member in members)

    async def count_online(self, event_id: UUID) -> int:
        return len(await self._live_user_ids(event_id))

    async def sweep(self, event_id: UUID) -> int:
        """Drop index entries older than the TTL. Returns the number removed."""
        cutoff = time.time() - self.ttl_seconds
        return int(await self.redis.zremrangebyscore(self._index_key(event_id), "-inf", cutoff))

    # -- internals ----------------------------------------------------------

    async def _live_user_ids(self, event_id: UUID) -> list[str]:
        cutoff = time.time() - self.ttl_seconds
        members = await self.redis.zrangebyscore(self._index_key(event_id), cutoff, "+inf")
        return [m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in members]

    def _record(self, user_id: str, raw: dict, last_seen: float) -> PresenceRecord:
        decoded = {
            (k.decode("utf-8") if isinstance(k, bytes) else k): (
                v.decode("utf-8") if isinstance(v, bytes) else v
            )
            for k, v in raw.items()
        }
        room_id = decoded.get("room_id") or None
        return PresenceRecord(
            user_id=user_id,
            x=_as_float(decoded.get("x")),
            y=_as_float(decoded.get("y")),
            direction=decoded.get("direction") or "down",
            status=decoded.get("status") or "online",
            room_id=room_id,
            last_seen=last_seen,
        )
