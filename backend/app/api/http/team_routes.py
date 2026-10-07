import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminMemberDep, CurrentEventMemberDep, DBSessionDep
from app.application.events.csv_import import ImportTeamsUseCase
from app.domain.common.enums import RoomType
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.room import RoomModel
from app.infrastructure.db.models.team import TeamModel
from app.infrastructure.db.models.team_member import TeamMemberModel
from app.infrastructure.db.models.user import UserModel
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/events/{event_id}/teams", tags=["teams"])
logger = get_logger(__name__)

MAX_CSV_BYTES = 2 * 1024 * 1024


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    project_title: str | None = Field(default=None, max_length=200)
    project_description: str | None = None
    room_id: uuid.UUID | None = None


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    project_title: str | None = Field(default=None, max_length=200)
    project_description: str | None = None
    room_id: uuid.UUID | None = None


class TeamMemberSummary(BaseModel):
    event_member_id: uuid.UUID
    display_name: str
    avatar_id: str | None


class TeamResponse(BaseModel):
    id: uuid.UUID
    name: str
    project_title: str | None
    project_description: str | None
    room_id: uuid.UUID | None


class TeamDetailResponse(TeamResponse):
    members: list[TeamMemberSummary]


async def _assert_room_assignable(
    session: AsyncSession, event_id: uuid.UUID, room_id: uuid.UUID
) -> None:
    """A team may only be attached to a team-type room in the same event."""
    room = await session.scalar(
        select(RoomModel).where(RoomModel.id == room_id, RoomModel.event_id == event_id)
    )
    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Room not found in this event."
        )
    if room.type != RoomType.TEAM.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Teams can only be assigned to team rooms.",
        )


@router.get("", response_model=list[TeamResponse])
async def list_teams(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
):
    teams = await session.scalars(
        select(TeamModel).where(TeamModel.event_id == event_id).order_by(TeamModel.name)
    )
    return list(teams)


@router.get("/me", response_model=TeamDetailResponse | None)
async def get_my_team(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    session: DBSessionDep,
):
    """The caller's team, or ``null``.

    Having no team is a normal state during a hackathon, so this returns null
    rather than a 404 the client has to special-case.
    """
    team = await session.scalar(
        select(TeamModel)
        .join(TeamMemberModel, TeamMemberModel.team_id == TeamModel.id)
        .where(
            TeamMemberModel.event_member_id == member.id,
            TeamModel.event_id == event_id,
        )
    )
    if team is None:
        return None

    rows = (
        await session.execute(
            select(TeamMemberModel.event_member_id, UserModel.display_name, UserModel.avatar_id)
            .join(EventMemberModel, EventMemberModel.id == TeamMemberModel.event_member_id)
            .join(UserModel, UserModel.id == EventMemberModel.user_id)
            .where(TeamMemberModel.team_id == team.id)
        )
    ).all()

    return TeamDetailResponse(
        id=team.id,
        name=team.name,
        project_title=team.project_title,
        project_description=team.project_description,
        room_id=team.room_id,
        members=[
            TeamMemberSummary(
                event_member_id=event_member_id,
                display_name=display_name,
                avatar_id=avatar_id,
            )
            for event_member_id, display_name, avatar_id in rows
        ],
    )


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    event_id: uuid.UUID,
    payload: TeamCreate,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    if payload.room_id is not None:
        await _assert_room_assignable(session, event_id, payload.room_id)

    team = TeamModel(
        event_id=event_id,
        name=payload.name,
        project_title=payload.project_title,
        project_description=payload.project_description,
        room_id=payload.room_id,
    )
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team(
    event_id: uuid.UUID,
    team_id: uuid.UUID,
    payload: TeamUpdate,
    member: AdminMemberDep,
    session: DBSessionDep,
):
    team = await session.scalar(
        select(TeamModel).where(TeamModel.id == team_id, TeamModel.event_id == event_id)
    )
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    if payload.room_id is not None:
        await _assert_room_assignable(session, event_id, payload.room_id)

    if payload.name is not None:
        team.name = payload.name
    if payload.project_title is not None:
        team.project_title = payload.project_title
    if payload.project_description is not None:
        team.project_description = payload.project_description
    if payload.room_id is not None:
        team.room_id = payload.room_id

    await session.commit()
    await session.refresh(team)
    return team


@router.post("/import")
async def import_teams(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    file: Annotated[UploadFile, File()],
    commit: Annotated[bool, Query(description="Write changes; false validates only")] = False,
    create_rooms: Annotated[bool, Query(description="Create a private room per team")] = True,
) -> dict:
    """Import teams from CSV: ``team_name``, ``emails``, optional ``project_title``.

    The previous implementation read the file and returned
    ``{"status": "import_started"}`` without doing anything at all.
    """
    content = await file.read()
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="CSV is too large."
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="CSV must be UTF-8 encoded."
        ) from exc

    use_case = ImportTeamsUseCase(session)
    if not commit:
        preview, rows = await use_case.preview(event_id, text)
        return {"committed": False, "preview": preview.to_payload(), "rowsReady": len(rows)}

    result = await use_case.commit(event_id, text, create_rooms=create_rooms)
    if not result["committed"]:
        await session.rollback()
        return result

    await session.commit()
    logger.info(
        "teams.csv_imported",
        event_id=str(event_id),
        teams=result.get("teamsCreated"),
        members=result.get("membersLinked"),
    )
    return result
