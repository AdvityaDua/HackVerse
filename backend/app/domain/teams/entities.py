from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Team:
    id: UUID
    event_id: UUID
    name: str
    project_title: str | None = None
    project_description: str | None = None
    room_id: UUID | None = None


@dataclass(frozen=True)
class TeamMember:
    id: UUID
    team_id: UUID
    event_member_id: UUID
