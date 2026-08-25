"""SMS consent: the words, the proof, and the gate.

Three things live here and they belong together.

**The words.** The exact disclosure a person agrees to. It lives on the server
and the form fetches it, rather than being typed into the React component,
because the record has to store the text that was actually on screen. If the
frontend owned a copy, the two would drift on the first copy edit and every
stored hash would become a hash of something nobody ever saw. Carriers audit
the wording; a proof that does not match the screen is not a proof.

**The proof.** Who agreed, to what, when, from where.

**The gate.** ``consent_for`` is what stands between a phone number and a
message. There is no path to SMS that does not come through it.

Why the wording is what it is, point by point, since each line is load-bearing
for a carrier review and none of it should be trimmed as "legalese":

- the brand is named, so the recipient knows who is texting
- the message types are named and split, because transactional consent and
  marketing consent are different permissions and cannot be bundled
- frequency is disclosed
- carrier rates are disclosed
- STOP and HELP are stated
- consent is declared not a condition of purchase, which is required the moment
  marketing is in scope

Also, and this is the one most often missed: the privacy policy must say that
mobile opt-in data is never shared with or sold to third parties. Toll-free and
10DLC verifications get rejected over that single sentence more than anything
else.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import (
    SMS_CONSENT_KINDS,
    SMS_CONSENT_METHODS,
    SMS_DISCLOSURE_VERSION,
    DealerSmsConsent,
)

__all__ = [
    "BRAND",
    "TERMS_URL",
    "PRIVACY_URL",
    "SMS_DISCLOSURE_VERSION",
    "ConsentDisclosure",
    "disclosure",
    "disclosure_hash",
    "record_consent",
    "consent_for",
    "revoke",
    "HELP_REPLY",
    "STOP_REPLY",
]

BRAND = "Qualified Commercial"
TERMS_URL = "https://qualifiedcommercial.com/terms"
PRIVACY_URL = "https://qualifiedcommercial.com/privacy"
SUPPORT_EMAIL = "support@qualifiedcommercial.com"

# Transactional: operational messages about the account and funding file. The
# channel remains optional because required file communications can use email.
TRANSACTIONAL_TEXT = (
    f"I agree to receive account and application text messages from {BRAND} about this "
    "funding file, including appointment reminders, secure links, bank-connection requests, "
    "document and signature requests, and status updates. Consent is optional and is not a "
    "condition of purchase, applying for funding, receiving funding, or using the platform. "
    "Message frequency varies. Message and data rates may apply. Reply STOP to opt out. "
    "Reply HELP for help."
)

# Marketing: everything that is us reaching out rather than the file needing
# something. Separate checkbox, separate row in the table, separately revocable.
MARKETING_TEXT = (
    f"I agree to receive promotional and marketing text messages from {BRAND}, including "
    "new loan program announcements, rate updates and offers. This is a separate optional "
    "consent and is not a condition of purchase, applying for funding, receiving funding, "
    "or using the platform. Message frequency varies. Message and data rates may apply. "
    "Reply STOP to opt out. Reply HELP for help."
)

# Not SMS consent, and deliberately its own checkbox rather than a line inside
# one of the above. Bundling agreement-to-terms with agreement-to-texts makes
# both weaker.
LEGAL_TEXT = (
    f"I have read and agree to the {BRAND} Terms and Conditions and Privacy Policy."
)

# The two auto-replies. Both are mandatory and both are quoted in the carrier
# registration, so they live in code beside the disclosure rather than being
# configured somewhere a reviewer cannot see.
STOP_REPLY = (
    f"{BRAND}: You are unsubscribed and will receive no further messages. "
    f"Reply HELP for help or email {SUPPORT_EMAIL}."
)
HELP_REPLY = (
    f"{BRAND} business funding support: {SUPPORT_EMAIL}. Msg&data rates may apply. "
    "Msg frequency varies. Reply STOP to unsubscribe."
)


@dataclass(frozen=True)
class ConsentDisclosure:
    version: str
    brand: str
    transactional: str
    marketing: str
    legal: str
    terms_url: str
    privacy_url: str
    support_email: str


def disclosure() -> ConsentDisclosure:
    """The single source of the words. The form renders this."""
    return ConsentDisclosure(
        version=SMS_DISCLOSURE_VERSION,
        brand=BRAND,
        transactional=TRANSACTIONAL_TEXT,
        marketing=MARKETING_TEXT,
        legal=LEGAL_TEXT,
        terms_url=TERMS_URL,
        privacy_url=PRIVACY_URL,
        support_email=SUPPORT_EMAIL,
    )


def text_for(kind: str) -> str:
    if kind == "transactional":
        return TRANSACTIONAL_TEXT
    if kind == "marketing":
        return MARKETING_TEXT
    raise ValueError(f"unknown consent kind: {kind}")


def disclosure_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def record_consent(
    db: AsyncSession,
    *,
    dealer_id,
    phone_e164: str,
    kind: str,
    method: str,
    captured_by_user_id=None,
    captured_by_name: str | None = None,
    consenter_name: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> DealerSmsConsent:
    """Write one grant. Caller commits.

    Note the text is taken from ``text_for`` rather than from the request. A
    client that could post its own disclosure text could post an empty string
    and manufacture a clean-looking record.
    """
    if kind not in SMS_CONSENT_KINDS:
        raise ValueError(f"unknown consent kind: {kind}")
    if method not in SMS_CONSENT_METHODS:
        raise ValueError(f"unknown consent method: {method}")

    text = text_for(kind)
    row = DealerSmsConsent(
        dealer_id=dealer_id,
        phone_e164=phone_e164,
        consent_kind=kind,
        granted=True,
        method=method,
        disclosure_version=SMS_DISCLOSURE_VERSION,
        disclosure_hash=disclosure_hash(text),
        disclosure_text=text,
        captured_by_user_id=captured_by_user_id,
        captured_by_name=(captured_by_name or None),
        consenter_name=(consenter_name or None),
        ip_address=(ip_address or None),
        # Column is 400 chars; a long UA would otherwise raise on insert and
        # lose the consent entirely over a cosmetic field.
        user_agent=(user_agent or None) and user_agent[:400],
    )
    db.add(row)
    await db.flush()
    return row


async def consent_for(
    db: AsyncSession, *, phone_e164: str, kind: str
) -> DealerSmsConsent | None:
    """The gate. Returns the live grant for this number and kind, or None.

    Deliberately keyed on the NUMBER, not the dealer. If a number opted out
    while attached to one file, it must stay opted out everywhere, otherwise
    every new file becomes a way to text someone who said stop.
    """
    if kind not in SMS_CONSENT_KINDS:
        return None
    rows = (
        await db.execute(
            select(DealerSmsConsent)
            .where(
                DealerSmsConsent.phone_e164 == phone_e164,
                DealerSmsConsent.consent_kind == kind,
            )
            .order_by(DealerSmsConsent.created_at.desc())
        )
    ).scalars().all()
    if not rows:
        return None
    # Any revocation anywhere on this number wins, regardless of a later grant
    # on another file. Re-granting has to be an explicit new opt-in gathered
    # after the revocation, which the timestamp comparison below allows.
    latest_revoke = max(
        (r.revoked_at for r in rows if r.revoked_at is not None), default=None
    )
    for r in rows:
        if not r.granted or r.revoked_at is not None:
            continue
        if latest_revoke is not None and r.created_at <= latest_revoke:
            continue
        return r
    return None


async def revoke(
    db: AsyncSession, *, phone_e164: str, reason: str = "STOP", kind: str | None = None
) -> int:
    """Opt out. A bare STOP revokes everything for the number, which is what a
    person means by it and what carriers require.
    """
    stmt = select(DealerSmsConsent).where(
        DealerSmsConsent.phone_e164 == phone_e164,
        DealerSmsConsent.revoked_at.is_(None),
        DealerSmsConsent.granted.is_(True),
    )
    if kind:
        stmt = stmt.where(DealerSmsConsent.consent_kind == kind)
    rows = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)
    for r in rows:
        r.revoked_at = now
        r.revoked_reason = reason[:120]
    await db.flush()
    return len(rows)
