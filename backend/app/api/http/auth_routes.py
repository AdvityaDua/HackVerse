import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUserDep, DBSessionDep, RedisDep, SettingsDep
from app.application.auth.google_user import get_or_create_google_user
from app.application.auth.invite_claim import InviteClaimError, claim_strict_invite
from app.domain.common.enums import InviteStatus, MemberStatus
from app.infrastructure.auth.google_oauth import (
    GoogleOAuthError,
    build_google_authorization_url,
    exchange_code_for_id_token,
    verify_google_id_token,
)
from app.infrastructure.auth.invite_tokens import hash_invite_token
from app.infrastructure.auth.sessions import SessionRevocationStore
from app.infrastructure.auth.tokens import (
    TokenError,
    create_oauth_state,
    create_session_token,
    verify_oauth_state,
    verify_session_token,
)
from app.infrastructure.config import Settings
from app.infrastructure.db.models.event_invite import EventInviteModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.user import UserModel
from app.infrastructure.observability.logging import get_logger
from app.infrastructure.redis.rate_limit import RateLimiter, auth_key

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)

#: Attempts per minute per client IP for the OAuth callback and invite claim.
AUTH_RATE_LIMIT = 20


class ClaimInviteRequest(BaseModel):
    invite_token: str = Field(min_length=10, max_length=128)


class ClaimInviteResponse(BaseModel):
    event_id: str
    event_member_id: str
    role: str


class EventMembershipSummary(BaseModel):
    event_id: str
    role: str
    status: str


class CurrentUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    avatar_id: str | None
    college: str | None
    headline: str | None
    onboarded: bool
    can_create_events: bool
    memberships: list[EventMembershipSummary]


class ProfileUpdateRequest(BaseModel):
    """Onboarding payload. Only the display name is required by the PRD."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    avatar_id: str | None = Field(default=None, max_length=80)
    college: str | None = Field(default=None, max_length=160)
    headline: str | None = Field(default=None, max_length=120)


class InvitePreviewResponse(BaseModel):
    event_id: str
    event_name: str
    event_logo_url: str | None
    role: str
    email: str
    requires_login: bool


async def _enforce_auth_rate_limit(request: Request, redis, scope: str) -> None:
    client_ip = request.client.host if request.client else "unknown"
    limiter = RateLimiter(redis)
    allowed = await limiter.allow(
        auth_key(scope, client_ip, int(time.time() // 60)), AUTH_RATE_LIMIT, 60
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a minute and try again.",
        )


async def _optional_current_user(
    request: Request,
    session: DBSessionDep,
    settings: SettingsDep,
) -> UserModel | None:
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if session_cookie is None:
        return None
    try:
        claims = verify_session_token(session_cookie)
    except TokenError:
        return None
    return await session.scalar(select(UserModel).where(UserModel.id == claims.user_id))


OptionalCurrentUserDep = Annotated[UserModel | None, Depends(_optional_current_user)]


@router.get("/google/start")
async def start_google_auth(
    invite_token: Annotated[str | None, Query(max_length=128)] = None,
) -> RedirectResponse:
    state = create_oauth_state(invite_token=invite_token)
    return RedirectResponse(
        build_google_authorization_url(state),
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/google/callback")
async def complete_google_auth(
    request: Request,
    code: Annotated[str, Query(max_length=2048)],
    state: Annotated[str, Query(max_length=2048)],
    session: DBSessionDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> RedirectResponse:
    await _enforce_auth_rate_limit(request, redis, "google_callback")

    try:
        invite_token = verify_oauth_state(state)
        google_id_token = await exchange_code_for_id_token(code)
        # google-auth's verifier is synchronous and fetches Google's signing
        # certificates over the network. Called inline it would freeze the
        # event loop — including every live WebSocket on this worker — right
        # at event kickoff when logins spike.
        identity = await run_in_threadpool(verify_google_id_token, google_id_token)
    except (TokenError, GoogleOAuthError) as exc:
        logger.warning("auth.google_callback_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google sign-in could not be completed.",
        ) from exc

    claim_error: str | None = None
    async with session.begin():
        user = await get_or_create_google_user(session, identity)
        event_id = None
        if invite_token:
            try:
                claim = await claim_strict_invite(session, invite_token=invite_token, user=user)
                event_id = str(claim.event_id)
            except InviteClaimError as exc:
                # The Google identity is valid; only the invite failed. Sign the
                # user in and let the frontend explain the invite problem.
                claim_error = str(exc)

    session_token = create_session_token(user.id, user.email)
    redirect_url = f"{settings.frontend_base_url}/auth/callback"
    params = []
    if event_id:
        params.append(f"eventId={event_id}")
    if claim_error:
        from urllib.parse import quote

        params.append(f"inviteError={quote(claim_error)}")
    if params:
        redirect_url = f"{redirect_url}?{'&'.join(params)}"

    logger.info("auth.signed_in", user_id=str(user.id), claimed_event=event_id)
    response = RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, session_token, settings)
    return response


@router.get("/invite/{invite_token}", response_model=InvitePreviewResponse)
async def resolve_invite(
    invite_token: str,
    session: DBSessionDep,
    current_user: OptionalCurrentUserDep,
) -> InvitePreviewResponse:
    """Describe an invite so the join page can render before authenticating.

    Returns data rather than redirecting, so the frontend controls the flow and
    can show a specific message for expired or claimed invites.
    """
    token_hash = hash_invite_token(invite_token)
    invite = await session.scalar(
        select(EventInviteModel).where(EventInviteModel.token_hash == token_hash)
    )
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
    if invite.status != InviteStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This invite has already been used or was revoked.",
        )
    from datetime import UTC, datetime

    if invite.expires_at is not None and invite.expires_at < datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This invite has expired.")

    await session.refresh(invite, ["event"])
    return InvitePreviewResponse(
        event_id=str(invite.event_id),
        event_name=invite.event.name,
        event_logo_url=invite.event.logo_url,
        role=invite.role,
        email=invite.email,
        requires_login=current_user is None,
    )


@router.post("/invite/claim", response_model=ClaimInviteResponse)
async def claim_invite(
    request: Request,
    payload: ClaimInviteRequest,
    current_user: CurrentUserDep,
    session: DBSessionDep,
    redis: RedisDep,
) -> ClaimInviteResponse:
    await _enforce_auth_rate_limit(request, redis, "invite_claim")
    try:
        result = await claim_strict_invite(
            session,
            invite_token=payload.invite_token,
            user=current_user,
        )
        await session.commit()
    except InviteClaimError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    logger.info(
        "auth.invite_claimed",
        user_id=str(current_user.id),
        event_id=str(result.event_id),
        role=result.role,
    )
    return ClaimInviteResponse(
        event_id=str(result.event_id),
        event_member_id=str(result.event_member_id),
        role=result.role,
    )


@router.get("/me", response_model=CurrentUserResponse)
async def me(current_user: CurrentUserDep, session: DBSessionDep) -> CurrentUserResponse:
    memberships = await session.scalars(
        select(EventMemberModel).where(
            EventMemberModel.user_id == current_user.id,
            EventMemberModel.status == MemberStatus.ACTIVE.value,
        )
    )
    return CurrentUserResponse(
        id=str(current_user.id),
        email=current_user.email,
        display_name=current_user.display_name,
        avatar_id=current_user.avatar_id,
        college=current_user.college,
        headline=current_user.headline,
        onboarded=current_user.onboarded,
        can_create_events=current_user.can_create_events,
        memberships=[
            EventMembershipSummary(
                event_id=str(m.event_id),
                role=m.role,
                status=m.status,
            )
            for m in memberships
        ],
    )


@router.patch("/me", response_model=CurrentUserResponse)
async def update_profile(
    payload: ProfileUpdateRequest,
    current_user: CurrentUserDep,
    session: DBSessionDep,
) -> CurrentUserResponse:
    """Complete onboarding: display name, avatar, college, headline.

    Without this the ``avatar_id`` and ``college`` columns could only ever be
    NULL, making the PRD's profile/avatar step impossible.
    """
    if payload.display_name is not None:
        current_user.display_name = payload.display_name.strip()
    if payload.avatar_id is not None:
        current_user.avatar_id = payload.avatar_id
    if payload.college is not None:
        current_user.college = payload.college.strip() or None
    if payload.headline is not None:
        current_user.headline = payload.headline.strip() or None

    if current_user.display_name and current_user.avatar_id:
        current_user.onboarded = True

    await session.commit()
    await session.refresh(current_user)
    return await me(current_user, session)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    settings: SettingsDep,
    redis: RedisDep,
) -> dict[str, str]:
    """Clear the cookie *and* revoke the token.

    Deleting the cookie alone left a valid credential in play for the rest of
    its lifetime — a real problem on shared college lab machines.
    """
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if session_cookie:
        try:
            claims = verify_session_token(session_cookie)
            await SessionRevocationStore(redis).revoke(claims.jti, claims.remaining_seconds)
            logger.info("auth.logged_out", user_id=str(claims.user_id))
        except TokenError:
            pass

    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return {"status": "ok"}


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
