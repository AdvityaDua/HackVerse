"""Connection manager tests.

Covers the three failures the audit found: single-tab-per-user keying,
head-of-line blocking on broadcast, and channel bookkeeping.
"""

import asyncio
import json
import uuid

import pytest

from app.api.ws.connection_manager import SEND_QUEUE_MAXSIZE, Connection, ConnectionManager

EVENT_ID = uuid.uuid4()


class FakeWebSocket:
    """Records what was sent; can be made to block or fail."""

    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.closed_with: int | None = None
        self._block = asyncio.Event() if block else None
        self._fail = fail

    async def send_text(self, raw: str) -> None:
        if self._fail:
            raise ConnectionResetError("socket is gone")
        if self._block is not None:
            await self._block.wait()
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = code

    def unblock(self) -> None:
        if self._block is not None:
            self._block.set()


def make_connection(user_id: uuid.UUID | None = None, **kwargs) -> Connection:
    return Connection(
        websocket=FakeWebSocket(**kwargs),
        event_id=EVENT_ID,
        user_id=user_id or uuid.uuid4(),
        member_id=uuid.uuid4(),
        role="participant",
    )


class FakeBus:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def set_dispatcher(self, dispatcher):
        self.dispatcher = dispatcher

    async def subscribe(self, channels):
        self.subscribed.extend(channels)

    async def unsubscribe(self, channels):
        self.unsubscribed.extend(channels)


@pytest.fixture
async def manager():
    mgr = ConnectionManager()
    mgr.bind_bus(FakeBus())
    yield mgr


async def drain() -> None:
    """Let sender tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)


class TestMultiTab:
    async def test_a_user_may_hold_several_connections(self, manager):
        """Regression: connections were keyed by user_id.

        A second tab silently replaced the first without closing it, and the
        first tab's eventual disconnect removed the second tab's entry.
        """
        user_id = uuid.uuid4()
        first, second = make_connection(user_id), make_connection(user_id)

        await manager.register(first)
        await manager.register(second)
        assert manager.user_connection_count(EVENT_ID, user_id) == 2

        await manager.unregister(first)
        assert manager.user_connection_count(EVENT_ID, user_id) == 1
        assert manager.connections_for_user(EVENT_ID, user_id) == [second]

        await manager.unregister(second)
        assert manager.user_connection_count(EVENT_ID, user_id) == 0

    async def test_unregistering_an_unknown_connection_is_harmless(self, manager):
        await manager.unregister(make_connection())


class TestDelivery:
    async def test_message_reaches_subscribers_of_the_channel(self, manager):
        connection = make_connection()
        await manager.register(connection)
        await manager.subscribe(connection, ["ev:chan"])

        await manager.dispatch(
            "ev:chan", json.dumps({"message": {"type": "x", "payload": {}}, "droppable": False})
        )
        await drain()

        assert len(connection.websocket.sent) == 1

    async def test_non_subscribers_receive_nothing(self, manager):
        subscriber, other = make_connection(), make_connection()
        await manager.register(subscriber)
        await manager.register(other)
        await manager.subscribe(subscriber, ["ev:chan"])

        await manager.dispatch("ev:chan", json.dumps({"message": {"type": "x", "payload": {}}}))
        await drain()

        assert len(subscriber.websocket.sent) == 1
        assert other.websocket.sent == []

    async def test_sender_can_be_excluded_from_its_own_broadcast(self, manager):
        sender, listener = make_connection(), make_connection()
        for connection in (sender, listener):
            await manager.register(connection)
            await manager.subscribe(connection, ["ev:chan"])

        await manager.dispatch(
            "ev:chan",
            json.dumps(
                {
                    "message": {"type": "player:update", "payload": {}},
                    "excludeUserId": str(sender.user_id),
                }
            ),
        )
        await drain()

        assert sender.websocket.sent == []
        assert len(listener.websocket.sent) == 1

    async def test_malformed_bus_payload_is_ignored(self, manager):
        connection = make_connection()
        await manager.register(connection)
        await manager.subscribe(connection, ["ev:chan"])

        await manager.dispatch("ev:chan", "not json")
        await manager.dispatch("ev:chan", json.dumps({"no_message_key": True}))
        await drain()

        assert connection.websocket.sent == []

    async def test_a_slow_client_does_not_block_others(self, manager):
        """Regression: broadcast awaited each send sequentially.

        One client on bad Wi-Fi stalled delivery for everyone behind it.
        """
        slow = make_connection(block=True)
        fast = make_connection()
        for connection in (slow, fast):
            await manager.register(connection)
            await manager.subscribe(connection, ["ev:chan"])

        await manager.dispatch("ev:chan", json.dumps({"message": {"type": "x", "payload": {}}}))
        await drain()

        assert len(fast.websocket.sent) == 1
        assert slow.websocket.sent == []

        slow.websocket.unblock()
        await drain()
        assert len(slow.websocket.sent) == 1

    async def test_a_dead_socket_does_not_abort_the_broadcast(self, manager):
        """Regression: a raising send killed delivery to the rest of the loop."""
        dead = make_connection(fail=True)
        alive = make_connection()
        for connection in (dead, alive):
            await manager.register(connection)
            await manager.subscribe(connection, ["ev:chan"])

        await manager.dispatch("ev:chan", json.dumps({"message": {"type": "x", "payload": {}}}))
        await drain()

        assert len(alive.websocket.sent) == 1


class TestBackpressure:
    async def test_droppable_frames_are_dropped_when_the_queue_is_full(self, manager):
        connection = make_connection(block=True)
        await manager.register(connection)

        for _ in range(SEND_QUEUE_MAXSIZE + 20):
            connection.enqueue('{"type":"player:update"}', droppable=True)

        # No exception, no unbounded growth, and the socket stays open.
        assert connection.websocket.closed_with is None

    async def test_a_hopelessly_behind_client_is_closed(self, manager):
        connection = make_connection(block=True)
        await manager.register(connection)

        for _ in range(SEND_QUEUE_MAXSIZE + 5):
            connection.enqueue('{"type":"chat:message"}', droppable=False)
        await drain()

        assert connection.closed


class TestChannelBookkeeping:
    async def test_subscribe_is_idempotent(self, manager):
        connection = make_connection()
        await manager.register(connection)

        await manager.subscribe(connection, ["ev:a"])
        await manager.subscribe(connection, ["ev:a"])

        assert manager._bus.subscribed.count("ev:a") == 1

    async def test_unregister_releases_every_channel(self, manager):
        connection = make_connection()
        await manager.register(connection)
        await manager.subscribe(connection, ["ev:a", "ev:b"])

        await manager.unregister(connection)

        assert set(manager._bus.unsubscribed) == {"ev:a", "ev:b"}
        assert connection.channels == set()

    async def test_channel_stays_subscribed_while_another_connection_wants_it(self, manager):
        first, second = make_connection(), make_connection()
        for connection in (first, second):
            await manager.register(connection)
            await manager.subscribe(connection, ["ev:a"])

        await manager.unregister(first)

        await manager.dispatch("ev:a", json.dumps({"message": {"type": "x", "payload": {}}}))
        await drain()
        assert len(second.websocket.sent) == 1
