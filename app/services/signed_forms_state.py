"""Whether a client form is being signed (and so is frozen against edits).

Kept apart from the signing service so the form store can ask without importing
the stamping and certificate machinery.
"""

from __future__ import annotations

from typing import Any


async def signing_started(db: Any, form: Any) -> bool:
    """True once any signer has signed the form's current version."""
    from sqlalchemy import select

    from app.models.client_form import ClientFormVersion

    if form is None or form.current_version_id is None or form.status != "submitted":
        return False
    try:
        from app.models.form_signing import FormSigningSession
    except ImportError:
        return False
    version = await db.get(ClientFormVersion, form.current_version_id)
    if version is None:
        return False
    session = (
        await db.execute(
            select(FormSigningSession).where(
                FormSigningSession.version_id == version.id,
                FormSigningSession.status.in_(["in_progress", "executed"]),
            )
        )
    ).scalars().first()
    return session is not None
