import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base


class InteractionModel(Base):
    """Append-only analytics substrate.

    This is the PRD's ``interaction_events`` table. It had been narrowed into an
    XP ledger, dropping ``actor_member_id``, ``target_type`` and ``metadata`` —
    which left nowhere to record a clicked sponsor URL, a WebRTC setup outcome
    or a TURN-relay flag, all of which the analytics requirements need. The
    gamification ``points_awarded`` column stays as a derived convenience.
    """

    __tablename__ = "interactions"
    __table_args__ = (
        Index("ix_interactions_event_user", "event_id", "user_id"),
        Index("ix_interactions_event_type_created", "event_id", "action_type", "created_at"),
        Index("ix_interactions_event_created", "event_id", "created_at"),
        Index(
            "ix_interactions_event_user_action_target",
            "event_id",
            "user_id",
            "action_type",
            "target_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    actor_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("event_members.id", ondelete="SET NULL"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    points_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
