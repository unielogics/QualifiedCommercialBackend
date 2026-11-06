"""Read-time repair of communication links, including historical alerts."""

from urllib.parse import quote
from uuid import UUID

from app.enums import Role
from app.schemas.notification import NotificationRead


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def communication_link(thread_id, *, meta=None, role=None):
    """Use namespaces rather than treating every message as a Gmail thread.

    File navigation requires explicit server-stored identity. A phone number,
    sender name or email subject is never used to guess the application file.
    The destination's read API independently applies its current user scope.
    """
    if not isinstance(thread_id, str) or len(thread_id) > 300:
        return None
    parts = thread_id.split(":")
    valid = False
    if len(parts) == 3 and parts[:2] == ["sms", "phone"]:
        from app.services.communication_scope import canonical_phone

        phone = canonical_phone(parts[2])
        if phone:
            thread_id, valid = f"sms:phone:{phone}", True
    elif len(parts) == 2 and parts[0] == "rep" and _uuid(parts[1]):
        thread_id, valid = f"rep:{_uuid(parts[1])}", True
    elif len(parts) == 2 and parts[0] == "loan" and _uuid(parts[1]):
        thread_id, valid = f"loan:{_uuid(parts[1])}", True
    elif len(parts) == 2 and parts[0] == "email" and parts[1] and len(parts[1]) <= 128:
        valid = True
    elif len(parts) == 3 and parts[0] == "intake" and _uuid(parts[1]) and parts[2] in {"client", "partner", "internal", "underwriter_ai"}:
        valid = True
    elif len(parts) == 3 and parts[0] == "dealer" and _uuid(parts[1]) and parts[2] in {"client", "desk"}:
        valid = True
    if not valid:
        return None
    context = meta or {}
    intake_id = _uuid(context.get("intake_id"))
    if intake_id and role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        channel = "email" if context.get("channel") == "email" else "client"
        link = f"/files/{intake_id}?section=communications&channel={channel}"
        if channel == "email" and parts[0] == "rep":
            link += f"&thread={parts[1]}"
        return link
    return f"/messages?thread={quote(thread_id, safe='')}"


def notification_read(row, *, user=None):
    result = NotificationRead.model_validate(row)
    if row.target_type == "communication_thread" and row.event_type in {"sms_received", "email_received"}:
        if result.body == "Open Inbox to review this message.":
            result.body = "Open the conversation to review this message."
        link = communication_link((row.meta or {}).get("thread_id") or row.target_id, meta=row.meta, role=getattr(user, "role", None))
        result.deep_link = link
        if link is None:
            result.meta = {**result.meta, "navigation_unavailable": True}
    return result
