import os

import pytest

# Settings are read at import time and cached, so the test environment has to
# be set before anything under app/ is imported.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-value-that-is-long-enough-32")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def settings():
    from app.infrastructure.config import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def fake_redis():
    """In-memory Redis so presence and rate-limit tests need no server."""
    from fakeredis.aioredis import FakeRedis

    return FakeRedis(decode_responses=True)


@pytest.fixture
def anyio_backend():
    return "asyncio"
