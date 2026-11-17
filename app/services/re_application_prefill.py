"""What the file already knows, as answers on the real-estate application.

Franco (2026-11-16): the client never types twice. Every answer onboarding
or the AI intake captured is pulled in and the client fills only the gaps.

Each value carries a tier and a source label the client and the desk see:

- ``A`` answered: the client typed it at onboarding, or the desk entered it.
- ``C`` please check: the AI read it from a document or a chat message; the
  client confirms it ("Looks right") or edits it before it counts.
- ``B`` bureau: from the soft credit check, read-only.

Every value goes through the schema's coercion first. A stored value that is
not what its field claims (a name in a phone field, "Primaary" as a property
type, "XXX" as a score) is never shown as an answer: it is dropped, the field
reads as missing, and the desk sees a warning naming what was dropped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.services import re_application_schema as schema
from app.services import real_estate_vocabulary as vocab

log = logging.getLogger(__name__)

TIER_ANSWERED = "A"
TIER_BUREAU = "B"
TIER_CHECK = "C"

SOURCE_APPLICATION = "Your application"
SOURCE_CHAT = "From our conversation"
SOURCE_DESK = "Entered by your advisor"
SOURCE_SCHEDULE = "Your real estate schedule"
SOURCE_DOCUMENT = "Read from your documents"
SOURCE_CREDIT = "Your soft credit check"
SOURCE_PROPERTY = "Your property address"
SOURCE_OWNERS = "Your ownership details"


@dataclass
class Prefill:
    values: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, dict[str, str]] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    has_coborrower: bool = False

    def put(self, path: str, value: Any, *, tier: str, source: str, raw: Any = None) -> bool:
        """Offer one value; the first good one for a path wins."""
        if path in self.values or value is None or value == "" or value == []:
            return False
        spec = schema.spec_for(path)
        if spec is None:
            return False
        try:
            coerced = schema.coerce_value(spec, value)
        except schema.FieldProblem:
            shown = raw if raw is not None else value
            self.warnings.append({
                "path": path,
                "label": spec["label"],
                "source": source,
                "message": f"The {spec['label'].lower()} on file ({str(shown)[:80]!r}) is not valid, so it was left blank for the client to answer.",
            })
            return False
        if coerced is None:
            return False
        self.values[path] = coerced
        self.provenance[path] = {"tier": tier, "source": source}
        return True

    def body(self) -> dict[str, Any]:
        body = schema.empty_body()
        body["has_coborrower"] = self.has_coborrower
        for path, value in self.values.items():
            schema.set_at(body, path, value)
        return body


_ADDRESS = re.compile(
    r"^\s*(?P<street>[^,]+?)\s*,\s*(?P<city>[^,]+?)\s*,?\s+(?P<state>[A-Za-z]{2})\s*,?\s+(?P<zip>\d{5}(?:-\d{4})?)\s*(?:,\s*(?:USA|US|United States))?\s*$"
)


def split_address(text: Any) -> dict[str, str] | None:
    """'100 EXAMPLE ST, CHARLOTTE NC 28202' → street, city, state, zip (None if unsure)."""
    raw = " ".join(str(text or "").split()).rstrip(",")
    match = _ADDRESS.match(raw)
    if not match:
        return None
    return {key: match.group(key).strip().rstrip(",") for key in ("street", "city", "state", "zip")}


def _tier_for(source: str | None) -> tuple[str, str]:
    if source in {"application_form"}:
        return TIER_ANSWERED, SOURCE_APPLICATION
    if source in {"desk"}:
        return TIER_ANSWERED, SOURCE_DESK
    return TIER_CHECK, SOURCE_CHAT


def _split_name(full: Any) -> tuple[str | None, str | None]:
    parts = " ".join(str(full or "").split()).split(" ")
    if len(parts) < 2:
        return (parts[0] or None) if parts else None, None
    return parts[0], parts[-1]


def _state(intake: Any) -> dict[str, Any]:
    state = getattr(intake, "intake_state", None)
    return state if isinstance(state, dict) else {}


def _from_intake(pre: Prefill, intake: Any) -> None:
    state = _state(intake)
    basics = state.get("funding_review_basics") if isinstance(state.get("funding_review_basics"), dict) else {}
    details = state.get("funding_review_details") if isinstance(state.get("funding_review_details"), dict) else {}
    sources = details.get("sources") if isinstance(details.get("sources"), dict) else {}

    # Loan request
    transaction = vocab.transaction_type_of(basics.get("transaction_type") or getattr(intake, "loan_purpose", None))
    purpose = schema.PURPOSE_BY_TRANSACTION.get(transaction or "")
    if purpose:
        tier, label = (TIER_CHECK, SOURCE_CHAT) if sources.get("transaction_type") == "chat" else (TIER_ANSWERED, SOURCE_APPLICATION)
        pre.put("request.loan_purpose", purpose, tier=tier, source=label)
    for key, path in (
        ("occupancy", "request.occupancy"),
        ("business_purpose", "request.business_purpose"),
        ("purpose_description", "request.purpose_description"),
    ):
        if details.get(key) is not None:
            tier, label = _tier_for(sources.get(key))
            pre.put(path, details[key], tier=tier, source=label)
    if details.get("down_payment_amount") is not None:
        tier, label = _tier_for(sources.get("down_payment_amount"))
        pre.put("request.down_payment", details["down_payment_amount"], tier=tier, source=label)
    from app.services import funding_requests

    amount = funding_requests.intake_requested_amount(intake)
    if amount is None:
        amount = basics.get("requested_amount") or getattr(intake, "requested_loan_amount", None)
    pre.put("request.amount_requested", amount, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)

    # Subject property
    address = split_address(basics.get("target_property_address"))
    if address:
        for key, value in address.items():
            pre.put(f"property.{key}", value, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    elif basics.get("target_property_address"):
        pre.put("property.street", basics["target_property_address"], tier=TIER_CHECK, source=SOURCE_APPLICATION)
    if details.get("property_type"):
        property_type, _ = vocab.fold_property_type(details["property_type"])
        tier, label = _tier_for(sources.get("property_type"))
        if property_type:
            pre.put("property.property_type", property_type, tier=tier, source=label)
        else:
            pre.warnings.append({
                "path": "property.property_type",
                "label": "Property type",
                "source": label,
                "message": f"The property type on file ({str(details['property_type'])[:80]!r}) is not a property type, so it was left blank for the client to answer.",
            })
    value = basics.get("estimated_value_or_purchase_price")
    if value:
        pre.put("property.purchase_price" if purpose == "purchase" else "property.as_is_value", value, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    if basics.get("monthly_rent") is not None:
        pre.put("property.monthly_rent_current", basics["monthly_rent"], tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    if details.get("existing_payoff_amount") is not None:
        tier, label = _tier_for(sources.get("existing_payoff_amount"))
        pre.put("property.payoff_amount" if purpose in {"rate_term_refi", "cash_out_refi", "bridge"} else "property.first_lien_balance",
                details["existing_payoff_amount"], tier=tier, source=label)

    # Borrower, from the person who started the file
    first, last = _split_name(getattr(intake, "full_name", None))
    pre.put("borrowers.borrower.first_name", first, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    pre.put("borrowers.borrower.last_name", last, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    pre.put("borrowers.borrower.email", getattr(intake, "email", None), tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    pre.put("borrowers.borrower.mobile_phone", getattr(intake, "phone", None), tier=TIER_ANSWERED, source=SOURCE_APPLICATION)

    # Credit: the bureau's number wins; otherwise what the client estimated
    credit = state.get("credit_pull") if isinstance(state.get("credit_pull"), dict) else {}
    fico = credit.get("fico")
    from_bureau = isinstance(fico, int) and not isinstance(fico, bool) and 300 <= fico <= 850 and credit.get("fico_source") != "intelligence-proxy"
    if from_bureau:
        pre.put("financial.credit_score_estimate", fico, tier=TIER_BUREAU, source=SOURCE_CREDIT)
    else:
        stated = str(basics.get("estimated_credit_tier") or "")
        match = re.search(r"\d{3}", stated)
        if match:
            pre.put("financial.credit_score_estimate", int(match.group()), tier=TIER_ANSWERED, source=SOURCE_APPLICATION, raw=stated)
        elif stated.strip() and stated.strip() not in vocab.CREDIT_TIERS:
            pre.warnings.append({
                "path": "financial.credit_score_estimate",
                "label": "Estimated middle credit score",
                "source": SOURCE_APPLICATION,
                "message": f"The credit estimate on file ({stated.strip()[:40]!r}) is not a score or a tier, so it was left blank for the client to answer.",
            })

    # The borrowing company, from what the client declared at onboarding
    company = basics.get("investor_name") or getattr(intake, "business_name", None)
    if company:
        pre.put("request.borrowing_as", "entity", tier=TIER_CHECK, source=SOURCE_APPLICATION)
        pre.put("entity.legal_name", company, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    from app.services import business_profile

    declared = business_profile.of_intake(intake)
    desk = state.get("desk_facts") if isinstance(state.get("desk_facts"), dict) else {}
    for key, path in (("street", "entity.street"), ("city", "entity.city"), ("state", "entity.state"), ("zip", "entity.zip"), ("website", "entity.website")):
        source = SOURCE_DESK if f"business_{key}" in desk else SOURCE_APPLICATION
        pre.put(path, declared.get(key), tier=TIER_ANSWERED, source=source)
    phone_source = SOURCE_DESK if "business_phone" in desk else SOURCE_APPLICATION
    pre.put("entity.phone", declared.get("business_phone"), tier=TIER_ANSWERED, source=phone_source, raw=declared.get("business_phone"))
    pre.put("entity.employees", declared.get("employee_count"), tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    if declared.get("description"):
        pre.put("entity.business_type", declared["description"][:300], tier=TIER_CHECK, source=SOURCE_APPLICATION)

    # An owner-occupied property is where the borrower lives.
    if vocab.is_owner_occupied(details.get("occupancy")) and address:
        for key, value in address.items():
            pre.put(f"borrowers.borrower.{key}", value, tier=TIER_CHECK, source=SOURCE_PROPERTY)


def _from_owners(pre: Prefill, owners: list[Any]) -> None:
    live = [owner for owner in owners if getattr(owner, "archived_at", None) is None]
    primary = next((owner for owner in live if getattr(owner, "is_primary", False)), live[0] if live else None)
    others = [owner for owner in live if owner is not primary and getattr(owner, "is_guarantor", False)]
    for role, owner in (("borrower", primary), ("coborrower", others[0] if others else None)):
        if owner is None:
            continue
        if role == "coborrower":
            pre.has_coborrower = True
        for key in ("first_name", "last_name", "email", "dob", "street", "city", "state", "zip"):
            value = getattr(owner, key, None)
            pre.put(f"borrowers.{role}.{key}", value.isoformat() if hasattr(value, "isoformat") else value, tier=TIER_ANSWERED, source=SOURCE_OWNERS)
        pre.put(f"borrowers.{role}.mobile_phone", getattr(owner, "phone", None), tier=TIER_ANSWERED, source=SOURCE_OWNERS)
    if primary is not None and getattr(primary, "ownership_pct", None) is not None:
        pre.put("entity.ownership_pct", primary.ownership_pct, tier=TIER_ANSWERED, source=SOURCE_OWNERS)


def _from_schedule(pre: Prefill, body: dict[str, Any] | None) -> None:
    if not isinstance(body, dict):
        return
    properties = [row for row in body.get("properties") or [] if isinstance(row, dict)]
    mortgages = [row for row in body.get("mortgages") or [] if isinstance(row, dict)]
    street = str(pre.values.get("property.street") or "").casefold()
    subject = next(
        (row for row in properties if street and str(row.get("address") or "").casefold().startswith(street[:12])),
        None,
    )
    if subject is not None:
        pre.put("property.property_type", subject.get("property_type") if subject.get("property_type") != "unknown" else None, tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        pre.put("property.units", subject.get("units"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        pre.put("property.original_purchase_price", subject.get("purchase_cost"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        pre.put("property.original_purchase_date", subject.get("purchase_date"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        pre.put("property.as_is_value", subject.get("estimated_value"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        for monthly, annual in (("monthly_property_tax", "property.annual_taxes"), ("monthly_insurance", "property.annual_insurance")):
            try:
                amount = float(subject.get(monthly)) * 12 if subject.get(monthly) not in (None, "") else None
            except (TypeError, ValueError):
                amount = None
            pre.put(annual, round(amount, 2) if amount is not None else None, tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        pre.put("property.monthly_hoa", subject.get("monthly_hoa"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        liens = [row for row in mortgages if row.get("property_row_id") == subject.get("id") and row.get("status") != "paid_off"]
        if liens:
            first = liens[0]
            pre.put("property.first_lien_balance", first.get("balance"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
            pre.put("property.first_lien_payment", first.get("monthly_payment"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
            pre.put("property.first_lien_rate", first.get("rate"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
            pre.put("property.first_lien_lender", first.get("lender"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
        if len(liens) > 1:
            pre.put("property.second_lien_balance", liens[1].get("balance"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
            pre.put("property.second_lien_payment", liens[1].get("monthly_payment"), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
    others = [row for row in properties if row is not subject]
    if properties and not body.get("no_real_estate"):
        pre.put("experience.properties_owned", len(others), tier=TIER_ANSWERED, source=SOURCE_SCHEDULE)
    rows = []
    for row in others[:50]:
        balance = sum(
            float(item.get("balance") or 0)
            for item in mortgages
            if item.get("property_row_id") == row.get("id") and item.get("status") != "paid_off"
        )
        payment = sum(
            float(item.get("monthly_payment") or 0)
            for item in mortgages
            if item.get("property_row_id") == row.get("id") and item.get("status") != "paid_off"
        )
        address = ", ".join(str(row.get(key)) for key in ("address", "city", "state", "postal_code") if row.get(key))
        if address:
            rows.append({
                "address": address,
                "estimated_value": row.get("estimated_value"),
                "mortgage_balance": round(balance, 2) if balance else None,
                "monthly_payment": round(payment, 2) if payment else None,
                "monthly_rent": row.get("monthly_gross_rent"),
            })
    if rows and "experience.other_properties" not in pre.values:
        clean, _ = schema.coerce({"experience": {"other_properties": rows}})
        pre.values["experience.other_properties"] = clean["experience"]["other_properties"]
        pre.provenance["experience.other_properties"] = {"tier": TIER_ANSWERED, "source": SOURCE_SCHEDULE}


_FACT_PATHS = {
    "borrower_email": "borrowers.borrower.email",
    "ein": "entity.ein",
    "tax_id": "entity.ein",
    "entity_type": "entity.entity_type",
    "formation_state": "entity.state_of_formation",
    "state_of_formation": "entity.state_of_formation",
    "formation_date": "entity.date_established",
    "date_established": "entity.date_established",
    "business_email": "entity.email",
    "legal_entity_name": "entity.legal_name",
}

_ENTITY_TYPES = {
    "llc": "llc", "limited liability company": "llc", "corporation": "corporation", "c corporation": "corporation",
    "inc": "corporation", "s corporation": "s_corporation", "s corp": "s_corporation", "partnership": "partnership",
    "sole proprietorship": "sole_proprietorship", "trust": "trust",
}


def _from_facts(pre: Prefill, facts: list[Any]) -> None:
    from app.services.extracted_facts import canonical_field_key

    for fact in facts:
        if getattr(fact, "status", None) == "rejected":
            continue
        path = _FACT_PATHS.get(canonical_field_key(getattr(fact, "field_key", None)))
        if not path:
            continue
        raw = (getattr(fact, "value", None) or {}).get("value") if isinstance(getattr(fact, "value", None), dict) else getattr(fact, "value", None)
        value = raw
        if path == "entity.entity_type":
            value = _ENTITY_TYPES.get(" ".join(str(raw or "").casefold().replace(".", "").split()))
        tier = TIER_ANSWERED if getattr(fact, "status", None) == "accepted" else TIER_CHECK
        pre.put(path, value, tier=tier, source=SOURCE_DOCUMENT, raw=raw)


def _from_profile(pre: Prefill, profile: Any) -> None:
    entity_type = _ENTITY_TYPES.get(" ".join(str(getattr(profile, "entity_type", None) or "").casefold().replace(".", "").split()))
    pre.put("entity.entity_type", entity_type, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
    uses = getattr(profile, "use_of_funds", None)
    if isinstance(uses, list) and uses:
        category_to_use = {
            "real_estate": "property_purchase",
            "working_capital": "business_working_capital",
            "inventory": "inventory_equipment",
            "equipment": "inventory_equipment",
            "debt_refinance": "debt_consolidation",
            "closing_fees": "other",
            "other": "other",
        }
        chosen = [category_to_use[item.get("category")] for item in uses if isinstance(item, dict) and item.get("category") in category_to_use]
        pre.put("request.use_of_funds", list(dict.fromkeys(chosen)), tier=TIER_ANSWERED, source=SOURCE_APPLICATION)
        detail = "; ".join(str(item.get("label")) for item in uses if isinstance(item, dict) and item.get("label"))
        pre.put("request.use_of_funds_detail", detail, tier=TIER_ANSWERED, source=SOURCE_APPLICATION)


async def _schedule_body(db: Any, profile: Any) -> dict[str, Any] | None:
    from app.models.financial_form_version import FinancialFormDraft, FinancialFormVersion
    from app.services.financial_workspace import existing_family

    family = await existing_family(db, profile.id, "real_estate_schedule")
    if family is None:
        return None
    if family.current_version_id:
        version = await db.get(FinancialFormVersion, family.current_version_id)
        if version is not None:
            return version.body
    draft = await db.get(FinancialFormDraft, family.id)
    return draft.body if draft is not None else None


async def build(db: Any, profile: Any, *, intake: Any = None) -> Prefill:
    """Everything the file knows, folded onto the application's fields."""
    from sqlalchemy import select

    from app.models.application_profile import ApplicationExtractedFact, ApplicationOwner

    pre = Prefill()
    if intake is not None:
        _from_intake(pre, intake)
    owners = list(
        (await db.execute(select(ApplicationOwner).where(ApplicationOwner.profile_id == profile.id).order_by(ApplicationOwner.created_at))).scalars().all()
    ) if hasattr(ApplicationOwner, "created_at") else list(
        (await db.execute(select(ApplicationOwner).where(ApplicationOwner.profile_id == profile.id))).scalars().all()
    )
    _from_owners(pre, owners)
    _from_profile(pre, profile)
    try:
        _from_schedule(pre, await _schedule_body(db, profile))
    except Exception:  # noqa: BLE001 - a schedule that cannot be read leaves those fields to the client
        log.exception("real-estate application prefill: schedule unreadable for profile %s", profile.id)
    facts = list(
        (await db.execute(select(ApplicationExtractedFact).where(ApplicationExtractedFact.profile_id == profile.id))).scalars().all()
    )
    _from_facts(pre, facts)
    return pre
