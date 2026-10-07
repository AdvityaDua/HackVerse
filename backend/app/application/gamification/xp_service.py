import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.interaction import InteractionModel


class XPService:
    """Records interactions and awards XP for the subset that earns it.

    The table written to here is the analytics substrate, not an XP-only
    ledger, so every call records an interaction even when it scores zero
    points. Analytics correctness does not depend on the gamification feature
    staying switched on.
    """

    ACTION_POINTS = {
        "visit_sponsor": 50,
        "easter_egg": 100,
        "chat_message": 5,
        "join_event": 20,
    }

    #: Actions whose XP may only be earned once per target.
    ONCE_PER_TARGET = frozenset({"visit_sponsor", "easter_egg"})

    async def record_interaction(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        user_id: uuid.UUID,
        action_type: str,
        target_id: str | None = None,
        *,
        actor_member_id: uuid.UUID | None = None,
        target_type: str | None = None,
        details: dict | None = None,
    ) -> int:
        points = self.ACTION_POINTS.get(action_type, 0)

        if points > 0 and target_id and action_type in self.ONCE_PER_TARGET:
            already_scored = await session.scalar(
                select(InteractionModel.id)
                .where(
                    InteractionModel.event_id == event_id,
                    InteractionModel.user_id == user_id,
                    InteractionModel.action_type == action_type,
                    InteractionModel.target_id == target_id,
                    InteractionModel.points_awarded > 0,
                )
                .limit(1)
            )
            if already_scored is not None:
                points = 0

        session.add(
            InteractionModel(
                event_id=event_id,
                user_id=user_id,
                actor_member_id=actor_member_id,
                action_type=action_type,
                points_awarded=points,
                target_type=target_type,
                target_id=target_id,
                details=details or {},
            )
        )
        await session.commit()
        return points
