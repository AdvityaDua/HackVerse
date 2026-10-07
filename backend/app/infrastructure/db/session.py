from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.asyncpg_database_url,
    connect_args=settings.asyncpg_connect_args,
    pool_pre_ping=True,
    # The realtime layer borrows connections per message; SQLAlchemy's default
    # pool of 5 saturates almost immediately under live event load.
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout_seconds,
    pool_recycle=1800,
)

async_session_maker = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)
