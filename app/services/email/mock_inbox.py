"""Local fake inbox — stand-in for Gmail Pub/Sub during dev.

Holds outgoing emails (so we can assert subject injection) and accepts
synthetic incoming emails for tests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.email.parser import ParsedEmail, parse_raw_email


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    body: str
    deal_id: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MockInbox:
    def __init__(self) -> None:
        self.outbox: list[OutgoingEmail] = []
        self.incoming: deque[ParsedEmail] = deque()

    def send(self, *, to: str, subject_with_deal_id: str, body: str, deal_id: str) -> None:
        self.outbox.append(OutgoingEmail(to=to, subject=subject_with_deal_id, body=body, deal_id=deal_id))

    def deliver_synthetic(
        self,
        *,
        sender: str,
        subject: str,
        body: str,
        attachments: list[Any] | None = None,
    ) -> ParsedEmail:
        """Simulate a lender reply landing in the inbox."""
        parsed = parse_raw_email(
            subject=subject, body=body, sender=sender, attachments=attachments or []
        )
        self.incoming.append(parsed)
        return parsed

    def drain(self) -> list[ParsedEmail]:
        out = list(self.incoming)
        self.incoming.clear()
        return out


_inbox: MockInbox | None = None


def get_mock_inbox() -> MockInbox:
    global _inbox
    if _inbox is None:
        _inbox = MockInbox()
    return _inbox
