from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class Event:
    id: UUID
    name: str
    slug: str
    timezone: str
    starts_at: datetime
    ends_at: datetime
    status: str


@dataclass(frozen=True)
class EventMember:
    id: UUID
    event_id: UUID
    user_id: UUID
    role: str
    status: str

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
