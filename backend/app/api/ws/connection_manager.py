"""Local WebSocket connection registry.

Three problems with the previous version are fixed here:

* Connections were keyed by ``user_id``, so a second browser tab silently
  replaced the first and the first tab's eventual disconnect deleted the second
  tab's entry. Connections now have their own identity and a user may hold
  several.
* Broadcasts awaited each ``send_text`` in a bare loop, so one slow client on
  bad Wi-Fi blocked delivery to everyone behind it, and one dead socket aborted
  the rest of the loop. Each connection now owns a bounded queue drained by its
  own task; a backed-up client drops position frames and is disconnected if it
  falls behind on messages that matter.
* Delivery was process-local. Fanout now arrives from the Redis bus and is
  matched against locally subscribed channels.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.domain.realtime.interest import Cell
from app.infrastructure.observability.logging import get_logger
from app.infrastructure.redis.pubsub import RedisEventBus

logger = get_logger(__name__)

#: How many pending messages a single client may accumulate before we treat it
#: as too slow to keep up.
SEND_QUEUE_MAXSIZE = 64


class Connection:
    """One WebSocket, with its own outbound queue and sender task."""

    def __init__(
        self,
        websocket: WebSocket,
        event_id: uuid.UUID,
        user_id: uuid.UUID,
        member_id: uuid.UUID,
        role: str,
    ) -> None:
        self.id = uuid.uuid4()
        self.websocket = websocket
        self.event_id = event_id
        self.user_id = user_id
        self.member_id = member_id
        self.role = role
        self.channels: set[str] = set()
        self.cell: Cell | None = None
        self.last_position: tuple[float, float] | None = None
        self.closed = False
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        self._sender: asyncio.Task[None] | None = None
        self._dropped = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._sender = asyncio.create_task(self._send_loop(), name=f"ws-send-{self.id}")

    async def stop(self) -> None:
        self.closed = True
        if self._sender is not None:
            self._sender.cancel()
            try:
                await self._sender
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("connection.sender_stop_error", exc_info=True)
            self._sender = None

    # -- sending ------------------------------------------------------------

    def enqueue(self, raw: str, *, droppable: bool) -> None:
        """Queue a pre-serialised message. Never blocks the caller."""
        if self.closed:
            return
        try:
            self._queue.put_nowait(raw)
        except asyncio.QueueFull:
            if droppable:
                self._dropped += 1
                return
            # The client is too far behind for messages we cannot drop. Close it
            # and let the frontend reconnect and resynchronise.
            logger.warning(
                "connection.too_slow",
                connection_id=str(self.id),
                user_id=str(self.user_id),
                dropped=self._dropped,
            )
            asyncio.create_task(self._force_close())

    async def send_now(self, message: dict[str, Any]) -> None:
        self.enqueue(json.dumps(message, separators=(",", ":")), droppable=False)

    async def _send_loop(self) -> None:
        while True:
            raw = await self._queue.get()
            try:
                await self.websocket.send_text(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Socket is gone; the receive loop will notice and clean up.
                self.closed = True
                return

    async def _force_close(self) -> None:
        self.closed = True
        try:
            await self.websocket.close(code=1013, reason="Client too slow")
        except Exception:
            pass


class ConnectionManager:
    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Connection] = {}
        self._by_user: dict[tuple[uuid.UUID, uuid.UUID], set[Connection]] = defaultdict(set)
        self._by_channel: dict[str, set[Connection]] = defaultdict(set)
        self._bus: RedisEventBus | None = None

    def bind_bus(self, bus: RedisEventBus) -> None:
        self._bus = bus
        bus.set_dispatcher(self.dispatch)

    # -- registration -------------------------------------------------------

    async def register(self, connection: Connection) -> None:
        self._by_id[connection.id] = connection
        self._by_user[(connection.event_id, connection.user_id)].add(connection)
        connection.start()

    async def unregister(self, connection: Connection) -> None:
        self._by_id.pop(connection.id, None)
        user_key = (connection.event_id, connection.user_id)
        peers = self._by_user.get(user_key)
        if peers is not None:
            peers.discard(connection)
            if not peers:
                del self._by_user[user_key]

        channels = list(connection.channels)
        for channel in channels:
            subscribers = self._by_channel.get(channel)
            if subscribers is None:
                continue
            subscribers.discard(connection)
            if not subscribers:
                del self._by_channel[channel]
        connection.channels.clear()
        if self._bus is not None and channels:
            await self._bus.unsubscribe(channels)
        await connection.stop()

    def user_connection_count(self, event_id: uuid.UUID, user_id: uuid.UUID) -> int:
        return len(self._by_user.get((event_id, user_id), ()))

    def connections_for_user(self, event_id: uuid.UUID, user_id: uuid.UUID) -> list[Connection]:
        return list(self._by_user.get((event_id, user_id), ()))

    @property
    def local_connection_count(self) -> int:
        return len(self._by_id)

    # -- channel interest ---------------------------------------------------

    async def subscribe(self, connection: Connection, channels: list[str]) -> None:
        fresh = [channel for channel in channels if channel not in connection.channels]
        if not fresh:
            return
        for channel in fresh:
            connection.channels.add(channel)
            self._by_channel[channel].add(connection)
        if self._bus is not None:
            await self._bus.subscribe(fresh)

    async def unsubscribe(self, connection: Connection, channels: list[str]) -> None:
        stale = [channel for channel in channels if channel in connection.channels]
        if not stale:
            return
        for channel in stale:
            connection.channels.discard(channel)
            subscribers = self._by_channel.get(channel)
            if subscribers is not None:
                subscribers.discard(connection)
                if not subscribers:
                    del self._by_channel[channel]
        if self._bus is not None:
            await self._bus.unsubscribe(stale)

    # -- delivery -----------------------------------------------------------

    async def dispatch(self, channel: str, raw: str) -> None:
        """Bus callback: deliver one published message to local subscribers."""
        subscribers = self._by_channel.get(channel)
        if not subscribers:
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("manager.bad_bus_payload", channel=channel)
            return

        message = parsed.get("message")
        if message is None:
            return
        droppable = bool(parsed.get("droppable"))
        exclude = parsed.get("excludeUserId")
        serialised = json.dumps(message, separators=(",", ":"))

        for connection in list(subscribers):
            if exclude is not None and str(connection.user_id) == exclude:
                continue
            connection.enqueue(serialised, droppable=droppable)

    async def send_local(self, connection: Connection, message: dict[str, Any]) -> None:
        """Deliver straight to one local socket, bypassing the bus.

        Used for synchronous replies (errors, acks) where a round trip through
        Redis would add latency for no benefit.
        """
        await connection.send_now(message)


manager = ConnectionManager()
