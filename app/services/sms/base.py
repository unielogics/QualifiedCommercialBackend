"""What every SMS transport has to answer.

Three providers now share one shape: AWS End User Messaging, Twilio, and an
Android handset reached over Tailscale. They fail in very different ways — a
sandboxed AWS account, an unregistered 10DLC campaign, a tablet that went to
sleep — and the thing calling them should not have to know which.

`available()` is separate from `send()` on purpose. A rep standing in a business
needs to be told "texting is switched off" before they wait for a message that
was never going to arrive, and that is a different answer from "the send was
attempted and failed".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SmsResult:
    """The outcome of one send attempt.

    `detail` is operator-facing: it ends up in delivery logs and, for failures,
    in front of whoever is waiting for the text. It should say what to do, not
    just what broke.
    """

    ok: bool
    provider: str
    message_id: str = ""
    detail: str = ""


class SmsProvider(Protocol):
    """A transport that can put a text on the wire.

    Implementations are synchronous. `deliver_link` runs the whole delivery path
    in a worker thread via `asyncio.to_thread`, so blocking IO here is correct
    and an async client would only add a second concurrency model.
    """

    name: str

    def available(self) -> bool:
        """Whether a send can actually reach a stranger's phone right now."""
        ...

    def unavailable_reason(self) -> str:
        """Operator-facing explanation for why `available()` is False."""
        ...

    def send(self, to_phone: str, body: str) -> SmsResult:
        """Send `body` to an E.164 number. Must not raise."""
        ...
