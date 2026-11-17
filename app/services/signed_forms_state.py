"""Whether a client form is being signed (and so is frozen against edits).

Kept apart from the signing service so the form store can ask without importing
the stamping and certificate machinery.
"""

from __future__ import annotations

from typing import Any


async def live_session(db: Any, version_id: Any) -> Any:
    """The signing pass on a version that still counts (in progress or executed), if any."""
    from sqlalchemy import select

    from app.models.form_signing import FormSigningSession

    if version_id is None:
        return None
    return (
        await db.execute(
            select(FormSigningSession).where(
                FormSigningSession.version_id == version_id,
                FormSigningSession.status.in_(["in_progress", "executed"]),
            )
        )
    ).scalars().first()


async def signing_started(db: Any, form: Any) -> bool:
    """True once any signer has signed the form's current version."""
    if form is None or form.current_version_id is None or form.status != "submitted":
        return False
    return await live_session(db, form.current_version_id) is not None
