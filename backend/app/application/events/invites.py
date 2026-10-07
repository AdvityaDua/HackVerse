"""Invite issuance.

``generate_invite_token`` existed but was never called from anywhere, so the
primary join path could only be exercised by hand-inserting rows. This module
is the missing half: it mints tokens, stores only their hashes, and hands the
raw token back exactly once for the organizer to distribute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.common.enums import InviteStatus, MemberStatus, Role
from app.infrastructure.auth.invite_tokens import (
    generate_invite_token,
    hash_invite_token,
    normalize_email,
)
from app.infrastructure.db.models.event_invite import EventInviteModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.user import UserModel

DEFAULT_INVITE_TTL_DAYS = 30


@dataclass(frozen=True)
class IssuedInvite:
    invite_id: UUID
    email: str
    role: str
    #: Raw token. Returned once at creation; only the hash is persisted.
    token: str
    expires_at: datetime | None
    already_member: bool


@dataclass(frozen=True)
class InviteIssueResult:
    issued: list[IssuedInvite]
    skipped: list[dict[str, str]]


async def issue_invites(
    session: AsyncSession,
    *,
    event_id: UUID,
    emails: list[str],
    role: Role,
    expires_in_days: int | None = DEFAULT_INVITE_TTL_DAYS,
    reissue: bool = False,
) -> InviteIssueResult:
    """Create one pending invite per email.

    Existing pending invites are revoked and replaced when ``reissue`` is set,
    otherwise the email is skipped so that resending to a whole CSV does not
    silently invalidate links people already received.
    """
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None

    issued: list[IssuedInvite] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()

    for raw_email in emails:
        email = normalize_email(raw_email)
        if not email or "@" not in email:
            skipped.append({"email": raw_email, "reason": "invalid_email"})
            continue
        if email in seen:
            skipped.append({"email": email, "reason": "duplicate_in_request"})
            continue
        seen.add(email)

        existing = await session.scalar(
            select(EventInviteModel).where(
                EventInviteModel.event_id == event_id,
                EventInviteModel.email == email,
                EventInviteModel.status == InviteStatus.PENDING.value,
            )
        )
        if existing is not None:
            if not reissue:
                skipped.append({"email": email, "reason": "pending_invite_exists"})
                continue
            existing.status = InviteStatus.REVOKED.value

        already_member = await _is_active_member(session, event_id, email)

        token = generate_invite_token()
        invite = EventInviteModel(
            event_id=event_id,
            email=email,
            role=role.value,
            status=InviteStatus.PENDING.value,
            token_hash=hash_invite_token(token),
            expires_at=expires_at,
        )
        session.add(invite)
        await session.flush()

        issued.append(
            IssuedInvite(
                invite_id=invite.id,
                email=email,
                role=role.value,
                token=token,
                expires_at=expires_at,
                already_member=already_member,
            )
        )

    return InviteIssueResult(issued=issued, skipped=skipped)


async def revoke_invite(session: AsyncSession, event_id: UUID, invite_id: UUID) -> bool:
    invite = await session.scalar(
        select(EventInviteModel).where(
            EventInviteModel.id == invite_id,
            EventInviteModel.event_id == event_id,
        )
    )
    if invite is None or invite.status != InviteStatus.PENDING.value:
        return False
    invite.status = InviteStatus.REVOKED.value
    return True


async def _is_active_member(session: AsyncSession, event_id: UUID, email: str) -> bool:
    user_id = await session.scalar(select(UserModel.id).where(UserModel.email == email))
    if user_id is None:
        return False
    member_status = await session.scalar(
        select(EventMemberModel.status).where(
            EventMemberModel.event_id == event_id,
            EventMemberModel.user_id == user_id,
        )
    )
    return member_status == MemberStatus.ACTIVE.value
