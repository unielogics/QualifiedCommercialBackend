import copy
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.application_profile import (
    ApplicationRequirementEvidence,
    ApplicationRequirementState,
)
from app.models.bucket import BucketFile
from app.models.financial_form_version import FinancialFormFamily, FinancialFormImport
from app.models.user import User
from app.services import real_estate_evidence as evidence
from app.services import real_estate_schema


class Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class MemoryDB:
    def __init__(self, rows):
        self.rows = list(rows)

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        pass

    async def get(self, model, row_id):
        return next((row for row in self.rows if isinstance(row, model) and row.id == row_id), None)

    async def execute(self, query):
        model = query.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if isinstance(row, model)]
        params = query.compile().params
        for attr in ("family_id", "profile_id", "source_file_id", "bucket_file_id", "content_hash", "requested_document_id", "requirement_state_id", "file_id", "idempotency_key"):
            values = [value for key, value in params.items() if key.startswith(attr + "_")]
            if values:
                rows = [row for row in rows if getattr(row, attr) == values[0]]
        if model is FinancialFormImport:
            rows = [row for row in rows if (row.proposed_body or {}).get("import_scope") == evidence.SCOPE]
            rows.reverse()
        return Result(rows)


@pytest.fixture
def setup(monkeypatch):
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=None)
    family = FinancialFormFamily(id=uuid4(), profile_id=profile.id, kind=evidence.KIND)
    prop = {"id": str(uuid4()), "address": "123 Main Street Unit 4", "city": "Miami", "state": "FL", "mortgage_status": "active"}
    loan = {"id": str(uuid4()), "property_row_id": prop["id"], "lender": "Example Bank", "loan_reference": "1111",
            "status": "active", "balance": "90000", "monthly_payment": "900"}
    second = {**loan, "id": str(uuid4()), "loan_reference": "2222"}
    draft = SimpleNamespace(revision=0, body=real_estate_schema.validate({"owner_name": "Investor", "properties": [prop], "mortgages": [loan, second]}))
    prop, loan, second = draft.body["properties"][0], *draft.body["mortgages"]
    raw = b"Mortgage statement: 123 Main Street Unit 4. Example Bank. Loan 1111. Balance 100000. Payment 1000. November 1 2026."
    file = BucketFile(id=uuid4(), bucket_id=profile.primary_bucket_id, file_name="Statement.pdf", content_type="application/pdf",
        content_hash=hashlib.sha256(raw).hexdigest(), status="uploaded", requested_document_id=None)
    actor = User(id=uuid4(), role=Role.LOAN_EXEC, name="Staff", email="staff@example.com")
    db = MemoryDB([family, file, actor])
    source_prop = {**prop, "id": str(uuid4())}
    source_loan = {**loan, "id": str(uuid4()), "property_row_id": source_prop["id"], "balance": "100000",
                   "monthly_payment": "1000", "statement_date": "2026-11-01"}
    extracted = real_estate_schema.validate({"properties": [source_prop], "mortgages": [source_loan]})
    extractor = AsyncMock(return_value=(extracted, {f"mortgages.{source_loan['id']}.balance": {"page": 1, "quote": "Balance 100000"}}, [], {1: raw.decode()}))

    async def save(_db, _profile, *, body, expected_revision, **kwargs):
        assert expected_revision == draft.revision
        draft.body = real_estate_schema.validate(body)
        draft.revision += 1
        return snapshot()

    def snapshot():
        return {"family_id": str(family.id), "body": copy.deepcopy(draft.body), "draft": {"revision": draft.revision}}

    monkeypatch.setattr(evidence.workspace, "resolve_family", AsyncMock(return_value=(family, draft)))
    monkeypatch.setattr(evidence.workspace, "save_draft", AsyncMock(side_effect=save))
    monkeypatch.setattr(evidence.workspace, "load_workspace", AsyncMock(side_effect=lambda *a, **k: snapshot()))
    monkeypatch.setattr(evidence.workspace, "_read_file", lambda _file: raw)
    monkeypatch.setattr(evidence.workspace, "_extract_full_form", extractor)
    return SimpleNamespace(db=db, profile=profile, family=family, prop=prop, loan=loan, second=second,
        draft=draft, raw=raw, file=file, actor=actor, extracted=extracted, source_loan=source_loan, extractor=extractor)


async def propose(s, **overrides):
    return await evidence.propose_import(s.db, s.profile, family_id=s.family.id, file_id=s.file.id,
        expected_revision=s.draft.revision, actor=s.actor, **overrides)


async def targeted(s, **overrides):
    return await propose(s, property_row_id=s.prop["id"], mortgage_row_id=s.loan["id"], **overrides)


async def apply(s, proposal, keys=(), **overrides):
    return await evidence.apply_import(s.db, s.profile, UUID(proposal["id"]), expected_revision=s.draft.revision,
        keys=list(keys), confirm_association=True, confirm_latest_statement=True, actor=s.actor, **overrides)


@pytest.mark.asyncio
async def test_preview_only_proposes_and_rebind_reuses_full_extraction_with_source_pages(setup):
    s = setup
    original = copy.deepcopy(s.draft.body)
    preview = await propose(s)
    assert preview["evidence"]["needs_association"] and preview["mappings"] == []
    assert len(preview["evidence"]["candidates"]) == 1
    matches = preview["evidence"]["associations"]
    assert any(match["mortgage_row_id"] == s.loan["id"] for match in matches)
    assert not any(match["mortgage_row_id"] == s.second["id"] for match in matches)
    bound = await targeted(s)
    mapping = next(item for item in bound["mappings"] if item["key"].endswith(".balance"))
    assert mapping["key"] == f"mortgages.{s.loan['id']}.balance"
    assert Decimal(mapping["before"]) == 90000 and mapping["conflict"]
    assert mapping["source"]["content_hash"] == s.file.content_hash and mapping["source"]["page"] == 1
    assert s.draft.body == original and s.draft.revision == 0
    s.extractor.assert_awaited_once()
    assert s.extractor.await_args.kwargs["document_role"] == "mortgage_statement"
    assert (await targeted(s))["id"] == bound["id"]


@pytest.mark.asyncio
async def test_zero_field_review_links_without_overwriting_manual_values_and_retry_is_idempotent(setup):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    assert Decimal(s.draft.body["mortgages"][0]["balance"]) == 90000
    assert s.draft.body["mortgages"][0][evidence.LINK_KEY] == [proposal["id"]]
    imported = await s.db.get(FinancialFormImport, UUID(proposal["id"]))
    assert imported.status == "applied" and imported.proposed_body["review"]["selected_keys"] == []
    await apply(s, proposal)
    assert s.draft.revision == 1


@pytest.mark.asyncio
async def test_only_selected_printed_fields_change_one_selected_loan(setup):
    s = setup
    proposal = await targeted(s)
    key = f"mortgages.{s.loan['id']}.balance"
    await apply(s, proposal, [key])
    assert Decimal(s.draft.body["mortgages"][0]["balance"]) == 100000
    assert Decimal(s.draft.body["mortgages"][0]["monthly_payment"]) == 900
    assert s.draft.body["mortgages"][1] == s.second
    assert set(s.draft.body["field_sources"]) == {key}


@pytest.mark.parametrize(("association", "latest"), [(False, True), (True, False), (False, False)])
@pytest.mark.asyncio
async def test_acceptance_requires_both_explicit_staff_confirmations(setup, association, latest):
    s = setup
    proposal = await targeted(s)
    with pytest.raises(HTTPException) as error:
        await evidence.apply_import(s.db, s.profile, UUID(proposal["id"]), expected_revision=0, keys=[],
            confirm_association=association, confirm_latest_statement=latest, actor=s.actor)
    assert error.value.status_code == 422 and s.draft.revision == 0


@pytest.mark.asyncio
async def test_changed_source_bytes_cannot_be_applied(setup, monkeypatch):
    s = setup
    proposal = await targeted(s)
    monkeypatch.setattr(evidence.workspace, "_read_file", lambda _file: b"changed PDF")
    with pytest.raises(HTTPException) as error:
        await apply(s, proposal)
    assert error.value.status_code == 409 and s.draft.revision == 0


@pytest.mark.asyncio
async def test_cross_profile_source_cannot_be_extracted(setup):
    s = setup
    s.file.bucket_id = uuid4()
    with pytest.raises(HTTPException) as error:
        await propose(s)
    assert error.value.status_code == 404
    s.extractor.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_printed_loans_require_source_selection_and_support_distinct_same_pdf_loans(setup):
    s = setup
    another = {**s.extracted["mortgages"][0], "id": str(uuid4()), "loan_reference": "2222"}
    s.extracted["mortgages"].append(another)
    preview = await propose(s)
    assert len(preview["evidence"]["candidates"]) == 2
    with pytest.raises(HTTPException) as error:
        await targeted(s)
    assert error.value.status_code == 422
    first = await targeted(s, source_mortgage_id=s.source_loan["id"])
    await apply(s, first)
    second = await propose(s, property_row_id=s.prop["id"], mortgage_row_id=s.second["id"], source_mortgage_id=another["id"])
    await apply(s, second)
    assert s.draft.body["mortgages"][0][evidence.LINK_KEY] != s.draft.body["mortgages"][1][evidence.LINK_KEY]
    s.extractor.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_source_loan_cannot_double_fulfill_different_destinations(setup):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    duplicate = await propose(s, property_row_id=s.prop["id"], mortgage_row_id=s.second["id"])
    with pytest.raises(HTTPException) as error:
        await apply(s, duplicate, association_override_reason="Reviewed but it is still the same printed source loan")
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_copied_proof_on_another_loan_rejected_before_publication(setup):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    copied = copy.deepcopy(s.draft.body)
    copied["mortgages"][1][evidence.LINK_KEY] = [proposal["id"]]
    with pytest.raises(HTTPException) as error:
        await evidence.validate_published_evidence(s.db, profile=s.profile, family=s.family, body=copied)
    assert error.value.status_code == 422 and "different property or mortgage" in error.value.detail


@pytest.mark.parametrize(("block", "field", "value"), [
    ("properties", "address", "123 Main Street Unit 5"), ("properties", "legal_owner", "Different owner"),
    ("mortgages", "lender", "Other Bank"), ("mortgages", "loan_reference", "3333"),
])
@pytest.mark.asyncio
async def test_repurposing_stable_ids_requires_new_evidence_review(setup, block, field, value):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    changed = copy.deepcopy(s.draft.body)
    changed[block][0][field] = value
    with pytest.raises(HTTPException) as error:
        await evidence.validate_published_evidence(s.db, profile=s.profile, family=s.family, body=changed)
    assert error.value.status_code == 422 and "changed" in error.value.detail


@pytest.mark.asyncio
async def test_balance_only_update_does_not_invalidate_reviewed_identity(setup):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    s.draft.body["mortgages"][0]["balance"] = "80000"
    await evidence.validate_published_evidence(s.db, profile=s.profile, family=s.family, body=s.draft.body)


@pytest.mark.asyncio
async def test_withdrawal_is_durable_and_old_draft_or_retry_cannot_restore_it(setup):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    old_body = copy.deepcopy(s.draft.body)
    await evidence.unlink_evidence(s.db, s.profile, family_id=s.family.id, expected_revision=s.draft.revision,
        mortgage_row_id=UUID(s.loan["id"]), import_id=UUID(proposal["id"]), actor=s.actor)
    assert s.draft.body["mortgages"][0][evidence.LINK_KEY] == []
    for operation in (evidence.validate_published_evidence(s.db, profile=s.profile, family=s.family, body=old_body), apply(s, proposal)):
        with pytest.raises(HTTPException):
            await operation


@pytest.mark.asyncio
async def test_rejected_proposal_cannot_be_accepted_by_retry(setup):
    s = setup
    proposal = await targeted(s)
    await evidence.reject_import(s.db, s.profile, UUID(proposal["id"]), actor=s.actor)
    with pytest.raises(HTTPException) as error:
        await apply(s, proposal)
    assert error.value.status_code == 409 and s.draft.revision == 0


@pytest.mark.asyncio
async def test_new_property_and_loan_require_accepted_printed_identity_and_keep_fk(setup):
    s = setup
    proposal = await propose(s, create_property=True, create_mortgage=True)
    with pytest.raises(HTTPException) as error:
        await apply(s, proposal)
    assert error.value.status_code == 422
    address = next(mapping["key"] for mapping in proposal["mappings"] if mapping["key"].startswith("properties.") and mapping["key"].endswith(".address"))
    lender = next(mapping["key"] for mapping in proposal["mappings"] if mapping["key"].startswith("mortgages.") and mapping["key"].endswith(".lender"))
    await apply(s, proposal, [address, lender])
    assert len(s.draft.body["properties"]) == 2 and len(s.draft.body["mortgages"]) == 3
    assert s.draft.body["mortgages"][-1]["property_row_id"] == s.draft.body["properties"][-1]["id"]


def test_matching_preserves_unit_and_never_uses_lender_alone_or_arbitrary_amount(setup):
    s = setup
    candidate = evidence.candidates(s.extracted)[0]
    candidate["property"]["address"] = "123 Main Street Unit 5"
    assert evidence.associations([candidate], s.draft.body) == []
    candidate["property"] = {}
    candidate["mortgage"]["loan_reference"] = None
    assert evidence.associations([candidate], s.draft.body) == []


@pytest.mark.asyncio
async def test_unverified_conflicting_reference_requires_written_association_override(setup):
    s = setup
    proposal = await propose(s, property_row_id=s.prop["id"], mortgage_row_id=s.second["id"])
    assert proposal["evidence"]["association_override_required"]
    with pytest.raises(HTTPException) as error:
        await apply(s, proposal)
    assert error.value.status_code == 422 and "Explain" in error.value.detail


@pytest.mark.asyncio
async def test_legacy_exact_request_upload_can_be_withdrawn_without_proof_or_moving_file(setup, monkeypatch):
    s = setup
    destination = uuid4()
    s.file.requested_document_id = destination
    state = ApplicationRequirementState(id=uuid4(), profile_id=s.profile.id, requirement_key="mortgage-request",
        requested_document_id=destination, status="verified", provenance={"source_kind": "financial_form_request",
            "source_context": {"family_id": str(s.family.id), "property_row_id": s.prop["id"], "mortgage_row_id": s.loan["id"]}})
    link = ApplicationRequirementEvidence(id=uuid4(), requirement_state_id=state.id, file_id=s.file.id,
        source="operator", verified_at=datetime.now(UTC), verified_by_user_id=s.actor.id)
    s.db.rows.extend([state, link])
    reject = AsyncMock()
    monkeypatch.setattr(evidence, "_decision", reject)
    await evidence.unlink_evidence(s.db, s.profile, family_id=s.family.id, expected_revision=0,
        mortgage_row_id=UUID(s.loan["id"]), file_id=s.file.id, actor=s.actor)
    reject.assert_awaited_once()
    assert reject.await_args.kwargs["accepted"] is False
    assert link.removed_at and link.removed_by_user_id == s.actor.id and link.verified_at is None
    assert s.file.requested_document_id == destination
    listed = await evidence.list_evidence(s.db, s.profile, family_id=s.family.id, actor=s.actor)
    assert listed["links"][0]["status"] == "invalid" and listed["links"][0]["withdrawn"]


@pytest.mark.asyncio
async def test_cancelled_collection_request_is_not_revived_by_saved_review_proof(setup, monkeypatch):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    decision = AsyncMock()
    monkeypatch.setattr(evidence, "_decision", decision)
    state = SimpleNamespace(status="not_applicable", provenance={"cancelled_at": "2026-11-06"})
    await evidence.sync_published_evidence(s.db, profile=s.profile, family=s.family,
        version=SimpleNamespace(id=uuid4(), body=s.draft.body), states=[state])
    decision.assert_not_awaited()


@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER, Role.CAPITAL_PARTNER])
@pytest.mark.asyncio
async def test_external_roles_cannot_forge_staff_review_acceptance(setup, role):
    s = setup
    proposal = await targeted(s)
    s.actor.role = role
    with pytest.raises(HTTPException) as error:
        await apply(s, proposal)
    assert error.value.status_code == 403 and s.draft.revision == 0


@pytest.mark.asyncio
async def test_current_reviewer_eligibility_is_rechecked_before_publication(setup):
    s = setup
    proposal = await targeted(s)
    await apply(s, proposal)
    s.actor.role = Role.DEALER_PARTNER
    with pytest.raises(HTTPException) as error:
        await evidence.validate_published_evidence(s.db, profile=s.profile, family=s.family, body=s.draft.body)
    assert error.value.status_code == 422 and "internal staff" in error.value.detail


@pytest.mark.asyncio
async def test_same_file_fresh_review_supersedes_proof_without_rejecting_physical_evidence(setup, monkeypatch):
    s = setup
    first = await targeted(s)
    await apply(s, first)
    old = await s.db.get(FinancialFormImport, UUID(first["id"]))
    second = await targeted(s)
    await apply(s, second)
    state = ApplicationRequirementState(id=uuid4(), profile_id=s.profile.id, requirement_key="mortgage-request", status="verified",
        provenance={"source_context": {"family_id": str(s.family.id), "property_row_id": s.prop["id"], "mortgage_row_id": s.loan["id"]}})
    link = ApplicationRequirementEvidence(id=uuid4(), requirement_state_id=state.id, file_id=s.file.id, source="operator",
        verified_at=datetime.now(UTC), verified_by_user_id=s.actor.id, provenance={"mortgage_statement_import_id": first["id"]})
    s.db.rows.extend([state, link])
    decision = AsyncMock()
    monkeypatch.setattr(evidence, "_decision", decision)
    monkeypatch.setattr(evidence.application_programs, "_latest_evidence_decisions", AsyncMock(return_value={}))
    await evidence.sync_published_evidence(s.db, profile=s.profile, family=s.family,
        version=SimpleNamespace(id=uuid4(), body=s.draft.body), states=[state])
    assert old.status == "superseded"
    decision.assert_awaited_once()
    assert decision.await_args.kwargs["accepted"] is True
    assert link.provenance["mortgage_statement_import_id"] == second["id"]


@pytest.mark.asyncio
async def test_duplicate_file_id_same_content_cannot_claim_another_loan_and_reuses_source_identity(setup):
    s = setup
    first = await targeted(s)
    await apply(s, first)
    duplicate = BucketFile(id=uuid4(), bucket_id=s.profile.primary_bucket_id, file_name="Statement copy.pdf",
        content_type="application/pdf", content_hash=s.file.content_hash, status="uploaded", requested_document_id=None)
    s.db.add(duplicate)
    proposal = await evidence.propose_import(s.db, s.profile, family_id=s.family.id, file_id=duplicate.id,
        expected_revision=s.draft.revision, property_row_id=s.prop["id"], mortgage_row_id=s.second["id"], actor=s.actor)
    assert proposal["evidence"]["source_mortgage_id"] == first["evidence"]["source_mortgage_id"]
    assert all(mapping["source"]["file_id"] == str(duplicate.id) for mapping in proposal["mappings"])
    with pytest.raises(HTTPException) as error:
        await apply(s, proposal, association_override_reason="This duplicate PDF cannot prove a second loan")
    assert error.value.status_code == 409
    s.extractor.assert_awaited_once()


@pytest.mark.parametrize("role", [Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER])
@pytest.mark.asyncio
async def test_evidence_router_blocks_external_referral_roles_before_profile_access(setup, monkeypatch, role):
    from app.routers import real_estate_evidence as routes

    s = setup
    s.actor.role = role
    load = AsyncMock()
    monkeypatch.setattr(routes, "_profile", load)
    with pytest.raises(HTTPException) as error:
        await routes._staff_profile(s.db, s.profile.id, s.actor)
    assert error.value.status_code == 403
    load.assert_not_awaited()
