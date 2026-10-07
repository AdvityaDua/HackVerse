"""TURN/STUN configuration for WebRTC.

This endpoint used to return a single public Google STUN server and no TURN at
all — for the risk the PRD ranks as its biggest technical one ("college Wi-Fi
may block peer connections; TURN fallback matters"). Media would have failed on
exactly the networks the product targets.

Credentials are generated per request using coturn's REST scheme
(``use-auth-secret``): the username is an expiry timestamp and the password is
an HMAC of it under the shared secret. The long-term secret never leaves the
server and leaked credentials expire on their own.
"""

import base64
import hashlib
import hmac
import time
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentEventMemberDep, SettingsDep

router = APIRouter(prefix="/events/{event_id}/turn-config", tags=["webrtc"])


class IceServerConfig(BaseModel):
    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class TurnConfigResponse(BaseModel):
    iceServers: list[IceServerConfig]
    #: Seconds until the returned credentials stop working.
    ttl_seconds: int
    #: False when no relay is configured; the client should warn that media
    #: will fail on restrictive networks rather than failing mysteriously.
    turn_available: bool


def build_turn_credentials(secret: str, ttl_seconds: int, identity: str) -> tuple[str, str]:
    expiry = int(time.time()) + ttl_seconds
    username = f"{expiry}:{identity}"
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode("ascii")


@router.get("", response_model=TurnConfigResponse)
async def get_turn_config(
    event_id: uuid.UUID,
    member: CurrentEventMemberDep,
    settings: SettingsDep,
):
    ice_servers = [IceServerConfig(urls=url) for url in settings.stun_urls]

    turn_available = bool(settings.turn_urls and settings.turn_shared_secret)
    if turn_available:
        username, credential = build_turn_credentials(
            settings.turn_shared_secret,
            settings.turn_credential_ttl_seconds,
            str(member.id),
        )
        ice_servers.append(
            IceServerConfig(
                urls=settings.turn_urls,
                username=username,
                credential=credential,
            )
        )

    return TurnConfigResponse(
        iceServers=ice_servers,
        ttl_seconds=settings.turn_credential_ttl_seconds,
        turn_available=turn_available,
    )
