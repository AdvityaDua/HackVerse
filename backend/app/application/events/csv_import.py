"""CSV import for members and teams.

The previous version parsed a headerless CSV and returned
``{"status": "dry_run_completed"}`` without writing anything, while the route
discarded the result entirely. Since invites require an exact email match, that
left no way at all to onboard a few hundred participants.

This implementation validates first and reports per-row errors so an organizer
sees problems before anything is committed — the PRD's "errors are shown before
import commit" requirement.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.common.enums import Role, RoomType
from app.infrastructure.auth.invite_tokens import normalize_email
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.room import RoomModel
from app.infrastructure.db.models.team import TeamModel
from app.infrastructure.db.models.team_member import TeamMemberModel
from app.infrastructure.db.models.user import UserModel

MAX_ROWS = 5000


@dataclass
class RowError:
    row: int
    field: str
    message: str

    def to_payload(self) -> dict[str, object]:
        return {"row": self.row, "field": self.field, "message": self.message}


@dataclass
class MemberRow:
    row: int
    email: str
    role: Role
    display_name: str | None = None


@dataclass
class TeamRow:
    row: int
    name: str
    project_title: str | None
    member_emails: list[str] = field(default_factory=list)


@dataclass
class ImportPreview:
    valid_rows: int
    errors: list[RowError]
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_payload(self) -> dict[str, object]:
        return {
            "validRows": self.valid_rows,
            "errors": [error.to_payload() for error in self.errors],
            "warnings": self.warnings,
        }


def _read_dict_rows(content: str) -> tuple[list[dict[str, str]], list[str]]:
    reader = csv.DictReader(io.StringIO(content))
    fieldnames = [name.strip().lower() for name in (reader.fieldnames or [])]
    rows = [
        {(key or "").strip().lower(): (value or "").strip() for key, value in raw.items()}
        for raw in reader
    ]
    return rows, fieldnames


class MemberCsvParser:
    """Expects a header row with ``email`` and optional ``role``/``name``."""

    REQUIRED_COLUMNS = frozenset({"email"})

    def parse(self, content: str) -> tuple[list[MemberRow], list[RowError]]:
        rows, fieldnames = _read_dict_rows(content)
        errors: list[RowError] = []

        missing = self.REQUIRED_COLUMNS - set(fieldnames)
        if missing:
            return [], [
                RowError(0, "header", f"Missing required column(s): {', '.join(sorted(missing))}")
            ]
        if len(rows) > MAX_ROWS:
            return [], [RowError(0, "file", f"At most {MAX_ROWS} rows per import.")]

        parsed: list[MemberRow] = []
        seen: set[str] = set()
        for index, row in enumerate(rows, start=2):  # row 1 is the header
            email = normalize_email(row.get("email", ""))
            if not email or "@" not in email:
                errors.append(RowError(index, "email", "Not a valid email address."))
                continue
            if email in seen:
                errors.append(RowError(index, "email", "Duplicate email in this file."))
                continue
            seen.add(email)

            role_value = (row.get("role") or Role.PARTICIPANT.value).lower()
            try:
                role = Role(role_value)
            except ValueError:
                expected = ", ".join(r.value for r in Role)
                errors.append(
                    RowError(index, "role", f"Unknown role '{role_value}'. Expected: {expected}.")
                )
                continue

            parsed.append(
                MemberRow(
                    row=index,
                    email=email,
                    role=role,
                    display_name=row.get("name") or None,
                )
            )
        return parsed, errors


class TeamCsvParser:
    """Expects a header row with ``team_name`` and ``emails``.

    ``emails`` holds a separator-delimited list such as ``a@x.com;b@x.com``.
    ``project_title`` is optional.
    """

    REQUIRED_COLUMNS = frozenset({"team_name", "emails"})

    def parse(self, content: str) -> tuple[list[TeamRow], list[RowError]]:
        rows, fieldnames = _read_dict_rows(content)
        errors: list[RowError] = []

        missing = self.REQUIRED_COLUMNS - set(fieldnames)
        if missing:
            return [], [
                RowError(0, "header", f"Missing required column(s): {', '.join(sorted(missing))}")
            ]
        if len(rows) > MAX_ROWS:
            return [], [RowError(0, "file", f"At most {MAX_ROWS} rows per import.")]

        parsed: list[TeamRow] = []
        seen_names: set[str] = set()
        email_to_team: dict[str, str] = {}

        for index, row in enumerate(rows, start=2):
            name = row.get("team_name", "")
            if not name:
                errors.append(RowError(index, "team_name", "Team name is required."))
                continue
            key = name.casefold()
            if key in seen_names:
                errors.append(RowError(index, "team_name", "Duplicate team name in this file."))
                continue
            seen_names.add(key)

            emails: list[str] = []
            for candidate in row.get("emails", "").replace(",", ";").split(";"):
                email = normalize_email(candidate)
                if not email:
                    continue
                if "@" not in email:
                    errors.append(RowError(index, "emails", f"'{candidate}' is not an email."))
                    continue
                if email in email_to_team:
                    # The PRD names this edge case explicitly.
                    errors.append(
                        RowError(
                            index,
                            "emails",
                            f"{email} is already assigned to team '{email_to_team[email]}'.",
                        )
                    )
                    continue
                email_to_team[email] = name
                emails.append(email)

            if not emails:
                errors.append(RowError(index, "emails", "At least one valid member email."))
                continue

            parsed.append(
                TeamRow(
                    row=index,
                    name=name,
                    project_title=row.get("project_title") or None,
                    member_emails=emails,
                )
            )
        return parsed, errors


class ImportMembersUseCase:
    """Validate a member CSV, then issue invites for every row."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.parser = MemberCsvParser()

    def preview(self, content: str) -> tuple[ImportPreview, list[MemberRow]]:
        rows, errors = self.parser.parse(content)
        return ImportPreview(valid_rows=len(rows), errors=errors), rows

    async def commit(self, event_id: UUID, content: str, reissue: bool = False) -> dict:
        from app.application.events.invites import issue_invites

        preview, rows = self.preview(content)
        if not preview.ok:
            return {"committed": False, "preview": preview.to_payload()}

        issued: list[dict] = []
        skipped: list[dict[str, str]] = []
        for role in Role:
            batch = [row.email for row in rows if row.role is role]
            if not batch:
                continue
            result = await issue_invites(
                self.session,
                event_id=event_id,
                emails=batch,
                role=role,
                reissue=reissue,
            )
            issued.extend(
                {
                    "email": invite.email,
                    "role": invite.role,
                    "token": invite.token,
                    "alreadyMember": invite.already_member,
                }
                for invite in result.issued
            )
            skipped.extend(result.skipped)

        return {
            "committed": True,
            "preview": preview.to_payload(),
            "invited": issued,
            "skipped": skipped,
        }


class ImportTeamsUseCase:
    """Validate a team CSV, then create teams, their rooms and memberships."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.parser = TeamCsvParser()

    async def preview(self, event_id: UUID, content: str) -> tuple[ImportPreview, list[TeamRow]]:
        rows, errors = self.parser.parse(content)
        warnings: list[str] = []

        if rows:
            existing_names = {
                name.casefold()
                for name in await self.session.scalars(
                    select(TeamModel.name).where(TeamModel.event_id == event_id)
                )
            }
            for row in rows:
                if row.name.casefold() in existing_names:
                    errors.append(
                        RowError(row.row, "team_name", "A team with this name already exists.")
                    )

            unknown = await self._unknown_emails(event_id, rows)
            if unknown:
                preview_list = ", ".join(sorted(unknown)[:10])
                warnings.append(
                    f"{len(unknown)} email(s) are not event members yet and will be linked "
                    f"once they claim an invite: {preview_list}"
                )

        return ImportPreview(valid_rows=len(rows), errors=errors, warnings=warnings), rows

    async def commit(self, event_id: UUID, content: str, create_rooms: bool = True) -> dict:
        preview, rows = await self.preview(event_id, content)
        if not preview.ok:
            return {"committed": False, "preview": preview.to_payload()}

        member_by_email = await self._member_ids_by_email(event_id, rows)
        created_teams = 0
        linked_members = 0
        unresolved: list[str] = []

        for row in rows:
            room_id = None
            room: RoomModel | None = None
            if create_rooms:
                slug = row.name.casefold().replace(" ", "-")[:100]
                room = RoomModel(
                    event_id=event_id,
                    name=f"{row.name} Room",
                    key=f"team-{slug}",
                    type=RoomType.TEAM.value,
                    access_policy={},
                )
                self.session.add(room)
                await self.session.flush()
                room_id = room.id

            team = TeamModel(
                event_id=event_id,
                name=row.name,
                project_title=row.project_title,
                room_id=room_id,
            )
            self.session.add(team)
            await self.session.flush()
            created_teams += 1

            if room is not None:
                # Record the team on the room policy too, so access does not
                # depend on a single FK lookup.
                room.access_policy = {"allowed_team_ids": [str(team.id)]}

            for email in row.member_emails:
                member_id = member_by_email.get(email)
                if member_id is None:
                    unresolved.append(email)
                    continue
                self.session.add(TeamMemberModel(team_id=team.id, event_member_id=member_id))
                linked_members += 1

        return {
            "committed": True,
            "preview": preview.to_payload(),
            "teamsCreated": created_teams,
            "membersLinked": linked_members,
            "unresolvedEmails": sorted(set(unresolved)),
        }

    async def _member_ids_by_email(self, event_id: UUID, rows: list[TeamRow]) -> dict[str, UUID]:
        emails = {email for row in rows for email in row.member_emails}
        if not emails:
            return {}
        result = await self.session.execute(
            select(UserModel.email, EventMemberModel.id)
            .join(EventMemberModel, EventMemberModel.user_id == UserModel.id)
            .where(EventMemberModel.event_id == event_id, UserModel.email.in_(emails))
        )
        return {email: member_id for email, member_id in result.all()}

    async def _unknown_emails(self, event_id: UUID, rows: list[TeamRow]) -> set[str]:
        emails = {email for row in rows for email in row.member_emails}
        known = set((await self._member_ids_by_email(event_id, rows)).keys())
        return emails - known
