"""Room access rules.

This previously granted access on ``room.type == "open"``, a value that exists
nowhere else in the system. Rooms created with the documented type ``public``
fell through to a blanket deny, which made Main Stage, Mentor Lounge, Sponsor
Booths and the Chill Zone admin-only. Types now come from the shared enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.common.enums import Role, RoomType
from app.domain.events.entities import EventMember
from app.domain.rooms.entities import Room
from app.domain.teams.entities import Team


@dataclass(frozen=True)
class RoomAccessDecision:
    allowed: bool
    code: str = "OK"
    message: str = ""

    @classmethod
    def allow(cls) -> RoomAccessDecision:
        return cls(allowed=True)

    @classmethod
    def deny(cls, code: str, message: str) -> RoomAccessDecision:
        return cls(allowed=False, code=code, message=message)


class CanEnterRoomPolicy:
    """Decide whether ``member`` may enter ``room``.

    ``grants`` carries temporary authorisations, e.g. a mentor whose request was
    accepted by a team and who may therefore enter that team's room.
    """

    def evaluate(
        self,
        member: EventMember,
        room: Room,
        user_teams: list[Team] | None = None,
        grants: set[UUID] | None = None,
    ) -> RoomAccessDecision:
        if member.event_id != room.event_id:
            # Event isolation: never acknowledge rooms from another event.
            return RoomAccessDecision.deny("ROOM_NOT_FOUND", "Room not found in this event.")

        if not member.is_active:
            return RoomAccessDecision.deny(
                "MEMBERSHIP_INACTIVE", "Your event membership is not active."
            )

        if member.is_admin:
            return RoomAccessDecision.allow()

        if grants and room.id in grants:
            return RoomAccessDecision.allow()

        try:
            room_type = RoomType(room.type)
        except ValueError:
            return RoomAccessDecision.deny(
                "ROOM_MISCONFIGURED", "This room is not configured correctly."
            )

        if room_type is RoomType.PUBLIC:
            return RoomAccessDecision.allow()

        if room_type is RoomType.TEAM:
            return self._team_access(room, user_teams or [])

        if room_type is RoomType.MENTOR:
            # The mentor lounge is a meeting point: mentors staff it and
            # participants visit it to ask for help.
            if member.role in {Role.MENTOR.value, Role.PARTICIPANT.value}:
                return RoomAccessDecision.allow()
            return RoomAccessDecision.deny(
                "ROOM_ACCESS_DENIED",
                "Only mentors and participants may enter the mentor lounge.",
            )

        if room_type is RoomType.SPONSOR:
            # Booth floors are public. A sponsor-only back area is modelled by
            # setting "sponsor_only": true on the room's access policy.
            if room.access_policy.get("sponsor_only") is True:
                allowed_ids = {
                    str(value) for value in room.access_policy.get("sponsor_member_ids", [])
                }
                if member.role == Role.SPONSOR.value and (
                    not allowed_ids or str(member.id) in allowed_ids
                ):
                    return RoomAccessDecision.allow()
                return RoomAccessDecision.deny(
                    "ROOM_ACCESS_DENIED", "This area is reserved for the sponsor team."
                )
            return RoomAccessDecision.allow()

        if room_type is RoomType.ADMIN:
            return RoomAccessDecision.deny(
                "ROOM_ACCESS_DENIED", "This area is reserved for organizers."
            )

        return RoomAccessDecision.deny("ROOM_ACCESS_DENIED", "You cannot enter this room.")

    def _team_access(self, room: Room, user_teams: list[Team]) -> RoomAccessDecision:
        allowed_team_ids = {str(value) for value in room.access_policy.get("allowed_team_ids", [])}
        for team in user_teams:
            if str(team.id) in allowed_team_ids or room.id == team.room_id:
                return RoomAccessDecision.allow()
        return RoomAccessDecision.deny(
            "ROOM_ACCESS_DENIED", "This team room is private to its members."
        )

    def check(
        self,
        member: EventMember,
        room: Room,
        user_teams: list[Team] | None = None,
        grants: set[UUID] | None = None,
    ) -> bool:
        """Boolean convenience wrapper."""
        return self.evaluate(member, room, user_teams, grants).allowed
