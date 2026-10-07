import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("google_sub", name="uq_users_google_sub"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    avatar_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    college: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: Free-text self-description shown on the join card, e.g. "Backend dev".
    headline: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Set true once onboarding (name + avatar) has been completed.
    onboarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Gate for creating events. Without it, any participant who joined any
    #: pilot could mint unlimited events and become their admin.
    can_create_events: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    event_memberships = relationship("EventMemberModel", back_populates="user")
