from app.infrastructure.auth.invite_tokens import hash_invite_token, normalize_email


def test_hash_invite_token_is_stable_and_not_raw_token() -> None:
    token = "abc123"

    assert hash_invite_token(token) == hash_invite_token(token)
    assert hash_invite_token(token) != token


def test_normalize_email_trims_and_lowercases() -> None:
    assert normalize_email(" Student@Example.COM ") == "student@example.com"
