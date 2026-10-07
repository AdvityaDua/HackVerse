"""Audit remediation: profiles, announcement targeting, grants, moderation, analytics

Addresses the schema gaps found in docs/backend-audit.md:

* users gains the onboarding fields that could otherwise only ever be NULL,
  plus the event-creation gate.
* announcements gains title/priority/target/target_id/pinned, without which
  targeting and pinning were unimplementable.
* interactions is restored to the PRD's interaction_events shape
  (actor_member_id, target_type, details jsonb) instead of being XP-only.
* room_grants backs time-boxed mentor access to private team rooms.
* moderation_actions and user_reports provide the required audit trail.
* CHECK constraints pin the enum vocabularies that were previously free-form
  strings, which is how a room type of "open" diverged from "public".

Revision ID: 202610070006
Revises: 202610070005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202610070006"
down_revision: str | None = "202610070005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- users ---------------------------------------------------------------
    op.add_column("users", sa.Column("headline", sa.String(length=120), nullable=True))
    op.add_column(
        "users",
        sa.Column("onboarded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("can_create_events", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Email comparisons must be case-insensitive: Foo@gmail.com and
    # foo@gmail.com are the same Google account but were two distinct rows.
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))")

    # -- announcements -------------------------------------------------------
    op.add_column(
        "announcements",
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
    )
    op.add_column(
        "announcements",
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
    )
    op.add_column(
        "announcements",
        sa.Column("target", sa.String(length=16), nullable=False, server_default="all"),
    )
    op.add_column(
        "announcements", sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "announcements",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_announcements_event_created", "announcements", ["event_id", "created_at"])
    op.create_index("ix_announcements_event_pinned", "announcements", ["event_id", "pinned"])
    op.create_check_constraint(
        "ck_announcements_priority",
        "announcements",
        "priority IN ('normal','high','critical')",
    )
    op.create_check_constraint(
        "ck_announcements_target",
        "announcements",
        "target IN ('all','participants','mentors','sponsors','team','room')",
    )

    # -- interactions --------------------------------------------------------
    op.add_column(
        "interactions",
        sa.Column("actor_member_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("interactions", sa.Column("target_type", sa.String(length=64), nullable=True))
    op.add_column(
        "interactions",
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_foreign_key(
        "fk_interactions_actor_member",
        "interactions",
        "event_members",
        ["actor_member_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_interactions_event_type_created",
        "interactions",
        ["event_id", "action_type", "created_at"],
    )
    op.create_index("ix_interactions_event_created", "interactions", ["event_id", "created_at"])

    # -- room_grants ---------------------------------------------------------
    op.create_table(
        "room_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_by_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_member_id"], ["event_members.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["granted_by_member_id"], ["event_members.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "event_member_id", name="uq_room_grants_room_member"),
    )
    op.create_index("ix_room_grants_member", "room_grants", ["event_id", "event_member_id"])
    op.create_index("ix_room_grants_expires", "room_grants", ["expires_at"])

    # -- moderation ----------------------------------------------------------
    op.create_table(
        "moderation_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_member_id"], ["event_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_member_id"], ["event_members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_moderation_actions_event_created", "moderation_actions", ["event_id", "created_at"]
    )
    op.create_index(
        "ix_moderation_actions_target", "moderation_actions", ["event_id", "target_member_id"]
    )

    op.create_table(
        "user_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reporter_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("resolved_by_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reporter_member_id"], ["event_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_member_id"], ["event_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["resolved_by_member_id"], ["event_members.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_reports_event_status", "user_reports", ["event_id", "status"])

    # -- enum vocabularies ---------------------------------------------------
    # Normalise any rooms created with the old, unenterable "open" type.
    op.execute("UPDATE rooms SET type = 'public' WHERE type = 'open'")
    op.create_check_constraint(
        "ck_rooms_type", "rooms", "type IN ('public','team','mentor','sponsor','admin')"
    )
    op.create_check_constraint(
        "ck_event_members_role",
        "event_members",
        "role IN ('participant','mentor','sponsor','admin')",
    )
    op.create_check_constraint(
        "ck_event_members_status",
        "event_members",
        "status IN ('invited','active','banned','removed')",
    )
    op.create_check_constraint(
        "ck_events_status", "events", "status IN ('draft','live','ended','archived')"
    )
    op.create_check_constraint(
        "ck_event_invites_status",
        "event_invites",
        "status IN ('pending','claimed','revoked')",
    )
    op.create_check_constraint(
        "ck_event_invites_role",
        "event_invites",
        "role IN ('participant','mentor','sponsor','admin')",
    )
    op.create_index("ix_events_status_starts_at", "events", ["status", "starts_at"])


def downgrade() -> None:
    op.drop_index("ix_events_status_starts_at", table_name="events")
    op.drop_constraint("ck_event_invites_role", "event_invites", type_="check")
    op.drop_constraint("ck_event_invites_status", "event_invites", type_="check")
    op.drop_constraint("ck_events_status", "events", type_="check")
    op.drop_constraint("ck_event_members_status", "event_members", type_="check")
    op.drop_constraint("ck_event_members_role", "event_members", type_="check")
    op.drop_constraint("ck_rooms_type", "rooms", type_="check")

    op.drop_index("ix_user_reports_event_status", table_name="user_reports")
    op.drop_table("user_reports")
    op.drop_index("ix_moderation_actions_target", table_name="moderation_actions")
    op.drop_index("ix_moderation_actions_event_created", table_name="moderation_actions")
    op.drop_table("moderation_actions")

    op.drop_index("ix_room_grants_expires", table_name="room_grants")
    op.drop_index("ix_room_grants_member", table_name="room_grants")
    op.drop_table("room_grants")

    op.drop_index("ix_interactions_event_created", table_name="interactions")
    op.drop_index("ix_interactions_event_type_created", table_name="interactions")
    op.drop_constraint("fk_interactions_actor_member", "interactions", type_="foreignkey")
    op.drop_column("interactions", "details")
    op.drop_column("interactions", "target_type")
    op.drop_column("interactions", "actor_member_id")

    op.drop_constraint("ck_announcements_target", "announcements", type_="check")
    op.drop_constraint("ck_announcements_priority", "announcements", type_="check")
    op.drop_index("ix_announcements_event_pinned", table_name="announcements")
    op.drop_index("ix_announcements_event_created", table_name="announcements")
    op.drop_column("announcements", "pinned")
    op.drop_column("announcements", "target_id")
    op.drop_column("announcements", "target")
    op.drop_column("announcements", "priority")
    op.drop_column("announcements", "title")

    op.execute("DROP INDEX IF EXISTS uq_users_email_lower")
    op.drop_column("users", "can_create_events")
    op.drop_column("users", "onboarded")
    op.drop_column("users", "headline")
