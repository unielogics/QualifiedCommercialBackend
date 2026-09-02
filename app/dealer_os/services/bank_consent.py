"""Bank connection consent: the words, the proof, and the gate.

Section 1.4 of the Plaid MSA requires end-user consent before an end user is
sent to connect an account. Before this, the flow went button click to bank
credential prompt with nothing in between.

Deliberately shaped like sms_consent.py, for the same reasons.

**The words live on the server.** The form fetches them; the React component
never owns a copy. If it did, the two would drift on the first copy edit and
every stored hash would be a hash of something nobody ever saw. A proof that
does not match the screen is not a proof.

**The client never sends the text back.** ``record`` takes no disclosure
argument — it reads the current wording itself. Accepting text from the browser
would let anyone store a consent to wording of their own choosing.

**The gate is ``has_consent``.** There is no path to a Plaid link token that
does not come through it.

Why each sentence in the disclosure is there, since none of it should be
trimmed as legalese:

- Plaid is named, because the MSA requires the end user be told who is
  retrieving their data
- credentials are stated to go to Plaid and not to us, which is the single fact
  a borrower most wants to know when a screen asks for their bank login
- the connection is stated to be read-only and limited to underwriting data;
  it may retrieve statements and create an Asset Report, but cannot move funds
- scope and duration are disclosed: twenty-four months back, refreshing about
  every thirty days, until disconnected
- Plaid's own policy is linked, and named as governing Plaid's use
- withdrawal is stated, because consent that cannot be withdrawn is not consent

The version is bumped whenever the wording changes. Old rows keep the version
and the text they were captured against, so a policy change never rewrites
history.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerBankConsent

__all__ = [
    "BANK_DISCLOSURE_VERSION",
    "BANK_CONSENT_METHODS",
    "disclosure",
    "has_consent",
    "record",
    "revoke",
    "ConsentState",
]

# Bump on ANY wording change. Separate from SMS_DISCLOSURE_VERSION — they are
# different disclosures and sharing a counter would misdate one of them.
BANK_DISCLOSURE_VERSION = "2026-09-02-products-v1"
_LEGACY_ASSETS_VERSION = "2026-08-24-assets-v1"

# How the consent was taken. `rep_attested` exists because a rep sometimes sits
# with a client; it is recorded honestly rather than disguised as self-service.
BANK_CONSENT_METHODS = ("self_web", "in_person_device", "rep_attested")

_DISCLOSURE_INTRO = (
    "I authorize Qualified Commercial LLC to use Plaid, Inc. to retrieve my "
    "business bank information from my financial institution for financing "
    "and underwriting.\n\n"
    "I understand that I will enter my bank login directly into Plaid's own "
    "window, and that Qualified Commercial does not receive or store those "
    "credentials.\n\n"
    "I understand this connection is read-only and cannot move funds, initiate "
    "payments, or create charges.\n\n"
)

_DISCLOSURE_END = (
    "The selected information may refresh about every 30 days until the bank "
    "is disconnected or the file setting is changed. Previously collected "
    "underwriting evidence remains in the financing file.\n\n"
    "I understand that Plaid's use of the information it collects is governed "
    "by Plaid's End User Privacy Policy at "
    "https://plaid.com/legal/#end-user-privacy-policy, and that I can manage "
    "or revoke my connections at https://my.plaid.com.\n\n"
    "I understand I may withdraw this authorization at any time by contacting "
    "support@qualifiedcommercial.com, and that doing so stops future retrieval."
)


@dataclass(frozen=True)
class ConsentState:
    granted: bool
    version: str | None = None
    at: datetime | None = None
    consenter_name: str | None = None
    product_scope: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(UTC)


def disclosure(products: list[str] | tuple[str, ...] | None = None) -> dict[str, str | list[str]]:
    """The exact words to put on screen, with the version they belong to."""
    scope = [value for value in ("assets", "statements") if value in set(products or ["assets"])]
    if not scope:
        scope = ["assets"]
    details: list[str] = []
    if "assets" in scope:
        details.append(
            "Plaid Assets: account identity, balances, and transaction history "
            "for an aggregate underwriting Asset Report (up to 210 days)."
        )
    if "statements" in scope:
        details.append(
            "Plaid Statements: available bank-produced statement PDF files for "
            "the requested date range (up to 24 months)."
        )
    text = _DISCLOSURE_INTRO + "I authorize:\n\n" + "\n".join(
        f"- {detail}" for detail in details
    ) + "\n\n" + _DISCLOSURE_END
    return {
        "version": BANK_DISCLOSURE_VERSION,
        "text": text,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "product_scope": scope,
    }


async def _latest(db: AsyncSession, dealer_id: uuid.UUID) -> DealerBankConsent | None:
    return (
        await db.execute(
            select(DealerBankConsent)
            .where(DealerBankConsent.dealer_id == dealer_id)
            .order_by(DealerBankConsent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def state(db: AsyncSession, dealer_id: uuid.UUID) -> ConsentState:
    row = await _latest(db, dealer_id)
    if row is None or not row.granted or row.revoked_at is not None:
        return ConsentState(granted=False)
    return ConsentState(
        granted=True,
        version=row.disclosure_version,
        at=row.created_at,
        consenter_name=row.consenter_name,
        product_scope=tuple(getattr(row, "product_scope", None) or []),
    )


async def has_consent(
    db: AsyncSession,
    dealer_id: uuid.UUID,
    required_products: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """The gate requires a live grant to the current product disclosure."""
    current = await state(db, dealer_id)
    required = set(required_products or ["assets"])
    version_allowed = current.version == BANK_DISCLOSURE_VERSION or (
        current.version == _LEGACY_ASSETS_VERSION and required <= {"assets"}
    )
    return current.granted and version_allowed and required <= set(current.product_scope)


async def record(
    db: AsyncSession,
    *,
    dealer_id: uuid.UUID,
    method: str,
    consenter_name: str | None,
    ip_address: str | None,
    user_agent: str | None,
    captured_by_user_id: uuid.UUID | None = None,
    captured_by_name: str | None = None,
    product_scope: list[str] | tuple[str, ...] | None = None,
) -> DealerBankConsent:
    """Store a grant against the wording currently on the server.

    Takes no disclosure text by design — see the module docstring. IP and user
    agent are passed in from the request by the caller and must never be read
    from a client-supplied body.
    """
    if method not in BANK_CONSENT_METHODS:
        raise ValueError(f"unknown consent method: {method}")

    d = disclosure(product_scope)
    row = DealerBankConsent(
        dealer_id=dealer_id,
        granted=True,
        method=method,
        disclosure_version=d["version"],
        disclosure_hash=d["hash"],
        disclosure_text=d["text"],
        product_scope=d["product_scope"],
        consenter_name=(consenter_name or None),
        ip_address=(ip_address or None),
        user_agent=((user_agent or "")[:400] or None),
        captured_by_user_id=captured_by_user_id,
        captured_by_name=captured_by_name,
    )
    db.add(row)
    await db.flush()
    return row


async def revoke(
    db: AsyncSession, *, dealer_id: uuid.UUID, reason: str = "withdrawn"
) -> bool:
    """Withdraw the live grant. Returns False when there was nothing to revoke.

    Marks the existing row rather than deleting it: the auditable fact is
    "consented, then withdrew", and a deleted row cannot say that.
    """
    row = await _latest(db, dealer_id)
    if row is None or not row.granted or row.revoked_at is not None:
        return False
    row.revoked_at = _now()
    row.revoked_reason = reason[:120]
    await db.flush()
    return True
