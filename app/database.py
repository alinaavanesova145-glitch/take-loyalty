"""
Async database engine and session management.

Uses SQLAlchemy 2.0's async engine with asyncpg as the driver. This module
exposes:
  - `engine`: the shared async engine
  - `AsyncSessionLocal`: a session factory
  - `get_db`: a FastAPI dependency that yields a session per-request and
    guarantees it is closed / rolled back correctly
  - `init_db`: creates tables and seeds the default branch on startup
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# `future=True` / SQLAlchemy 2.0 style engine. `pool_pre_ping` avoids stale
# connections when using pooled providers like Supabase / Neon after idle
# periods.
# SQLite (aiosqlite) uses NullPool and does not support pool_size/max_overflow.
# Only pass those kwargs for PostgreSQL (asyncpg) connections.
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if not _is_sqlite:
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base:
    """Shared declarative base placeholder (real Base lives in models.py to
    avoid circular imports; kept here only as a type reference note)."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a request-scoped AsyncSession."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """
    Create all tables (if they don't exist) and seed the default branch.

    For a production rollout across 6 branches, prefer Alembic migrations
    (see the `migrations/` folder generated in the setup instructions) over
    relying on `create_all`. This function remains useful for the pilot /
    local development bootstrap.
    """
    # Imported here (not at module top) to avoid a circular import between
    # database.py and models.py, since models.py imports Base from here.
    from sqlalchemy import select

    from app.models import Base, Branch

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Branch).where(Branch.id == settings.DEFAULT_BRANCH_ID)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            session.add(
                Branch(
                    id=settings.DEFAULT_BRANCH_ID,
                    name=settings.DEFAULT_BRANCH_NAME,
                    address=settings.DEFAULT_BRANCH_ADDRESS,
                    is_active=True,
                )
            )
            await session.commit()
