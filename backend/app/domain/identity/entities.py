from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class User:
    id: UUID
    email: str
    google_sub: str
    display_name: str
    avatar_id: str | None = None
    college: str | None = None
