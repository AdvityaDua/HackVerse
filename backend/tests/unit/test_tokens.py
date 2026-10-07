from uuid import uuid4

from app.infrastructure.auth.tokens import (
    TokenError,
    create_oauth_state,
    create_session_token,
    verify_oauth_state,
    verify_session_token,
)


def test_session_token_round_trip() -> None:
    user_id = uuid4()

    token = create_session_token(user_id, "student@example.com")
    claims = verify_session_token(token)

    assert claims.user_id == user_id
    assert claims.email == "student@example.com"


def test_tampered_session_token_is_rejected() -> None:
    token = create_session_token(uuid4(), "student@example.com")
    tampered = f"{token}x"

    try:
        verify_session_token(tampered)
    except TokenError:
        return

    raise AssertionError("Tampered token should be rejected.")


def test_oauth_state_preserves_invite_token() -> None:
    state = create_oauth_state(invite_token="invite-token")

    assert verify_oauth_state(state) == "invite-token"
