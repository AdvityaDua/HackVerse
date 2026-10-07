"""Health and readiness.

The old endpoint returned HTTP 200 even when it reported ``degraded``, so a
load balancer would happily keep routing traffic to an instance whose database
had gone away. Readiness now fails with 503 so orchestrators can act on it.
"""

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.ws.connection_manager import manager
from app.infrastructure.config import get_settings
from app.infrastructure.db.session import async_session_maker
from app.infrastructure.observability.logging import get_logger
from app.infrastructure.redis.client import get_redis_client

router = APIRouter()
logger = get_logger(__name__)


async def _check_database() -> bool:
    try:
        async with async_session_maker() as session:
            await session.execute(text("select 1"))
        return True
    except Exception:
        logger.warning("health.database_unavailable", exc_info=True)
        return False


async def _check_redis() -> bool:
    try:
        return bool(await get_redis_client().ping())
    except Exception:
        logger.warning("health.redis_unavailable", exc_info=True)
        return False


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Is the process up? Deliberately dependency-free."""
    return {"status": "ok"}


@router.get("/health")
async def health_check(response: Response) -> dict[str, object]:
    settings = get_settings()
    checks = {
        "app": True,
        "database": await _check_database(),
        "redis": await _check_redis(),
    }
    healthy = all(checks.values())
    if not healthy:
        # Signal unreadiness so this instance is pulled from rotation.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "name": settings.app_name,
        "environment": settings.app_env,
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "turn_configured": bool(settings.turn_urls and settings.turn_shared_secret),
        "websocket_connections": manager.local_connection_count,
    }
