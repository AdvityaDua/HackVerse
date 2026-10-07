"""The only sanctioned way to push a message to connected clients.

REST handlers call this, never the WebSocket connection manager directly. That
keeps ``api/http`` from depending on ``api/ws`` transport state, and it means
every broadcast goes through Redis and therefore reaches all replicas.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.application.realtime.channels import (
    cell_channel,
    event_channel,
    role_channel,
    room_channel,
    user_channel,
)
from app.domain.realtime.interest import Cell
from app.infrastructure.redis.pubsub import RedisEventBus

#: Message types safe to drop when a client's send queue is backed up. Position
#: frames are superseded by the next one; chat and announcements are not.
DROPPABLE_TYPES = frozenset({"player:update", "presence:count", "nearby:update"})


def envelope(type_: str, payload: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
    return {"type": type_, "requestId": request_id, "payload": payload}


class RealtimePublisher:
    def __init__(self, bus: RedisEventBus) -> None:
        self._bus = bus

    async def _emit(
        self,
        channel: str,
        message: dict[str, Any],
        *,
        exclude_user_id: UUID | None = None,
    ) -> None:
        await self._bus.publish(
            channel,
            {
                "message": message,
                "droppable": message.get("type") in DROPPABLE_TYPES,
                "excludeUserId": str(exclude_user_id) if exclude_user_id else None,
            },
        )

    async def to_event(
        self,
        event_id: UUID,
        message: dict[str, Any],
        *,
        exclude_user_id: UUID | None = None,
    ) -> None:
        await self._emit(event_channel(event_id), message, exclude_user_id=exclude_user_id)

    async def to_user(self, event_id: UUID, user_id: UUID, message: dict[str, Any]) -> None:
        await self._emit(user_channel(event_id, user_id), message)

    async def to_room(
        self,
        event_id: UUID,
        room_id: str,
        message: dict[str, Any],
        *,
        exclude_user_id: UUID | None = None,
    ) -> None:
        await self._emit(room_channel(event_id, room_id), message, exclude_user_id=exclude_user_id)

    async def to_role(self, event_id: UUID, role: str, message: dict[str, Any]) -> None:
        await self._emit(role_channel(event_id, role), message)

    async def to_cell(
        self,
        event_id: UUID,
        cell: Cell,
        message: dict[str, Any],
        *,
        exclude_user_id: UUID | None = None,
    ) -> None:
        await self._emit(cell_channel(event_id, cell), message, exclude_user_id=exclude_user_id)


def get_publisher() -> RealtimePublisher:
    """FastAPI dependency: the process-wide publisher."""
    from app.infrastructure.redis.pubsub import get_event_bus

    return RealtimePublisher(get_event_bus())
