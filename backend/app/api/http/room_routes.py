import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AdminMemberDep, CurrentEventMemberDep, DBSessionDep
from app.application.realtime.publisher import envelope, get_publisher
from app.domain.common.enums import MemberStatus, RoomType
from app.infrastructure.db.models.chat_message import ChatMessageModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.room import RoomModel
from app.infrastructure.db.models.room_grant import RoomGrantModel
from app.infrastructure.db.models.user import UserModel
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/events/{event_id}/rooms", tags=["rooms"])
logger = get_logger(__name__)

ROOM_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
DEFAULT_GRANT_MINUTES = 60


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    key: str = Field(min_length=1, max_length=120, pattern=ROOM_KEY_PATTERN)
    #: Enum-typed: an unknown room type used to make the room unenterable.
    type: RoomType
    map_zone_id: str | None = Field(default=None, max_length=160)
    max_capacity: int | None = Field(default=None, ge=1, le=10_000)
    access_policy: dict = Field(default_factory=dict)


class RoomUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    type: RoomType | None = None
    map_zone_id: str | None = Field(default=None, max_length=160)
    max_capacity: int | None = Field(default=None, ge=1, le=10_000)
    access_policy: dict | None = None


class RoomResponse(BaseModel):
    id: uuid.UUID
    name: str
    key: str
    type: str
    map_zone_id: str | None
    max_capacity: int | None
    access_policy: dict


class RoomAuthorizeRequest(BaseModel):
    event_member_id: uuid.UUID
    expires_in_minutes: int = Field(default=DEFAULT_GRANT_MINUTES, ge=1, le=24 * 60)
    reason: str | None = Field(default=None, max_length=200)


class RoomGrantResponse(BaseModel):
    room_id: uuid.UUID
    event_member_id: uuid.UUID
    expires_at: datetime


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    sender_id: uuid.UUID | None
    sender_name: str | None
    content: str
    created_at: datetime


@router.get("", response_model=list[RoomResponse])
async def list_rooms(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
):
    rooms = await session.scalars(
        select(RoomModel).where(RoomModel.event_id == event_id).order_by(RoomModel.name)
    )
    return list(rooms)


@router.post("", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
async def create_room(
    event_id: uuid.UUID,
    payload: RoomCreate,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    room = RoomModel(
        event_id=event_id,
        name=payload.name,
        key=payload.key,
        type=payload.type.value,
        map_zone_id=payload.map_zone_id,
        max_capacity=payload.max_capacity,
        access_policy=payload.access_policy,
    )
    session.add(room)
    await session.commit()
    await session.refresh(room)
    logger.info("room.created", event_id=str(event_id), room_id=str(room.id), type=room.type)
    return room


@router.patch("/{room_id}", response_model=RoomResponse)
async def update_room(
    event_id: uuid.UUID,
    room_id: uuid.UUID,
    payload: RoomUpdate,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    room = await session.scalar(
        select(RoomModel).where(RoomModel.id == room_id, RoomModel.event_id == event_id)
    )
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

    if payload.name is not None:
        room.name = payload.name
    if payload.type is not None:
        room.type = payload.type.value
    if payload.map_zone_id is not None:
        room.map_zone_id = payload.map_zone_id
    if payload.max_capacity is not None:
        room.max_capacity = payload.max_capacity
    if payload.access_policy is not None:
        room.access_policy = payload.access_policy

    await session.commit()
    await session.refresh(room)
    return room


@router.post("/{room_id}/authorize", response_model=RoomGrantResponse)
async def authorize_room_access(
    event_id: uuid.UUID,
    room_id: uuid.UUID,
    payload: RoomAuthorizeRequest,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    """Grant one member time-boxed access to a room.

    This is how a mentor enters a private team room: a revocable, expiring
    grant rather than a permanent membership or a role exemption.
    """
    room = await session.scalar(
        select(RoomModel).where(RoomModel.id == room_id, RoomModel.event_id == event_id)
    )
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

    target = await session.scalar(
        select(EventMemberModel).where(
            EventMemberModel.id == payload.event_member_id,
            EventMemberModel.event_id == event_id,
            EventMemberModel.status == MemberStatus.ACTIVE.value,
        )
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in this event"
        )

    expires_at = datetime.now(UTC) + timedelta(minutes=payload.expires_in_minutes)
    existing = await session.scalar(
        select(RoomGrantModel).where(
            RoomGrantModel.room_id == room_id,
            RoomGrantModel.event_member_id == target.id,
        )
    )
    if existing is not None:
        existing.expires_at = expires_at
        existing.revoked_at = None
        existing.reason = payload.reason
        existing.granted_by_member_id = member.id
    else:
        session.add(
            RoomGrantModel(
                event_id=event_id,
                room_id=room_id,
                event_member_id=target.id,
                granted_by_member_id=member.id,
                reason=payload.reason,
                expires_at=expires_at,
            )
        )
    await session.commit()

    logger.info(
        "room.access_granted",
        event_id=str(event_id),
        room_id=str(room_id),
        member_id=str(target.id),
    )
    await get_publisher().to_user(
        event_id,
        target.user_id,
        envelope(
            "room:access:granted",
            {
                "roomId": str(room_id),
                "roomName": room.name,
                "expiresAt": expires_at.isoformat(),
            },
        ),
    )
    return RoomGrantResponse(room_id=room_id, event_member_id=target.id, expires_at=expires_at)


@router.delete("/{room_id}/authorize/{event_member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_room_access(
    event_id: uuid.UUID,
    room_id: uuid.UUID,
    event_member_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
) -> None:
    grant = await session.scalar(
        select(RoomGrantModel).where(
            RoomGrantModel.room_id == room_id,
            RoomGrantModel.event_member_id == event_member_id,
            RoomGrantModel.event_id == event_id,
        )
    )
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    grant.revoked_at = datetime.now(UTC)
    await session.commit()
    logger.info("room.access_revoked", room_id=str(room_id), member_id=str(event_member_id))


@router.get("/{room_id}/messages", response_model=list[ChatMessageResponse])
async def room_history(
    event_id: uuid.UUID,
    room_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
    limit: int = 50,
):
    """Recent messages for a room the caller is authorised to read.

    The client needs this on room entry; without it, reconnecting users saw an
    empty conversation.
    """
    from app.application.rooms.authorization import RoomAuthorizationService, to_member_entity

    service = RoomAuthorizationService()
    cache = await service.load(session, event_id, member.id)
    decision, _ = service.authorize(cache, to_member_entity(member), room_id)
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.message)

    rows = (
        await session.execute(
            select(ChatMessageModel, UserModel.display_name)
            .outerjoin(UserModel, UserModel.id == ChatMessageModel.sender_id)
            .where(
                ChatMessageModel.event_id == event_id,
                ChatMessageModel.room_id == room_id,
            )
            .order_by(ChatMessageModel.created_at.desc())
            .limit(min(max(limit, 1), 200))
        )
    ).all()

    return [
        ChatMessageResponse(
            id=message.id,
            room_id=message.room_id,
            sender_id=message.sender_id,
            sender_name=display_name,
            content=message.content,
            created_at=message.created_at,
        )
        for message, display_name in reversed(rows)
    ]
