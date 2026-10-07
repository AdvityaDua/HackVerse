"""Pub/sub channel naming.

One place to construct every channel name so the publisher and the subscriber
cannot drift apart.
"""

from __future__ import annotations

from uuid import UUID

from app.domain.realtime.interest import Cell


def event_channel(event_id: UUID) -> str:
    """Everyone in the event: announcements, join/leave, moderation."""
    return f"ev:{event_id}:all"


def user_channel(event_id: UUID, user_id: UUID) -> str:
    """One member's own feed: errors, RTC forwarding, mentor notifications."""
    return f"ev:{event_id}:user:{user_id}"


def room_channel(event_id: UUID, room_id: str) -> str:
    """Members currently inside a room: room chat and room presence."""
    return f"ev:{event_id}:roomch:{room_id}"


def cell_channel(event_id: UUID, cell: Cell) -> str:
    """Players in one spatial cell: movement updates."""
    return f"ev:{event_id}:cell:{cell[0]}:{cell[1]}"


def role_channel(event_id: UUID, role: str) -> str:
    """Everyone in the event holding a given role: targeted announcements."""
    return f"ev:{event_id}:role:{role}"
