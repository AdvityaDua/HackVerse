"""Fail if the SQLAlchemy models and the migrations have diverged.

The audit found several model fields with no corresponding migration. Running
this in CI makes that a build failure rather than a runtime surprise.

Usage: ``uv run python scripts/check_schema_drift.py`` against a database that
has already been migrated to head.
"""

from __future__ import annotations

import asyncio
import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

# Importing the models package registers every table on Base.metadata.
import app.infrastructure.db.models  # noqa: F401  (side-effect import)
from app.infrastructure.db.base import Base
from app.infrastructure.db.session import engine

#: Differences we deliberately tolerate. The lower(email) unique index is
#: created with raw SQL and has no declarative counterpart.
IGNORED_INDEX_NAMES = {"uq_users_email_lower"}


def _is_ignored(diff: object) -> bool:
    if not isinstance(diff, tuple) or not diff:
        return False
    kind = diff[0]
    if kind in {"add_index", "remove_index"}:
        index = diff[-1]
        return getattr(index, "name", None) in IGNORED_INDEX_NAMES
    return False


async def main() -> int:
    async with engine.connect() as connection:
        diffs = await connection.run_sync(
            lambda sync_connection: compare_metadata(
                MigrationContext.configure(sync_connection), Base.metadata
            )
        )

    real = [diff for diff in diffs if not _is_ignored(diff)]
    if not real:
        print("Schema matches models.")
        return 0

    print("Schema drift detected between models and migrations:\n")
    for diff in real:
        print(f"  - {diff}")
    print("\nGenerate a migration with: uv run alembic revision --autogenerate -m '...'")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
