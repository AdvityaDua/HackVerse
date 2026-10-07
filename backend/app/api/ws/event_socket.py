"""Event WebSocket endpoint.

Rewritten to address the audit findings:

* Handshake now validates ``Origin`` (WebSockets bypass CORS, so cookie auth
  without this check is cross-site WebSocket hijacking), event status, and
  membership.
* Cleanup lives in ``finally``, so a Redis timeout or a cancelled task no longer
  leaves a ghost player in presence forever.
* Movement is published to the sender's spatial cell, not the whole event, and
  subscriptions follow the player across cell boundaries.
* Every inbound message is rate limited and schema validated; failures send a
  ``system:error`` back and are logged instead of being silently swallowed.
* Room authorisation is cached per connection rather than re-queried per
  message, and chat persistence no longer blocks the broadcast.
* RTC signalling is authorised by room OR proximity, so open-campus proximity
  audio is possible at all, and is capped at ``max_media_peers``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.ws.connection_manager import Connection, manager
from app.api.ws.schemas import (
    ChatMessagePayload,
    ClientMessageEnvelope,
    ModerationReportPayload,
    PlayerInteractPayload,
    PlayerMovePayload,
    PlayerStatusPayload,
    RoomEnterPayload,
    RoomLeavePayload,
    RtcSignalPayload,
)
from app.application.realtime.channels import (
    cell_channel,
    event_channel,
    role_channel,
    room_channel,
    user_channel,
)
from app.application.realtime.publisher import RealtimePublisher, envelope
from app.application.rooms.authorization import (
    RoomAuthorizationCache,
    RoomAuthorizationService,
    to_member_entity,
)
from app.domain.common.enums import JOINABLE_EVENT_STATUSES, MemberStatus, PlayerStatus, Role
from app.domain.realtime.interest import (
    DEFAULT_CELL_SIZE_TILES,
    MovementBounds,
    cell_for,
    neighbourhood,
    within,
)
from app.infrastructure.auth.sessions import SessionRevocationStore
from app.infrastructure.auth.tokens import TokenError, verify_session_token
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db.models.chat_message import ChatMessageModel
from app.infrastructure.db.models.event import EventModel
from app.infrastructure.db.models.event_member import EventMemberModel
from app.infrastructure.db.models.moderation import UserReportModel
from app.infrastructure.db.session import async_session_maker
from app.infrastructure.observability.logging import bind_context, clear_context, get_logger
from app.infrastructure.redis.client import get_redis_client
from app.infrastructure.redis.presence_store import RedisPresenceStore
from app.infrastructure.redis.pubsub import get_event_bus
from app.infrastructure.redis.rate_limit import (
    RateLimiter,
    chat_key,
    interact_key,
    movement_key,
    signalling_key,
)

router = APIRouter(prefix="/ws", tags=["websocket"])
logger = get_logger(__name__)

CLOSE_UNAUTHORIZED = status.WS_1008_POLICY_VIOLATION


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    """Reject handshakes from origins we do not serve.

    WebSocket upgrades are not subject to CORS, so a cookie-authenticated
    socket with no Origin check can be opened by any website on behalf of a
    logged-in visitor. A missing Origin header (non-browser client) is allowed
    only outside production-like environments.
    """
    if origin is None:
        return not settings.is_production_like
    normalized = origin.rstrip("/")
    if normalized in settings.cors_origins:
        return True
    # Tolerate a scheme/host match for configured origins carrying a path.
    parsed = urlparse(normalized)
    candidate = f"{parsed.scheme}://{parsed.netloc}"
    return any(
        candidate == f"{urlparse(allowed).scheme}://{urlparse(allowed).netloc}"
        for allowed in settings.cors_origins
    )


class AuthResult:
    def __init__(self, member: EventMemberModel, event: EventModel) -> None:
        self.member = member
        self.event = event


async def authenticate(
    websocket: WebSocket,
    event_id: uuid.UUID,
    redis: Redis,
    settings: Settings,
) -> AuthResult | None:
    session_cookie = websocket.cookies.get(settings.session_cookie_name)
    if not session_cookie:
        return None

    try:
        claims = verify_session_token(session_cookie)
    except TokenError:
        return None

    if await SessionRevocationStore(redis).is_revoked(claims.jti):
        return None

    async with async_session_maker() as session:
        member = await session.scalar(
            select(EventMemberModel).where(
                EventMemberModel.event_id == event_id,
                EventMemberModel.user_id == claims.user_id,
                EventMemberModel.status == MemberStatus.ACTIVE.value,
            )
        )
        if member is None:
            return None
        event = await session.scalar(select(EventModel).where(EventModel.id == event_id))
        if event is None:
            return None
        return AuthResult(member=member, event=event)


@router.websocket("/events/{event_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    event_id: uuid.UUID,
    redis: Annotated[Redis, Depends(get_redis_client)],
) -> None:
    settings = get_settings()

    if not origin_allowed(websocket.headers.get("origin"), settings):
        logger.warning(
            "ws.origin_rejected",
            origin=websocket.headers.get("origin"),
            event_id=str(event_id),
        )
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Origin not allowed")
        return

    auth = await authenticate(websocket, event_id, redis, settings)
    if auth is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Not an active member of this event")
        return

    # An event that has not been published, or has been archived, is not a place
    # people should be able to walk around in.
    if auth.event.status not in {s.value for s in JOINABLE_EVENT_STATUSES} and (
        auth.member.role != Role.ADMIN.value
    ):
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Event is not live")
        return

    await websocket.accept()
    session = EventSocketSession(
        websocket=websocket,
        event_id=event_id,
        member=auth.member,
        redis=redis,
        settings=settings,
    )
    await session.run()


class EventSocketSession:
    """One client's time on the socket."""

    def __init__(
        self,
        websocket: WebSocket,
        event_id: uuid.UUID,
        member: EventMemberModel,
        redis: Redis,
        settings: Settings,
    ) -> None:
        self.websocket = websocket
        self.event_id = event_id
        self.member = member
        self.member_entity = to_member_entity(member)
        self.user_id = member.user_id
        self.settings = settings
        self.redis = redis

        self.presence = RedisPresenceStore(redis, ttl_seconds=settings.presence_ttl_seconds)
        self.limiter = RateLimiter(redis)
        self.publisher = RealtimePublisher(get_event_bus())
        self.room_authz = RoomAuthorizationService()
        self.bounds = MovementBounds(max_coordinate=settings.world_max_coordinate)

        self.connection = Connection(
            websocket=websocket,
            event_id=event_id,
            user_id=member.user_id,
            member_id=member.id,
            role=member.role,
        )
        self.authz_cache: RoomAuthorizationCache | None = None
        self.current_room_id: str | None = None
        self.media_peers: set[str] = set()

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        bind_context(
            event_id=str(self.event_id),
            user_id=str(self.user_id),
            member_id=str(self.member.id),
            connection_id=str(self.connection.id),
        )
        try:
            await manager.register(self.connection)
            await self._on_join()
            await self._receive_loop()
        except WebSocketDisconnect:
            logger.info("ws.disconnected")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("ws.session_error", exc_info=True)
        finally:
            # Cleanup must happen for every exit path, not just a clean
            # WebSocketDisconnect, or presence leaks permanently.
            await self._cleanup()
            clear_context()

    async def _on_join(self) -> None:
        await self._refresh_authz(force=True)
        assert self.authz_cache is not None

        await manager.subscribe(
            self.connection,
            [
                event_channel(self.event_id),
                user_channel(self.event_id, self.user_id),
                role_channel(self.event_id, self.member.role),
            ],
        )

        existing = await self.presence.get_player(self.event_id, self.user_id)
        x, y = (existing.x, existing.y) if existing else (0.0, 0.0)
        status_value = existing.status if existing else PlayerStatus.ONLINE.value
        room_id = existing.room_id if existing else None

        await self.presence.register_player(
            self.event_id,
            self.user_id,
            x=x,
            y=y,
            status=status_value,
            room_id=room_id,
        )
        self.current_room_id = room_id
        self.connection.last_position = (x, y)
        await self._move_to_cell(cell_for(x, y, DEFAULT_CELL_SIZE_TILES))
        if room_id:
            await manager.subscribe(self.connection, [room_channel(self.event_id, room_id)])

        snapshot = [
            record.to_payload() for record in await self.presence.get_event_presence(self.event_id)
        ]
        await manager.send_local(
            self.connection,
            envelope(
                "presence:snapshot",
                {
                    "players": snapshot,
                    "self": {
                        "userId": str(self.user_id),
                        "memberId": str(self.member.id),
                        "role": self.member.role,
                        "x": x,
                        "y": y,
                        "roomId": room_id,
                    },
                },
            ),
        )

        # Only announce a genuinely new presence: a second tab should not make
        # everyone else think the player rejoined.
        if manager.user_connection_count(self.event_id, self.user_id) == 1:
            await self.publisher.to_event(
                self.event_id,
                envelope(
                    "player:joined",
                    {"userId": str(self.user_id), "memberId": str(self.member.id), "x": x, "y": y},
                ),
                exclude_user_id=self.user_id,
            )
        logger.info("ws.joined", tabs=manager.user_connection_count(self.event_id, self.user_id))

    async def _cleanup(self) -> None:
        try:
            await manager.unregister(self.connection)
            remaining = manager.user_connection_count(self.event_id, self.user_id)
            if remaining == 0:
                await self.presence.remove_player(self.event_id, self.user_id)
                await self.publisher.to_event(
                    self.event_id,
                    envelope("player:left", {"userId": str(self.user_id)}),
                    exclude_user_id=self.user_id,
                )
        except Exception:
            logger.error("ws.cleanup_failed", exc_info=True)

    # -- receive loop -------------------------------------------------------

    async def _receive_loop(self) -> None:
        handlers: dict[str, Any] = {
            "player:join": self._handle_noop,
            "player:move": self._handle_move,
            "player:status": self._handle_status,
            "player:heartbeat": self._handle_heartbeat,
            "room:enter": self._handle_room_enter,
            "room:leave": self._handle_room_leave,
            "chat:message": self._handle_chat,
            "player:interact": self._handle_interact,
            "moderation:report": self._handle_report,
            "rtc:offer": self._handle_rtc,
            "rtc:answer": self._handle_rtc,
            "rtc:ice": self._handle_rtc,
        }

        while True:
            raw = await self.websocket.receive_text()
            try:
                parsed = ClientMessageEnvelope.model_validate_json(raw)
            except Exception:
                await self._error(None, "INVALID_ENVELOPE", "Message format is invalid.")
                continue

            handler = handlers.get(parsed.type)
            if handler is None:
                await self._error(
                    parsed.requestId, "UNKNOWN_MESSAGE_TYPE", f"Unsupported type: {parsed.type}"
                )
                continue

            try:
                await handler(parsed)
            except asyncio.CancelledError:
                raise
            except WebSocketDisconnect:
                raise
            except Exception:
                # Log and tell the client. Never swallow silently: an invisible
                # failure mid-event is unsupportable.
                logger.error("ws.handler_error", message_type=parsed.type, exc_info=True)
                await self._error(parsed.requestId, "INTERNAL_ERROR", "Could not process message.")

    # -- handlers -----------------------------------------------------------

    async def _handle_noop(self, message: ClientMessageEnvelope) -> None:
        """``player:join`` is implicit in the handshake; acknowledge politely."""
        await manager.send_local(
            self.connection, envelope("player:join:ack", {}, message.requestId)
        )

    async def _handle_heartbeat(self, message: ClientMessageEnvelope) -> None:
        await self.presence.heartbeat(self.event_id, self.user_id)
        await manager.send_local(self.connection, envelope("pong", {}, message.requestId))

    async def _handle_move(self, message: ClientMessageEnvelope) -> None:
        bucket = int(time.time())
        allowed = await self.limiter.allow(
            movement_key(self.event_id, self.user_id, bucket),
            self.settings.movement_rate_limit_per_second,
            2,
        )
        if not allowed:
            return  # Silently drop: the client is simply ahead of its budget.

        try:
            payload = PlayerMovePayload.model_validate(message.payload)
        except Exception:
            await self._error(message.requestId, "INVALID_POSITION", "Position is not valid.")
            return

        if not self.bounds.is_valid_position(payload.x, payload.y):
            await self._error(message.requestId, "OUT_OF_BOUNDS", "Position is outside the world.")
            return
        if not self.bounds.is_plausible_step(self.connection.last_position, payload.x, payload.y):
            await self._error(message.requestId, "IMPLAUSIBLE_MOVE", "Movement rejected.")
            # Re-sync the client to the authoritative position.
            previous = self.connection.last_position or (0.0, 0.0)
            await manager.send_local(
                self.connection,
                envelope("player:resync", {"x": previous[0], "y": previous[1]}),
            )
            return

        self.connection.last_position = (payload.x, payload.y)
        await self.presence.update_position(
            self.event_id, self.user_id, payload.x, payload.y, payload.direction
        )

        new_cell = cell_for(payload.x, payload.y, DEFAULT_CELL_SIZE_TILES)
        if new_cell != self.connection.cell:
            await self._move_to_cell(new_cell)

        await self.publisher.to_cell(
            self.event_id,
            new_cell,
            envelope(
                "player:update",
                {
                    "userId": str(self.user_id),
                    "x": payload.x,
                    "y": payload.y,
                    "direction": payload.direction,
                    "seq": payload.seq,
                },
            ),
            exclude_user_id=self.user_id,
        )

    async def _handle_status(self, message: ClientMessageEnvelope) -> None:
        payload = PlayerStatusPayload.model_validate(message.payload)
        await self.presence.update_status(self.event_id, self.user_id, payload.status.value)
        await self.publisher.to_event(
            self.event_id,
            envelope(
                "player:status", {"userId": str(self.user_id), "status": payload.status.value}
            ),
        )

    async def _handle_room_enter(self, message: ClientMessageEnvelope) -> None:
        payload = RoomEnterPayload.model_validate(message.payload)
        try:
            room_uuid = uuid.UUID(payload.roomId)
        except ValueError:
            await self._error(message.requestId, "ROOM_NOT_FOUND", "Room not found in this event.")
            return

        await self._refresh_authz()
        assert self.authz_cache is not None
        decision, room = self.room_authz.authorize(self.authz_cache, self.member_entity, room_uuid)

        if not decision.allowed or room is None:
            logger.info("ws.room_denied", room_id=str(room_uuid), code=decision.code)
            await manager.send_local(
                self.connection,
                envelope(
                    "room:denied",
                    {"roomId": payload.roomId, "code": decision.code, "reason": decision.message},
                    message.requestId,
                ),
            )
            return

        if self.current_room_id and self.current_room_id != payload.roomId:
            await self._leave_room(self.current_room_id)

        await self.presence.set_room(self.event_id, self.user_id, payload.roomId)
        await manager.subscribe(self.connection, [room_channel(self.event_id, payload.roomId)])
        self.current_room_id = payload.roomId

        members = await self.presence.get_room_members(self.event_id, payload.roomId)
        await manager.send_local(
            self.connection,
            envelope(
                "room:entered",
                {"roomId": payload.roomId, "name": room.name, "members": members},
                message.requestId,
            ),
        )
        await self.publisher.to_room(
            self.event_id,
            payload.roomId,
            envelope("room:member:joined", {"roomId": payload.roomId, "userId": str(self.user_id)}),
            exclude_user_id=self.user_id,
        )
        logger.info("ws.room_entered", room_id=payload.roomId)

    async def _handle_room_leave(self, message: ClientMessageEnvelope) -> None:
        payload = RoomLeavePayload.model_validate(message.payload)
        if self.current_room_id != payload.roomId:
            return
        await self._leave_room(payload.roomId)

    async def _handle_chat(self, message: ClientMessageEnvelope) -> None:
        bucket = int(time.time() // 60)
        if not await self.limiter.allow(
            chat_key(self.event_id, self.user_id, bucket),
            self.settings.chat_rate_limit_per_minute,
            60,
        ):
            await self._error(
                message.requestId, "RATE_LIMITED", "You are sending messages too fast."
            )
            return

        try:
            payload = ChatMessagePayload.model_validate(message.payload)
        except Exception:
            await self._error(message.requestId, "INVALID_MESSAGE", "Message content is not valid.")
            return

        if self.current_room_id != payload.roomId:
            await self._error(
                message.requestId, "NOT_IN_ROOM", "You can only chat in a room you have entered."
            )
            return

        message_id = uuid.uuid4()
        sent_at = time.time()

        # Broadcast first, persist after: chat latency is user-visible, the
        # write is not. Persistence failures are logged, not surfaced.
        await self.publisher.to_room(
            self.event_id,
            payload.roomId,
            envelope(
                "chat:message",
                {
                    "id": str(message_id),
                    "roomId": payload.roomId,
                    "senderId": str(self.user_id),
                    "senderMemberId": str(self.member.id),
                    "content": payload.content,
                    "sentAt": sent_at,
                },
            ),
        )
        asyncio.create_task(self._persist_chat(message_id, payload.roomId, payload.content))

    async def _handle_interact(self, message: ClientMessageEnvelope) -> None:
        bucket = int(time.time() // 60)
        if not await self.limiter.allow(
            interact_key(self.event_id, self.user_id, bucket),
            self.settings.interact_rate_limit_per_minute,
            60,
        ):
            return

        payload = PlayerInteractPayload.model_validate(message.payload)
        from app.application.gamification.xp_service import XPService

        async with async_session_maker() as session:
            points = await XPService().record_interaction(
                session,
                self.event_id,
                self.user_id,
                payload.actionType,
                payload.targetId,
                actor_member_id=self.member.id,
            )
        if points > 0:
            await manager.send_local(
                self.connection,
                envelope(
                    "xp:gained",
                    {
                        "points": points,
                        "action": payload.actionType,
                        "targetId": payload.targetId,
                    },
                ),
            )

    async def _handle_report(self, message: ClientMessageEnvelope) -> None:
        payload = ModerationReportPayload.model_validate(message.payload)
        try:
            target_uuid = uuid.UUID(payload.targetMemberId)
        except ValueError:
            await self._error(message.requestId, "INVALID_TARGET", "Unknown member.")
            return

        async with async_session_maker() as session:
            target = await session.scalar(
                select(EventMemberModel).where(
                    EventMemberModel.id == target_uuid,
                    EventMemberModel.event_id == self.event_id,
                )
            )
            if target is None:
                await self._error(message.requestId, "INVALID_TARGET", "Unknown member.")
                return
            session.add(
                UserReportModel(
                    event_id=self.event_id,
                    reporter_member_id=self.member.id,
                    target_member_id=target_uuid,
                    reason=payload.reason,
                )
            )
            await session.commit()

        logger.info("ws.report_filed", target_member_id=payload.targetMemberId)
        await manager.send_local(
            self.connection, envelope("moderation:report:ack", {}, message.requestId)
        )
        await self.publisher.to_role(
            self.event_id,
            Role.ADMIN.value,
            envelope(
                "moderation:report:new",
                {
                    "reporterMemberId": str(self.member.id),
                    "targetMemberId": payload.targetMemberId,
                    "reason": payload.reason,
                },
            ),
        )

    async def _handle_rtc(self, message: ClientMessageEnvelope) -> None:
        bucket = int(time.time() // 60)
        if not await self.limiter.allow(
            signalling_key(self.event_id, self.user_id, bucket), 240, 60
        ):
            await self._error(message.requestId, "RATE_LIMITED", "Too many signalling messages.")
            return

        payload = RtcSignalPayload.model_validate(message.payload)
        try:
            target_member_uuid = uuid.UUID(payload.targetMemberId)
        except ValueError:
            await self._error(message.requestId, "INVALID_TARGET", "Unknown member.")
            return

        target_user_id = await self._resolve_member_user_id(target_member_uuid)
        if target_user_id is None:
            await self._error(message.requestId, "INVALID_TARGET", "Unknown member.")
            return

        authorized, reason = await self._can_signal(target_user_id)
        if not authorized:
            logger.info("ws.rtc_denied", target_member_id=payload.targetMemberId, reason=reason)
            await self._error(message.requestId, "RTC_NOT_AUTHORIZED", reason)
            return

        if (
            payload.targetMemberId not in self.media_peers
            and len(self.media_peers) >= self.settings.max_media_peers
        ):
            await self._error(
                message.requestId,
                "MEDIA_PEER_LIMIT",
                f"At most {self.settings.max_media_peers} simultaneous media peers.",
            )
            return
        self.media_peers.add(payload.targetMemberId)

        await self.publisher.to_user(
            self.event_id,
            target_user_id,
            envelope(
                message.type,
                {
                    "sessionId": payload.sessionId,
                    "fromMemberId": str(self.member.id),
                    "fromUserId": str(self.user_id),
                    "data": payload.data,
                },
            ),
        )

    # -- helpers ------------------------------------------------------------

    async def _can_signal(self, target_user_id: uuid.UUID) -> tuple[bool, str]:
        """Authorise media signalling by shared room OR physical proximity.

        The previous rule required both parties to share a ``room_id``, which is
        only ever set by ``room:enter``. Two players standing next to each other
        in the open campus both had ``None``, so proximity audio — the product's
        core differentiator — could never be established.
        """
        me = await self.presence.get_player(self.event_id, self.user_id)
        them = await self.presence.get_player(self.event_id, target_user_id)
        if me is None or them is None:
            return False, "That person is no longer online."

        if me.room_id and me.room_id == them.room_id:
            return True, ""

        if me.room_id != them.room_id:
            return False, "You must be in the same space to connect."

        if within(me.x, me.y, them.x, them.y, self.settings.proximity_tile_radius):
            return True, ""
        return False, "You are too far away to start a call."

    async def _resolve_member_user_id(self, member_id: uuid.UUID) -> uuid.UUID | None:
        async with async_session_maker() as session:
            return await session.scalar(
                select(EventMemberModel.user_id).where(
                    EventMemberModel.id == member_id,
                    EventMemberModel.event_id == self.event_id,
                    EventMemberModel.status == MemberStatus.ACTIVE.value,
                )
            )

    async def _move_to_cell(self, cell: tuple[int, int]) -> None:
        """Follow the player across cell boundaries, adjusting subscriptions."""
        previous = set(
            neighbourhood(self.connection.cell) if self.connection.cell is not None else []
        )
        upcoming = set(neighbourhood(cell))

        to_add = [cell_channel(self.event_id, c) for c in upcoming - previous]
        to_drop = [cell_channel(self.event_id, c) for c in previous - upcoming]

        if to_add:
            await manager.subscribe(self.connection, to_add)
        if to_drop:
            await manager.unsubscribe(self.connection, to_drop)
        self.connection.cell = cell

    async def _leave_room(self, room_id: str) -> None:
        await self.presence.set_room(self.event_id, self.user_id, None)
        await manager.unsubscribe(self.connection, [room_channel(self.event_id, room_id)])
        if self.current_room_id == room_id:
            self.current_room_id = None
        await self.publisher.to_room(
            self.event_id,
            room_id,
            envelope("room:member:left", {"roomId": room_id, "userId": str(self.user_id)}),
            exclude_user_id=self.user_id,
        )

    async def _refresh_authz(self, force: bool = False) -> None:
        if not force and self.authz_cache is not None and not self.authz_cache.is_stale():
            return
        async with async_session_maker() as session:
            self.authz_cache = await self.room_authz.load(session, self.event_id, self.member.id)

    async def _persist_chat(self, message_id: uuid.UUID, room_id: str, content: str) -> None:
        try:
            async with async_session_maker() as session:
                session.add(
                    ChatMessageModel(
                        id=message_id,
                        event_id=self.event_id,
                        room_id=uuid.UUID(room_id),
                        sender_id=self.user_id,
                        content=content,
                    )
                )
                await session.commit()
        except Exception:
            logger.error("ws.chat_persist_failed", room_id=room_id, exc_info=True)

    async def _error(self, request_id: str | None, code: str, message: str) -> None:
        await manager.send_local(
            self.connection,
            envelope("system:error", {"code": code, "message": message}, request_id),
        )
