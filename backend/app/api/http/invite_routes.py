"""Invite issuance and member CSV import.

Neither existed before: ``generate_invite_token`` was dead code and the team
import route discarded its own result, so onboarding a few hundred participants
meant hand-writing rows into ``event_invites``.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AdminMemberDep, DBSessionDep, SettingsDep
from app.application.events.csv_import import ImportMembersUseCase
from app.application.events.invites import issue_invites, revoke_invite
from app.domain.common.enums import InviteStatus, Role
from app.infrastructure.db.models.event_invite import EventInviteModel
from app.infrastructure.observability.logging import get_logger

router = APIRouter(prefix="/events/{event_id}/invites", tags=["invites"])
logger = get_logger(__name__)

MAX_EMAILS_PER_REQUEST = 500
MAX_CSV_BYTES = 2 * 1024 * 1024


class InviteCreateRequest(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=MAX_EMAILS_PER_REQUEST)
    role: Role = Role.PARTICIPANT
    expires_in_days: int | None = Field(default=30, ge=1, le=365)
    reissue: bool = False


class IssuedInviteResponse(BaseModel):
    invite_id: uuid.UUID
    email: str
    role: str
    #: Full join URL. Shown once — only the hash is stored server-side.
    join_url: str
    expires_at: datetime | None
    already_member: bool


class InviteCreateResponse(BaseModel):
    issued: list[IssuedInviteResponse]
    skipped: list[dict]


class InviteListItem(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    expires_at: datetime | None
    created_at: datetime
    claimed_at: datetime | None


@router.post("", response_model=InviteCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_invites(
    event_id: uuid.UUID,
    payload: InviteCreateRequest,
    member: AdminMemberDep,
    session: DBSessionDep,
    settings: SettingsDep,
):
    result = await issue_invites(
        session,
        event_id=event_id,
        emails=payload.emails,
        role=payload.role,
        expires_in_days=payload.expires_in_days,
        reissue=payload.reissue,
    )
    await session.commit()

    logger.info(
        "invites.issued",
        event_id=str(event_id),
        count=len(result.issued),
        role=payload.role.value,
    )
    base = settings.frontend_base_url.rstrip("/")
    return InviteCreateResponse(
        issued=[
            IssuedInviteResponse(
                invite_id=invite.invite_id,
                email=invite.email,
                role=invite.role,
                join_url=f"{base}/invite/{invite.token}",
                expires_at=invite.expires_at,
                already_member=invite.already_member,
            )
            for invite in result.issued
        ],
        skipped=result.skipped,
    )


@router.get("", response_model=list[InviteListItem])
async def list_invites(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    invite_status: InviteStatus | None = None,
):
    """List invites. Raw tokens are never returned — only their hashes exist."""
    query = select(EventInviteModel).where(EventInviteModel.event_id == event_id)
    if invite_status is not None:
        query = query.where(EventInviteModel.status == invite_status.value)
    invites = await session.scalars(query.order_by(EventInviteModel.created_at.desc()).limit(1000))
    return list(invites)


@router.post("/{invite_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(
    event_id: uuid.UUID,
    invite_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
) -> None:
    if not await revoke_invite(session, event_id, invite_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No pending invite with that id."
        )
    await session.commit()
    logger.info("invites.revoked", event_id=str(event_id), invite_id=str(invite_id))


@router.post("/import")
async def import_members_csv(
    event_id: uuid.UUID,
    member: AdminMemberDep,
    session: DBSessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    commit: Annotated[bool, Query(description="Write changes; false validates only")] = False,
    reissue: Annotated[bool, Query(description="Replace existing pending invites")] = False,
) -> dict:
    """Import members from CSV with columns ``email`` and optional ``role``/``name``.

    Defaults to a dry run: the response lists per-row errors so the organizer
    can fix the file before anything is written.
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

    use_case = ImportMembersUseCase(session)
    if not commit:
        preview, rows = use_case.preview(text)
        return {"committed": False, "preview": preview.to_payload(), "rowsReady": len(rows)}

    result = await use_case.commit(event_id, text, reissue=reissue)
    if not result["committed"]:
        await session.rollback()
        return result

    await session.commit()
    base = settings.frontend_base_url.rstrip("/")
    for invite in result.get("invited", []):
        invite["joinUrl"] = f"{base}/invite/{invite.pop('token')}"
    logger.info(
        "invites.csv_imported", event_id=str(event_id), count=len(result.get("invited", []))
    )
    return result
