import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base


class RoomModel(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("event_id", "key", name="uq_rooms_event_key"),
        Index("ix_rooms_event_type", "event_id", "type"),
        Index("ix_rooms_event_map_zone", "event_id", "map_zone_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    access_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    map_zone_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    max_capacity: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    event = relationship("EventModel", back_populates="rooms")
    teams = relationship("TeamModel", back_populates="room")
