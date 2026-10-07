import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base


class TeamMemberModel(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "event_member_id", name="uq_team_members_team_member"),
        Index("ix_team_members_team_id", "team_id"),
        Index("ix_team_members_event_member_id", "event_member_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("event_members.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    team = relationship("TeamModel", back_populates="members")
    event_member = relationship("EventMemberModel", back_populates="team_memberships")
