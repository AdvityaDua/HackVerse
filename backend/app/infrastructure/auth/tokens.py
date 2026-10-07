import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.infrastructure.config import get_settings


class TokenError(ValueError):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _sign(payload: str, secret: str) -> str:
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(signature)


def _encode_payload(payload: dict[str, Any], secret: str) -> str:
    raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded_payload = _b64encode(raw_payload)
    signature = _sign(encoded_payload, secret)
    return f"{encoded_payload}.{signature}"


def _decode_payload(token: str, secret: str) -> dict[str, Any]:
    try:
        encoded_payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise TokenError("Malformed token.") from exc

    expected = _sign(encoded_payload, secret)
    if not hmac.compare_digest(signature, expected):
        raise TokenError("Invalid token signature.")

    try:
        payload = json.loads(_b64decode(encoded_payload))
    except (json.JSONDecodeError, ValueError) as exc:
        raise TokenError("Invalid token payload.") from exc

    expires_at = payload.get("exp")
    if expires_at is not None and int(expires_at) < int(datetime.now(UTC).timestamp()):
        raise TokenError("Token has expired.")

    return payload


@dataclass(frozen=True)
class SessionClaims:
    user_id: UUID
    email: str
    jti: str
    expires_at: int

    @property
    def remaining_seconds(self) -> int:
        return max(0, self.expires_at - int(datetime.now(UTC).timestamp()))


def create_session_token(user_id: UUID, email: str) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    return _encode_payload(
        {
            "typ": "session",
            "sub": str(user_id),
            "email": email,
            "jti": secrets.token_urlsafe(16),
            "exp": int(expires_at.timestamp()),
        },
        settings.session_secret,
    )


def verify_session_token(token: str) -> SessionClaims:
    settings = get_settings()
    payload = _decode_payload(token, settings.session_secret)
    if payload.get("typ") != "session":
        raise TokenError("Invalid token type.")
    expires_at = payload.get("exp")
    if expires_at is None:
        raise TokenError("Session token is missing an expiry.")
    return SessionClaims(
        user_id=UUID(payload["sub"]),
        email=payload["email"],
        jti=payload.get("jti", ""),
        expires_at=int(expires_at),
    )


def create_oauth_state(invite_token: str | None = None) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    return _encode_payload(
        {
            "typ": "oauth_state",
            "nonce": secrets.token_urlsafe(16),
            "invite_token": invite_token,
            "exp": int(expires_at.timestamp()),
        },
        settings.session_secret,
    )


def verify_oauth_state(state: str) -> str | None:
    settings = get_settings()
    payload = _decode_payload(state, settings.session_secret)
    if payload.get("typ") != "oauth_state":
        raise TokenError("Invalid token type.")
    return payload.get("invite_token")
