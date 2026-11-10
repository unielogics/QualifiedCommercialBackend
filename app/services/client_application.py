"""Client-owned initial application answers, behind the existing room PIN."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException

from app.schemas.client_application import ClientApplicationFields, ClientApplicationRead
from app.services import business_profile, funding_requests


def funding_need_editable(intake, profile) -> bool:
    from app.services.mca_funding_policy import PROGRAM_KEY

    state = intake.intake_state or {}
    return not (getattr(profile, "funding_category", None) == PROGRAM_KEY
                or state.get("funding_intent") == PROGRAM_KEY
                or (state.get("main_street_details") or {}).get("intent") == PROGRAM_KEY)


def requested_amount_editable(intake, profile) -> bool:
    if funding_requests.current_for_profile(profile) or funding_requests.current_for_intake_snapshot(intake):
        return False
    return not (
        (intake.intake_state or {}).get("client_application_submitted_at")
        and funding_requests.intake_requested_amount(intake) is not None
    )


def read_application(intake, profile) -> ClientApplicationRead:
    state = intake.intake_state or {}
    basics = state.get("funding_review_basics") or {}
    declared = business_profile.of_intake(intake)
    amount = funding_requests.current_for_profile(profile) or funding_requests.intake_requested_amount(intake)
    # Older files may contain incomplete contact/profile answers. Reading must
    # show those answers for correction instead of validating them as new input.
    return ClientApplicationRead.model_construct(
        vertical=profile.vertical,
        full_name=intake.full_name or None,
        email=intake.email or None,
        phone=intake.phone,
        business_name=intake.business_name,
        loan_purpose=intake.loan_purpose,
        requested_loan_amount=float(amount) if amount is not None else None,
        requested_amount_editable=requested_amount_editable(intake, profile),
        funding_need_editable=funding_need_editable(intake, profile),
        years_in_business=state.get("years_in_business"),
        business_website="N/A" if declared["website_none"] else declared["website"],
        business_description=declared["description"],
        **{f"business_{field}": declared[field] for field in ("street", "city", "state", "zip")},
        target_property_address=basics.get("target_property_address"),
        transaction_type=basics.get("transaction_type"),
        estimated_value_or_purchase_price=basics.get("estimated_value_or_purchase_price"),
        monthly_rent=basics.get("monthly_rent"),
        industry=declared["industry"],
        funding_need=(state.get("main_street_details") or {}).get("intent"),
        submitted_at=datetime.fromisoformat(state["client_application_submitted_at"]) if state.get("client_application_submitted_at") else None,
    )


def validate_submission(values: ClientApplicationFields, vertical: str) -> None:
    required = {
        "full_name": "Full name", "email": "Email", "phone": "Mobile number",
        "business_website": "Business website (or N/A)",
        "business_description": "Business description", "loan_purpose": "Funding purpose",
        "requested_loan_amount": "Requested amount",
        "business_street": "Business street address", "business_city": "Business city",
        "business_state": "Business state", "business_zip": "Business ZIP code", "industry": "Industry",
    }
    if vertical == "real_estate":
        required.update(target_property_address="Property address", transaction_type="Transaction type", estimated_value_or_purchase_price="Property value / purchase price")
    else:
        required["business_name"] = "Legal business name"
    if vertical == "main_street":
        from app.services.main_street_programs import intent_kind

        required.update(industry="Industry", funding_need="Funding need")
        if values.funding_need and intent_kind(values.funding_need) != "lending":
            required.pop("loan_purpose")
            required.pop("requested_loan_amount")
    missing = [label for key, label in required.items() if getattr(values, key) in (None, "")]
    if missing:
        raise HTTPException(422, "Complete these application fields: " + ", ".join(missing) + ".")


def apply_application(intake, profile, link, client, values: ClientApplicationFields, *, submit: bool) -> None:
    """Keep the intake, CRM identity and room recipient on the same answers."""
    if not funding_need_editable(intake, profile) and values.funding_need != "merchant_cash_advance":
        raise HTTPException(403, "Ask your advisor to review a change from this MCA product.")
    if not requested_amount_editable(intake, profile):
        # A profile-first row lock in the route keeps a reviewed correction
        # stable while these borrower answers are saved. Only an initial draft
        # amount may change freely; submitted/reviewed requests use staff review.
        current = funding_requests.current_for_profile(profile)
        proposed = Decimal(str(values.requested_loan_amount)) if values.requested_loan_amount is not None else None
        if current is not None and proposed != current:
            raise HTTPException(403, "Ask your advisor to review changes to the requested amount.")
        if current is None or funding_requests.current_for_intake_snapshot(intake) is not None:
            funding_requests.require_initial_or_unchanged_amount(intake, values.requested_loan_amount)
    governed_request = bool(funding_requests.current_for_profile(profile) or funding_requests.current_for_intake_snapshot(intake))
    data = values.model_dump()
    for key in ("full_name", "phone", "business_name", "loan_purpose"):
        setattr(intake, key, data[key])
    if not governed_request:
        intake.requested_loan_amount = values.requested_loan_amount
    intake.email = str(values.email or "").lower()
    state = dict(intake.intake_state or {})
    state["business_profile"] = business_profile.record(values) or {}
    state["years_in_business"] = values.years_in_business
    if profile.vertical == "main_street":
        details = dict(state.get("main_street_details") or {})
        details.update(industry=values.industry, intent=values.funding_need, years_in_business=values.years_in_business)
        state["main_street_details"] = details
        state["funding_intent"] = values.funding_need
    from app.services.application_profiles import canonical_funding_category

    profile.funding_category = canonical_funding_category(values.funding_need if profile.vertical == "main_street" else values.loan_purpose)
    if profile.vertical == "real_estate":
        basics = dict(state.get("funding_review_basics") or {})
        for key in ("target_property_address", "transaction_type", "estimated_value_or_purchase_price", "monthly_rent"):
            basics[key] = data[key]
        basics["investor_name"] = values.business_name
        if not governed_request:
            basics["requested_amount"] = values.requested_loan_amount
        state["funding_review_basics"] = basics
        assets = [dict(row) for row in intake.asset_rows or []]
        if values.target_property_address or values.estimated_value_or_purchase_price:
            if not assets:
                assets.append({})
            assets[0].update(address=values.target_property_address, estimated_property_value=values.estimated_value_or_purchase_price)
            intake.asset_rows = assets
    if submit:
        state["client_application_submitted_at"] = datetime.now(UTC).isoformat()
    intake.intake_state = state
    if client is not None:
        client.name = values.full_name[:160]
        client.email = intake.email or None
        client.phone = values.phone
        client.lead_intake = {**(client.lead_intake or {}), "business_name": values.business_name}
    link.recipient_name = values.full_name
    link.recipient_email = intake.email or None


def sync_seeded_owner(owner, previous: ClientApplicationRead, values: ClientApplicationFields) -> None:
    parts = (previous.full_name or "").strip().split(None, 1)
    seeded_name = f"{(parts[0] if parts else 'Owner')[:80]} {(parts[1] if len(parts) > 1 else 'Unknown')[:80]}"
    matches_seed = owner.full_name == previous.full_name or (getattr(owner, "backfill_needs_review", False) and owner.full_name == seeded_name)
    if not owner.is_primary or not matches_seed or owner.has_invite or owner.credit_complete or getattr(owner, "credit_pull_id", None):
        return
    parts = values.full_name.strip().split(None, 1)
    owner.first_name = parts[0][:80]
    owner.last_name = (parts[1] if len(parts) > 1 else "Unknown")[:80]
    if (owner.email or "") == (previous.email or ""):
        owner.email = str(values.email).lower() if values.email else None
    if (owner.phone or "") == (previous.phone or ""):
        owner.phone = values.phone


async def sync_main_street_documents(db, intake, previous: ClientApplicationRead, values: ClientApplicationFields) -> None:
    if (previous.industry, previous.funding_need) == (values.industry, values.funding_need):
        return
    from sqlalchemy import select

    from app.models.bucket import BucketRequestedDocument
    from app.services.main_street_programs import documents_for

    old_names = {row["name"] for row in documents_for(previous.funding_need, previous.industry)}
    templates = {row["name"]: row for row in documents_for(values.funding_need, values.industry)}
    existing = list((await db.execute(select(BucketRequestedDocument).where(BucketRequestedDocument.bucket_id == intake.bucket_id))).scalars().all())
    by_name = {row.name: row for row in existing}
    for row in existing:
        if not row.is_custom and row.name in old_names and row.name not in templates:
            # Preserve requests and their evidence as optional supporting items.
            row.required = False
    for name, template in templates.items():
        row = by_name.get(name)
        if row is None:
            db.add(BucketRequestedDocument(bucket_id=intake.bucket_id, name=name, category=template["category"], description=template.get("description", ""), required=bool(template.get("required", True)), allow_multiple_files=bool(template.get("allow_multiple_files", False)), status="requested", is_custom=False))
        elif not row.is_custom:
            row.required = bool(template.get("required", True))
