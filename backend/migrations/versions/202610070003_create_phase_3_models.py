"""create phase 3 schemas

Revision ID: 202610070003
Revises: 202610070002
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202610070003"
down_revision: str | None = "202610070002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. maps
    op.create_table(
        "maps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("map_json_url", sa.Text(), nullable=False),
        sa.Column("tileset_url", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("spawn_points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_maps_event_active", "maps", ["event_id", "is_active"], unique=False)
    op.create_index("ix_maps_event_id", "maps", ["event_id"], unique=False)

    # 2. rooms
    op.create_table(
        "rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("access_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("map_zone_id", sa.String(length=160), nullable=True),
        sa.Column("max_capacity", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "key", name="uq_rooms_event_key"),
    )
    op.create_index("ix_rooms_event_map_zone", "rooms", ["event_id", "map_zone_id"], unique=False)
    op.create_index("ix_rooms_event_type", "rooms", ["event_id", "type"], unique=False)

    # 3. teams
    op.create_table(
        "teams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("project_title", sa.String(length=200), nullable=True),
        sa.Column("project_description", sa.Text(), nullable=True),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "name", name="uq_teams_event_name"),
    )
    op.create_index("ix_teams_event_id", "teams", ["event_id"], unique=False)
    op.create_index("ix_teams_room_id", "teams", ["room_id"], unique=False)

    # 4. team_members
    op.create_table(
        "team_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_member_id"], ["event_members.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "event_member_id", name="uq_team_members_team_member"),
    )
    op.create_index(
        "ix_team_members_event_member_id", "team_members", ["event_member_id"], unique=False
    )
    op.create_index("ix_team_members_team_id", "team_members", ["team_id"], unique=False)


def downgrade() -> None:
    # 4. team_members
    op.drop_index("ix_team_members_team_id", table_name="team_members")
    op.drop_index("ix_team_members_event_member_id", table_name="team_members")
    op.drop_table("team_members")

    # 3. teams
    op.drop_index("ix_teams_room_id", table_name="teams")
    op.drop_index("ix_teams_event_id", table_name="teams")
    op.drop_table("teams")

    # 2. rooms
    op.drop_index("ix_rooms_event_type", table_name="rooms")
    op.drop_index("ix_rooms_event_map_zone", table_name="rooms")
    op.drop_table("rooms")

    # 1. maps
    op.drop_index("ix_maps_event_id", table_name="maps")
    op.drop_index("ix_maps_event_active", table_name="maps")
    op.drop_table("maps")
