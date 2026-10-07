"""Room entry authorisation with a per-connection cache.

``room:enter`` previously ran three queries (room, team memberships, teams) on
every message, opening a fresh session each time. Room definitions and team
membership change rarely during an event, so they are cached for the lifetime of
a WebSocket connection and refreshed on a short TTL.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.common.enums import RoomType
from app.domain.events.entities import EventMember
from app.domain.rooms.entities import Room
from app.domain.rooms.policies import CanEnterRoomPolicy, RoomAccessDecision
from app.domain.teams.entities import Team
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.room import RoomModel
from app.infrastructure.db.models.room_grant import RoomGrantModel
from app.infrastructure.db.models.team import TeamModel
from app.infrastructure.db.models.team_member import TeamMemberModel

CACHE_TTL_SECONDS = 30.0


def to_member_entity(model: EventMemberModel) -> EventMember:
    return EventMember(
        id=model.id,
        event_id=model.event_id,
        user_id=model.user_id,
        role=model.role,
        status=model.status,
    )


def to_room_entity(model: RoomModel) -> Room:
    return Room(
        id=model.id,
        event_id=model.event_id,
        name=model.name,
        key=model.key,
        type=model.type,
        access_policy=model.access_policy or {},
        map_zone_id=model.map_zone_id,
        max_capacity=model.max_capacity,
    )


@dataclass
class RoomAuthorizationCache:
    """Per-connection snapshot of the data needed to authorise room entry."""

    rooms: dict[UUID, Room] = field(default_factory=dict)
    teams: list[Team] = field(default_factory=list)
    grants: set[UUID] = field(default_factory=set)
    loaded_at: float = 0.0

    def is_stale(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.loaded_at > CACHE_TTL_SECONDS

    def invalidate(self) -> None:
        self.loaded_at = 0.0


class RoomAuthorizationService:
    def __init__(self, policy: CanEnterRoomPolicy | None = None) -> None:
        self.policy = policy or CanEnterRoomPolicy()

    async def load(
        self,
        session: AsyncSession,
        event_id: UUID,
        member_id: UUID,
    ) -> RoomAuthorizationCache:
        rooms = {
            model.id: to_room_entity(model)
            for model in await session.scalars(
                select(RoomModel).where(RoomModel.event_id == event_id)
            )
        }

        team_rows = (
            await session.execute(
                select(TeamModel)
                .join(TeamMemberModel, TeamMemberModel.team_id == TeamModel.id)
                .where(
                    TeamMemberModel.event_member_id == member_id,
                    TeamModel.event_id == event_id,
                )
            )
        ).scalars()
        teams = [
            Team(id=row.id, event_id=row.event_id, name=row.name, room_id=row.room_id)
            for row in team_rows
        ]

        grants = {
            row.room_id
            for row in await session.scalars(
                select(RoomGrantModel).where(
                    RoomGrantModel.event_member_id == member_id,
                    RoomGrantModel.event_id == event_id,
                )
            )
            if row.is_active()
        }

        return RoomAuthorizationCache(
            rooms=rooms,
            teams=teams,
            grants=grants,
            loaded_at=time.monotonic(),
        )

    def authorize(
        self,
        cache: RoomAuthorizationCache,
        member: EventMember,
        room_id: UUID,
    ) -> tuple[RoomAccessDecision, Room | None]:
        room = cache.rooms.get(room_id)
        if room is None:
            return (
                RoomAccessDecision.deny("ROOM_NOT_FOUND", "Room not found in this event."),
                None,
            )
        decision = self.policy.evaluate(member, room, cache.teams, cache.grants)
        return decision, room

    def default_room_for(self, cache: RoomAuthorizationCache) -> Room | None:
        """The member's own team room, used to restore state after a reconnect."""
        for team in cache.teams:
            if team.room_id and team.room_id in cache.rooms:
                return cache.rooms[team.room_id]
        for room in cache.rooms.values():
            if room.type == RoomType.PUBLIC.value:
                return room
        return None
