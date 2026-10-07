"""Tests for the audit's security findings."""

import pytest
from pydantic import ValidationError

from app.api.ws.schemas import ChatMessagePayload, ClientMessageEnvelope, PlayerMovePayload


class TestConfigGuards:
    """S2: a production deploy must not fall back to scaffold defaults."""

    def _settings(self, **overrides):
        from app.infrastructure.config import Settings

        base = {
            "app_env": "production",
            "session_secret": "x" * 40,
            "session_cookie_secure": True,
            "google_client_id": "id",
            "google_client_secret": "secret",
        }
        base.update(overrides)
        return Settings(**base)

    def test_valid_production_config_is_accepted(self):
        assert self._settings().is_production_like

    def test_default_secret_is_rejected_in_production(self):
        with pytest.raises(ValidationError, match="SESSION_SECRET"):
            self._settings(session_secret="change-me")

    def test_short_secret_is_rejected(self):
        with pytest.raises(ValidationError, match="at least 32"):
            self._settings(session_secret="tooshort")

    def test_insecure_cookie_is_rejected_in_production(self):
        with pytest.raises(ValidationError, match="SESSION_COOKIE_SECURE"):
            self._settings(session_cookie_secure=False)

    def test_missing_google_credentials_are_rejected(self):
        with pytest.raises(ValidationError, match="GOOGLE_CLIENT_ID"):
            self._settings(google_client_id="")

    def test_local_env_keeps_the_scaffold_defaults_usable(self):
        from app.infrastructure.config import Settings

        local = Settings(app_env="local", session_secret="change-me")
        assert not local.is_production_like

    def test_cors_origins_always_include_the_frontend(self):
        from app.infrastructure.config import Settings

        settings = Settings(
            app_env="local",
            frontend_base_url="http://localhost:5173/",
            allowed_origins=["https://preview.example.com"],
        )
        assert "http://localhost:5173" in settings.cors_origins
        assert "https://preview.example.com" in settings.cors_origins


class TestWebSocketOriginCheck:
    """S1: WebSocket upgrades bypass CORS, so Origin must be checked."""

    def _settings(self, env: str):
        from app.infrastructure.config import Settings

        return Settings(
            app_env=env,
            session_secret="x" * 40,
            session_cookie_secure=env != "local",
            google_client_id="id",
            google_client_secret="secret",
            frontend_base_url="https://campus.example.com",
        )

    def test_configured_origin_is_allowed(self):
        from app.api.ws.event_socket import origin_allowed

        assert origin_allowed("https://campus.example.com", self._settings("production"))

    def test_foreign_origin_is_rejected(self):
        """This is the cross-site WebSocket hijacking case."""
        from app.api.ws.event_socket import origin_allowed

        assert not origin_allowed("https://evil.example.com", self._settings("production"))

    def test_trailing_slash_does_not_matter(self):
        from app.api.ws.event_socket import origin_allowed

        assert origin_allowed("https://campus.example.com/", self._settings("production"))

    def test_missing_origin_is_rejected_in_production(self):
        from app.api.ws.event_socket import origin_allowed

        assert not origin_allowed(None, self._settings("production"))

    def test_missing_origin_is_tolerated_locally_for_test_clients(self):
        from app.api.ws.event_socket import origin_allowed

        assert origin_allowed(None, self._settings("local"))


class TestSessionTokens:
    def test_tokens_carry_a_unique_jti(self):
        """A12: revocation needs a per-token identifier."""
        import uuid

        from app.infrastructure.auth.tokens import create_session_token, verify_session_token

        user_id = uuid.uuid4()
        first = verify_session_token(create_session_token(user_id, "a@example.com"))
        second = verify_session_token(create_session_token(user_id, "a@example.com"))

        assert first.jti and second.jti
        assert first.jti != second.jti
        assert first.remaining_seconds > 0

    def test_tampered_token_is_rejected(self):
        import uuid

        from app.infrastructure.auth.tokens import (
            TokenError,
            create_session_token,
            verify_session_token,
        )

        token = create_session_token(uuid.uuid4(), "a@example.com")
        payload, signature = token.split(".", 1)
        with pytest.raises(TokenError):
            verify_session_token(f"{payload}x.{signature}")

    def test_oauth_state_cannot_be_used_as_a_session(self):
        from app.infrastructure.auth.tokens import (
            TokenError,
            create_oauth_state,
            verify_session_token,
        )

        with pytest.raises(TokenError, match="token type"):
            verify_session_token(create_oauth_state("invite"))


class TestSessionRevocation:
    async def test_revoked_jti_is_reported(self, fake_redis):
        from app.infrastructure.auth.sessions import SessionRevocationStore

        store = SessionRevocationStore(fake_redis)
        assert not await store.is_revoked("abc")

        await store.revoke("abc", ttl_seconds=60)
        assert await store.is_revoked("abc")

    async def test_expired_tokens_are_not_stored(self, fake_redis):
        from app.infrastructure.auth.sessions import SessionRevocationStore

        store = SessionRevocationStore(fake_redis)
        await store.revoke("already-expired", ttl_seconds=0)
        assert not await store.is_revoked("already-expired")


class TestPayloadValidation:
    """S7/S8: inbound WebSocket payloads are validated, not trusted."""

    @pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "NaN"])
    def test_non_finite_coordinates_are_rejected(self, bad):
        with pytest.raises(ValidationError):
            PlayerMovePayload.model_validate({"x": float(bad), "y": 0})

    def test_out_of_world_coordinates_are_rejected(self):
        with pytest.raises(ValidationError):
            PlayerMovePayload.model_validate({"x": 10**9, "y": 0})

    def test_unknown_direction_is_rejected(self):
        with pytest.raises(ValidationError):
            PlayerMovePayload.model_validate({"x": 0, "y": 0, "direction": "sideways"})

    def test_extra_movement_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            PlayerMovePayload.model_validate({"x": 0, "y": 0, "speedHack": True})

    def test_oversized_chat_is_rejected(self):
        with pytest.raises(ValidationError):
            ChatMessagePayload.model_validate({"roomId": "r", "content": "x" * 2001})

    def test_blank_chat_is_rejected(self):
        with pytest.raises(ValidationError):
            ChatMessagePayload.model_validate({"roomId": "r", "content": "   "})

    def test_chat_is_trimmed(self):
        payload = ChatMessagePayload.model_validate({"roomId": "r", "content": "  hi  "})
        assert payload.content == "hi"

    def test_envelope_tolerates_unknown_top_level_keys(self):
        envelope = ClientMessageEnvelope.model_validate_json(
            '{"type":"player:move","payload":{},"somethingNew":1}'
        )
        assert envelope.type == "player:move"

    def test_envelope_requires_a_type(self):
        with pytest.raises(ValidationError):
            ClientMessageEnvelope.model_validate({"payload": {}})


class TestRateLimiter:
    async def test_allows_up_to_the_limit_then_blocks(self, fake_redis):
        from app.infrastructure.redis.rate_limit import RateLimiter

        limiter = RateLimiter(fake_redis)
        assert all([await limiter.allow("k", 3, 60) for _ in range(3)])
        assert not await limiter.allow("k", 3, 60)

    async def test_separate_keys_have_separate_budgets(self, fake_redis):
        from app.infrastructure.redis.rate_limit import RateLimiter

        limiter = RateLimiter(fake_redis)
        assert await limiter.allow("a", 1, 60)
        assert await limiter.allow("b", 1, 60)
        assert not await limiter.allow("a", 1, 60)

    async def test_zero_limit_blocks_everything(self, fake_redis):
        from app.infrastructure.redis.rate_limit import RateLimiter

        assert not await RateLimiter(fake_redis).allow("k", 0, 60)


class TestTurnCredentials:
    def test_credentials_are_time_limited_and_hmac_signed(self):
        import base64
        import hashlib
        import hmac

        from app.api.http.turn_routes import build_turn_credentials

        username, credential = build_turn_credentials("s3cret", 3600, "member-1")
        expiry, identity = username.split(":", 1)

        assert identity == "member-1"
        assert int(expiry) > 0

        expected = base64.b64encode(
            hmac.new(b"s3cret", username.encode(), hashlib.sha1).digest()
        ).decode()
        assert credential == expected

    def test_different_members_get_different_credentials(self):
        from app.api.http.turn_routes import build_turn_credentials

        first, _ = build_turn_credentials("s", 60, "a")
        second, _ = build_turn_credentials("s", 60, "b")
        assert first != second
