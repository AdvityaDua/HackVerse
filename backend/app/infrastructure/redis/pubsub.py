"""Redis pub/sub fanout.

Without this, every broadcast is confined to the process that produced it: an
announcement created on API replica A never reaches clients connected to
replica B, and the service cannot be scaled or rolling-deployed at all.

One pub/sub connection per process. Channel subscriptions are reference counted
so that the Nth connection interested in a channel is cheap, and the channel is
dropped from Redis once nobody local cares.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

from app.infrastructure.observability.logging import get_logger

logger = get_logger(__name__)

Dispatcher = Callable[[str, str], Awaitable[None]]


class RedisEventBus:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._pubsub = None
        self._reader: asyncio.Task[None] | None = None
        self._refcounts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self._dispatcher: Dispatcher | None = None
        self._started = False

    def set_dispatcher(self, dispatcher: Dispatcher) -> None:
        """Register the callback invoked as ``dispatcher(channel, raw_message)``."""
        self._dispatcher = dispatcher

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            self._reader = asyncio.create_task(self._read_loop(), name="redis-event-bus")
            self._started = True
            logger.info("event_bus.started")

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._started = False
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):  # noqa: B014 - best effort
                    pass
                self._reader = None
            if self._pubsub is not None:
                try:
                    await self._pubsub.aclose()
                except Exception:
                    logger.warning("event_bus.pubsub_close_failed", exc_info=True)
                self._pubsub = None
            self._refcounts.clear()
            logger.info("event_bus.stopped")

    async def subscribe(self, channels: list[str]) -> None:
        """Add interest in ``channels``, subscribing in Redis on first interest."""
        if not channels or self._pubsub is None:
            return
        new: list[str] = []
        async with self._lock:
            for channel in channels:
                if self._refcounts[channel] == 0:
                    new.append(channel)
                self._refcounts[channel] += 1
        if new:
            await self._pubsub.subscribe(*new)

    async def unsubscribe(self, channels: list[str]) -> None:
        """Drop interest in ``channels``, unsubscribing when nobody local remains."""
        if not channels or self._pubsub is None:
            return
        dead: list[str] = []
        async with self._lock:
            for channel in channels:
                if self._refcounts.get(channel, 0) == 0:
                    continue
                self._refcounts[channel] -= 1
                if self._refcounts[channel] == 0:
                    del self._refcounts[channel]
                    dead.append(channel)
        if dead:
            try:
                await self._pubsub.unsubscribe(*dead)
            except Exception:
                logger.warning("event_bus.unsubscribe_failed", channels=dead, exc_info=True)

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        try:
            await self._redis.publish(channel, json.dumps(payload, separators=(",", ":")))
        except Exception:
            # A failed fanout must not take down the request that triggered it.
            logger.error("event_bus.publish_failed", channel=channel, exc_info=True)

    async def _read_loop(self) -> None:
        assert self._pubsub is not None
        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                if self._dispatcher is None:
                    continue
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8")
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await self._dispatcher(channel, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("event_bus.read_loop_error", exc_info=True)
                await asyncio.sleep(0.5)


_bus: RedisEventBus | None = None


def get_event_bus() -> RedisEventBus:
    """Process-wide bus instance. Created on first use, started by the lifespan."""
    global _bus
    if _bus is None:
        from app.infrastructure.redis.client import get_redis_client

        _bus = RedisEventBus(get_redis_client())
    return _bus


def reset_event_bus() -> None:
    """Test hook: drop the cached bus so a fresh Redis client is picked up."""
    global _bus
    _bus = None
