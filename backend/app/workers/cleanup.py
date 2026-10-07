"""Periodic maintenance.

Two jobs the plan called for and that did not exist:

* chat retention — the PRD defaults to 30 days, configurable per deployment;
  without this, ``chat_messages`` grows without bound.
* presence sweeping — per-player records expire on their own TTL, but the
  sorted-set index needs its stale entries trimmed so counts stay honest.

Run with ``python -m app.workers.cleanup`` from a scheduler, or import
``run_once`` from a task runner.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.infrastructure.config import get_settings
from app.infrastructure.db.models.chat_message import ChatMessageModel
from app.infrastructure.db.models.event import EventModel
from app.infrastructure.db.models.room_grant import RoomGrantModel
from app.infrastructure.db.session import async_session_maker
from app.infrastructure.observability.logging import configure_logging, get_logger
from app.infrastructure.redis.client import get_redis_client
from app.infrastructure.redis.presence_store import RedisPresenceStore

logger = get_logger(__name__)


async def purge_old_chat(retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    async with async_session_maker() as session:
        result = await session.execute(
            delete(ChatMessageModel).where(ChatMessageModel.created_at < cutoff)
        )
        await session.commit()
    deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("cleanup.chat_purged", deleted=deleted, retention_days=retention_days)
    return deleted


async def purge_expired_grants() -> int:
    """Remove room grants that expired more than a day ago."""
    cutoff = datetime.now(UTC) - timedelta(days=1)
    async with async_session_maker() as session:
        result = await session.execute(
            delete(RoomGrantModel).where(
                RoomGrantModel.expires_at.isnot(None),
                RoomGrantModel.expires_at < cutoff,
            )
        )
        await session.commit()
    deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("cleanup.grants_purged", deleted=deleted)
    return deleted


async def sweep_presence() -> int:
    """Trim stale entries from each live event's presence index."""
    settings = get_settings()
    store = RedisPresenceStore(get_redis_client(), ttl_seconds=settings.presence_ttl_seconds)
    swept = 0
    async with async_session_maker() as session:
        event_ids = list(
            await session.scalars(
                select(EventModel.id).where(EventModel.status.in_(["live", "draft"]))
            )
        )
    for event_id in event_ids:
        swept += await store.sweep(event_id)
    if swept:
        logger.info("cleanup.presence_swept", removed=swept, events=len(event_ids))
    return swept


async def run_once() -> dict[str, int]:
    settings = get_settings()
    return {
        "chat_deleted": await purge_old_chat(settings.chat_retention_days),
        "grants_deleted": await purge_expired_grants(),
        "presence_swept": await sweep_presence(),
    }


async def main() -> None:
    configure_logging()
    result = await run_once()
    logger.info("cleanup.completed", **result)


if __name__ == "__main__":
    asyncio.run(main())
