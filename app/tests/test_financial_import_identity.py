import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os.models import DealerDebt
from app.models.bucket import BucketFile
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormImport,
    FinancialFormVersion,
)
from app.services import financial_workspace as ws


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


class ImportDB:
    def __init__(self):
        self.objects, self.imports, self.versions = {}, [], []

    def add(self, row):
        self.objects[(type(row), row.id)] = row
        if isinstance(row, FinancialFormImport):
            self.imports.append(row)
        elif isinstance(row, FinancialFormVersion):
            self.versions.append(row)

    async def get(self, model, row_id):
        return self.objects.get((model, row_id))

    async def flush(self):
        pass

    async def execute(self, statement):
        model = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if model is FinancialFormImport:
            source = next(value for key, value in params.items() if key.startswith("source_file_id_"))
            return Result(scalar=next((row for row in reversed(self.imports) if row.source_file_id == source), None))
        if model is FinancialFormVersion:
            return Result(self.versions)
        if model is BucketFile:
            ids = next(value for key, value in params.items() if key.startswith("id_"))
            return Result([self.objects[(BucketFile, value)] for value in ids if (BucketFile, value) in self.objects])
        if model is DealerDebt:
            return Result()
        raise AssertionError(f"Unexpected query: {statement}")


def debt(**changes):
    return {"id": str(uuid4()), "lender": "Oakwood", "balance": "100", "monthly_payment": "10",
            "status": "active", **changes}


@pytest.mark.asyncio
async def test_two_revised_source_pdfs_update_one_obligation_and_keep_payoff_association():
    db = ImportDB()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind="debt_schedule", identity={}, label="Debt", identity_key="test")
    payoff_id, agreement_id = uuid4(), uuid4()
    original = debt(origin="admin", count_in_dscr=False, vendor_key="OAKWOOD",
        evidence={"source_file_id": str(agreement_id), "source": "agreement"},
        evidence_links=[{"file_id": str(payoff_id), "role": "payoff", "is_current": True, "payoff_amount": "95"}])
    draft = FinancialFormDraft(family_id=family.id, body={"debts": [original]}, revision=4, updated_at=ws.now())
    db.objects[(FinancialFormFamily, family.id)] = family
    db.objects[(BucketFile, payoff_id)] = SimpleNamespace(id=payoff_id, bucket_id=profile.primary_bucket_id)
    for amount in ("80", "70"):
        file_id = uuid4()
        raw = f"%PDF balance {amount}".encode()
        content_hash = hashlib.sha256(raw).hexdigest()
        file = SimpleNamespace(id=file_id, bucket_id=profile.primary_bucket_id, deleted_at=None, status="uploaded",
            content_type="application/pdf", file_name="Revised schedule.pdf", content_hash=content_hash,
            uploaded_by_name="Client", created_at=ws.now())
        db.objects[(BucketFile, file_id)] = file
        analysis = SimpleNamespace(id=uuid4(), content_hash=content_hash, classification="debt_schedule", analysis={"key_facts": {}})
        extracted = {"debts": [debt(lender=" OAKWOOD ", balance=amount, monthly_payment=None)]}
        with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(
            ws, "_read_file", return_value=raw
        ), patch.object(ws, "_extract_full_form", AsyncMock(return_value=(extracted, {}, [], {}))), patch.object(
            ws, "load_workspace", AsyncMock(return_value={})
        ):
            proposed = await ws.propose_import(db, profile, kind="debt_schedule", file_id=file_id, family_id=family.id, analysis=analysis)
            assert proposed["proposed_body"]["debts"][0]["id"] == original["id"]
            assert f"{original['id']}.balance" in {mapping["key"] for mapping in proposed["mappings"]}
            # A source that omitted the payment must not claim the old answer.
            assert f"{original['id']}.monthly_payment" not in {mapping["key"] for mapping in proposed["mappings"]}
            await ws.apply_import(db, profile, db.imports[-1].id, expected_revision=draft.revision)
        assert len(draft.body["debts"]) == 1
        accepted = draft.body["debts"][0]
        assert accepted["id"] == original["id"] and accepted["balance"] == amount
        assert accepted["monthly_payment"] == "10.00" and accepted["count_in_dscr"] is False
        assert accepted["vendor_key"] == "OAKWOOD" and accepted["origin"] == "admin"
        assert accepted["evidence"]["source_file_id"] == str(agreement_id)
        assert any(link["file_id"] == str(payoff_id) and link["is_current"] for link in accepted["evidence_links"])
    assert len(draft.body["debts"][0]["evidence_links"]) == 3


def test_distinct_loans_at_one_lender_align_by_unique_figures_without_name_collapsing():
    first = debt(original_amount="1000", monthly_payment="10", balance="900")
    second = debt(original_amount="2000", monthly_payment="20", balance="1800")
    incoming_first = debt(original_amount="1000", monthly_payment="10", balance="850")
    incoming_second = debt(original_amount="2000", monthly_payment="20", balance="1750")
    warnings = []
    aligned, _ = ws._align_debt_import_rows({"debts": [incoming_second, incoming_first]}, {"debts": [first, second]}, warnings)
    assert [row["id"] for row in aligned["debts"]] == [second["id"], first["id"]]
    assert warnings == []


def test_new_second_loan_cannot_take_existing_identity_even_if_it_appears_first():
    first = debt(original_amount="1000", monthly_payment="10", balance="900")
    new = debt(original_amount="2000", monthly_payment="20", balance="1800")
    revised_first = debt(original_amount="1000", monthly_payment="10", balance="850")
    aligned, _ = ws._align_debt_import_rows({"debts": [new, revised_first]}, {"debts": [first]}, [])
    assert [row["id"] for row in aligned["debts"]] == [new["id"], first["id"]]


def test_ambiguous_same_lender_rows_stay_separate_with_review_warning():
    first, second, proposed = debt(), debt(), debt()
    warnings = []
    aligned, _ = ws._align_debt_import_rows({"debts": [proposed]}, {"debts": [first, second]}, warnings)
    assert aligned["debts"][0]["id"] == proposed["id"]
    assert warnings and "multiple possible obligations" in warnings[0]


def test_name_suffixes_are_never_silently_collapsed_and_exclusion_is_preserved():
    existing = debt(status="dismissed", origin="admin", count_in_dscr=False)
    revised, suffixed = debt(balance="90"), debt(lender="Oakwood LLC")
    aligned, _ = ws._align_debt_import_rows({"debts": [revised, suffixed]}, {"debts": [existing]}, [])
    assert aligned["debts"][0]["id"] == existing["id"]
    assert aligned["debts"][0]["status"] == "dismissed"
    assert aligned["debts"][0]["count_in_dscr"] is False
    assert aligned["debts"][1]["id"] == suffixed["id"]


def test_cached_source_remap_preserves_later_reconciliation_and_ordinal_citations():
    existing = debt(status="dismissed", origin="admin", count_in_dscr=False)
    proposed = debt(id=existing["id"], balance="80", status="active")
    current = copy.deepcopy(existing)
    current["lender"] = "Corrected lender name"
    aligned, _ = ws._align_debt_import_rows({"debts": [proposed]}, {"debts": [current]}, [])
    assert aligned["debts"][0]["id"] == current["id"] and aligned["debts"][0]["status"] == "dismissed"
    records = ws._mapping_records("debt_schedule", aligned, SimpleNamespace(id=uuid4()), None, "a" * 64,
        {"r1.balance": {"page": 1, "quote": "Current balance $80"}}, {1: "Current balance $80"})
    balance = next(record for record in records if record["key"] == f"{existing['id']}.balance")
    assert balance["confidence"] == "high" and balance["source"]["page"] == 1


@pytest.mark.asyncio
async def test_unselected_obligation_fields_do_not_attach_source_metadata():
    db = ImportDB()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind="debt_schedule", identity={}, label="Debt", identity_key="test")
    original = debt(status="dismissed", origin="admin", evidence={"source": "original"}, evidence_links=[])
    draft = FinancialFormDraft(family_id=family.id, body={"business_name": "Old business", "debts": [original]}, revision=2, updated_at=ws.now())
    source_id = uuid4()
    imported = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=source_id, content_hash="a" * 64,
        base_revision=2, status="proposed", proposed_body={"debts": [debt(id=original["id"], evidence={"source": "new source"},
            evidence_links=[{"file_id": str(source_id), "role": "supporting"}])]},
        mappings=[{"key": "business_name", "value": "New business", "source": {"file_id": str(source_id)}},
                  {"key": f"{original['id']}.balance", "value": "80", "source": {"file_id": str(source_id)}}])
    db.add(imported)
    db.objects[(FinancialFormFamily, family.id)] = family
    db.objects[(BucketFile, source_id)] = SimpleNamespace(bucket_id=profile.primary_bucket_id, deleted_at=None, content_hash="a" * 64)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})):
        await ws.apply_import(db, profile, imported.id, expected_revision=2, selected_keys=["business_name"])
    assert draft.body["business_name"] == "New business"
    accepted = draft.body["debts"][0]
    assert accepted["id"] == original["id"] and accepted["balance"] == original["balance"]
    assert accepted["status"] == "dismissed" and accepted["evidence"] == original["evidence"]
    assert accepted["evidence_links"] == []


@pytest.mark.asyncio
async def test_number_only_selection_cannot_create_an_unnamed_imported_obligation():
    db = ImportDB()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind="debt_schedule", identity={}, label="Debt", identity_key="test")
    draft = FinancialFormDraft(family_id=family.id, body={"debts": []}, revision=2, updated_at=ws.now())
    source_id, proposed = uuid4(), debt()
    imported = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=source_id, content_hash="a" * 64,
        base_revision=2, status="proposed", proposed_body={"debts": [proposed]}, mappings=[
            {"key": f"{proposed['id']}.lender", "value": "Oakwood", "source": {"file_id": str(source_id)}},
            {"key": f"{proposed['id']}.balance", "value": "80", "source": {"file_id": str(source_id)}},
        ])
    db.add(imported)
    db.objects[(FinancialFormFamily, family.id)] = family
    db.objects[(BucketFile, source_id)] = SimpleNamespace(bucket_id=profile.primary_bucket_id, deleted_at=None, content_hash="a" * 64)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))):
        with pytest.raises(HTTPException) as error:
            await ws.apply_import(db, profile, imported.id, expected_revision=2, selected_keys=[f"{proposed['id']}.balance"])
    assert error.value.status_code == 422 and "Select the lender field" in error.value.detail
    assert draft.body["debts"] == [] and draft.revision == 2
