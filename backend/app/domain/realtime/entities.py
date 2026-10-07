from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class PlayerPosition:
    user_id: UUID
    x: float
    y: float
    status: str


@dataclass(frozen=True)
class PresenceSnapshot:
    event_id: UUID
    players: list[PlayerPosition]
