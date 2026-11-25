"""Explicit staff-note selection for AI context, independent of message history."""
from __future__ import annotations


def is_internal_note(note) -> bool:
    return bool(getattr(note, "visibility", None) == "admin" and getattr(note, "channel", "partner") == "internal"
        and getattr(note, "share_id", None) is None and getattr(note, "vendor_access_id", None) is None)


def included_in_ai(note) -> bool:
    return is_internal_note(note) and getattr(note, "include_in_ai_context", False) is True


def ai_note_context(note) -> dict | None:
    if not included_in_ai(note):
        return None
    return {"content": note.content, "author_name": note.author_name,
        "created_at": note.created_at.isoformat(), "context_type": "internal_staff_note"}
