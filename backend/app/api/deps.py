from collections.abc import AsyncIterator, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.common.enums import MemberStatus, Role
from app.infrastructure.auth.sessions import SessionRevocationStore
from app.infrastructure.auth.tokens import TokenError, verify_session_token
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.user import UserModel
from app.infrastructure.db.session import async_session_maker
from app.infrastructure.redis.client import get_redis_client


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session


DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis_client)]


async def get_current_user(
    request: Request,
    session: DBSessionDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> UserModel:
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if session_cookie is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )

    try:
        claims = verify_session_token(session_cookie)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session.",
        ) from exc

    if await SessionRevocationStore(redis).is_revoked(claims.jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been revoked.",
        )

    user = await session.scalar(select(UserModel).where(UserModel.id == claims.user_id))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )
    return user


CurrentUserDep = Annotated[UserModel, Depends(get_current_user)]


async def get_current_event_member(
    event_id: UUID,
    current_user: CurrentUserDep,
    session: DBSessionDep,
) -> EventMemberModel:
    member = await session.scalar(
        select(EventMemberModel).where(
            EventMemberModel.event_id == event_id,
            EventMemberModel.user_id == current_user.id,
            EventMemberModel.status == MemberStatus.ACTIVE.value,
        )
    )
    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an active member of this event.",
        )
    return member


CurrentEventMemberDep = Annotated[EventMemberModel, Depends(get_current_event_member)]


def require_event_role(*roles: Role) -> Callable[..., EventMemberModel]:
    """Build a dependency callable asserting the member holds one of ``roles``.

    Returns the raw callable; call sites wrap it in ``Depends`` themselves.
    """
    allowed = {Role(role).value for role in roles}

    def role_dependency(member: CurrentEventMemberDep) -> EventMemberModel:
        if member.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(sorted(allowed))}",
            )
        return member

    return role_dependency


AdminMemberDep = Annotated[EventMemberModel, Depends(require_event_role(Role.ADMIN))]
