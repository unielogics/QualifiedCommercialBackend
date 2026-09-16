"""Transaction-scoped serialization for the singleton application settings blob."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# AppSettings is a singleton stored as JSON. A row lock cannot protect the
# first write when the row does not exist yet, so every settings writer and
# every security-sensitive locked read also takes this transaction lock.
APP_SETTINGS_ADVISORY_LOCK_KEY = int.from_bytes(b"QC_APPST", "big", signed=True)


async def lock_app_settings(db: AsyncSession) -> None:
    """Serialize AppSettings reads/writes, including first-row creation."""
    await db.execute(select(func.pg_advisory_xact_lock(APP_SETTINGS_ADVISORY_LOCK_KEY)))
