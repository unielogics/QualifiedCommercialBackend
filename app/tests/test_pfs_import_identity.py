import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.bucket import BucketFile
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormImport,
)
from app.services import financial_workspace as ws
from app.tests.test_financial_import_identity import ImportDB, Result


def line(**values):
    return {"id": str(uuid4()), **values}


@pytest.mark.asyncio
async def test_revised_pfs_pdfs_update_one_property_and_preserve_existing_row_metadata():
    db = ImportDB()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind="pfs", identity={"subject": "Jane Smith", "as_of": "2026-11-05"}, label="Jane", identity_key="test")
    body = ws.empty_body("pfs")
    original = line(property_address="100 Main Street", original_cost="300000", present_market_value="450000",
                    mortgage_balance="200000", review_note="Address confirmed", source_document="original-source")
    body["schedules"]["real_estate"] = [original]
    draft = FinancialFormDraft(family_id=family.id, body=body, revision=4, updated_at=ws.now())
    db.objects[(FinancialFormFamily, family.id)] = family
    for market_value, mortgage in (("500000", "190000"), ("550000", "180000")):
        file_id = uuid4()
        raw = f"%PDF property value {market_value}".encode()
        content_hash = hashlib.sha256(raw).hexdigest()
        file = SimpleNamespace(id=file_id, bucket_id=profile.primary_bucket_id, deleted_at=None, status="uploaded",
            content_type="application/pdf", file_name="Revised PFS.pdf", content_hash=content_hash,
            uploaded_by_name="Jane Smith", created_at=ws.now())
        db.objects[(BucketFile, file_id)] = file
        analysis = SimpleNamespace(id=uuid4(), content_hash=content_hash, classification="personal_financial_statement",
            analysis={"key_facts": {"applicant_name": "Jane Smith", "statement_date": "2026-11-05"}})
        extracted = ws.empty_body("pfs")
        extracted["schedules"]["real_estate"] = [line(property_address=" 100 MAIN STREET ", present_market_value=market_value,
                                                          mortgage_balance=mortgage)]
        with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(
            ws, "_read_file", return_value=raw
        ), patch.object(ws, "_extract_full_form", AsyncMock(return_value=(extracted, {}, [], {}))), patch.object(
            ws, "load_workspace", AsyncMock(return_value={})
        ):
            proposal = await ws.propose_import(db, profile, kind="pfs", file_id=file_id, family_id=family.id, analysis=analysis)
            assert proposal["proposed_body"]["schedules"]["real_estate"][0]["id"] == original["id"]
            assert f"real_estate.{original['id']}.original_cost" not in {mapping["key"] for mapping in proposal["mappings"]}
            await ws.apply_import(db, profile, db.imports[-1].id, expected_revision=draft.revision)
        properties = draft.body["schedules"]["real_estate"]
        assert len(properties) == 1 and properties[0]["id"] == original["id"]
        assert properties[0]["present_market_value"] == market_value and properties[0]["mortgage_balance"] == mortgage
        assert properties[0]["original_cost"] == "300000"
        assert properties[0]["review_note"] == "Address confirmed" and properties[0]["source_document"] == "original-source"


def test_stock_lots_with_same_security_and_shares_require_unique_stable_cost():
    first = line(name_of_security="Acme", number_of_shares="100", cost="1000", market_value="1200")
    second = line(name_of_security="Acme", number_of_shares="100", cost="1500", market_value="1200")
    proposed = {"schedules": {"stocks_and_bonds": [
        line(name_of_security="ACME", number_of_shares="100", cost="1500", market_value="1300"),
        line(name_of_security="ACME", number_of_shares="100", cost="1000", market_value="1300"),
    ]}}
    warnings = []
    aligned, _ = ws._align_pfs_import_rows(proposed, {"schedules": {"stocks_and_bonds": [first, second]}}, warnings)
    assert [row["id"] for row in aligned["schedules"]["stocks_and_bonds"]] == [second["id"], first["id"]]
    assert warnings == []


def test_ambiguous_stock_lots_cannot_match_on_current_market_values():
    stored = [line(name_of_security="Acme", number_of_shares="100", cost="1000", market_value=value)
              for value in ("1200", "1300")]
    incoming = [line(name_of_security="Acme", number_of_shares="100", cost="1000", market_value=value)
                for value in ("1200", "1300")]
    warnings = []
    aligned, _ = ws._align_pfs_import_rows({"schedules": {"stocks_and_bonds": incoming}},
        {"schedules": {"stocks_and_bonds": stored}}, warnings)
    assert [row["id"] for row in aligned["schedules"]["stocks_and_bonds"]] == [row["id"] for row in incoming]
    assert len(warnings) == 2 and all("multiple rows" in warning for warning in warnings)


def test_matching_amounts_cannot_identify_an_unknown_pfs_asset_or_insurance_policy():
    retirement = line(institution="Bank A", account_type="IRA", present_value="1000")
    unknown = line(present_value="1000")
    policies = [line(insurance_company="Insurer", beneficiary="Jane", face_amount=value) for value in ("100000", "200000")]
    incoming_policy = line(insurance_company="Insurer", beneficiary="Jane", face_amount="100000")
    warnings = []
    aligned, _ = ws._align_pfs_import_rows({"schedules": {"retirement": [unknown], "life_insurance": [incoming_policy]}},
        {"schedules": {"retirement": [retirement], "life_insurance": policies}}, warnings)
    assert aligned["schedules"]["retirement"][0]["id"] == unknown["id"]
    assert aligned["schedules"]["life_insurance"][0]["id"] == incoming_policy["id"]
    assert any("no complete identifying description" in warning for warning in warnings)
    assert any("multiple rows" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_cached_pfs_remap_rewrites_identity_keys_and_keeps_source_page_references():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind="pfs", identity={}, identity_key="test", label="PFS")
    old = line(property_address="100 Main Street", mortgage_balance="200000", verification="Reviewed")
    source = line(property_address="100 Main Street", mortgage_balance="180000")
    current_body = ws.empty_body("pfs")
    current_body["schedules"]["real_estate"] = [old]
    proposed_body = copy.deepcopy(current_body)
    proposed_body["schedules"]["real_estate"] = [source]
    draft = FinancialFormDraft(family_id=family.id, body=current_body, revision=8, updated_at=ws.now())
    source_id = uuid4()
    cached = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=source_id, content_hash="a" * 64,
        status="applied", base_revision=2, proposed_body=proposed_body, warnings=[],
        mappings=[{"key": f"real_estate.{source['id']}.mortgage_balance", "value": "180000",
                   "source": {"file_id": str(source_id), "page": 2}, "confidence": "high"}])
    file = SimpleNamespace(id=source_id, bucket_id=profile.primary_bucket_id, deleted_at=None, status="uploaded",
        content_type="application/pdf", file_name="PFS.pdf", content_hash="a" * 64)
    db = SimpleNamespace(get=AsyncMock(return_value=file), execute=AsyncMock(return_value=Result(scalar=cached)), add=lambda _: None, flush=AsyncMock())
    analysis = SimpleNamespace(content_hash="a" * 64, classification="personal_financial_statement", analysis={"key_facts": {}})
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_extract_full_form", AsyncMock()) as model:
        remapped = await ws.propose_import(db, profile, kind="pfs", file_id=source_id, family_id=family.id, analysis=analysis)
    assert remapped["mappings"][0]["key"] == f"real_estate.{old['id']}.mortgage_balance"
    assert remapped["mappings"][0]["source"]["page"] == 2
    assert remapped["proposed_body"]["schedules"]["real_estate"][0]["verification"] == "Reviewed"
    model.assert_not_awaited()


def test_aligned_pfs_rows_retain_ordinal_source_quotes():
    old = line(property_address="100 Main Street", mortgage_balance="200000")
    proposed = line(property_address="100 Main Street", mortgage_balance="180000")
    body, _ = ws._align_pfs_import_rows({"schedules": {"real_estate": [proposed]}}, {"schedules": {"real_estate": [old]}}, [])
    mappings = ws._mapping_records("pfs", body, SimpleNamespace(id=uuid4()), None, "a" * 64,
        {"real_estate.r1.mortgage_balance": {"page": 2, "quote": "Mortgage balance $180,000"}}, {2: "Mortgage balance $180,000"})
    mortgage = next(mapping for mapping in mappings if mapping["key"] == f"real_estate.{old['id']}.mortgage_balance")
    assert mortgage["confidence"] == "high" and mortgage["source"]["page"] == 2
