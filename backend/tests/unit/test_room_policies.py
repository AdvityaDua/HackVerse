"""Room access policy tests.

These are the tests whose absence let the ``"open"`` vs ``"public"`` bug ship:
every public space in the campus was admin-only and nothing caught it.
"""

import uuid

import pytest

from app.domain.common.enums import MemberStatus, Role, RoomType
from app.domain.events.entities import EventMember
from app.domain.rooms.entities import Room
from app.domain.rooms.policies import CanEnterRoomPolicy
from app.domain.teams.entities import Team

EVENT_ID = uuid.uuid4()
OTHER_EVENT_ID = uuid.uuid4()


def make_member(
    role: Role = Role.PARTICIPANT,
    status: MemberStatus = MemberStatus.ACTIVE,
    event_id: uuid.UUID = EVENT_ID,
) -> EventMember:
    return EventMember(
        id=uuid.uuid4(),
        event_id=event_id,
        user_id=uuid.uuid4(),
        role=role.value,
        status=status.value,
    )


def make_room(
    room_type: RoomType,
    access_policy: dict | None = None,
    event_id: uuid.UUID = EVENT_ID,
    room_id: uuid.UUID | None = None,
) -> Room:
    return Room(
        id=room_id or uuid.uuid4(),
        event_id=event_id,
        name=f"{room_type.value} room",
        key=room_type.value,
        type=room_type.value,
        access_policy=access_policy or {},
    )


@pytest.fixture
def policy() -> CanEnterRoomPolicy:
    return CanEnterRoomPolicy()


class TestPublicRooms:
    def test_participant_can_enter_public_room(self, policy):
        """Regression: the policy checked for "open", a type nothing emits.

        Main Stage, Mentor Lounge, Sponsor Booths and Chill Zone are all
        public rooms, and every one of them was admin-only.
        """
        decision = policy.evaluate(make_member(), make_room(RoomType.PUBLIC))
        assert decision.allowed

    @pytest.mark.parametrize("role", list(Role))
    def test_every_role_can_enter_public_room(self, policy, role):
        assert policy.check(make_member(role=role), make_room(RoomType.PUBLIC))

    def test_unknown_room_type_is_denied_with_a_reason(self, policy):
        room = Room(
            id=uuid.uuid4(),
            event_id=EVENT_ID,
            name="legacy",
            key="legacy",
            type="open",  # the old value; no longer granted implicitly
            access_policy={},
        )
        decision = policy.evaluate(make_member(), room)
        assert not decision.allowed
        assert decision.code == "ROOM_MISCONFIGURED"


class TestTeamRooms:
    def test_team_member_can_enter_via_room_id(self, policy):
        room = make_room(RoomType.TEAM)
        team = Team(id=uuid.uuid4(), event_id=EVENT_ID, name="Team A", room_id=room.id)
        assert policy.check(make_member(), room, [team])

    def test_team_member_can_enter_via_access_policy(self, policy):
        team_id = uuid.uuid4()
        room = make_room(RoomType.TEAM, {"allowed_team_ids": [str(team_id)]})
        team = Team(id=team_id, event_id=EVENT_ID, name="Team A", room_id=None)
        assert policy.check(make_member(), room, [team])

    def test_non_member_is_denied(self, policy):
        room = make_room(RoomType.TEAM)
        other_team = Team(id=uuid.uuid4(), event_id=EVENT_ID, name="Team B", room_id=uuid.uuid4())
        decision = policy.evaluate(make_member(), room, [other_team])
        assert not decision.allowed
        assert decision.code == "ROOM_ACCESS_DENIED"

    def test_user_with_no_team_is_denied(self, policy):
        decision = policy.evaluate(make_member(), make_room(RoomType.TEAM), [])
        assert not decision.allowed

    def test_mentor_with_grant_can_enter_team_room(self, policy):
        """The mentor flow: a time-boxed grant, not a role exemption."""
        room = make_room(RoomType.TEAM)
        mentor = make_member(role=Role.MENTOR)
        assert not policy.check(mentor, room, [])
        assert policy.check(mentor, room, [], grants={room.id})

    def test_grant_for_a_different_room_does_not_leak(self, policy):
        room = make_room(RoomType.TEAM)
        mentor = make_member(role=Role.MENTOR)
        assert not policy.check(mentor, room, [], grants={uuid.uuid4()})


class TestMentorAndSponsorRooms:
    def test_mentor_and_participant_can_enter_mentor_lounge(self, policy):
        room = make_room(RoomType.MENTOR)
        assert policy.check(make_member(role=Role.MENTOR), room)
        assert policy.check(make_member(role=Role.PARTICIPANT), room)

    def test_sponsor_cannot_enter_mentor_lounge(self, policy):
        decision = policy.evaluate(make_member(role=Role.SPONSOR), make_room(RoomType.MENTOR))
        assert not decision.allowed

    def test_sponsor_booth_floor_is_public(self, policy):
        assert policy.check(make_member(), make_room(RoomType.SPONSOR))

    def test_sponsor_only_back_area_excludes_participants(self, policy):
        room = make_room(RoomType.SPONSOR, {"sponsor_only": True})
        assert not policy.check(make_member(role=Role.PARTICIPANT), room)
        assert policy.check(make_member(role=Role.SPONSOR), room)

    def test_sponsor_only_area_can_be_restricted_to_named_members(self, policy):
        allowed = make_member(role=Role.SPONSOR)
        other = make_member(role=Role.SPONSOR)
        room = make_room(
            RoomType.SPONSOR,
            {"sponsor_only": True, "sponsor_member_ids": [str(allowed.id)]},
        )
        assert policy.check(allowed, room)
        assert not policy.check(other, room)


class TestAdminAndIsolation:
    def test_admin_can_enter_any_room(self, policy):
        admin = make_member(role=Role.ADMIN)
        for room_type in RoomType:
            assert policy.check(admin, make_room(room_type)), room_type

    def test_admin_room_denies_everyone_else(self, policy):
        for role in (Role.PARTICIPANT, Role.MENTOR, Role.SPONSOR):
            assert not policy.check(make_member(role=role), make_room(RoomType.ADMIN))

    def test_cross_event_access_is_denied_even_for_admins(self, policy):
        """Event isolation holds regardless of role."""
        admin = make_member(role=Role.ADMIN, event_id=EVENT_ID)
        foreign_room = make_room(RoomType.PUBLIC, event_id=OTHER_EVENT_ID)
        decision = policy.evaluate(admin, foreign_room)
        assert not decision.allowed
        assert decision.code == "ROOM_NOT_FOUND"

    @pytest.mark.parametrize(
        "status", [MemberStatus.BANNED, MemberStatus.REMOVED, MemberStatus.INVITED]
    )
    def test_inactive_members_cannot_enter_any_room(self, policy, status):
        member = make_member(status=status)
        decision = policy.evaluate(member, make_room(RoomType.PUBLIC))
        assert not decision.allowed
        assert decision.code == "MEMBERSHIP_INACTIVE"

    def test_banned_admin_cannot_enter(self, policy):
        """Status is checked before the admin shortcut."""
        member = make_member(role=Role.ADMIN, status=MemberStatus.BANNED)
        assert not policy.check(member, make_room(RoomType.PUBLIC))
