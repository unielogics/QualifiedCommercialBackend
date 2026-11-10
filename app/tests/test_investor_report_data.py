from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers import application_profiles as profile_routes
from app.routers import dealer_ai_intake as core
from app.schemas.investor_collateral import CollateralPatch
from app.services import investor_collateral, investor_report


def property_row(**updates):
    return {"id": str(uuid4()), "address": {"street": "120 Market Street", "city": "Fictional City", "state": "FL", "postal_code": "33101"},
        "estimated_value": "500000.00", "existing_liens_total": "250000.00", "available_lien_amount": None,
        "currency": "USD", "source_file_ids": [], **updates}


@pytest.mark.parametrize("value", ["NaN", "Infinity", -1, "1.001", "1000000000000", True])
def test_collateral_money_rejects_invalid_or_unbounded_values(value):
    with pytest.raises(ValidationError):
        CollateralPatch(expected_revision=0, enabled=True, properties=[property_row(available_lien_amount=value)])


def test_collateral_requires_address_and_does_not_infer_lendable_equity():
    result = CollateralPatch(expected_revision=0, enabled=True, properties=[property_row()])
    assert result.properties[0].available_lien_amount is None
    with pytest.raises(ValidationError):
        CollateralPatch(expected_revision=0, enabled=True, properties=[])
    with pytest.raises(ValidationError):
        CollateralPatch(expected_revision=0, enabled=True, properties=[property_row(address={"street": "Only a street"})])
    repeated = property_row()
    with pytest.raises(ValidationError):
        CollateralPatch(expected_revision=0, enabled=True, properties=[repeated, repeated])


@pytest.mark.parametrize("role", [Role.CLIENT, Role.BROKER, Role.FIELD_REP, Role.LENDER, Role.VENDOR])
def test_collateral_is_staff_only(role):
    with pytest.raises(HTTPException) as reason:
        investor_collateral.require_staff(SimpleNamespace(role=role))
    assert reason.value.status_code == 403


@pytest.mark.asyncio
async def test_collateral_stale_editor_cannot_overwrite_saved_rows():
    saved = SimpleNamespace(id=uuid4(), investor_collateral={"revision": 4, "enabled": True, "properties": [property_row()]})
    before = deepcopy(saved.investor_collateral)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: saved)), flush=AsyncMock())
    with pytest.raises(HTTPException) as reason:
        await investor_collateral.update(db, saved, CollateralPatch(expected_revision=3, enabled=False), SimpleNamespace(role=Role.LOAN_EXEC, id=uuid4()))
    assert reason.value.status_code == 409
    assert saved.investor_collateral == before
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabling_collateral_preserves_rows_and_audits_before_after():
    saved = SimpleNamespace(id=uuid4(), primary_bucket_id=None, investor_collateral={"revision": 1, "enabled": True, "properties": [property_row()]})
    before = deepcopy(saved.investor_collateral)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: saved)), flush=AsyncMock())
    actor = SimpleNamespace(role=Role.LOAN_EXEC, id=uuid4())
    with patch.object(investor_collateral, "property_options", AsyncMock(return_value=[])), patch.object(investor_collateral, "read", AsyncMock(side_effect=lambda _db, p, _user: p.investor_collateral)), patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit:
        after = await investor_collateral.update(db, saved, CollateralPatch(expected_revision=1, enabled=False, properties=[]), actor)
    assert after["revision"] == 2 and after["enabled"] is False
    assert after["properties"] == before["properties"]
    assert audit.await_args.kwargs["metadata"]["before"] == before
    assert audit.await_args.kwargs["metadata"]["after"] == after
    assert before["enabled"] is True


@pytest.mark.asyncio
async def test_cross_file_document_link_is_rejected():
    saved = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), investor_collateral={})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(scalar_one=lambda: saved), SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))]), flush=AsyncMock())
    payload = CollateralPatch(expected_revision=0, enabled=True, properties=[property_row(source_file_ids=[str(uuid4())])])
    with pytest.raises(HTTPException) as reason:
        await investor_collateral.update(db, saved, payload, SimpleNamespace(role=Role.SUPER_ADMIN, id=uuid4()))
    assert reason.value.status_code == 422
    assert saved.investor_collateral == {}


@pytest.mark.asyncio
async def test_collateral_routes_honor_profile_scope():
    actor = SimpleNamespace(role=Role.LOAN_EXEC, id=uuid4())
    db = SimpleNamespace(commit=AsyncMock())
    with patch.object(profile_routes.profiles, "load_profile", AsyncMock(side_effect=HTTPException(404, "Not found"))):
        for action in (
            profile_routes.get_application_collateral(uuid4(), actor, db),
            profile_routes.patch_application_collateral(uuid4(), CollateralPatch(expected_revision=0, enabled=False), actor, db),
        ):
            with pytest.raises(HTTPException) as reason:
                await action
            assert reason.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_property_does_not_infer_currency_or_available_lien_capacity():
    property_id, pdf_file_id = str(uuid4()), uuid4()
    version = SimpleNamespace(id=uuid4(), version_number=3, pdf_file_id=pdf_file_id, body={"owner_name": "Synthetic LLC",
        "properties": [{"id": property_id, "address": "20 Business Road", "city": "Fictional City", "state": "FL", "postal_code": "33101",
                        "estimated_value": "500000", "mortgage_status": "active"}],
        "mortgages": [{"property_row_id": property_id, "status": "active", "balance": "250000"}]})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(SimpleNamespace(), version)])))
    options = await investor_collateral.property_options(db, SimpleNamespace(id=uuid4()))
    assert options[0]["currency"] is None and options[0]["currency_needs_confirmation"] is True
    assert options[0]["existing_liens_total"] == 250000
    assert "available_lien_amount" not in options[0]
    assert options[0]["source_file_ids"] == [str(pdf_file_id)]


@pytest.mark.asyncio
async def test_overstated_declared_lien_capacity_is_flagged_without_changing_the_amount():
    row = property_row(available_lien_amount="300000")
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=None, investor_collateral={"properties": [row], "enabled": True})
    with patch.object(investor_collateral, "property_options", AsyncMock(return_value=[])):
        response = await investor_collateral.read(SimpleNamespace(), profile)
    assert any("exceeds estimated value" in warning for warning in response.warnings)
    assert response.properties[0].available_lien_amount == 300000


def bank_fixture(currency="USD", deposits=100, file_id=None):
    file_id = str(file_id or uuid4())
    file = SimpleNamespace(id=file_id, file_name="Statement.pdf", content_hash="a" * 64, document_version_number=2)
    analysis = SimpleNamespace(id=uuid4(), classification="bank_statement", analysis={"key_facts": {"currency": currency}, "financial_fact_citations": {"balance": {"page": 2}}})
    return file, analysis, {"counted": True, "series": [{"month": "2026-01", "deposits": deposits, "withdrawals": 90, "net": deposits - 90,
        "ending_balance": -10, "beginning_balance": 0, "nsf": 0, "file_ids": [file_id]}]}


def test_banking_uses_counted_canonical_accounts_preserves_zero_negative_and_unknowns():
    f1, a1, account1 = bank_fixture()
    f2, a2, account2 = bank_fixture(deposits=200)
    analysis = {str(f1.id): a1, str(f2.id): a2}
    register = investor_report.Register([f1, f2], analysis)
    result = investor_report.banking({"bank": {"accounts": [account1, account2, {"counted": False, "series": []}]}}, analysis, register)
    assert result["account_count"] == 2
    month = result["months"][0]
    assert month["deposits"] == 300 and month["withdrawals"] == 180 and month["net_cash_flow"] == 120
    assert month["ending_balance"] == -20 and month["beginning_balance"] == 0 and month["nsf_count"] == 0
    account2["series"][0]["ending_balance"] = None
    unknown = investor_report.banking({"bank": {"accounts": [account1, account2]}}, analysis, register)
    assert unknown["months"][0]["ending_balance"] is None
    assert "deposits are not recognized revenue" in " ".join(result["coverage_notes"])


def test_mixed_or_missing_currency_uses_explicit_single_account_instead_of_unsafe_sum():
    f1, a1, account1 = bank_fixture("USD")
    f2, a2, account2 = bank_fixture(None, deposits=200)
    analysis = {str(f1.id): a1, str(f2.id): a2}
    result = investor_report.banking({"bank": {"accounts": [account1, account2]}}, analysis, investor_report.Register([f1, f2], analysis))
    assert result["currency"] is None
    assert result["account_count"] == 2 and result["displayed_account_count"] == 1
    assert result["aggregation_scope"] == "single_account_currency_not_comparable"
    assert result["months"][0]["deposits"] != 300
    assert "not combined" in " ".join(result["coverage_notes"])


def test_credit_projection_contains_bands_and_counts_without_raw_personal_data():
    owner = SimpleNamespace(id=uuid4(), credit_required=True, ownership_pct=20, full_name="Private Owner", street="Secret home", dob="1970-01-01",
        credit_summary={"credit_risk": {"flag_count": 3, "high_count": 1, "flags": [{"title": "Secret creditor 123-45-6789"}]}})
    rows = {str(owner.id): {"report_available": True, "score_verified": True, "status": "collected", "score": 661,
        "pulled_at": "2026-01-05T00:00:00Z", "credit_pull_id": str(uuid4()), "fico_source": "TransUnion FICO 4", "raw": "123-45-6789"}}
    register = investor_report.Register([], {})
    summary = investor_report.credit_summary([owner], rows, register)
    assert summary["score_band"] == "660-679"
    assert summary["required_owner_count"] == 1 and summary["current_required_report_count"] == 1
    assert summary["risk_flag_count"] == 3 and summary["bureaus"] == ["TransUnion"]
    payload = str((summary, register.rows))
    for private in ("Private Owner", "Secret home", "1970-01-01", "Secret creditor", "123-45-6789", "661"):
        assert private not in payload
    owner.credit_required = False
    pending = investor_report.credit_summary([owner], rows, investor_report.Register([], {}))
    assert "roster is not established" in pending["risk_summary"]


def test_business_address_never_falls_back_to_personal_or_property_address():
    intake = SimpleNamespace(business_name="Synthetic LLC", street="Private home", asset_rows=[{"address": "A collateral address"}],
        intake_state={"business_profile": {"street": "One Business Road", "city": "Business City", "state": "FL", "zip": "33101"},
            "desk_facts": {"business_street": "Two Business Road"}})
    data = investor_report.company(intake, SimpleNamespace(industry=None, naics_code=None))
    assert data["address"]["street"] == "Two Business Road"
    assert "Private home" not in str(data)
    intake.intake_state = {}
    assert not investor_report.company(intake, SimpleNamespace())["formatted_address"]


def test_credit_note_distinguishes_complete_zero_required_roster_from_unknown_ownership():
    owners = [SimpleNamespace(id=uuid4(), credit_required=False, ownership_pct=10, backfill_needs_review=False) for _ in range(10)]
    current = investor_report.credit_summary(owners, {}, investor_report.Register([], {}))
    assert "No owners meet" in current["risk_summary"]
    assert current["coverage_status"] == "not_required"
    owners[0].ownership_pct = None
    unknown = investor_report.credit_summary(owners, {}, investor_report.Register([], {}))
    assert "confirm ownership completeness" in unknown["risk_summary"]
    assert unknown["coverage_status"] == "pending"


@pytest.mark.asyncio
async def test_report_and_fingerprint_are_deterministic_and_collateral_changes_make_stale():
    intake = SimpleNamespace(business_name="Synthetic LLC", intake_state={})
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=None, investor_collateral={}, use_of_funds_revision=2)
    args = (SimpleNamespace(), intake, profile, {}, {}, [], {})
    first = await investor_report.build(*args)
    second = await investor_report.build(*args)
    assert first == second
    def fingerprint(report):
        return core._package_input_fingerprint(source_metadata={}, context_snapshot={}, financials={"investor_report": report})
    assert fingerprint(first) == fingerprint(second)
    profile.investor_collateral = {"enabled": False, "revision": 1, "properties": []}
    changed = await investor_report.build(*args)
    assert fingerprint(first) != fingerprint(changed)
    changed["layout_version"] = "another-template"
    assert fingerprint(first) != fingerprint(changed)


def test_private_snapshot_is_retained_but_removed_from_standard_artifact_response():
    now = datetime.now(UTC)
    artifact = SimpleNamespace(id=uuid4(), intake_id=uuid4(), artifact_type="investor_presentation", title="Synthetic report", body_text=None,
        body_json={"_reference_manifest": {"references": [{"id": "S1", "label": "Frozen reference"}]},
            "_analysis_snapshot": {"operating_earnings": {"improvement_plan": {"plan_notes": "Frozen detailed assumption"}}},
            "reference_manifest": {"available": True, "count": 1}},
        s3_key=None, created_by_user_id=None, created_at=now, updated_at=now)
    result = core._artifact_read(artifact)
    assert "Frozen reference" not in str(result.body_json)
    assert "Frozen detailed assumption" not in str(result.body_json)
    assert result.body_json["reference_manifest"]["count"] == 1
    assert artifact.body_json["_reference_manifest"]["references"][0]["label"] == "Frozen reference"


@pytest.mark.asyncio
async def test_reference_route_reads_requested_generation_and_rejects_cross_file_artifact():
    intake_id, artifact_id, generation = uuid4(), uuid4(), str(uuid4())
    artifact = SimpleNamespace(id=artifact_id, created_at=datetime.now(UTC), body_json={"_package": {"generation_id": generation},
        "_reference_manifest": {"references": [{"id": "S1", "label": "Frozen file v1"}]},
        "_analysis_snapshot": {"operating_earnings": {"improvement_plan": {"plan_notes": "Frozen assumption"}}}})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: artifact)))
    with patch.object(core, "_load_admin_dealer_lead", AsyncMock()):
        response = await core.get_package_readiness_references(intake_id, artifact_id, SimpleNamespace(role=Role.SUPER_ADMIN), db)
        assert response["generation_id"] == generation
        assert response["references"][0]["label"] == "Frozen file v1"
        assert response["analysis_notes"][0]["text"] == "Frozen assumption"
        compiled = db.execute.await_args.args[0].compile()
        assert intake_id in compiled.params.values() and artifact_id in compiled.params.values()
        db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
        with pytest.raises(HTTPException) as reason:
            await core.get_package_readiness_references(intake_id, uuid4(), SimpleNamespace(role=Role.SUPER_ADMIN), db)
        assert reason.value.status_code == 404


def delivery_artifact(kind, generation, *, private=None):
    now = datetime.now(UTC)
    return SimpleNamespace(id=uuid4(), intake_id=uuid4(), artifact_type=kind, title=f"Synthetic {kind}", body_text="Synthetic review",
        body_json={"_package": {"generation_id": generation, "status": "current"},
            "_pdf": {"package_document_types": list(core._PACKAGE_READINESS_TYPES), "package_input_sha256": "f" * 64,
                "report_layout_version": investor_report.LAYOUT_VERSION},
            "investor_report_format": {"version": investor_report.LAYOUT_VERSION, "max_pages": 5, "page_count": 5}, **(private or {})},
        s3_key=None, created_by_user_id=None, created_at=now, updated_at=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "generation", "hash", "superseded", "layout", "pages", "missing"])
async def test_delivery_prefers_current_bounded_report_and_rejects_mixed_or_legacy_presentations(bad):
    generation = str(uuid4())
    summary, packet, presentation = [delivery_artifact(kind, generation) for kind in core._PACKAGE_READINESS_TYPES]
    if bad == "generation":
        presentation.body_json["_package"]["generation_id"] = str(uuid4())
    elif bad == "hash":
        presentation.body_json["_pdf"]["package_input_sha256"] = "b" * 64
    elif bad == "superseded":
        presentation.body_json["_package"]["status"] = "superseded"
    elif bad == "layout":
        presentation.body_json["investor_report_format"]["version"] = "old-unbounded-slides"
    elif bad == "pages":
        presentation.body_json["investor_report_format"]["page_count"] = 6
    with patch.object(core, "_latest_artifact", AsyncMock(return_value=None if bad == "missing" else presentation)):
        if bad:
            with pytest.raises(HTTPException) as reason:
                await core._primary_delivery_artifact(SimpleNamespace(), SimpleNamespace(id=uuid4()), summary, packet)
            assert reason.value.status_code == 409
        else:
            result = await core._primary_delivery_artifact(SimpleNamespace(), SimpleNamespace(id=uuid4()), summary, packet)
            assert result is presentation and result is not packet
    summary.body_json["_pdf"] = {"package_document_types": ["executive_summary", "lender_packet"]}
    assert await core._primary_delivery_artifact(SimpleNamespace(), SimpleNamespace(id=uuid4()), summary, packet) is packet


@pytest.mark.asyncio
async def test_email_preview_exposes_the_same_five_page_primary_report_without_sending():
    generation = str(uuid4())
    summary, packet, presentation = [delivery_artifact(kind, generation) for kind in core._PACKAGE_READINESS_TYPES]
    intake = SimpleNamespace(id=uuid4(), business_name="Synthetic LLC", full_name="Synthetic Operator")
    db = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)
    payload = core.VendorEmailPreviewRequest(to_emails=["review@example.com"], include_lender_packet=True)
    with patch.object(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake)), \
         patch.object(core, "_fresh_package_artifact_pair", AsyncMock(return_value=(summary, packet))), \
         patch.object(core, "_latest_artifact", AsyncMock(return_value=presentation)), \
         patch.object(core, "_generate_management_json", AsyncMock(return_value={"subject": "Review", "body": "Synthetic review"})), \
         patch.object(core, "send_as_user", AsyncMock()) as send:
        response = await core.preview_dealer_ai_vendor_email(intake.id, payload, user, db)
    assert response.lender_packet.id == presentation.id
    assert response.lender_packet.artifact_type == "investor_presentation"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_send_attaches_only_the_current_primary_report_when_package_pdf_selected():
    from app.services.email.ses_client import SesSendResult
    generation = str(uuid4())
    summary, packet, presentation = [delivery_artifact(kind, generation) for kind in core._PACKAGE_READINESS_TYPES]
    packet.s3_key, presentation.s3_key = "legacy/detailed-packet.pdf", "current/investor-report.pdf"
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), bucket=SimpleNamespace(files=[], requested_documents=[]))
    added = []

    def add(row):
        row.id, row.created_at, row.updated_at = uuid4(), datetime.now(UTC), datetime.now(UTC)
        added.append(row)

    db = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(), SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])), SimpleNamespace()]),
        add=add, commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN, name="Synthetic Operator", email="operator@example.com")
    payload = core.VendorEmailSendRequest(to_emails=["review@example.com"], include_lender_packet=True, attach_lender_packet=True,
        attach_executive_summary=False, attach_package_zip=False, bucket_access="none", subject="Synthetic report", body="Please review.")
    with (
        patch.object(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake)),
        patch.object(core, "_fresh_package_artifact_pair", AsyncMock(return_value=(summary, packet))),
        patch.object(core, "_latest_artifact", AsyncMock(return_value=presentation)),
        patch.object(core, "_s3_bytes", AsyncMock(return_value=b"synthetic attachment bytes")) as read,
        patch.object(core, "send_as_user", AsyncMock(return_value=SesSendResult(True, "synthetic-provider-id", "sent"))) as send,
        patch.object(core, "_log", AsyncMock()),
    ):
        result = await core.send_dealer_ai_vendor_email(intake.id, payload, user, db)
    read.assert_awaited_once_with("current/investor-report.pdf")
    assert len(send.await_args.kwargs["attachments"]) == 1
    assert "investor_presentation" in send.await_args.kwargs["attachments"][0][0]
    assert added[0].lender_packet_artifact_id == presentation.id
    assert result.email_sends[0].ses_status == "sent"
