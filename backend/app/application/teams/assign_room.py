import uuid


class AssignTeamRoomUseCase:
    def __init__(self, team_repository, room_repository, unit_of_work):
        self.team_repository = team_repository
        self.room_repository = room_repository
        self.unit_of_work = unit_of_work

    async def execute(self, event_id: uuid.UUID, team_id: uuid.UUID, room_id: uuid.UUID) -> None:
        """
        Assigns a team to a specific room in the event.
        Validates that both the team and room belong to the event, and the room is a team room.
        """
        async with self.unit_of_work:
            team = await self.team_repository.get_by_id(team_id)
            if not team or team.event_id != event_id:
                raise ValueError("Team not found or belongs to another event")

            room = await self.room_repository.get_by_id(room_id)
            if not room or room.event_id != event_id:
                raise ValueError("Room not found or belongs to another event")

            if room.type != "team":
                raise ValueError("Can only assign teams to team rooms")

            team.room_id = room.id
            await self.team_repository.save(team)
