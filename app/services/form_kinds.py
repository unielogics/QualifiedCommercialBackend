"""One registry of the online forms a requirement can collect.

A requirement of kind ``form`` names its form in ``collection_config.form_kind``
and, when the form ends in a signature, carries a ``signature`` block:

    {"form_kind": "re_application",
     "signature": {"required": true,
                   "signers": ["borrower", "coborrower"],
                   "consents": ["business_purpose", "accuracy",
                                "credit_authorization", "information_sharing", "esign"],
                   "satisfies": ["signed_credit_authorization"],
                   "credit_reuse_days": 30}}

Franco (2026-11-16): the workflow is configuration, not code. A program's
requirements, one file's checklist (the requirement library, a desk request)
or a lender's offering can carry such a row, and materializing it switches on
everything the form needs: the link, the room row, the chat's "Complete now",
the desk read-back and, when configured, the signature step.

Before this module the same facts lived in eight hand-kept lists (the
requirement library, readiness slot creation, the chat's links and actions,
the form-link email, file requests, form read-backs and the public link
gate). They now read from here. The financial-sheet internals (sheet layout,
xlsx export, worksheets) keep their own lists on purpose: those describe
sheets, not requirements.

Writes are strict (an unknown kind, consent or satisfier is refused with a
message); reads are tolerant (a stored config is returned without what this
registry no longer knows), because a read that refuses an old row takes the
page down with it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

ENGINE_FINANCIAL = "financial_workspace"
ENGINE_CLIENT_FORM = "client_form"

SIGNER_ROLES: tuple[str, ...] = ("borrower", "coborrower")
MAX_CREDIT_REUSE_DAYS = 365


@dataclass(frozen=True)
class FormKind:
    key: str
    label: str
    #: The checklist row's category in the room.
    slot_category: str
    #: What the filed PDF's analysis is classified as.
    classification: str | None
    engine: str
    #: Requirement keys that have always collected this form.
    requirement_keys: tuple[str, ...] = ()
    #: The client chat may offer "Complete now" for it.
    chat_complete_now: bool = True
    #: The desk can add it to one file from the requirement library.
    requestable: bool = True
    #: A requirement may configure a signature at its end.
    signable: bool = False
    default_signature: Mapping[str, Any] | None = None
    #: The public form link may open it (financial kinds go through their own routes).
    link_kind: bool = True
    #: The requirement category a new row for this form is filed under.
    category: str = "financials"

    def read(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "slot_category": self.slot_category,
            "category": self.category,
            "engine": self.engine,
            "requirement_keys": list(self.requirement_keys),
            "chat_complete_now": self.chat_complete_now,
            "requestable": self.requestable,
            "signable": self.signable,
            "default_signature": dict(self.default_signature) if self.default_signature else None,
        }


_KINDS: tuple[FormKind, ...] = (
    FormKind(
        key="pfs",
        label="Personal financial statement",
        slot_category="Personal Financials",
        classification="personal_financial_statement",
        engine=ENGINE_FINANCIAL,
        requirement_keys=("owner_personal_financial_statement",),
    ),
    FormKind(
        key="debt_schedule",
        label="Business debt schedule",
        slot_category="Debts",
        classification="debt_schedule",
        engine=ENGINE_FINANCIAL,
        requirement_keys=("business_debt_schedule", "refinance_debt_schedule"),
    ),
    FormKind(
        key="p_and_l",
        label="Profit and loss statement",
        slot_category="Financials",
        classification="current_p_and_l",
        engine=ENGINE_FINANCIAL,
        requirement_keys=("ytd_p_and_l_balance_sheet", "ytd_profit_and_loss"),
    ),
    FormKind(
        key="balance_sheet",
        label="Balance sheet",
        slot_category="Financials",
        classification="balance_sheet",
        engine=ENGINE_FINANCIAL,
        requirement_keys=("ytd_p_and_l_balance_sheet",),
    ),
    FormKind(
        key="real_estate_schedule",
        label="Real estate schedule",
        slot_category="Real estate",
        classification="real_estate_schedule",
        engine=ENGINE_FINANCIAL,
        requirement_keys=("real_estate_schedule",),
        category="property_data",
    ),
    FormKind(
        key="operating_plan",
        label="Operating expenses and earnings plan",
        slot_category="Financials",
        classification="operating_plan",
        engine=ENGINE_FINANCIAL,
        requirement_keys=("operating_expenses_and_earnings_plan",),
    ),
)

KINDS: dict[str, FormKind] = {kind.key: kind for kind in _KINDS}


def register(kind: FormKind) -> FormKind:
    """Add a kind (the application's form registers itself beside its schema)."""
    KINDS[kind.key] = kind
    return kind


def get(key: Any) -> FormKind | None:
    return KINDS.get(str(key)) if key else None


def labels() -> dict[str, str]:
    return {kind.key: kind.label for kind in KINDS.values()}


def financial_kinds() -> frozenset[str]:
    """Kinds stored in the versioned financial-form engine."""
    return frozenset(kind.key for kind in KINDS.values() if kind.engine == ENGINE_FINANCIAL)


def link_kinds() -> frozenset[str]:
    """Kinds a single-form public link may open."""
    return frozenset(kind.key for kind in KINDS.values() if kind.link_kind)


def kind_for_classification(classification: Any) -> str | None:
    for kind in KINDS.values():
        if kind.classification and kind.classification == classification:
            return kind.key
    return None


def kinds_by_requirement_key() -> dict[str, tuple[str, ...]]:
    """Requirement key → the forms that answer it, in registry order."""
    out: dict[str, list[str]] = {}
    for kind in KINDS.values():
        for key in kind.requirement_keys:
            out.setdefault(key, []).append(kind.key)
    return {key: tuple(kinds) for key, kinds in out.items()}


def kinds_for_requirement(key: Any, canonical_key: Any = None, config: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """The forms a requirement collects: its own configured form first, then its key's."""
    configured = (config or {}).get("form_kind") if isinstance(config, Mapping) else None
    if configured and get(configured):
        return (str(configured),)
    by_key = kinds_by_requirement_key()
    return by_key.get(str(canonical_key or "")) or by_key.get(str(key or ""), ())


def requirement_key_for(kind: Any) -> str | None:
    """The requirement a form answers on the checklist when nothing else says."""
    found = get(kind)
    return found.requirement_keys[0] if found and found.requirement_keys else None


def slot_category(kind: Any, fallback: str | None = None) -> str | None:
    found = get(kind)
    return found.slot_category if found else fallback


# ---------------------------------------------------------------------------
# Consents a signature can carry. Each is versioned and hashed so the signing
# certificate can say exactly which words were agreed to.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Consent:
    key: str
    label: str
    version: str
    text: str
    #: Draft wording waits for the CEO's approval before signing goes live.
    draft: bool = False
    roles: tuple[str, ...] = field(default=SIGNER_ROLES)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def read(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "version": self.version,
            "text": self.text,
            "sha256": self.sha256,
            "draft": self.draft,
        }


def _credit_authorization() -> Consent:
    from app.services import document_signature

    return Consent(
        key="credit_authorization",
        label="Credit authorization",
        version=document_signature.CREDIT_AUTHORIZATION_DOCUMENT_VERSION,
        text=document_signature.credit_authorization_document_text(),
    )


_DRAFT_VERSION = "draft-2026-11-17"

_CONSENTS: dict[str, Consent] = {
    "business_purpose": Consent(
        key="business_purpose",
        label="Business purpose",
        version=_DRAFT_VERSION,
        draft=True,
        text=(
            "I certify that the financing requested is for business or investment purposes and not for personal, "
            "family or household purposes. If the property securing it is my residence, I certify the proceeds will "
            "be used only for the business purposes stated in this application."
        ),
    ),
    "accuracy": Consent(
        key="accuracy",
        label="Accuracy",
        version=_DRAFT_VERSION,
        draft=True,
        text=(
            "I certify that the information in this application is true and complete. I understand Qualified "
            "Commercial, its partners and any prospective lender will rely on it, and that knowingly false "
            "statements may lead to denial and may be a crime under federal law."
        ),
    ),
    "information_sharing": Consent(
        key="information_sharing",
        label="Information sharing",
        version="re-app-sharing-v1",
        draft=True,
        text=(
            "I authorize Qualified Commercial LLC to share this application, the documents in my file and "
            "information about my request with its lending partners and associates, and with any prospective "
            "lender, investor, servicer, title company, attorney or appraiser involved in arranging or closing the "
            "financing I requested, so they can review and process it. They may contact me about this request. This "
            "does not authorize a hard credit inquiry or guarantee financing. I may revoke it in writing for future "
            "sharing."
        ),
    ),
    "esign": Consent(
        key="esign",
        label="Electronic signature",
        version="esign-2026-07-25-1",
        text="I consent to use electronic records and signatures under the U.S. E-SIGN Act and UETA.",
    ),
}

CONSENT_KEYS: tuple[str, ...] = ("business_purpose", "accuracy", "credit_authorization", "information_sharing", "esign")


def consent(key: Any) -> Consent | None:
    if key == "credit_authorization":
        return _credit_authorization()
    return _CONSENTS.get(str(key)) if key else None


def consents() -> list[Consent]:
    return [found for key in CONSENT_KEYS if (found := consent(key)) is not None]


#: Other requirements a signed form may also satisfy, and the consent each needs.
SATISFIERS: dict[str, str] = {"signed_credit_authorization": "credit_authorization"}


# ---------------------------------------------------------------------------
# The collection_config contract.
# ---------------------------------------------------------------------------


class FormConfigError(ValueError):
    """A requirement's form configuration cannot be saved as written."""


def _unique(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value)
        if text not in out:
            out.append(text)
    return out


def _signature(raw: Any, kind: FormKind | None, *, strict: bool) -> dict[str, Any] | None:
    if raw in (None, {}, False):
        return None
    if not isinstance(raw, Mapping):
        if strict:
            raise FormConfigError("The signature settings must be an object")
        return None
    if not raw.get("required", True):
        return None
    if kind is None or not kind.signable:
        if strict:
            raise FormConfigError(
                f"{kind.label if kind else 'This form'} cannot end in a signature"
            )
        return None
    defaults = dict(kind.default_signature or {})
    signers = raw.get("signers", defaults.get("signers", ["borrower"]))
    consent_keys = raw.get("consents", defaults.get("consents", ["esign"]))
    satisfies = raw.get("satisfies", defaults.get("satisfies", []))
    reuse = raw.get("credit_reuse_days", defaults.get("credit_reuse_days"))
    if not isinstance(signers, list) or not isinstance(consent_keys, list) or not isinstance(satisfies, list):
        if strict:
            raise FormConfigError("signers, consents and satisfies must be lists")
        signers = signers if isinstance(signers, list) else ["borrower"]
        consent_keys = consent_keys if isinstance(consent_keys, list) else ["esign"]
        satisfies = satisfies if isinstance(satisfies, list) else []
    unknown_signers = [role for role in signers if role not in SIGNER_ROLES]
    unknown_consents = [key for key in consent_keys if key not in CONSENT_KEYS]
    unknown_satisfiers = [key for key in satisfies if key not in SATISFIERS]
    if strict and (unknown_signers or unknown_consents or unknown_satisfiers):
        problems = [
            f"unknown signer {', '.join(map(str, unknown_signers))}" if unknown_signers else "",
            f"unknown consent {', '.join(map(str, unknown_consents))}" if unknown_consents else "",
            f"unknown requirement {', '.join(map(str, unknown_satisfiers))}" if unknown_satisfiers else "",
        ]
        raise FormConfigError("The signature settings name " + "; ".join(part for part in problems if part))
    signers = _unique(role for role in signers if role in SIGNER_ROLES) or ["borrower"]
    if "borrower" not in signers:
        signers.insert(0, "borrower")
    consent_keys = _unique(key for key in consent_keys if key in CONSENT_KEYS)
    if "esign" not in consent_keys:
        consent_keys.append("esign")
    satisfies = _unique(key for key in satisfies if key in SATISFIERS)
    missing = [key for key in satisfies if SATISFIERS[key] not in consent_keys]
    if missing:
        if strict:
            raise FormConfigError(
                "A signature can only count as " + ", ".join(missing) + " when it carries "
                + ", ".join(SATISFIERS[key].replace("_", " ") for key in missing)
            )
        satisfies = [key for key in satisfies if key not in missing]
    if reuse is not None:
        if isinstance(reuse, bool) or not isinstance(reuse, int) or not 0 <= reuse <= MAX_CREDIT_REUSE_DAYS:
            if strict:
                raise FormConfigError(f"credit_reuse_days must be a whole number from 0 to {MAX_CREDIT_REUSE_DAYS}")
            reuse = None
    out: dict[str, Any] = {"required": True, "signers": signers, "consents": consent_keys, "satisfies": satisfies}
    if reuse is not None:
        out["credit_reuse_days"] = reuse
    return out


def normalize_collection_config(
    config: Any,
    *,
    requirement_kind: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """The config as the registry understands it.

    Only the form keys are looked at; every other key passes through untouched.
    """
    if not isinstance(config, Mapping):
        if strict and config not in (None, {}):
            raise FormConfigError("collection_config must be an object")
        return {}
    out = dict(config)
    kind_key = out.get("form_kind")
    kind = get(kind_key)
    if kind_key and kind is None:
        if strict:
            raise FormConfigError(f"Unknown online form {kind_key!r}")
    if kind_key and requirement_kind not in (None, "form") and strict:
        raise FormConfigError("Only a form requirement can name an online form")
    if "signature" in out:
        signature = _signature(out.get("signature"), kind, strict=strict)
        if signature is None:
            out.pop("signature", None)
        else:
            out["signature"] = signature
    return out


def signature_of(source: Any) -> dict[str, Any] | None:
    """The normalized signature block of a requirement (or a config), if one is required."""
    config = source if isinstance(source, Mapping) else getattr(source, "collection_config", None)
    if not isinstance(config, Mapping) or not config.get("signature"):
        return None
    normalized = normalize_collection_config(config, strict=False)
    return normalized.get("signature")


def merge_signatures(blocks: Iterable[Mapping[str, Any] | None]) -> dict[str, Any] | None:
    """When several sources require the same form, signing honours all of them:
    the union of signers, consents and satisfiers, and the smallest reuse window."""
    merged: dict[str, Any] | None = None
    for block in blocks:
        if not block:
            continue
        if merged is None:
            merged = {"required": True, "signers": [], "consents": [], "satisfies": []}
        for key in ("signers", "consents", "satisfies"):
            merged[key] = _unique([*merged[key], *(block.get(key) or [])])
        reuse = block.get("credit_reuse_days")
        if isinstance(reuse, int) and not isinstance(reuse, bool):
            current = merged.get("credit_reuse_days")
            merged["credit_reuse_days"] = reuse if current is None else min(current, reuse)
    return merged


def catalog() -> dict[str, Any]:
    """What the desk's editors read: every kind, every consent, every satisfier."""
    return {
        "kinds": [kind.read() for kind in KINDS.values()],
        "consents": [found.read() for found in consents()],
        "satisfiers": [{"key": key, "consent": needed} for key, needed in SATISFIERS.items()],
        "signer_roles": list(SIGNER_ROLES),
        "max_credit_reuse_days": MAX_CREDIT_REUSE_DAYS,
    }
