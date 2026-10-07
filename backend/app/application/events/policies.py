from app.domain.common.errors import AuthorizationError
from app.domain.events.entities import EventMember


def ensure_active_event_member(member: EventMember) -> None:
    if not member.is_active:
        raise AuthorizationError("Event membership is not active.")


def ensure_event_admin(member: EventMember) -> None:
    ensure_active_event_member(member)
    if not member.is_admin:
        raise AuthorizationError("Admin role is required.")
