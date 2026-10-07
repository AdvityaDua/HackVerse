"""Organizer analytics.

Produces the PRD's validation and technical-health metrics from the
``interactions`` append-only log plus durable tables. Deliberately simple SQL
aggregation rather than any BI tooling.
"""

import csv
import io
import uuid
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import distinct, func, select

from app.api.deps import AdminMemberDep, DBSessionDep, RedisDep
from app.domain.common.enums import MemberStatus, Role
from app.infrastructure.db.models.announcement import AnnouncementModel
from app.infrastructure.db.models.chat_message import ChatMessageModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.interaction import InteractionModel
from app.infrastructure.db.models.moderation import UserReportModel
from app.infrastructure.db.models.team import TeamModel
from app.infrastructure.redis.presence_store import RedisPresenceStore

router = APIRouter(prefix="/events/{event_id}/analytics", tags=["analytics"])


class SummaryResponse(BaseModel):
    registered_members: int
    active_members: int
    unique_visitors: int
    online_now: int
    teams: int
    chat_messages: int
    announcements: int
    room_visits: int
    stage_attendance: int
    sponsor_booth_visits: int
    sponsor_link_clicks: int
    mentor_requests_created: int
    mentor_requests_accepted: int
    mentor_requests_completed: int
    members_by_role: dict[str, int]


class HealthResponse(BaseModel):
    online_now: int
    rtc_setup_success: int
    rtc_setup_failure: int
    rtc_success_rate: float | None
    turn_relay_share: float | None
    open_reports: int
    interactions_last_hour: int


async def _count_action(session: DBSessionDep, event_id: uuid.UUID, action: str) -> int:
    value = await session.scalar(
        select(func.count(InteractionModel.id)).where(
            InteractionModel.event_id == event_id,
            InteractionModel.action_type == action,
        )
    )
    return int(value or 0)


@router.get("/summary", response_model=SummaryResponse)
async def summary(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    redis: RedisDep,
):
    registered = int(
        await session.scalar(
            select(func.count(EventMemberModel.id)).where(EventMemberModel.event_id == event_id)
        )
        or 0
    )
    active = int(
        await session.scalar(
            select(func.count(EventMemberModel.id)).where(
                EventMemberModel.event_id == event_id,
                EventMemberModel.status == MemberStatus.ACTIVE.value,
            )
        )
        or 0
    )
    unique_visitors = int(
        await session.scalar(
            select(func.count(distinct(InteractionModel.user_id))).where(
                InteractionModel.event_id == event_id
            )
        )
        or 0
    )
    role_rows = (
        await session.execute(
            select(EventMemberModel.role, func.count(EventMemberModel.id))
            .where(EventMemberModel.event_id == event_id)
            .group_by(EventMemberModel.role)
        )
    ).all()

    teams = int(
        await session.scalar(select(func.count(TeamModel.id)).where(TeamModel.event_id == event_id))
        or 0
    )
    chat_messages = int(
        await session.scalar(
            select(func.count(ChatMessageModel.id)).where(ChatMessageModel.event_id == event_id)
        )
        or 0
    )
    announcements = int(
        await session.scalar(
            select(func.count(AnnouncementModel.id)).where(AnnouncementModel.event_id == event_id)
        )
        or 0
    )

    return SummaryResponse(
        registered_members=registered,
        active_members=active,
        unique_visitors=unique_visitors,
        online_now=await RedisPresenceStore(redis).count_online(event_id),
        teams=teams,
        chat_messages=chat_messages,
        announcements=announcements,
        room_visits=await _count_action(session, event_id, "room_visit"),
        stage_attendance=await _count_action(session, event_id, "stage_attend"),
        sponsor_booth_visits=await _count_action(session, event_id, "visit_sponsor"),
        sponsor_link_clicks=await _count_action(session, event_id, "sponsor_link_click"),
        mentor_requests_created=await _count_action(session, event_id, "mentor_request_created"),
        mentor_requests_accepted=await _count_action(session, event_id, "mentor_request_accepted"),
        mentor_requests_completed=await _count_action(
            session, event_id, "mentor_request_completed"
        ),
        members_by_role={role: int(count) for role, count in role_rows},
    )


@router.get("/health", response_model=HealthResponse)
async def technical_health(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    redis: RedisDep,
):
    from datetime import UTC, timedelta

    success = await _count_action(session, event_id, "rtc_setup_success")
    failure = await _count_action(session, event_id, "rtc_setup_failure")
    total = success + failure

    relayed = int(
        await session.scalar(
            select(func.count(InteractionModel.id)).where(
                InteractionModel.event_id == event_id,
                InteractionModel.action_type == "rtc_setup_success",
                InteractionModel.details["relay"].as_boolean().is_(True),
            )
        )
        or 0
    )

    open_reports = int(
        await session.scalar(
            select(func.count(UserReportModel.id)).where(
                UserReportModel.event_id == event_id,
                UserReportModel.status == "open",
            )
        )
        or 0
    )
    last_hour = int(
        await session.scalar(
            select(func.count(InteractionModel.id)).where(
                InteractionModel.event_id == event_id,
                InteractionModel.created_at >= datetime.now(UTC) - timedelta(hours=1),
            )
        )
        or 0
    )

    return HealthResponse(
        online_now=await RedisPresenceStore(redis).count_online(event_id),
        rtc_setup_success=success,
        rtc_setup_failure=failure,
        rtc_success_rate=(success / total) if total else None,
        turn_relay_share=(relayed / success) if success else None,
        open_reports=open_reports,
        interactions_last_hour=last_hour,
    )


@router.get("/export")
async def export_interactions(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    """CSV export of the interaction log, aggregated per member and action.

    Aggregated rather than row-level so an export cannot become a
    per-participant behavioural dossier handed to a sponsor.
    """
    rows = (
        await session.execute(
            select(
                InteractionModel.actor_member_id,
                InteractionModel.action_type,
                func.count(InteractionModel.id),
                func.sum(InteractionModel.points_awarded),
                func.min(InteractionModel.created_at),
                func.max(InteractionModel.created_at),
            )
            .where(InteractionModel.event_id == event_id)
            .group_by(InteractionModel.actor_member_id, InteractionModel.action_type)
            .order_by(InteractionModel.action_type)
        )
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["member_id", "action_type", "count", "points", "first_seen", "last_seen"])
    for member_id, action_type, count, points, first_seen, last_seen in rows:
        writer.writerow(
            [
                str(member_id) if member_id else "",
                action_type,
                count,
                points or 0,
                first_seen.isoformat() if first_seen else "",
                last_seen.isoformat() if last_seen else "",
            ]
        )
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="hackverse-{event_id}-analytics.csv"'
        },
    )


@router.get("/presence")
async def live_presence(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    redis: RedisDep,
) -> dict:
    """Per-area counts for the live operations view."""
    records = await RedisPresenceStore(redis).get_event_presence(event_id)
    by_room: dict[str, int] = {}
    for record in records:
        key = record.room_id or "campus"
        by_room[key] = by_room.get(key, 0) + 1
    return {"online": len(records), "byArea": by_room}


@router.get("/roles")
async def role_breakdown(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
) -> dict:
    rows = (
        await session.execute(
            select(EventMemberModel.role, func.count(EventMemberModel.id))
            .where(
                EventMemberModel.event_id == event_id,
                EventMemberModel.status == MemberStatus.ACTIVE.value,
            )
            .group_by(EventMemberModel.role)
        )
    ).all()
    counts = {role.value: 0 for role in Role}
    counts.update({role: int(count) for role, count in rows})
    return counts
