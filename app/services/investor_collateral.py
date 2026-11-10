"""Proposed collateral is an audited disclosure, never a title/lien approval."""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile
from app.models.financial_form_version import FinancialFormFamily, FinancialFormVersion
from app.schemas.investor_collateral import CollateralPatch, CollateralRead

EDIT_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}


def require_staff(user):
    if user.role not in EDIT_ROLES:
        raise HTTPException(403, "Only authorized underwriting staff can manage proposed collateral")


def stored(profile):
    value = getattr(profile, "investor_collateral", None)
    return deepcopy(value) if isinstance(value, dict) else {}


def _amount(value):
    try:
        result = Decimal(str(value))
        return float(result) if result.is_finite() and result >= 0 else None
    except (ValueError, TypeError, InvalidOperation):
        return None


async def property_options(db, profile):
    rows = (await db.execute(select(FinancialFormFamily, FinancialFormVersion).join(
        FinancialFormVersion, FinancialFormVersion.id == FinancialFormFamily.current_version_id).where(
        FinancialFormFamily.profile_id == profile.id, FinancialFormFamily.kind == "real_estate_schedule",
        FinancialFormVersion.status == "published"))).all()
    result = []
    for _family, version in rows:
        body = version.body or {}
        for row in body.get("properties") or []:
            mortgages = [item for item in body.get("mortgages") or [] if str(item.get("property_row_id")) == str(row.get("id")) and item.get("status") != "paid_off"]
            balances = [_amount(item.get("balance")) for item in mortgages]
            liens = sum(balances) if balances and all(value is not None for value in balances) else 0 if row.get("mortgage_status") == "free_clear" and not mortgages else None
            address = {"street": str(row.get("address") or ""), "city": str(row.get("city") or ""), "state": str(row.get("state") or ""), "postal_code": str(row.get("postal_code") or "")}
            result.append({"id": str(row.get("id")), "label": ", ".join(value for value in address.values() if value), "address": address,
                "owner_name": row.get("legal_owner") or body.get("owner_name"), "property_type": row.get("property_type"),
                "estimated_value": _amount(row.get("estimated_value")), "existing_liens_total": liens,
                "currency": body.get("currency"), "currency_needs_confirmation": not bool(body.get("currency")), "version_id": str(version.id), "version_number": version.version_number,
                "valuation_date": row.get("valuation_date"), "source_file_ids": [str(version.pdf_file_id)] if version.pdf_file_id else []})
    return result


async def read(db, profile, user=None):
    from app.services.investor_report import safe_label
    from app.services.provenance import is_internal_package_output
    value = stored(profile)
    files = list((await db.execute(select(BucketFile).where(BucketFile.bucket_id == profile.primary_bucket_id,
        BucketFile.status == "uploaded", BucketFile.deleted_at.is_(None)).order_by(BucketFile.created_at.desc()))).scalars().all()) if profile.primary_bucket_id else []
    warnings = ["Staff-disclosed collateral only. Title, valuation, lien priority and available lending capacity require review."]
    for row in value.get("properties") or []:
        amount, estimated, liens = (_amount(row.get(key)) for key in ("available_lien_amount", "estimated_value", "existing_liens_total"))
        if amount is not None and estimated is not None and liens is not None and amount > max(0, estimated - liens):
            warnings.append("A declared available lien amount exceeds estimated value less disclosed liens; reconcile valuation and lien evidence.")
    return CollateralRead(profile_id=profile.id, enabled=value.get("enabled", False), properties=value.get("properties") or [],
        revision=value.get("revision", 0), updated_at=value.get("updated_at"), updated_by_user_id=value.get("updated_by_user_id"),
        can_edit=bool(user and user.role in EDIT_ROLES), property_options=await property_options(db, profile),
        source_file_options=[{"id": str(file.id), "label": safe_label(file.file_name)} for file in files if not is_internal_package_output(file)],
        warnings=warnings)


async def update(db, profile, payload: CollateralPatch, user):
    require_staff(user)
    locked = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one()
    before = stored(locked)
    if int(before.get("revision", 0)) != payload.expected_revision:
        raise HTTPException(409, "Collateral changed; reload the saved details before saving")
    source_ids = {file_id for row in payload.properties for file_id in row.source_file_ids}
    if source_ids:
        from app.services.provenance import is_internal_package_output
        scoped = {file.id for file in (await db.execute(select(BucketFile).where(BucketFile.id.in_(source_ids),
            BucketFile.bucket_id == locked.primary_bucket_id, BucketFile.status == "uploaded", BucketFile.deleted_at.is_(None)))).scalars().all() if not is_internal_package_output(file)}
        if scoped != source_ids:
            raise HTTPException(422, "Link only available documents from this application")
    valid_properties = {item["id"] for item in await property_options(db, locked)}
    retained_properties = {str(row.get("real_estate_property_id")) for row in before.get("properties", [])}
    if any(row.real_estate_property_id and str(row.real_estate_property_id) not in valid_properties | retained_properties for row in payload.properties):
        raise HTTPException(422, "Choose a property from this file's published real estate schedule")
    properties = [row.model_dump(mode="json") for row in payload.properties]
    # Toggling inclusion off must not silently erase the previously entered rows.
    if not payload.enabled and not properties and before.get("properties"):
        properties = deepcopy(before["properties"])
    locked.investor_collateral = {"enabled": payload.enabled, "properties": properties,
        "revision": payload.expected_revision + 1, "updated_at": datetime.now(UTC).isoformat(), "updated_by_user_id": str(user.id)}
    from app.services.application_profiles import log_profile_action
    await log_profile_action(db, locked, user, "investor_collateral.update", "Updated proposed real estate collateral",
        metadata={"before": before, "after": locked.investor_collateral, "revision": payload.expected_revision + 1})
    await db.flush()
    return await read(db, locked, user)
