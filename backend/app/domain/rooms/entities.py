from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class Room:
    id: UUID
    event_id: UUID
    name: str
    key: str
    type: str
    access_policy: dict = field(default_factory=dict)
    map_zone_id: str | None = None
    max_capacity: int | None = None
