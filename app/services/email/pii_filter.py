"""One-Way Mirror PII filter.

Strips Lender identity from text shown to BROKER or CLIENT participants per
the Fintech Orchestrator privacy rule. The intent is conservative: when in
doubt, redact. Operators can review the original in the audit log.

The filter is two-layered:

1. Heuristic name/email scrubbing — catches direct identity leaks.
2. Generic pronoun rewrite — replaces personal sign-offs with institutional
   phrasing.

Real-world relay flow uses this on every Lender → Broker/Client message
draft before it lands in the email_drafts table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# A reasonable stop-list of email-ending phrases that often carry PII.
SIGN_OFF_PATTERNS = [
    re.compile(r"^(?:best|thanks|regards|cheers|sincerely|warmly|all the best|kind regards)[,.\s]*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^--+\s*$", re.MULTILINE),  # email signature divider
]

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)?\d{3}[\s.-]?\d{4}\b")
URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class RedactionContext:
    """Per-loan context to drive identity scrubbing."""

    lender_emails: frozenset[str]
    """Emails of LENDER-role participants on this loan. Always scrubbed."""
    lender_names: frozenset[str]
    """Display names + companies of LENDER-role participants. Best-effort match."""

    @classmethod
    def from_participants(cls, participants: Iterable) -> "RedactionContext":
        emails: set[str] = set()
        names: set[str] = set()
        for p in participants:
            role = getattr(p, "role", None)
            # Don't redact participants the operator has explicitly un-hidden.
            if role == "lender" and getattr(p, "hide_identity", True):
                if getattr(p, "email", None):
                    emails.add(p.email.lower())
                for n in (getattr(p, "display_name", None), getattr(p, "company", None)):
                    if n:
                        names.add(n.strip())
        return cls(lender_emails=frozenset(emails), lender_names=frozenset(names))


def _strip_signoff(text: str) -> str:
    """Cut everything from the first email signature divider onward."""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if any(p.match(line) for p in SIGN_OFF_PATTERNS):
            break
        out.append(line)
    return "\n".join(out).rstrip()


def redact_text(text: str, ctx: RedactionContext, *, replacement: str = "the lender") -> str:
    """Apply the One-Way Mirror filter to a single piece of text.

    Order matters: name matches first (longest first to avoid partial overlap),
    then email/phone/url. Sign-off block always trimmed last.
    """
    if not text:
        return text
    out = text

    # Replace any lender display names / company names. Sort longest-first so
    # "Sarah Thompson at JP Morgan" is replaced before just "Sarah".
    for name in sorted(ctx.lender_names, key=len, reverse=True):
        if not name:
            continue
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        out = pattern.sub(replacement, out)

    # Replace any lender email mentions.
    def _email_sub(m: re.Match) -> str:
        return replacement if m.group(0).lower() in ctx.lender_emails else m.group(0)

    out = EMAIL_RE.sub(_email_sub, out)

    # Phones / URLs from the lender are rare in body but always risky — drop
    # them outright on the safe side. (We don't know which phone belongs to
    # whom from text alone.)
    out = PHONE_RE.sub("[redacted]", out)
    out = URL_RE.sub("[link]", out)

    return _strip_signoff(out)


def reframe_request_for_broker(*, lender_text: str, ctx: RedactionContext) -> str:
    """Convert a raw Lender → Broker request into Fintech Orchestrator phrasing.

    Used when the AI hasn't yet rewritten the request; this gives a clean
    fallback so PII never leaks even if the LLM call fails.
    """
    redacted = redact_text(lender_text, ctx, replacement="the underwriter")
    if not redacted.strip():
        return "The underwriter has sent a follow-up. Open the deal to review the requested items."
    return (
        "Update from the underwriter:\n\n"
        f"{redacted}\n\n"
        "— forwarded by QC Fintech Orchestrator"
    )
