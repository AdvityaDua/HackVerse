import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminMemberDep, CurrentEventMemberDep, CurrentUserDep, DBSessionDep
from app.domain.common.enums import EventStatus, MemberStatus, Role
from app.infrastructure.db.models.event import EventModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.map import MapModel
from app.infrastructure.db.models.room import RoomModel
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/events", tags=["events"])
logger = get_logger(__name__)

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class EventCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=2, max_length=120, pattern=SLUG_PATTERN)
    timezone: str = Field(default="Asia/Kolkata", max_length=80)
    starts_at: datetime
    ends_at: datetime

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"'{value}' is not a known IANA time zone") from exc
        return value

    @model_validator(mode="after")
    def coherent_window(self) -> "EventCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class EventUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    logo_url: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    external_links: dict | None = None


class EventResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    timezone: str
    logo_url: str | None
    starts_at: datetime
    ends_at: datetime
    external_links: dict


class MapConfigResponse(BaseModel):
    id: uuid.UUID
    name: str
    map_json_url: str
    tileset_url: str | None = None
    is_active: bool
    spawn_points: list = Field(default_factory=list)


class RoomConfigResponse(BaseModel):
    id: uuid.UUID
    name: str
    key: str
    type: str
    map_zone_id: str | None


class EventConfigResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    timezone: str
    logo_url: str | None
    starts_at: datetime
    ends_at: datetime
    external_links: dict
    active_map: MapConfigResponse | None
    maps: list[MapConfigResponse]
    rooms: list[RoomConfigResponse]
    my_role: str
    features: dict = Field(default_factory=dict)


class PublishIssue(BaseModel):
    code: str
    message: str


async def _get_event(session: AsyncSession, event_id: uuid.UUID) -> EventModel:
    event = await session.scalar(select(EventModel).where(EventModel.id == event_id))
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event


@router.get("", response_model=list[EventResponse])
async def list_my_events(
    current_user: CurrentUserDep,
    session: DBSessionDep,
):
    """Events the caller belongs to. Needed by the organizer dashboard."""
    events = await session.scalars(
        select(EventModel)
        .join(EventMemberModel, EventMemberModel.event_id == EventModel.id)
        .where(
            EventMemberModel.user_id == current_user.id,
            EventMemberModel.status == MemberStatus.ACTIVE.value,
        )
        .order_by(EventModel.starts_at.desc())
    )
    return list(events)


@router.post("", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
async def create_event(
    payload: EventCreate,
    current_user: CurrentUserDep,
    session: DBSessionDep,
):
    """Create a draft event.

    Gated on ``can_create_events`` rather than merely being signed in:
    previously any authenticated participant could mint unlimited events,
    become their admin, and upload unlimited assets.
    """
    if not current_user.can_create_events:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is not allowed to create events.",
        )

    existing = await session.scalar(select(EventModel.id).where(EventModel.slug == payload.slug))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That slug is already in use."
        )

    event = EventModel(
        name=payload.name,
        slug=payload.slug,
        timezone=payload.timezone,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        status=EventStatus.DRAFT.value,
        created_by_user_id=current_user.id,
    )
    session.add(event)
    await session.flush()

    session.add(
        EventMemberModel(
            event_id=event.id,
            user_id=current_user.id,
            role=Role.ADMIN.value,
            status=MemberStatus.ACTIVE.value,
            joined_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await session.refresh(event)
    logger.info("event.created", event_id=str(event.id), slug=event.slug)
    return event


@router.get("/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
):
    return await _get_event(session, event_id)


@router.patch("/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: uuid.UUID,
    payload: EventUpdate,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    event = await _get_event(session, event_id)

    if payload.name is not None:
        event.name = payload.name
    if payload.logo_url is not None:
        event.logo_url = payload.logo_url
    if payload.starts_at is not None:
        event.starts_at = payload.starts_at
    if payload.ends_at is not None:
        event.ends_at = payload.ends_at
    if payload.external_links is not None:
        event.external_links = payload.external_links

    if event.ends_at <= event.starts_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ends_at must be after starts_at",
        )

    await session.commit()
    await session.refresh(event)
    return event


async def _publish_issues(session: AsyncSession, event: EventModel) -> list[PublishIssue]:
    issues: list[PublishIssue] = []

    if event.ends_at <= event.starts_at:
        issues.append(PublishIssue(code="INVALID_WINDOW", message="End time is before start time."))

    active_map = await session.scalar(
        select(MapModel).where(MapModel.event_id == event.id, MapModel.is_active.is_(True))
    )
    if active_map is None:
        issues.append(
            PublishIssue(code="NO_ACTIVE_MAP", message="Select an active map for the campus.")
        )
    elif not active_map.spawn_points:
        issues.append(
            PublishIssue(
                code="NO_SPAWN_POINTS",
                message="The active map has no spawn points, so participants cannot enter.",
            )
        )

    has_room = await session.scalar(
        select(RoomModel.id).where(RoomModel.event_id == event.id).limit(1)
    )
    if has_room is None:
        issues.append(
            PublishIssue(code="NO_ROOMS", message="Create at least one room before publishing.")
        )

    has_admin = await session.scalar(
        select(EventMemberModel.id)
        .where(
            EventMemberModel.event_id == event.id,
            EventMemberModel.role == Role.ADMIN.value,
            EventMemberModel.status == MemberStatus.ACTIVE.value,
        )
        .limit(1)
    )
    if has_admin is None:
        issues.append(
            PublishIssue(code="NO_ADMIN", message="The event needs at least one active organizer.")
        )

    return issues


@router.get("/{event_id}/publish-checklist", response_model=list[PublishIssue])
async def publish_checklist(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    """Pre-flight check so the dashboard can show blockers before publishing."""
    return await _publish_issues(session, await _get_event(session, event_id))


@router.post("/{event_id}/publish", response_model=EventResponse)
async def publish_event(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    force: Annotated[bool, Query(description="Publish despite warnings")] = False,
):
    """Publish a draft event, refusing to publish something unplayable."""
    event = await _get_event(session, event_id)

    if event.status == EventStatus.LIVE.value:
        return event
    if event.status in {EventStatus.ENDED.value, EventStatus.ARCHIVED.value}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {event.status} event cannot be published again.",
        )

    issues = await _publish_issues(session, event)
    if issues and not force:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="; ".join(issue.message for issue in issues),
        )

    event.status = EventStatus.LIVE.value
    await session.commit()
    await session.refresh(event)
    logger.info("event.published", event_id=str(event.id), forced=force)
    return event


@router.post("/{event_id}/end", response_model=EventResponse)
async def end_event(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    event = await _get_event(session, event_id)
    event.status = EventStatus.ENDED.value
    await session.commit()
    await session.refresh(event)
    logger.info("event.ended", event_id=str(event.id))
    return event


@router.get("/{event_id}/config", response_model=EventConfigResponse)
async def get_event_config(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
):
    """Everything the client needs to render the campus, in one call."""
    event = await _get_event(session, event_id)

    maps = [
        MapConfigResponse(
            id=m.id,
            name=m.name,
            map_json_url=m.map_json_url,
            tileset_url=m.tileset_url,
            is_active=m.is_active,
            spawn_points=m.spawn_points or [],
        )
        for m in await session.scalars(select(MapModel).where(MapModel.event_id == event_id))
    ]
    rooms = [
        RoomConfigResponse(id=r.id, name=r.name, key=r.key, type=r.type, map_zone_id=r.map_zone_id)
        for r in await session.scalars(select(RoomModel).where(RoomModel.event_id == event_id))
    ]

    return EventConfigResponse(
        id=event.id,
        name=event.name,
        slug=event.slug,
        status=event.status,
        timezone=event.timezone,
        logo_url=event.logo_url,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        external_links=event.external_links or {},
        active_map=next((m for m in maps if m.is_active), None),
        maps=maps,
        rooms=rooms,
        my_role=member.role,
        features={},
    )


@router.get("/{event_id}/links")
async def get_event_links(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
) -> dict:
    event = await _get_event(session, event_id)
    return {"links": event.external_links or {}}
