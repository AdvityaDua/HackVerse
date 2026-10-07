"""Gamification leaderboard.

This endpoint appears in neither the PRD nor the implementation plan, so it is
gated behind ``ENABLE_GAMIFICATION`` until that product question is settled —
the campus should not ship a scoreboard by accident. See
``docs/backend-audit.md`` section 10.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from app.api.deps import CurrentEventMemberDep, DBSessionDep, SettingsDep
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.interaction import InteractionModel
from app.infrastructure.db.models.user import UserModel

router = APIRouter(prefix="/events/{event_id}/leaderboard", tags=["gamification"])


class LeaderboardEntry(BaseModel):
    user_id: uuid.UUID
    display_name: str
    total_xp: int
    rank: int


@router.get("", response_model=list[LeaderboardEntry])
async def get_leaderboard(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
    settings: SettingsDep,
):
    if not settings.enable_gamification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Gamification is not enabled for this deployment.",
        )

    # The previous query selected ``UserModel.name``, which does not exist on
    # the model — the column is ``display_name`` — so every call raised.
    stmt = (
        select(
            InteractionModel.user_id,
            UserModel.display_name,
            func.sum(InteractionModel.points_awarded).label("total_xp"),
        )
        .join(UserModel, UserModel.id == InteractionModel.user_id)
        .join(
            EventMemberModel,
            (EventMemberModel.user_id == InteractionModel.user_id)
            & (EventMemberModel.event_id == InteractionModel.event_id),
        )
        .where(
            InteractionModel.event_id == event_id,
            # Exclude removed and banned members from the public scoreboard.
            EventMemberModel.status == "active",
        )
        .group_by(InteractionModel.user_id, UserModel.display_name)
        .having(func.sum(InteractionModel.points_awarded) > 0)
        .order_by(desc("total_xp"))
        .limit(100)
    )

    rows = (await session.execute(stmt)).all()
    return [
        LeaderboardEntry(
            user_id=user_id,
            display_name=display_name,
            total_xp=int(total_xp or 0),
            rank=rank,
        )
        for rank, (user_id, display_name, total_xp) in enumerate(rows, start=1)
    ]
