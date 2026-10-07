import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminMemberDep, DBSessionDep
from app.application.realtime.publisher import envelope, get_publisher
from app.domain.common.enums import MemberStatus, ModerationAction, Role
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.moderation import ModerationActionModel, UserReportModel
from app.infrastructure.db.models.user import UserModel
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/events/{event_id}/members", tags=["members"])
reports_router = APIRouter(prefix="/events/{event_id}/reports", tags=["moderation"])
logger = get_logger(__name__)


class MemberResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    role: str
    status: str
    display_name: str
    email: str
    college: str | None
    joined_at: datetime | None


class MemberUpdate(BaseModel):
    """Role and status are enums, so a typo is a 422 rather than a lockout.

    These were free-form strings before: writing ``role="admn"`` silently
    locked a user out of their own event.
    """

    role: Role | None = None
    status: MemberStatus | None = None


class ModerationRequest(BaseModel):
    action: ModerationAction
    reason: str | None = Field(default=None, max_length=500)


class ModerationResponse(BaseModel):
    member_id: uuid.UUID
    action: str
    status: str


class ModerationLogEntry(BaseModel):
    id: uuid.UUID
    actor_member_id: uuid.UUID | None
    target_member_id: uuid.UUID | None
    action: str
    reason: str | None
    created_at: datetime


class ReportResponse(BaseModel):
    id: uuid.UUID
    reporter_member_id: uuid.UUID | None
    target_member_id: uuid.UUID | None
    reason: str
    status: str
    created_at: datetime


async def _load_member(
    session: AsyncSession, event_id: uuid.UUID, member_id: uuid.UUID
) -> EventMemberModel:
    target = await session.scalar(
        select(EventMemberModel).where(
            EventMemberModel.event_id == event_id,
            EventMemberModel.id == member_id,
        )
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return target


async def _active_admin_count(session: AsyncSession, event_id: uuid.UUID) -> int:
    value = await session.scalar(
        select(func.count(EventMemberModel.id)).where(
            EventMemberModel.event_id == event_id,
            EventMemberModel.role == Role.ADMIN.value,
            EventMemberModel.status == MemberStatus.ACTIVE.value,
        )
    )
    return int(value or 0)


def _to_response(member: EventMemberModel, user: UserModel | None) -> MemberResponse:
    return MemberResponse(
        id=member.id,
        user_id=member.user_id,
        role=member.role,
        status=member.status,
        display_name=user.display_name if user else "",
        email=user.email if user else "",
        college=user.college if user else None,
        joined_at=member.joined_at,
    )


@router.get("", response_model=list[MemberResponse])
async def list_members(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    role: Role | None = None,
    member_status: MemberStatus | None = None,
):
    """List members with identity joined in, so the admin UI is usable.

    Emails are visible to organizers only; this route is admin-gated and the
    participant-facing member lists never include them.
    """
    query = (
        select(EventMemberModel, UserModel)
        .join(UserModel, UserModel.id == EventMemberModel.user_id)
        .where(EventMemberModel.event_id == event_id)
    )
    if role is not None:
        query = query.where(EventMemberModel.role == role.value)
    if member_status is not None:
        query = query.where(EventMemberModel.status == member_status.value)

    rows = (await session.execute(query.order_by(UserModel.display_name))).all()
    return [_to_response(event_member, user) for event_member, user in rows]


@router.patch("/{member_id}", response_model=MemberResponse)
async def update_member(
    event_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: MemberUpdate,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    target = await _load_member(session, event_id, member_id)

    losing_admin = target.role == Role.ADMIN.value and (
        (payload.role is not None and payload.role is not Role.ADMIN)
        or (payload.status is not None and payload.status is not MemberStatus.ACTIVE)
    )
    if losing_admin and await _active_admin_count(session, event_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An event must keep at least one active organizer.",
        )

    if payload.role is not None:
        target.role = payload.role.value
    if payload.status is not None:
        target.status = payload.status.value

    await session.commit()
    await session.refresh(target)
    user = await session.scalar(select(UserModel).where(UserModel.id == target.user_id))
    return _to_response(target, user)


@router.post("/{member_id}/moderate", response_model=ModerationResponse)
async def moderate_member(
    event_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: ModerationRequest,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    """Mute, kick or ban a member, with an audit trail and a live push.

    Writing a status to the database is not enough: an already-connected member
    has to be told and disconnected, across every tab they have open.
    """
    target = await _load_member(session, event_id, member_id)

    if target.id == member.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot moderate yourself."
        )
    if (
        target.role == Role.ADMIN.value
        and payload.action in {ModerationAction.KICK, ModerationAction.BAN}
        and await _active_admin_count(session, event_id) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An event must keep at least one active organizer.",
        )

    if payload.action is ModerationAction.BAN:
        target.status = MemberStatus.BANNED.value
    elif payload.action is ModerationAction.UNBAN:
        target.status = MemberStatus.ACTIVE.value

    session.add(
        ModerationActionModel(
            event_id=event_id,
            actor_member_id=member.id,
            target_member_id=target.id,
            action=payload.action.value,
            reason=payload.reason,
        )
    )
    await session.commit()
    await session.refresh(target)

    logger.info(
        "moderation.action",
        event_id=str(event_id),
        action=payload.action.value,
        target_member_id=str(target.id),
        actor_member_id=str(member.id),
    )

    await get_publisher().to_user(
        event_id,
        target.user_id,
        envelope(
            "moderation:action",
            {
                "action": payload.action.value,
                "reason": payload.reason or "",
                "memberId": str(target.id),
            },
        ),
    )

    if payload.action in {ModerationAction.KICK, ModerationAction.BAN}:
        from app.api.ws.connection_manager import manager

        for connection in manager.connections_for_user(event_id, target.user_id):
            try:
                await connection.websocket.close(code=1008, reason=payload.action.value)
            except Exception:
                logger.debug("moderation.socket_close_failed", exc_info=True)

    return ModerationResponse(
        member_id=target.id, action=payload.action.value, status=target.status
    )


@router.get("/{member_id}/moderation-log", response_model=list[ModerationLogEntry])
async def member_moderation_log(
    event_id: uuid.UUID,
    member_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    entries = await session.scalars(
        select(ModerationActionModel)
        .where(
            ModerationActionModel.event_id == event_id,
            ModerationActionModel.target_member_id == member_id,
        )
        .order_by(ModerationActionModel.created_at.desc())
        .limit(100)
    )
    return list(entries)


@reports_router.get("", response_model=list[ReportResponse])
async def list_reports(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    open_only: bool = True,
):
    query = select(UserReportModel).where(UserReportModel.event_id == event_id)
    if open_only:
        query = query.where(UserReportModel.status == "open")
    reports = await session.scalars(query.order_by(UserReportModel.created_at.desc()).limit(200))
    return list(reports)


@reports_router.post("/{report_id}/resolve", response_model=ReportResponse)
async def resolve_report(
    event_id: uuid.UUID,
    report_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    report = await session.scalar(
        select(UserReportModel).where(
            UserReportModel.id == report_id,
            UserReportModel.event_id == event_id,
        )
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    report.status = "resolved"
    report.resolved_by_member_id = member.id
    report.resolved_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(report)
    return report
