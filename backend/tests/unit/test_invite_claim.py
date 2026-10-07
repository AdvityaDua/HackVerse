from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth.invite_claim import InviteClaimError, claim_strict_invite
from app.infrastructure.auth.invite_tokens import hash_invite_token
from app.infrastructure.db.models.event_invite import EventInviteModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.user import UserModel


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, invite: EventInviteModel, existing_member: EventMemberModel | None = None):
        self.invite = invite
        self.existing_member = existing_member
        self.added = []
        self.flush_count = 0
        self._execute_count = 0

    async def scalar(self, _statement):
        self._execute_count += 1
        if self._execute_count == 1:
            return self.invite
        return self.existing_member

    def add(self, value):
        self.added.append(value)
        if isinstance(value, EventMemberModel):
            value.id = uuid4()
            self.existing_member = value

    async def flush(self):
        self.flush_count += 1


@pytest.mark.asyncio
async def test_claim_strict_invite_creates_active_member_for_matching_email() -> None:
    token = "invite-token"
    user = UserModel(
        id=uuid4(),
        email="student@example.com",
        google_sub="google-sub",
        display_name="Student",
    )
    invite = EventInviteModel(
        id=uuid4(),
        event_id=uuid4(),
        email="Student@Example.com",
        role="participant",
        status="pending",
        token_hash=hash_invite_token(token),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session = FakeSession(invite)

    result = await claim_strict_invite(session, invite_token=token, user=user)

    assert result.event_id == invite.event_id
    assert result.role == "participant"
    assert invite.status == "claimed"
    assert invite.claimed_by_user_id == user.id
    assert session.existing_member is not None
    assert session.existing_member.status == "active"


@pytest.mark.asyncio
async def test_claim_strict_invite_rejects_mismatched_google_email() -> None:
    user = UserModel(
        id=uuid4(),
        email="other@example.com",
        google_sub="google-sub",
        display_name="Other",
    )
    invite = EventInviteModel(
        id=uuid4(),
        event_id=uuid4(),
        email="student@example.com",
        role="participant",
        status="pending",
        token_hash=hash_invite_token("invite-token"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session = FakeSession(invite)

    with pytest.raises(InviteClaimError, match="does not match"):
        await claim_strict_invite(session, invite_token="invite-token", user=user)


def _make_invite(email: str = "student@example.com", **overrides) -> EventInviteModel:
    defaults = dict(
        id=uuid4(),
        event_id=uuid4(),
        email=email,
        role="participant",
        status="pending",
        token_hash=hash_invite_token("invite-token"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    defaults.update(overrides)
    return EventInviteModel(**defaults)


def _make_user(email: str = "student@example.com") -> UserModel:
    return UserModel(
        id=uuid4(),
        email=email,
        google_sub="google-sub",
        display_name="Student",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_status", ["banned", "removed"])
async def test_claim_strict_invite_refuses_to_reactivate_blocked_member(blocked_status) -> None:
    """A banned member must not be able to re-enter by claiming another invite.

    The previous implementation set ``status = "active"`` unconditionally, so
    any pending invite matching a banned user's email silently lifted the ban.
    """
    user = _make_user()
    invite = _make_invite()
    banned_member = EventMemberModel(
        id=uuid4(),
        event_id=invite.event_id,
        user_id=user.id,
        role="participant",
        status=blocked_status,
    )
    session = FakeSession(invite, existing_member=banned_member)

    with pytest.raises(InviteClaimError, match="cannot join"):
        await claim_strict_invite(session, invite_token="invite-token", user=user)

    assert banned_member.status == blocked_status
    assert invite.status == "pending"


@pytest.mark.asyncio
async def test_claim_strict_invite_reactivates_a_merely_invited_member() -> None:
    user = _make_user()
    invite = _make_invite(role="mentor")
    pending_member = EventMemberModel(
        id=uuid4(),
        event_id=invite.event_id,
        user_id=user.id,
        role="participant",
        status="invited",
    )
    session = FakeSession(invite, existing_member=pending_member)

    result = await claim_strict_invite(session, invite_token="invite-token", user=user)

    assert pending_member.status == "active"
    assert result.role == "mentor"


@pytest.mark.asyncio
async def test_claim_strict_invite_rejects_already_claimed_invite() -> None:
    session = FakeSession(_make_invite(status="claimed"))
    with pytest.raises(InviteClaimError, match="not claimable"):
        await claim_strict_invite(session, invite_token="invite-token", user=_make_user())


@pytest.mark.asyncio
async def test_claim_strict_invite_rejects_revoked_invite() -> None:
    session = FakeSession(_make_invite(status="revoked"))
    with pytest.raises(InviteClaimError, match="not claimable"):
        await claim_strict_invite(session, invite_token="invite-token", user=_make_user())


@pytest.mark.asyncio
async def test_claim_strict_invite_rejects_expired_invite() -> None:
    session = FakeSession(_make_invite(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    with pytest.raises(InviteClaimError, match="expired"):
        await claim_strict_invite(session, invite_token="invite-token", user=_make_user())


@pytest.mark.asyncio
async def test_claim_strict_invite_rejects_unknown_token() -> None:
    session = FakeSession(None)
    with pytest.raises(InviteClaimError, match="not found"):
        await claim_strict_invite(session, invite_token="nope", user=_make_user())


def _typecheck_session(session: FakeSession) -> AsyncSession:
    return session  # type: ignore[return-value]
