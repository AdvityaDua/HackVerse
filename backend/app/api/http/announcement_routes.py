import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminMemberDep, CurrentEventMemberDep, DBSessionDep
from app.application.realtime.publisher import envelope, get_publisher
from app.domain.common.enums import AnnouncementPriority, AnnouncementTarget, Role
from app.infrastructure.db.models.announcement import AnnouncementModel
from app.infrastructure.db.models.room import RoomModel
from app.infrastructure.db.models.team import TeamModel
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/events/{event_id}/announcements", tags=["announcements"])
logger = get_logger(__name__)

ROLE_FOR_TARGET = {
    AnnouncementTarget.PARTICIPANTS: Role.PARTICIPANT,
    AnnouncementTarget.MENTORS: Role.MENTOR,
    AnnouncementTarget.SPONSORS: Role.SPONSOR,
}


class AnnouncementCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    priority: AnnouncementPriority = AnnouncementPriority.NORMAL
    target: AnnouncementTarget = AnnouncementTarget.ALL
    #: Required when target is ``team`` or ``room``.
    target_id: uuid.UUID | None = None
    pinned: bool = False


class AnnouncementResponse(BaseModel):
    id: uuid.UUID
    title: str
    content: str
    priority: str
    target: str
    target_id: uuid.UUID | None
    pinned: bool
    created_by_user_id: uuid.UUID | None
    created_at: datetime


async def _assert_target_exists(
    session: AsyncSession, event_id: uuid.UUID, payload: AnnouncementCreate
) -> None:
    if payload.target is AnnouncementTarget.TEAM:
        exists = await session.scalar(
            select(TeamModel.id).where(
                TeamModel.id == payload.target_id, TeamModel.event_id == event_id
            )
        )
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Team not found in this event."
            )
        return

    exists = await session.scalar(
        select(RoomModel.id).where(
            RoomModel.id == payload.target_id, RoomModel.event_id == event_id
        )
    )
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Room not found in this event."
        )


@router.post("", response_model=AnnouncementResponse, status_code=status.HTTP_201_CREATED)
async def create_announcement(
    event_id: uuid.UUID,
    payload: AnnouncementCreate,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    """Create and deliver an announcement.

    Delivery goes through the Redis-backed publisher rather than the local
    WebSocket registry. The previous version called the in-process connection
    manager directly, so an announcement created on one replica never reached
    clients connected to another — silently failing the PRD's three-second
    delivery requirement as soon as the service scaled past one instance.
    """
    if payload.target in {AnnouncementTarget.TEAM, AnnouncementTarget.ROOM}:
        if payload.target_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"target_id is required when target is '{payload.target.value}'.",
            )
        await _assert_target_exists(session, event_id, payload)

    announcement = AnnouncementModel(
        event_id=event_id,
        title=payload.title,
        content=payload.content,
        priority=payload.priority.value,
        target=payload.target.value,
        target_id=payload.target_id,
        # Critical announcements pin by default: the PRD requires they stay
        # visible rather than scrolling away in a toast.
        pinned=payload.pinned or payload.priority is AnnouncementPriority.CRITICAL,
        created_by_user_id=member.user_id,
    )
    session.add(announcement)
    await session.commit()
    await session.refresh(announcement)

    message = envelope(
        "announcement:new",
        {
            "id": str(announcement.id),
            "title": announcement.title,
            "content": announcement.content,
            "priority": announcement.priority,
            "pinned": announcement.pinned,
            "createdAt": announcement.created_at.isoformat(),
        },
    )
    publisher = get_publisher()

    if payload.target is AnnouncementTarget.ALL:
        await publisher.to_event(event_id, message)
    elif payload.target in ROLE_FOR_TARGET:
        await publisher.to_role(event_id, ROLE_FOR_TARGET[payload.target].value, message)
    elif payload.target is AnnouncementTarget.ROOM:
        await publisher.to_room(event_id, str(payload.target_id), message)
    elif payload.target is AnnouncementTarget.TEAM:
        room_id = await session.scalar(
            select(TeamModel.room_id).where(
                TeamModel.id == payload.target_id, TeamModel.event_id == event_id
            )
        )
        if room_id is not None:
            await publisher.to_room(event_id, str(room_id), message)

    logger.info(
        "announcement.created",
        event_id=str(event_id),
        announcement_id=str(announcement.id),
        priority=announcement.priority,
        target=announcement.target,
    )
    return announcement


@router.get("", response_model=list[AnnouncementResponse])
async def list_announcements(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
    limit: int = 50,
):
    """Announcement history, used for catch-up after a reconnect.

    Role-targeted announcements are filtered to the caller's own role, so a
    participant cannot read mentor-only messages out of the history endpoint.
    """
    capped = min(max(limit, 1), 200)
    query = select(AnnouncementModel).where(AnnouncementModel.event_id == event_id)

    if member.role != Role.ADMIN.value:
        visible = [AnnouncementTarget.ALL.value]
        for target, role in ROLE_FOR_TARGET.items():
            if member.role == role.value:
                visible.append(target.value)
        query = query.where(AnnouncementModel.target.in_(visible))

    announcements = await session.scalars(
        query.order_by(AnnouncementModel.created_at.desc()).limit(capped)
    )
    return list(announcements)
