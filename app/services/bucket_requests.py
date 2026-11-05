"""The lifecycle of a requested document, beyond requested and uploaded.

A request can now be closed with a reason. Archiving a bucket closes every ask
it left open; restoring reopens exactly those, because the reason names the
archive and nothing else. Every place that asks "what is still outstanding?"
goes through `is_open_request`, so a closed ask never reads as a missing
document to the AI, the desk, or the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

OPEN_STATUS = "requested"
UPLOADED_STATUS = "uploaded"
CLOSED_STATUS = "closed"
ARCHIVE_REASON = "bucket_archived"


class _RequestedDocument(Protocol):
    status: str
    closed_at: datetime | None
    closed_reason: str | None


def is_open_request(doc: object) -> bool:
    """Still owed by the client: not uploaded, not closed."""

    status = getattr(doc, "status", None)
    return status not in (UPLOADED_STATUS, CLOSED_STATUS) and status != "not_applicable"


def source_kind(requirement_source: dict | None) -> str:
    """Where an ask came from, in words the desk can act on.

    Program-readiness playbooks and system-supporting requirements are rules;
    everything without a recorded source was typed by a person.
    """

    kind = (requirement_source or {}).get("kind") if isinstance(requirement_source, dict) else None
    if kind == "program_readiness":
        return "rule"
    if kind == "system_supporting":
        return "system"
    return "manual"


def close_open_requests(docs: list[_RequestedDocument], *, reason: str, at: datetime) -> int:
    """Close every open ask in `docs`; returns how many were closed."""

    closed = 0
    for doc in docs:
        if doc.status != OPEN_STATUS:
            continue
        doc.status = CLOSED_STATUS
        doc.closed_at = at
        doc.closed_reason = reason
        closed += 1
    return closed


def reopen_requests_closed_for(docs: list[_RequestedDocument], *, reason: str) -> int:
    """Reopen only the asks closed for `reason`; returns how many reopened."""

    reopened = 0
    for doc in docs:
        if doc.status != CLOSED_STATUS or doc.closed_reason != reason:
            continue
        doc.status = OPEN_STATUS
        doc.closed_at = None
        doc.closed_reason = None
        reopened += 1
    return reopened
