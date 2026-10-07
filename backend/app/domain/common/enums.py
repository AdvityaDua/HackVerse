"""Shared vocabulary for every stringly-typed field in the system.

Keep these in sync with the CHECK constraints in the migrations. Routes should
accept these types directly so that invalid values fail as 422s rather than
being written to the database.
"""

from enum import StrEnum


class Role(StrEnum):
    PARTICIPANT = "participant"
    MENTOR = "mentor"
    SPONSOR = "sponsor"
    ADMIN = "admin"


class MemberStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    BANNED = "banned"
    REMOVED = "removed"


class RoomType(StrEnum):
    PUBLIC = "public"
    TEAM = "team"
    MENTOR = "mentor"
    SPONSOR = "sponsor"
    ADMIN = "admin"


class EventStatus(StrEnum):
    DRAFT = "draft"
    LIVE = "live"
    ENDED = "ended"
    ARCHIVED = "archived"


class InviteStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    REVOKED = "revoked"


class AnnouncementPriority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class AnnouncementTarget(StrEnum):
    ALL = "all"
    PARTICIPANTS = "participants"
    MENTORS = "mentors"
    SPONSORS = "sponsors"
    TEAM = "team"
    ROOM = "room"


class PlayerStatus(StrEnum):
    ONLINE = "online"
    AWAY = "away"
    BUSY = "busy"
    DND = "dnd"
    MENTORING = "mentoring"


class ModerationAction(StrEnum):
    MUTE = "mute"
    UNMUTE = "unmute"
    KICK = "kick"
    BAN = "ban"
    UNBAN = "unban"


#: Statuses that must never be silently reactivated by an invite claim.
BLOCKED_MEMBER_STATUSES = frozenset({MemberStatus.BANNED, MemberStatus.REMOVED})

#: Event statuses in which participants may enter the campus.
JOINABLE_EVENT_STATUSES = frozenset({EventStatus.LIVE})
