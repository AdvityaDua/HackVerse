"""create phase 8 schemas

Revision ID: 202610070005
Revises: 202610070004
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202610070005"
down_revision: str | None = "202610070004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("points_awarded", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interactions_event_user", "interactions", ["event_id", "user_id"], unique=False
    )
    op.create_index(
        "ix_interactions_event_user_action_target",
        "interactions",
        ["event_id", "user_id", "action_type", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_event_user_action_target", table_name="interactions")
    op.drop_index("ix_interactions_event_user", table_name="interactions")
    op.drop_table("interactions")
