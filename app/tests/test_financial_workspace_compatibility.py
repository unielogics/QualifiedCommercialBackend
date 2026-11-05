from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select

from app.models.bucket import BucketFile
from app.routers.financial_workspace_debts import reconcile_debt
from app.schemas.financial_debt_actions import FinancialDebtAction
from app.services.financial_version_selection import current_financial_file_clause
from app.services.financial_workspace_legacy import merge_source_debts
from app.services.public_underwriting_packet_pdf import extract_debt_schedule


def test_borrower_can_correct_only_own_rows_without_replacing_sources():
    own, desk = str(uuid4()), str(uuid4())
    current = {"debts": [
        {"id": own, "lender": "My loan", "balance": "200", "origin": "client_form", "status": "active", "vendor_key": "source-key"},
        {"id": desk, "lender": "Desk loan", "balance": "300", "origin": "admin", "status": "active"},
    ]}
    old = deepcopy(current)
    result = merge_source_debts(current, {"debts": [
        {"id": own, "balance": "150", "origin": "admin", "vendor_key": "spoof"},
        {"id": desk, "balance": "0", "status": "dismissed"},
    ]})
    assert current == old
    assert result["debts"][0]["balance"] == "150"
    assert result["debts"][0]["origin"] == "client_form"
    assert result["debts"][0]["vendor_key"] == "source-key"
    assert result["debts"][1] == old["debts"][1]


def test_empty_borrower_save_soft_deletes_only_own_active_rows():
    current = {"debts": [{"id": str(uuid4()), "origin": origin, "status": "active", "lender": origin}
                         for origin in ("client_form", "admin", "ai_draft")]}
    result = merge_source_debts(current, {"debts": []})
    assert [row["status"] for row in result["debts"]] == ["dismissed", "active", "active"]
    assert merge_source_debts(current, {}) == current


def test_borrower_native_terms_survive_the_legacy_adapter_without_source_privileges():
    from app.services.financial_workspace import validate_body

    row_id = str(uuid4())
    current = {"debts": [{"id": row_id, "origin": "client_form", "status": "active", "lender": "Example", "balance": "1000"}]}
    merged = merge_source_debts(current, {"debts": [{"id": row_id, "factor_rate": "1.375", "payment_amount": "100", "payment_frequency": "weekly", "vendor_key": "spoof", "count_in_dscr": False}]})
    row = validate_body("debt_schedule", merged)["debts"][0]
    assert row["factor_rate"] == "1.375" and row["payment_amount"] == "100.00"
    assert row["monthly_payment"] == "433.00" and row["payment_frequency"] == "weekly"
    assert "vendor_key" not in row and row.get("count_in_dscr", True)


def test_borrower_can_declare_no_debt_but_cannot_remove_another_sources_obligations():
    from app.services.financial_workspace import validate_body

    empty = merge_source_debts({"debts": []}, {"debts": [], "no_debt": True})
    assert validate_body("debt_schedule", empty)["no_debt"] is True
    desk = {"debts": [{"id": str(uuid4()), "origin": "admin", "status": "active", "lender": "Desk loan", "balance": "1000", "monthly_payment": "100"}]}
    merged = merge_source_debts(desk, {"debts": [], "no_debt": True})
    assert merged["debts"] == desk["debts"]
    with pytest.raises(HTTPException):
        validate_body("debt_schedule", merged)


def test_adding_a_client_obligation_replaces_an_old_no_debt_declaration():
    merged = merge_source_debts({"debts": [], "no_debt": True}, {"debts": [{"lender": "Example", "balance": "1000", "monthly_payment": "100"}]})
    assert merged["no_debt"] is False and len(merged["debts"]) == 1


def test_old_form_round_trip_preserves_unique_row_identity_without_ids():
    row_id = str(uuid4())
    current = {"debts": [{"id": row_id, "origin": "client_form", "status": "active", "lender": "Example", "balance": "10"}]}
    result = merge_source_debts(current, {"debts": [{"lender": "Example", "balance": "20"}]})
    assert len(result["debts"]) == 1
    assert result["debts"][0]["id"] == row_id


def test_debt_aggregation_preserves_unknown_payments_and_explicit_zero():
    partial = extract_debt_schedule([{"classification": "debt_schedule", "key_facts": {"debts": [
        {"lender": "A", "current_balance": 100, "monthly_payment": 10},
        {"lender": "B", "current_balance": 200, "monthly_payment": None},
    ]}}])
    assert partial["total_monthly_debt_service"] is None
    assert partial["total_outstanding_balance"] == 300
    empty = extract_debt_schedule([{"classification": "debt_schedule", "key_facts": {
        "total_monthly_debt_service": 0, "total_outstanding_balance": 0, "debts": [],
    }}])
    assert empty["total_monthly_debt_service"] == 0 and empty["total_outstanding_balance"] == 0


def test_pdf_ordinal_references_survive_stable_debt_identity_assignment():
    from app.services.financial_workspace import _mapping_records, validate_body
    body = validate_body("debt_schedule", {"debts": [{"lender": "Example", "balance": "100"}]})
    file = SimpleNamespace(id=uuid4())
    records = _mapping_records("debt_schedule", body, file, None, "a" * 64,
        {"debts.r1.balance": {"page": 2, "quote": "Example balance $100"}}, {2: "Example balance $100"})
    balance = next(row for row in records if row["key"].endswith(".balance"))
    assert balance["confidence"] == "high"
    assert balance["source"]["page"] == 2
    assert balance["key"].startswith(body["debts"][0]["id"])


def test_subcent_values_cannot_diverge_between_pdf_facts_and_database_projection():
    from app.services.financial_workspace import validate_body
    with pytest.raises(HTTPException) as error:
        validate_body("debt_schedule", {"debts": [{"lender": "Example", "balance": "100.001"}]})
    assert error.value.status_code == 422
    body = validate_body("debt_schedule", {"debts": [{"lender": "Example", "balance": "100.00", "rate": "12.125"}]})
    assert body["debts"][0]["balance"] == "100.00"


def test_sql_selection_excludes_previous_versions_and_pending_originals():
    engine = create_engine("sqlite://")
    family, v1, v2, v3 = [uuid4() for _ in range(4)]
    original, published, pending, other = [uuid4() for _ in range(4)]
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE bucket_files (id CHAR(32) PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE financial_form_families (id CHAR(32) PRIMARY KEY, current_version_id CHAR(32))")
        conn.exec_driver_sql("CREATE TABLE financial_form_versions (id CHAR(32) PRIMARY KEY, family_id CHAR(32), source_file_id CHAR(32), pdf_file_id CHAR(32))")
        for file_id in (original, published, pending, other):
            conn.exec_driver_sql("INSERT INTO bucket_files VALUES (?)", (file_id.hex,))
        conn.exec_driver_sql("INSERT INTO financial_form_families VALUES (?, ?)", (family.hex, v2.hex))
        for version, file_id in ((v1, original), (v2, published), (v3, pending)):
            conn.exec_driver_sql("INSERT INTO financial_form_versions VALUES (?, ?, ?, ?)", (version.hex, family.hex, file_id.hex, file_id.hex))
        selected = set(conn.execute(select(BucketFile.id).where(current_financial_file_clause(BucketFile.id))).scalars())
        assert selected == {published, other}
    engine.dispose()


@pytest.mark.asyncio
async def test_linking_payoff_rejects_another_profiles_document_before_mutation():
    profile, file_id = SimpleNamespace(id=uuid4()), uuid4()
    payload = FinancialDebtAction(op="link_document", row_id=uuid4(), expected_revision=0, file_id=file_id, role="payoff")
    db = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(id=uuid4())
    with (patch("app.routers.application_profiles._require_statement_staff"),
          patch("app.services.application_profiles.load_profile", AsyncMock(return_value=profile)),
          patch("app.services.application_profiles.evidence_state", AsyncMock(return_value=SimpleNamespace(files=[]))),
          patch("app.services.financial_workspace.save_draft", AsyncMock()) as write):
        with pytest.raises(HTTPException) as error:
            await reconcile_debt(profile.id, payload, user, db)
        assert error.value.status_code == 404
        write.assert_not_called()
        db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_version_originals_and_pdfs_cannot_be_removed_through_bucket_trash():
    from app.routers import buckets
    file = SimpleNamespace(id=uuid4(), deleted_at=None)
    def result(row):
        return SimpleNamespace(scalar_one_or_none=lambda: row)
    db = SimpleNamespace(info={}, execute=AsyncMock(side_effect=[result(file), result(uuid4())]), commit=AsyncMock())
    with patch.object(buckets, "_load_bucket_or_404", AsyncMock()):
        with pytest.raises(HTTPException) as error:
            await buckets.delete_admin_file(uuid4(), file.id, SimpleNamespace(), SimpleNamespace(id=uuid4()), db)
    assert error.value.status_code == 409 and file.deleted_at is None
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_pdf_download_returns_the_exact_current_immutable_artifact():
    from app.routers import application_profiles as router
    profile, current = SimpleNamespace(id=uuid4()), uuid4()
    snapshot = {"current_version_id": str(current)}
    with (patch.object(router.profiles, "load_profile", AsyncMock(return_value=profile)),
          patch.object(router, "_require_statement_staff"),
          patch.object(router, "managed_snapshot", AsyncMock(return_value=snapshot)),
          patch("app.services.financial_workspace.version_pdf", AsyncMock(return_value=(b"saved-pdf-bytes", "Debt v2.pdf"))) as pdf,
          patch.object(router.dealer_forms_pdf, "render_debt_schedule_pdf") as renderer):
        response = await router.financial_form_pdf(profile.id, "debt_schedule", SimpleNamespace(), SimpleNamespace())
    assert response.body == b"saved-pdf-bytes"
    assert "v2.pdf" in response.headers["content-disposition"]
    assert pdf.await_args.args[2] == current
    renderer.assert_not_called()
