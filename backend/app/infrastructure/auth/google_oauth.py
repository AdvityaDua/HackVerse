from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests
from google.oauth2 import id_token

from app.infrastructure.config import get_settings


class GoogleOAuthError(ValueError):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    display_name: str
    picture_url: str | None = None


def build_google_authorization_url(state: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "select_account",
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


async def exchange_code_for_id_token(code: str) -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if response.status_code >= 400:
        raise GoogleOAuthError("Google token exchange failed.")

    token_response = response.json()
    google_id_token = token_response.get("id_token")
    if not google_id_token:
        raise GoogleOAuthError("Google did not return an ID token.")
    return google_id_token


def verify_google_id_token(google_id_token: str) -> GoogleIdentity:
    settings = get_settings()
    try:
        claims = id_token.verify_oauth2_token(
            google_id_token,
            requests.Request(),
            settings.google_client_id,
        )
    except ValueError as exc:
        raise GoogleOAuthError("Google ID token verification failed.") from exc

    email = claims.get("email")
    sub = claims.get("sub")
    if not email or not sub:
        raise GoogleOAuthError("Google ID token is missing required identity claims.")

    email_verified = bool(claims.get("email_verified"))
    if not email_verified:
        raise GoogleOAuthError("Google email is not verified.")

    return GoogleIdentity(
        sub=sub,
        email=email,
        email_verified=email_verified,
        display_name=claims.get("name") or email.split("@", 1)[0],
        picture_url=claims.get("picture"),
    )
