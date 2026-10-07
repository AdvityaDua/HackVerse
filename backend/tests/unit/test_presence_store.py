"""Presence store tests.

The previous implementation put the TTL on the event-wide hash, refreshed by
any player's movement. These tests pin the two behaviours that broke: ghosts
that never expired, and a quiet event wiping everyone at once.
"""

import uuid

import pytest

from app.domain.common.enums import PlayerStatus
from app.infrastructure.redis.presence_store import RedisPresenceStore

EVENT_ID = uuid.uuid4()


@pytest.fixture
def store(fake_redis) -> RedisPresenceStore:
    return RedisPresenceStore(fake_redis, ttl_seconds=60)


async def test_register_then_read_round_trips(store):
    user_id = uuid.uuid4()
    await store.register_player(EVENT_ID, user_id, x=3.5, y=4.5, status=PlayerStatus.ONLINE.value)

    record = await store.get_player(EVENT_ID, user_id)
    assert record is not None
    assert (record.x, record.y) == (3.5, 4.5)
    assert record.status == PlayerStatus.ONLINE.value
    assert record.room_id is None


async def test_each_player_has_an_independent_ttl(store, fake_redis):
    """Regression: the TTL used to be on the shared hash.

    A player who dropped without a clean close stayed in presence forever as
    long as anyone else kept moving.
    """
    stale = uuid.uuid4()
    active = uuid.uuid4()
    await store.register_player(EVENT_ID, stale, x=0, y=0, status="online")
    await store.register_player(EVENT_ID, active, x=1, y=1, status="online")

    ttl_stale = await fake_redis.ttl(f"ev:{EVENT_ID}:player:{stale}")
    assert 0 < ttl_stale <= 60

    # Another player moving must not extend the stale player's record.
    await store.update_position(EVENT_ID, active, 2, 2)
    assert await fake_redis.ttl(f"ev:{EVENT_ID}:player:{stale}") <= ttl_stale


async def test_expired_record_is_swept_from_the_index(store, fake_redis):
    user_id = uuid.uuid4()
    await store.register_player(EVENT_ID, user_id, x=0, y=0, status="online")

    # Simulate the per-player key expiring while the index entry lingers.
    await fake_redis.delete(f"ev:{EVENT_ID}:player:{user_id}")

    assert await store.get_event_presence(EVENT_ID) == []
    assert await store.count_online(EVENT_ID) == 0


async def test_movement_does_not_reset_status(store):
    """Regression: update_position defaulted status back to "online".

    A mentor who marked themselves busy had it silently cleared by walking.
    """
    user_id = uuid.uuid4()
    await store.register_player(EVENT_ID, user_id, x=0, y=0, status="online")
    await store.update_status(EVENT_ID, user_id, PlayerStatus.BUSY.value)

    await store.update_position(EVENT_ID, user_id, 5, 5)

    record = await store.get_player(EVENT_ID, user_id)
    assert record.status == PlayerStatus.BUSY.value
    assert (record.x, record.y) == (5.0, 5.0)


async def test_movement_does_not_clear_room(store):
    user_id = uuid.uuid4()
    room_id = str(uuid.uuid4())
    await store.register_player(EVENT_ID, user_id, x=0, y=0, status="online")
    await store.set_room(EVENT_ID, user_id, room_id)

    await store.update_position(EVENT_ID, user_id, 1, 1)

    record = await store.get_player(EVENT_ID, user_id)
    assert record.room_id == room_id


async def test_room_membership_tracks_enter_and_leave(store):
    user_id = uuid.uuid4()
    room_a, room_b = str(uuid.uuid4()), str(uuid.uuid4())
    await store.register_player(EVENT_ID, user_id, x=0, y=0, status="online")

    await store.set_room(EVENT_ID, user_id, room_a)
    assert await store.get_room_members(EVENT_ID, room_a) == [str(user_id)]

    # Moving rooms must remove the player from the old one.
    await store.set_room(EVENT_ID, user_id, room_b)
    assert await store.get_room_members(EVENT_ID, room_a) == []
    assert await store.get_room_members(EVENT_ID, room_b) == [str(user_id)]

    await store.set_room(EVENT_ID, user_id, None)
    assert await store.get_room_members(EVENT_ID, room_b) == []


async def test_remove_player_clears_index_and_room(store):
    user_id = uuid.uuid4()
    room_id = str(uuid.uuid4())
    await store.register_player(EVENT_ID, user_id, x=0, y=0, status="online")
    await store.set_room(EVENT_ID, user_id, room_id)

    await store.remove_player(EVENT_ID, user_id)

    assert await store.get_player(EVENT_ID, user_id) is None
    assert await store.count_online(EVENT_ID) == 0
    assert await store.get_room_members(EVENT_ID, room_id) == []


async def test_presence_is_isolated_per_event(store):
    user_id = uuid.uuid4()
    other_event = uuid.uuid4()
    await store.register_player(EVENT_ID, user_id, x=0, y=0, status="online")

    assert await store.count_online(other_event) == 0
    assert await store.get_player(other_event, user_id) is None
