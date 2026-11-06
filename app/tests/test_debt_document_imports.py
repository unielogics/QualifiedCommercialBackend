import copy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

from app.dealer_os.models import DealerDebt
from app.models.bucket import BucketFile, BucketFileAnalysis
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormImport,
    FinancialFormVersion,
)
from app.services import bucket_ai
from app.services import financial_debt_document_imports as contracts
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


class DB:
    def __init__(self):
        self.info, self.objects, self.imports, self.analyses, self.versions = {}, {}, [], [], []
        self.queries = []

    def add(self, row):
        self.objects[(type(row), row.id)] = row
        if isinstance(row, FinancialFormImport):
            self.imports.append(row)
        elif isinstance(row, FinancialFormVersion):
            self.versions.append(row)

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def flush(self):
        pass

    async def execute(self, query):
        self.queries.append(query)
        model, params = query.column_descriptions[0]["entity"], query.compile().params
        if model is BucketFile:
            requested = set(params.get("id_1", []))
            sources = {key: row for (kind, key), row in self.objects.items() if kind is BucketFile}
            rows = []
            for key, source in sources.items():
                if key not in requested or getattr(source, "deleted_at", None) is not None:
                    continue
                if getattr(source, "status", "uploaded") == "uploading" or getattr(source, "document_version_status", "current") == "duplicate":
                    continue
                family = getattr(source, "document_family_id", None) or key
                if any(
                    getattr(other, "bucket_id", None) == source.bucket_id
                    and (getattr(other, "document_family_id", None) or other_id) == family
                    and (getattr(other, "document_version_number", None) or 1) > (getattr(source, "document_version_number", None) or 1)
                    and getattr(other, "status", "uploaded") != "uploading"
                    and getattr(other, "document_version_status", "current") != "duplicate"
                    for other_id, other in sources.items()
                ):
                    continue
                allowed = next((value for name, value in params.items() if name.startswith("document_version_status_") and isinstance(value, list)), None)
                if allowed is not None and getattr(source, "document_version_status", "current") not in allowed:
                    continue
                rows.append(key if query.column_descriptions[0]["name"] == "id" else source)
            return Result(rows)
        if model is BucketFileAnalysis:
            requested = params["bucket_file_id_1"]
            rows = [row for row in self.analyses if row.bucket_file_id in requested] if isinstance(requested, list) else [row for row in self.analyses if row.bucket_file_id == requested]
            if "content_hash_1" in params:
                rows = [row for row in rows if row.content_hash == params["content_hash_1"]]
            return Result(reversed(rows), scalar=rows[-1] if rows else None)
        if model is FinancialFormImport:
            rows = [row for row in self.imports if row.family_id == params["family_id_1"] and row.source_file_id == params["source_file_id_1"]]
            if "param_1" in params:
                rows = [row for row in rows if row.proposed_body.get("target_debt_id") == params["param_1"]
                        and row.proposed_body.get("document_role") == params["param_2"]]
            else:
                rows = [row for row in rows if not row.proposed_body.get("target_debt_id")]
            if "content_hash_1" in params:
                rows = [row for row in rows if row.content_hash == params["content_hash_1"]]
            return Result(scalar=rows[-1] if rows else None)
        if model is FinancialFormVersion:
            return Result(self.versions)
        if model is DealerDebt:
            return Result()
        raise AssertionError(f"Unexpected query: {query}")


def debt(lender="Kapfi", **values):
    return {"id": str(uuid4()), "lender": lender, "origin": "admin", "status": "active", "count_in_dscr": True,
            "balance": "40000", "monthly_payment": None, "evidence_links": [], **values}


def context(*rows, classification="loan_agreement"):
    db = DB()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), dealer_id=None, intake_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind="debt_schedule", identity={"subject": "business"}, identity_key="test", label="Debt")
    draft = FinancialFormDraft(family_id=family.id, body={"debts": list(rows) or [debt()]}, revision=4, updated_at=ws.now())
    raw = b"%PDF contract source"
    file = SimpleNamespace(id=uuid4(), bucket_id=profile.primary_bucket_id, status="uploaded", deleted_at=None,
        content_type="application/pdf", file_name="Kapfi contract.pdf", content_hash=hashlib.sha256(raw).hexdigest(),
        uploaded_by_name="Client", created_at=ws.now())
    analysis = SimpleNamespace(id=uuid4(), bucket_file_id=file.id, content_hash=file.content_hash,
        status="completed", provider="legacy_analysis", classification=classification, analysis={"key_facts": {}})
    db.objects.update({(BucketFile, file.id): file, (FinancialFormFamily, family.id): family})
    db.analyses.append(analysis)
    return db, profile, family, draft, file, raw


def extracted(*rows):
    return {"debts": list(rows)}, {}, [], {}


@pytest.mark.asyncio
async def test_contract_preview_maps_native_terms_to_selected_loan_without_linking_or_publishing():
    selected, other = debt(factor_rate="1.25"), debt("Other loan", balance="9000")
    db, profile, family, draft, file, raw = context(selected, other)
    source = debt("Kapfi Capital LLC", factor_rate="1.38", payment_amount="420", payment_frequency="daily")
    references = {"r1.factor_rate": {"page": 1, "quote": "Factor rate 1.38"},
                  "r1.payment_amount": {"page": 2, "quote": "Payment $420 daily"},
                  "r1.payment_frequency": {"page": 2, "quote": "Payment $420 daily"}}
    before = copy.deepcopy(draft.body)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw), patch.object(
        ws, "_extract_full_form", AsyncMock(return_value=({"debts": [source]}, references, [], {1: "Factor rate 1.38", 2: "Payment $420 daily"}))
    ):
        proposal = await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id,
            expected_revision=4, family_id=family.id)
    assert draft.body == before and draft.revision == 4
    assert not db.versions and len(db.imports) == 1
    assert proposal["target_row_id"] == selected["id"] and proposal["role"] == "agreement"
    assert all(mapping["key"].startswith(selected["id"] + ".") for mapping in proposal["mappings"])
    by_field = {mapping["key"].rpartition(".")[2]: mapping for mapping in proposal["mappings"]}
    assert by_field["factor_rate"]["value"] == "1.380"
    assert by_field["factor_rate"]["before"] == "1.25" and by_field["factor_rate"]["conflict"]
    assert by_field["factor_rate"]["source"]["page"] == 1 and by_field["factor_rate"]["confidence"] == "high"
    assert by_field["monthly_payment"]["value"] == "8820.00" and by_field["monthly_payment"]["derived"]
    assert by_field["monthly_payment"]["source"]["kind"] == "calculation"
    assert by_field["monthly_payment"]["source"]["derived_from"]["payment_amount"]["page"] == 2
    assert "rate" not in by_field  # A factor must never turn into APR.


@pytest.mark.asyncio
async def test_apply_updates_selected_fields_and_links_only_this_loan_preserving_other_evidence():
    payoff_id = uuid4()
    selected = debt(monthly_payment="1000", evidence_links=[{"file_id": str(payoff_id), "role": "payoff", "is_current": True}])
    other = debt("Other loan", factor_rate="1.1", monthly_payment="500")
    db, profile, family, draft, file, raw = context(selected, other)
    db.objects[(BucketFile, payoff_id)] = SimpleNamespace(id=payoff_id, bucket_id=profile.primary_bucket_id)
    source = debt("Kapfi Capital LLC", factor_rate="1.38", payment_amount="420", payment_frequency="daily")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw), patch.object(
        ws, "_extract_full_form", AsyncMock(return_value=extracted(source))
    ), patch.object(ws, "load_workspace", AsyncMock(return_value={})):
        proposal = await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
        chosen = [f"{selected['id']}.{field}" for field in ("factor_rate", "payment_amount", "payment_frequency", "monthly_payment")]
        await ws.apply_import(db, profile, db.imports[-1].id, expected_revision=4, selected_keys=chosen)
    accepted, untouched = draft.body["debts"]
    assert accepted["id"] == selected["id"] and accepted["lender"] == "Kapfi"
    assert accepted["factor_rate"] == "1.380" and accepted["payment_amount"] == "420.00"
    assert accepted["payment_frequency"] == "daily" and accepted["monthly_payment"] == "8820.00"
    assert {(link["file_id"], link["role"]) for link in accepted["evidence_links"]} == {(str(payoff_id), "payoff"), (str(file.id), "agreement")}
    assert next(link for link in accepted["evidence_links"] if link["role"] == "payoff")["is_current"]
    assert untouched["factor_rate"] == "1.100" and untouched["evidence_links"] == []
    assert draft.body["field_sources"][f"{selected['id']}.payment_amount"]["content_hash"] == file.content_hash
    assert draft.revision == 5 and db.imports[-1].status == "applied" and not db.versions
    assert proposal["proposed_body"]["target_debt_id"] == selected["id"]


@pytest.mark.asyncio
async def test_unchecked_conflicting_monthly_cannot_change_via_selected_native_terms():
    selected = debt(monthly_payment="1000")
    db, profile, family, draft, file, raw = context(selected)
    original = copy.deepcopy(draft.body)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw), patch.object(
        ws, "_extract_full_form", AsyncMock(return_value=extracted(debt(payment_amount="420", payment_frequency="daily")))
    ):
        await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
        with pytest.raises(HTTPException) as error:
            await ws.apply_import(db, profile, db.imports[-1].id, expected_revision=4,
                selected_keys=[f"{selected['id']}.payment_amount", f"{selected['id']}.payment_frequency"])
    assert error.value.status_code == 409 and error.value.detail["code"] == "monthly_equivalent_review_required"
    assert draft.body == original and draft.revision == 4 and db.imports[-1].status == "proposed"


@pytest.mark.asyncio
async def test_printed_monthly_difference_is_retained_as_warning_beside_calculated_equivalent():
    selected = debt(monthly_payment="1000")
    db, profile, family, draft, file, raw = context(selected)
    source = debt(payment_amount="420", payment_frequency="daily", monthly_payment="8400")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw), patch.object(
        ws, "_extract_full_form", AsyncMock(return_value=extracted(source))
    ):
        proposal = await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
    monthly = next(mapping for mapping in proposal["mappings"] if mapping["key"] == f"{selected['id']}.monthly_payment")
    assert monthly["derived"] and monthly["value"] == "8820.00" and monthly["conflict"]
    assert monthly["source"]["printed_monthly_payment"] == "8400.00"
    assert any("also states a monthly payment" in warning for warning in proposal["warnings"])


@pytest.mark.asyncio
async def test_same_pdf_proposals_are_target_scoped_and_can_link_two_distinct_loans_after_review():
    first, second = debt("Lender A"), debt("Lender B")
    db, profile, family, draft, file, raw = context(first, second, classification="collateral_debt_evidence")
    source = extracted(debt("Lender A", factor_rate="1.38", payment_amount="420", payment_frequency="daily"),
                       debt("Lender B", factor_rate="1.2", payment_amount="100", payment_frequency="weekly"))
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw), patch.object(
        ws, "_extract_full_form", AsyncMock(return_value=source)
    ) as model, patch.object(ws, "load_workspace", AsyncMock(return_value={})):
        a = await contracts.propose_document_import(db, profile, row_id=UUID(first["id"]), file_id=file.id, expected_revision=4)
        b = await contracts.propose_document_import(db, profile, row_id=UUID(second["id"]), file_id=file.id, expected_revision=4)
        assert a["id"] != b["id"]
        assert next(m["value"] for m in a["mappings"] if m["key"].endswith(".factor_rate")) == "1.380"
        assert next(m["value"] for m in b["mappings"] if m["key"].endswith(".factor_rate")) == "1.200"
        await ws.apply_import(db, profile, UUID(a["id"]), expected_revision=4)
        b2 = await contracts.propose_document_import(db, profile, row_id=UUID(second["id"]), file_id=file.id, expected_revision=5)
        assert b2["id"] != b["id"] and b2["base_revision"] == 5
        await ws.apply_import(db, profile, UUID(b2["id"]), expected_revision=5)
        assert model.await_count == 2  # Row B remaps its own cached extraction.
    accepted_a, accepted_b = draft.body["debts"]
    assert accepted_a["factor_rate"] == "1.380" and accepted_b["factor_rate"] == "1.200"
    assert all(any(link["file_id"] == str(file.id) for link in row["evidence_links"]) for row in draft.body["debts"])


@pytest.mark.asyncio
async def test_whole_schedule_import_does_not_reuse_a_targeted_contract_cache():
    selected = debt()
    db, profile, family, draft, file, raw = context(selected, classification="collateral_debt_evidence")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw), patch.object(
        ws, "_extract_full_form", AsyncMock(side_effect=[extracted(debt(payment_amount="420", payment_frequency="daily")), extracted(debt(balance="39000"))])
    ) as model:
        targeted = await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
        whole = await ws.propose_import(db, profile, kind="debt_schedule", file_id=file.id, family_id=family.id, analysis=db.analyses[0])
    assert model.await_count == 2 and whole["id"] != targeted["id"]
    assert not whole["proposed_body"].get("target_debt_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["changed_bytes", "inactive_target", "cross_target_mapping"])
async def test_apply_rechecks_hash_target_and_mapping_scope_before_attaching(failure):
    selected, other = debt(), debt("Other loan")
    db, profile, family, draft, file, raw = context(selected, other)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file", return_value=raw) as read, patch.object(
        ws, "_extract_full_form", AsyncMock(return_value=extracted(debt(factor_rate="1.38")))
    ):
        await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
        if failure == "changed_bytes":
            read.return_value = b"changed PDF bytes"
        elif failure == "inactive_target":
            selected["status"] = "dismissed"
        else:
            db.imports[-1].mappings[0]["key"] = f"{other['id']}.factor_rate"
        with pytest.raises(HTTPException) as error:
            await ws.apply_import(db, profile, db.imports[-1].id, expected_revision=4)
    assert error.value.status_code in {409, 422}
    assert not any(row["evidence_links"] for row in draft.body["debts"])
    assert draft.revision == 4 and db.imports[-1].status == "proposed"


@pytest.mark.asyncio
async def test_foreign_file_and_wrong_target_are_rejected_without_reading_pdf():
    db, profile, family, draft, file, raw = context()
    file.bucket_id = uuid4()
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file") as read:
        with pytest.raises(HTTPException) as wrong_file:
            await contracts.propose_document_import(db, profile, row_id=UUID(draft.body["debts"][0]["id"]), file_id=file.id, expected_revision=4)
        with pytest.raises(HTTPException) as wrong_row:
            await contracts.propose_document_import(db, profile, row_id=uuid4(), file_id=file.id, expected_revision=4)
    assert wrong_file.value.status_code == wrong_row.value.status_code == 404
    read.assert_not_called()


@pytest.mark.asyncio
async def test_explicitly_selected_supporting_bucket_contract_is_allowed_and_attached():
    selected = debt()
    db, profile, family, draft, file, raw = context(selected)
    profile.intake_id, file.bucket_id = uuid4(), uuid4()
    with patch.object(ws, "source_files_for_profile", AsyncMock(return_value=[file])), patch.object(
        ws, "resolve_family", AsyncMock(return_value=(family, draft))
    ), patch.object(ws, "_read_file", return_value=raw), patch.object(ws, "_extract_full_form", AsyncMock(return_value=extracted(debt(factor_rate="1.38")))), patch.object(
        ws, "load_workspace", AsyncMock(return_value={})
    ):
        result = await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
        await ws.apply_import(db, profile, UUID(result["id"]), expected_revision=4)
    assert draft.body["debts"][0]["evidence_links"][0]["file_id"] == str(file.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["bank_statement", "tax_return", "personal_financial_statement"])
async def test_clearly_unrelated_documents_are_refused(classification):
    db, profile, family, draft, file, raw = context(classification=classification)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_extract_full_form", AsyncMock()) as model:
        with pytest.raises(HTTPException) as error:
            await contracts.propose_document_import(db, profile, row_id=UUID(draft.body["debts"][0]["id"]), file_id=file.id, expected_revision=4)
    assert error.value.status_code == 409 and not db.imports
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_generated_financial_version_cannot_be_its_own_contract_source():
    db, profile, family, draft, file, raw = context(classification="debt_schedule")
    db.analyses[0].provider = "drafted_form"
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_read_file") as read:
        with pytest.raises(HTTPException) as error:
            await contracts.propose_document_import(db, profile, row_id=UUID(draft.body["debts"][0]["id"]), file_id=file.id, expected_revision=4)
    assert error.value.status_code == 409 and not db.imports
    read.assert_not_called()


def test_multiple_same_lender_contracts_are_ambiguous_and_amounts_cannot_guess_one():
    target = debt(monthly_payment="100")
    with pytest.raises(HTTPException) as error:
        contracts.select_obligation({"debts": [debt(payment_amount="100"), debt(payment_amount="200")]}, target)
    assert error.value.status_code == 409 and error.value.detail["code"] == "ambiguous_contract_obligation"
    with pytest.raises(HTTPException) as empty:
        contracts.select_obligation({"debts": [{"id": str(uuid4()), "lender": "Kapfi", "notes": "Unreadable terms"}]}, target)
    assert empty.value.status_code == 422


@pytest.mark.asyncio
async def test_declared_acroform_native_terms_override_model_values():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    fields = []
    for name, value in {"ds.r1.factor_rate": "1.38", "ds.r1.payment_amount": "420", "ds.r1.payment_frequency": "daily"}.items():
        fields.append(writer._add_object(DictionaryObject({NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject(name), NameObject("/V"): TextStringObject(value)})))
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(DictionaryObject({NameObject("/Fields"): ArrayObject(fields)}))
    output = io.BytesIO()
    writer.write(output)
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": {
        "debts": [{"lender": "Kapfi", "factor_rate": "9.99", "payment_amount": "1", "payment_frequency": "monthly"}]
    }}))])
    with patch.object(bucket_ai, "_append_review_file_content", return_value=(True, 1, 0)), patch(
        "app.services.ai.bedrock_client.get_client", return_value=object()
    ), patch("app.services.ai.bedrock_client.model_heavy", return_value="test"), patch(
        "app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)
    ):
        body, _, _, _ = await ws._extract_full_form(AsyncMock(), "debt_schedule", SimpleNamespace(id=uuid4()), output.getvalue(), extra_instructions=contracts.CONTRACT_INSTRUCTIONS)
    accepted = body["debts"][0]
    assert accepted["factor_rate"] == "1.380" and accepted["payment_amount"] == "420.00"
    assert accepted["payment_frequency"] == "daily" and accepted["monthly_payment"] == "8820.00"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,status,deleted", [
    ("pending", "uploaded", None), ("failed", "uploaded", None),
    ("rejected", "rejected", None), ("current", "uploaded", ws.now()),
])
async def test_replaced_contract_is_not_remapped_or_attached_when_latest_source_is_unusable(state, status, deleted):
    selected = debt()
    db, profile, family, draft, source, _raw = context(selected)
    source.document_family_id = source.id
    source.document_version_number = 1
    latest_id = uuid4()
    db.objects[(BucketFile, latest_id)] = SimpleNamespace(
        id=latest_id, bucket_id=source.bucket_id, document_family_id=source.id,
        document_version_number=2, document_version_status=state,
        status=status, deleted_at=deleted,
    )
    before = copy.deepcopy(draft.body)
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "_extract_full_form", AsyncMock()) as extract:
        with pytest.raises(HTTPException) as error:
            await contracts.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=source.id, expected_revision=4)
    assert error.value.status_code == 409
    extract.assert_not_called()
    assert not db.imports and not db.versions and draft.body == before
