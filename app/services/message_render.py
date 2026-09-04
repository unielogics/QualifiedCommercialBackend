"""Placeholder rendering shared by every client-facing booking message.

Confirmation, reminder and pre-call texts are all authored by the host in the
booking settings, so they all need the same substitution rules and the same
guarantees: an unknown placeholder is left alone rather than raising (a typo in
settings should send a slightly odd message, not nothing), an empty template
means "use the default", and every SMS ends with the carrier opt-out notice
whether or not the author remembered it.
"""
from __future__ import annotations

from collections.abc import Mapping

#: Carriers expect opt-out language on automated recurring messages, so this is
#: appended to every SMS rather than left to whoever wrote the text.
STOP_NOTICE = "Reply STOP to opt out."

#: Every placeholder a host may use, with what it renders as. The settings UI
#: shows this list; the renderer accepts nothing else.
PLACEHOLDERS: dict[str, str] = {
    "{name}": "the client's full name",
    "{first}": "the client's first name",
    "{rep}": "the host or rep's name",
    "{business}": "the business name",
    "{date}": "the call date, e.g. Tuesday, September 8",
    "{time}": "the call date and time, e.g. Tuesday, September 8 at 10:00 AM EDT",
    "{join_link}": "the video join link, blank for phone or in-person",
    "{room_link}": "the client's secure room link",
    "{pin}": "the secure room PIN (confirmation SMS and PIN email only)",
    "{missing}": "what is still needed before the call, e.g. 'connect your business bank and authorize a soft credit check'",
    "{done}": "how many of the three pre-call steps are done, e.g. '1 of 3'",
    "{precall}": "a ready-made 'still needed before your call' sentence, blank when nothing is needed",
    "{video}": "the short video to watch before the call, blank when none is set",
}

#: Placeholders that may only appear in the two messages that deliver the PIN.
#: Anywhere else they would put the code into a message the PIN was deliberately
#: kept out of.
PIN_ONLY = frozenset({"{pin}"})


def render(template: str | None, values: Mapping[str, str], *, fallback: str = "") -> str:
    """Substitute placeholders in a host-authored template.

    Whitespace is collapsed for SMS-style bodies; multi-line email bodies keep
    their line breaks (callers pass ``collapse=False`` via :func:`render_lines`).
    """
    body = (template or "").strip()
    if not body:
        body = fallback
    for token, value in values.items():
        body = body.replace(token, value or "")
    return " ".join(body.split())


def render_lines(template: str | None, values: Mapping[str, str], *, fallback: str = "") -> str:
    """Like :func:`render` but preserves line breaks, for email bodies."""
    body = (template or "").strip()
    if not body:
        body = fallback
    for token, value in values.items():
        body = body.replace(token, value or "")
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(lines).strip()


def with_stop_notice(body: str) -> str:
    """Every automated SMS carries the opt-out notice exactly once."""
    text = body.strip()
    if STOP_NOTICE.lower() in text.lower():
        return text
    return f"{text} {STOP_NOTICE}".strip()


def disallowed_placeholders(template: str | None, *, allow_pin: bool = False) -> list[str]:
    """Which placeholders in a template are not allowed where it is used."""
    text = template or ""
    return sorted(token for token in PIN_ONLY if token in text and not allow_pin)
