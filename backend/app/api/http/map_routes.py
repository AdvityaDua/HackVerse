"""Map selection.

Organizers pick from a catalogue of maps we author, rather than uploading their
own. The previous ``POST /maps/upload`` accepted any file under a
client-supplied name and served it from the app's own origin; removing it
retires that vector along with the object-storage dependency it implied.

If bring-your-own-map is ever needed, the seam is
``app/infrastructure/storage/object_store.py``: add an S3/R2 adapter and a
route that writes through it.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api.deps import AdminMemberDep, CurrentEventMemberDep, DBSessionDep, SettingsDep
from app.application.maps import catalog
from app.application.maps.parser import MapParseError
from app.infrastructure.db.models.map import MapModel
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/events/{event_id}/maps", tags=["maps"])
logger = get_logger(__name__)


class CatalogueEntry(BaseModel):
    """A map we ship, available for any event to use."""

    key: str
    name: str
    url: str
    width: int
    height: int
    tile_size: int
    spawn_points: list[str]


class MapResponse(BaseModel):
    id: uuid.UUID
    name: str
    map_json_url: str
    tileset_url: str | None
    is_active: bool
    version: int
    spawn_points: list


class MapRegisterRequest(BaseModel):
    #: Key from ``GET /events/{id}/maps/catalogue``.
    map_key: str = Field(min_length=1, max_length=120)
    #: Defaults to the catalogue entry's own name.
    name: str | None = Field(default=None, min_length=1, max_length=160)
    activate: bool = True


@router.get("/catalogue", response_model=list[CatalogueEntry])
async def list_catalogue(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    settings: SettingsDep,
):
    """Maps available to choose from."""
    return [
        CatalogueEntry(
            key=entry.key,
            name=entry.name,
            url=entry.url,
            width=entry.width,
            height=entry.height,
            tile_size=entry.tile_size,
            spawn_points=entry.spawn_names,
        )
        for entry in catalog.load_all(settings.storage_public_base_url)
    ]


@router.get("", response_model=list[MapResponse])
async def list_maps(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
):
    """Maps registered to this event."""
    maps = await session.scalars(
        select(MapModel).where(MapModel.event_id == event_id).order_by(MapModel.created_at.desc())
    )
    return list(maps)


@router.post("", response_model=MapResponse, status_code=status.HTTP_201_CREATED)
async def register_map(
    event_id: uuid.UUID,
    payload: MapRegisterRequest,
    member: AdminMemberDep,
    session: DBSessionDep,
    settings: SettingsDep,
):
    """Attach a catalogue map to this event.

    The bundled file is parsed here rather than trusted, so a broken export is
    a 400 at setup time instead of a blank campus during the event.
    """
    try:
        entry = catalog.load(payload.map_key, settings.storage_public_base_url)
    except catalog.MapNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MapParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    existing = await session.scalar(
        select(MapModel).where(
            MapModel.event_id == event_id,
            MapModel.map_json_url == entry.url,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{entry.key}' is already registered for this event.",
        )

    if payload.activate:
        # Exactly one active map per event, so the client never has to guess
        # which of several is the campus.
        await session.execute(
            update(MapModel).where(MapModel.event_id == event_id).values(is_active=False)
        )

    new_map = MapModel(
        event_id=event_id,
        name=payload.name or entry.name,
        map_json_url=entry.url,
        tileset_url=(
            f"{settings.storage_public_base_url.rstrip('/')}{catalog.PUBLIC_PREFIX}/"
            f"{entry.tileset_image}"
            if entry.tileset_image
            else None
        ),
        spawn_points=entry.spawn_points,
        is_active=payload.activate,
        created_by_user_id=member.user_id,
    )
    session.add(new_map)
    await session.commit()
    await session.refresh(new_map)

    logger.info(
        "map.registered",
        event_id=str(event_id),
        map_key=entry.key,
        spawn_points=len(entry.spawn_points),
    )
    return new_map


@router.post("/{map_id}/activate", response_model=MapResponse)
async def activate_map(
    event_id: uuid.UUID,
    map_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    target = await session.scalar(
        select(MapModel).where(MapModel.id == map_id, MapModel.event_id == event_id)
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not target.spawn_points:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This map has no spawn points, so participants could not enter.",
        )

    await session.execute(
        update(MapModel).where(MapModel.event_id == event_id).values(is_active=False)
    )
    target.is_active = True
    await session.commit()
    await session.refresh(target)
    logger.info("map.activated", event_id=str(event_id), map_id=str(map_id))
    return target
