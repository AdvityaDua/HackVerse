from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.auth.google_oauth import GoogleIdentity
from app.infrastructure.auth.invite_tokens import normalize_email
from app.infrastructure.db.models.user import UserModel


async def get_or_create_google_user(
    session: AsyncSession,
    identity: GoogleIdentity,
) -> UserModel:
    normalized_email = normalize_email(identity.email)
    user = await session.scalar(select(UserModel).where(UserModel.google_sub == identity.sub))
    if user is not None:
        if user.email != normalized_email:
            user.email = normalized_email
        if not user.display_name:
            user.display_name = identity.display_name
        await session.flush()
        return user

    user = await session.scalar(select(UserModel).where(UserModel.email == normalized_email))
    if user is not None:
        user.google_sub = identity.sub
        if not user.display_name:
            user.display_name = identity.display_name
        await session.flush()
        return user

    user = UserModel(
        email=normalized_email,
        google_sub=identity.sub,
        display_name=identity.display_name,
    )
    session.add(user)
    await session.flush()
    return user
