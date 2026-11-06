"""Origin-channel grouping; delivery channels do not classify an alert."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import and_, case, func, or_

from app.models.notification import Notification

NotificationGroup = Literal["sms", "email", "general"]


def group_for(event_type: str, category: str, meta: dict | None) -> NotificationGroup:
    channel = str((meta or {}).get("channel") or "").lower()
    event = event_type.lower()
    if event.startswith("sms_") or category == "messages" and channel == "sms":
        return "sms"
    if event.startswith("email_") or category == "messages" and channel == "email":
        return "email"
    return "general"


def group_expression():
    event = func.lower(Notification.event_type)
    channel = func.lower(Notification.meta["channel"].astext)
    return case((or_(event.like("sms\\_%", escape="\\"), and_(Notification.category == "messages", channel == "sms")), "sms"),
                (or_(event.like("email\\_%", escape="\\"), and_(Notification.category == "messages", channel == "email")), "email"), else_="general")
