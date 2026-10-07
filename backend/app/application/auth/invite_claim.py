from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.common.enums import (
    BLOCKED_MEMBER_STATUSES,
    InviteStatus,
    MemberStatus,
)
from app.infrastructure.auth.invite_tokens import hash_invite_token, normalize_email
from app.infrastructure.db.models.event_invite import EventInviteModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.user import UserModel


class InviteClaimError(ValueError):
    pass


@dataclass(frozen=True)
class InviteClaimResult:
    event_id: UUID
    event_member_id: UUID
    role: str


async def claim_strict_invite(
    session: AsyncSession,
    *,
    invite_token: str,
    user: UserModel,
) -> InviteClaimResult:
    token_hash = hash_invite_token(invite_token)
    invite = await session.scalar(
        select(EventInviteModel)
        .where(EventInviteModel.token_hash == token_hash)
        # Serialise concurrent claims of the same token so a double-submit
        # cannot produce two claims.
        .with_for_update()
    )
    if invite is None:
        raise InviteClaimError("Invite not found.")
    if invite.status != InviteStatus.PENDING.value:
        raise InviteClaimError("Invite is not claimable.")
    if invite.expires_at is not None and invite.expires_at < datetime.now(UTC):
        raise InviteClaimError("Invite has expired.")
    if normalize_email(invite.email) != normalize_email(user.email):
        raise InviteClaimError("Invite email does not match the signed-in Google account.")

    existing_member = await session.scalar(
        select(EventMemberModel).where(
            EventMemberModel.event_id == invite.event_id,
            EventMemberModel.user_id == user.id,
        )
    )
    if existing_member is None:
        existing_member = EventMemberModel(
            event_id=invite.event_id,
            user_id=user.id,
            role=invite.role,
            status=MemberStatus.ACTIVE.value,
            invite_token_hash=invite.token_hash,
            joined_at=datetime.now(UTC),
        )
        session.add(existing_member)
        await session.flush()
    else:
        # A banned or removed member must not be silently reactivated by
        # claiming another invite that happens to match their email. Only an
        # organizer can lift a ban.
        if existing_member.status in {status.value for status in BLOCKED_MEMBER_STATUSES}:
            raise InviteClaimError("This account cannot join the event. Contact the organizers.")
        existing_member.role = invite.role
        existing_member.status = MemberStatus.ACTIVE.value
        existing_member.invite_token_hash = invite.token_hash
        existing_member.joined_at = existing_member.joined_at or datetime.now(UTC)

    invite.status = InviteStatus.CLAIMED.value
    invite.claimed_by_user_id = user.id
    invite.claimed_event_member_id = existing_member.id
    invite.claimed_at = datetime.now(UTC)
    await session.flush()

    return InviteClaimResult(
        event_id=invite.event_id,
        event_member_id=existing_member.id,
        role=existing_member.role,
    )
