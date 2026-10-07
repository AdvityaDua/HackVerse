"""WebSocket message contracts.

Every inbound payload is validated here rather than trusted. Coordinates are
bounded and finite (an ``inf`` or ``NaN`` position used to serialise into JSON
that breaks every receiving client), chat has a length cap, and status values
come from the shared enum.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.common.enums import PlayerStatus

MAX_COORDINATE = 100_000.0
MAX_CHAT_LENGTH = 2000

Direction = Literal["up", "down", "left", "right"]


class ClientMessageEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = Field(max_length=64)
    requestId: str | None = Field(default=None, max_length=128)
    sentAt: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class ServerMessageEnvelope(BaseModel):
    type: str
    requestId: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PlayerJoinPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roomId: str | None = Field(default=None, max_length=64)


class PlayerMovePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: Annotated[float, Field(allow_inf_nan=False)]
    y: Annotated[float, Field(allow_inf_nan=False)]
    direction: Direction = "down"
    seq: int | None = Field(default=None, ge=0)

    @field_validator("x", "y")
    @classmethod
    def bounded(cls, value: float) -> float:
        if abs(value) > MAX_COORDINATE:
            raise ValueError("coordinate out of world bounds")
        return value


class PlayerStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PlayerStatus


class RoomEnterPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roomId: str = Field(min_length=1, max_length=64)


class RoomLeavePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roomId: str = Field(min_length=1, max_length=64)


class ChatMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roomId: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=MAX_CHAT_LENGTH)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message is empty")
        return stripped


class PlayerInteractPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actionType: str = Field(min_length=1, max_length=64)
    targetId: str | None = Field(default=None, max_length=128)


class RtcSignalPayload(BaseModel):
    """Offer, answer and ICE share one shape; ``data`` carries the SDP blob.

    The field is ``targetMemberId`` to match the PRD contract; the previous
    ``targetUserId`` spelling was a frontend/backend mismatch waiting to happen.
    """

    model_config = ConfigDict(extra="forbid")

    targetMemberId: str = Field(min_length=1, max_length=64)
    sessionId: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


class ModerationReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targetMemberId: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
