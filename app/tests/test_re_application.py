"""The real-estate loan application: schema, prefill, the live form and its link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket
from app.models.client_form import ClientFormVersion
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import client_forms, form_kinds, form_link_codes
from app.services import re_application_prefill as prefill
from app.services import re_application_schema as schema
from app.tests._sqlite_schema import sqlite_session, sqlite_template

HELOC_STATE = {
    "messages": [],
    "funding_review_basics": {
        "transaction_type": "Home equity (HELOC)",
        "target_property_address": "100 EXAMPLE ST, CHARLOTTE NC 28202",
        "requested_amount": 300000,
        "estimated_value_or_purchase_price": 550000,
        "estimated_credit_tier": "XXX",
        "investor_name": "Sample Holdings",
    },
    "funding_review_details": {
        "property_type": "Primaary",
        "occupancy": "primary_residence",
        "business_purpose": True,
        "existing_payoff_amount": 184321.5,
        "sources": {"occupancy": "application_form", "business_purpose": "application_form", "existing_payoff_amount": "chat"},
    },
    "business_profile": {"business_phone": "Jordan Sample", "street": "100 EXAMPLE ST", "city": "CHARLOTTE", "state": "NC", "zip": "28202"},
}


# --- the schema -------------------------------------------------------------------


def test_a_heloc_on_a_home_never_asks_purchase_rehab_or_rent_questions() -> None:
    body, _ = schema.coerce({"request": {"loan_purpose": "heloc", "occupancy": "primary_residence"}})
    paths = {path for _, path, _ in schema.applicable_paths(body)}
    for absent in ("request.down_payment", "property.purchase_price", "property.rehab_budget", "property.arv", "property.monthly_rent_current", "property.payoff_amount"):
        assert absent not in paths
    for present in ("property.first_lien_balance", "request.purpose_description", "borrowers.borrower.employer_name", "financial.exit_strategy"):
        assert present in paths


def test_a_purchase_of_a_rental_asks_rent_and_down_payment_not_employment() -> None:
    body, _ = schema.coerce({"request": {"loan_purpose": "purchase", "occupancy": "investment", "program_interest": "fix_and_flip"}})
    paths = {path for _, path, _ in schema.applicable_paths(body)}
    assert {"request.down_payment", "property.purchase_price", "property.rehab_budget", "property.arv", "property.monthly_rent_current"} <= paths
    assert "borrowers.borrower.employer_name" not in paths and "request.purpose_description" not in paths


def test_bad_values_are_refused_never_stored() -> None:
    body, problems = schema.coerce({
        "property": {"property_type": "Primaary", "first_lien_balance": "184321.5"},
        "borrowers": {"borrower": {"mobile_phone": "Jordan Sample", "ssn_last4": "XXX-XX-1234"}},
        "financial": {"credit_score_estimate": "XXX"},
    })
    assert set(problems) == {"property.property_type", "borrowers.borrower.mobile_phone", "financial.credit_score_estimate"}
    assert "property_type" not in body["property"] and body["property"]["first_lien_balance"] == 184321.5
    assert body["borrowers"]["borrower"]["ssn_last4"] == "1234"
    assert schema.display(schema.spec_for("property.first_lien_balance"), 184321.5) == "$184,321.50"
    assert schema.display(schema.spec_for("borrowers.borrower.ssn_last4"), "1234") == "XXX-XX-1234"
    with pytest.raises(schema.FieldProblem):
        schema.coerce_value(schema.spec_for("borrowers.borrower.ssn_last4"), "123-45-6789")


def test_a_coborrower_is_asked_only_when_there_is_one() -> None:
    alone, _ = schema.coerce({})
    together, _ = schema.coerce({"has_coborrower": True})
    assert not any(path.startswith("borrowers.coborrower.") for _, path, _ in schema.applicable_paths(alone))
    assert any(path.startswith("borrowers.coborrower.") for _, path, _ in schema.applicable_paths(together))


def test_the_application_is_registered_as_a_signable_form() -> None:
    kind = form_kinds.get("re_application")
    assert kind is not None and kind.signable and kind.engine == form_kinds.ENGINE_CLIENT_FORM
    assert form_kinds.kinds_for_requirement("signed_re_application") == ("re_application",)
    assert "re_application" not in form_kinds.financial_kinds()
    signature = form_kinds.normalize_collection_config({"form_kind": "re_application", "signature": {"required": True}}, requirement_kind="form")["signature"]
    assert signature["satisfies"] == ["signed_credit_authorization"] and "credit_authorization" in signature["consents"]


# --- a real file ------------------------------------------------------------------


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "re_application")


@pytest.fixture
async def db(template_database, tmp_path):
    async with sqlite_session(template_database, tmp_path) as session:
        yield session


async def _heloc_file(db, state=None):
    bucket = Bucket(name="Sample room")
    db.add(bucket)
    await db.flush()
    intake = PublicUnderwritingIntake(
        bucket_id=bucket.id, token_hash=uuid.uuid4().hex, full_name="Jordan Sample", email="jordan@example.com",
        phone="+17045550123", business_name="Sample Holdings", variant="real_estate_dscr_v1", status="collecting",
        loan_purpose="Home equity (HELOC)", requested_loan_amount=300000, intake_state=state or dict(HELOC_STATE),
    )
    db.add(intake)
    await db.flush()
    profile = ApplicationProfile(intake_id=intake.id, primary_bucket_id=bucket.id)
    db.add(profile)
    await db.flush()
    return intake, profile


async def test_prefill_fills_what_onboarding_knows_and_drops_what_is_not_true(db) -> None:
    intake, profile = await _heloc_file(db)
    pre = await prefill.build(db, profile, intake=intake)
    assert pre.values["request.loan_purpose"] == "heloc"
    assert pre.values["request.occupancy"] == "primary_residence"
    assert pre.values["property.street"] == "100 EXAMPLE ST" and pre.values["property.zip"] == "28202"
    assert pre.values["property.first_lien_balance"] == 184321.5
    assert pre.provenance["property.first_lien_balance"]["tier"] == prefill.TIER_CHECK
    assert pre.values["borrowers.borrower.email"] == "jordan@example.com"
    assert "property.property_type" not in pre.values
    assert "entity.phone" not in pre.values
    assert "financial.credit_score_estimate" not in pre.values
    warned = {warning["path"] for warning in pre.warnings}
    assert {"property.property_type", "entity.phone", "financial.credit_score_estimate"} <= warned


async def test_the_form_asks_only_for_gaps_and_ai_suggestions_need_a_check(db) -> None:
    _, profile = await _heloc_file(db)
    view = await client_forms.read(db, profile, audience="client")
    assert "warnings" not in view
    assert "property.first_lien_balance" in view["please_check"]
    assert "property.first_lien_balance" in view["completion"]["missing"]
    assert "request.loan_purpose" not in view["completion"]["missing"]
    assert view["provenance"]["request.loan_purpose"] == {"tier": "A", "source": prefill.SOURCE_APPLICATION}

    view = await client_forms.save(db, profile, changes={}, confirm=["property.first_lien_balance"], has_coborrower=None, revision=0, actor="client")
    assert "property.first_lien_balance" not in view["please_check"]
    assert view["provenance"]["property.first_lien_balance"]["confirmed"] is True
    assert view["revision"] == 1


async def test_saves_refuse_a_stale_revision_and_bad_values(db) -> None:
    _, profile = await _heloc_file(db)
    await client_forms.save(db, profile, changes={"property.property_type": "single_family"}, confirm=[], has_coborrower=None, revision=0, actor="client")
    with pytest.raises(HTTPException) as stale:
        await client_forms.save(db, profile, changes={"property.units": 1}, confirm=[], has_coborrower=None, revision=0, actor="client")
    assert stale.value.status_code == 409 and stale.value.detail["code"] == "revision_conflict"
    with pytest.raises(HTTPException) as bad:
        await client_forms.save(db, profile, changes={"borrowers.borrower.mobile_phone": "Jordan Sample"}, confirm=[], has_coborrower=None, revision=1, actor="client")
    assert bad.value.status_code == 422 and "borrowers.borrower.mobile_phone" in bad.value.detail["fields"]


def _sample(path: str, spec: dict) -> object:
    kind = spec["type"]
    if path == "request.business_purpose":
        return True
    return {
        "text": "Sample", "textarea": "Sample answer", "money": 1000, "integer": 3, "year": 2001, "percent": 50,
        "score": 712, "date": "2001-02-03", "yesno": False, "email": "sample@example.com", "phone": "7045550123",
        "ssn_last4": "1234", "state": "NC", "zip": "28202", "ein": "12-3456789",
    }.get(kind) if kind not in ("select", "multiselect") else (
        spec["options"][0]["value"] if kind == "select" else [spec["options"][0]["value"]]
    )


async def _complete(db, profile) -> dict:
    view = await client_forms.read(db, profile, audience="desk")
    for _ in range(6):
        if view["completion"]["complete"] and not view["please_check"]:
            break
        changes = {path: _sample(path, schema.spec_for(path)) for path in view["completion"]["missing"] if path not in view["please_check"]}
        view = await client_forms.save(db, profile, changes=changes, confirm=view["please_check"], has_coborrower=None, revision=view["revision"], actor="desk")
    return view


async def test_submitting_freezes_a_version_files_its_pdf_and_writes_answers_back(db) -> None:
    intake, profile = await _heloc_file(db)
    with pytest.raises(HTTPException) as incomplete:
        await client_forms.submit(db, profile, revision=0, actor="client")
    assert incomplete.value.status_code == 409 and incomplete.value.detail["code"] == "incomplete"

    view = await _complete(db, profile)
    assert view["completion"]["complete"]
    stored: dict[str, bytes] = {}
    with (
        patch("app.routers.dealer_ai_intake._bucket_storage_config", lambda: ("bucket", "prefix", "kms")),
        patch("app.routers.dealer_ai_intake._put_bucket_object", lambda key, content_type, data, **kwargs: stored.setdefault(key, data) and "v1"),
    ):
        view = await client_forms.submit(db, profile, revision=view["revision"], actor="client", idempotency_key="submit-once-1")
        again = await client_forms.submit(db, profile, revision=view["revision"], actor="client", idempotency_key="submit-once-1")
    assert view["status"] == "submitted" and view["version"]["number"] == 1
    assert again["version"]["number"] == 1
    (key, pdf), = stored.items()
    assert key.startswith("prefix/client-forms/") and pdf.startswith(b"%PDF")
    version = await db.get(ClientFormVersion, uuid.UUID(view["version"]["id"]))
    assert version.body_sha256 == client_forms.body_sha256(version.body)
    details = intake.intake_state["funding_review_details"]
    assert details["property_type"] == schema.get_at(version.body, "property.property_type")
    assert details["sources"]["property_type"] == "application_form"

    # An edit after submitting needs a new submission.
    view = await client_forms.save(db, profile, changes={"notes.notes": "One more thing"}, confirm=[], has_coborrower=None, revision=view["revision"], actor="client")
    assert view["status"] == "draft"


# --- the link's email code ----------------------------------------------------------


def _link():
    return SimpleNamespace(id=uuid.uuid4(), pin_hash=None, pin_set_at=None, pin_attempts=0, pin_locked_until=None)


def test_a_code_opens_the_form_once_and_wrong_codes_lock_it() -> None:
    link = _link()
    now = datetime.now(UTC)
    code = form_link_codes.new_code(link, now=now)
    session, expires = form_link_codes.verify_code(link, code, now=now + timedelta(minutes=1))
    assert form_link_codes.session_valid(session, link) and expires > now
    assert link.pin_hash is None
    with pytest.raises(HTTPException) as reused:
        form_link_codes.verify_code(link, code, now=now + timedelta(minutes=2))
    assert reused.value.status_code == 410

    link = _link()
    form_link_codes.new_code(link, now=now)
    for attempt in range(form_link_codes.MAX_ATTEMPTS):
        with pytest.raises(HTTPException):
            form_link_codes.verify_code(link, "000000" if attempt % 2 else "999999", now=now)
    assert link.pin_locked_until is not None
    with pytest.raises(HTTPException) as locked:
        form_link_codes.new_code(link, now=now + timedelta(minutes=1))
    assert locked.value.status_code == 429


def test_a_code_expires_and_sessions_belong_to_their_link() -> None:
    link = _link()
    now = datetime.now(UTC)
    code = form_link_codes.new_code(link, now=now)
    with pytest.raises(HTTPException) as expired:
        form_link_codes.verify_code(link, code, now=now + timedelta(minutes=16))
    assert expired.value.status_code == 410
    session, _ = form_link_codes.issue_session(link)
    assert not form_link_codes.session_valid(session, _link())
    assert not form_link_codes.session_valid("garbage", link)
    assert form_link_codes.mask_email("jordan@example.com") == "j*****@example.com"
