"""Module 2 — Air-gap email parsing.

Pull the QC deal ID out of the subject line, separate body from attachments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEAL_ID_RE = re.compile(r"\[QC-([A-Za-z0-9_-]{2,32})\]")


@dataclass(frozen=True)
class ParsedAttachment:
    filename: str
    mime_type: str
    size_bytes: int
    payload_b64: str  # opaque blob — saved to S3 by the consumer


@dataclass(frozen=True)
class ParsedEmail:
    subject: str
    body: str
    sender: str
    deal_id: str | None
    attachments: list[ParsedAttachment] = field(default_factory=list)

    @property
    def is_qc_deal(self) -> bool:
        return self.deal_id is not None


def extract_deal_id(subject: str) -> str | None:
    m = DEAL_ID_RE.search(subject)
    return m.group(1) if m else None


def inject_deal_id(subject: str, deal_id: str) -> str:
    """Used by outbound mail. Idempotent — won't double-inject."""
    if extract_deal_id(subject) == deal_id:
        return subject
    return f"[QC-{deal_id}] {subject}"


def parse_raw_email(
    *,
    subject: str,
    body: str,
    sender: str,
    attachments: list[ParsedAttachment] | None = None,
) -> ParsedEmail:
    return ParsedEmail(
        subject=subject,
        body=body,
        sender=sender,
        deal_id=extract_deal_id(subject),
        attachments=attachments or [],
    )
