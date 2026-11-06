from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import financial_debt_intake as intake


def source(classification="debt_schedule", count=1, **raw):
    return {"file_id": str(uuid4()), "file_name": "Uploaded source.pdf", "content_hash": uuid4().hex,
            "analysis_id": str(uuid4()), "classification": classification, "confidence": "high", "created_at": "2026-10-01",
            "facts": {"entity": "Example Inc", "business_name": "Example Inc", "debts": [
                {"lender": f"Funder {index}", "current_balance": 1000, "monthly_payment": 100, **raw}
                for index in range(count)]}}


def body():
    return {"business_name": "Example Inc", "debts": []}


def test_uploaded_full_schedule_wins_over_agreements_and_payoff_rows():
    schedule = source(count=7)
    contracts = [source("floorplan_mca_inventory"), source("payoff_or_mortgage_statement")]
    proposals = intake.document_proposals(body(), [schedule, *contracts], uuid4())
    assert len(proposals) == 7
    assert all(row["body"]["origin"] == "ai_draft" and row["eligible"] for row in proposals)
    assert all(row["body"]["evidence_links"][0]["file_id"] == schedule["file_id"] for row in proposals)


def test_empty_debt_array_does_not_hide_loan_rows_or_printed_top_level_terms():
    document = source("floorplan_mca_inventory", count=0)
    document["facts"]["loans"] = [{"lender": "Other funder", "monthly_payment": 100}]
    assert len(intake.document_proposals(body(), [document], uuid4())) == 1
    document["facts"].pop("loans")
    document["facts"].update(funder="Top-level funder", monthly_payment=100)
    assert len(intake.document_proposals(body(), [document], uuid4())) == 1


def test_current_original_is_preferred_over_a_newer_alternative_upload():
    current, alternative = source(count=7), source(count=9)
    alternative["created_at"] = "2026-11-01"
    proposals = intake.document_proposals(body(), [alternative, current], uuid4(), current["file_id"])
    assert len(proposals) == 7 and all(row["file_ids"][0] == current["file_id"] for row in proposals)


def test_stated_monthly_is_not_multiplied_by_daily_cadence_or_copied_to_native_payment():
    row, = intake.document_proposals(body(), [source(payment_frequency="daily", monthly_payment=500, factor_rate="1.49")], uuid4())
    assert row["body"]["monthly_payment"] == 500
    assert row["body"]["payment_amount"] is None and row["body"]["monthly_payment_source"] == "stated"
    assert row["body"]["factor_rate"] is not None and row["body"]["rate"] is None


def test_missing_or_invalid_financial_values_preserve_the_named_loan_and_source():
    row, = intake.document_proposals(body(), [source(monthly_payment="varies", current_balance="unknown", factor_rate="not stated")], uuid4())
    assert row["eligible"] and row["lender"] == "Funder 0"
    assert row["body"]["monthly_payment"] is None and row["body"]["balance"] is None and row["body"]["factor_rate"] is None
    assert row["warnings"] and row["source_refs"][0]["analysis_id"]


@pytest.mark.parametrize("field", ["monthly_payment", "current_balance", "original_amount", "payoff_amount", "payment_amount"])
def test_invalid_negative_debt_amounts_do_not_delete_a_named_source_obligation(field):
    proposal, = intake.document_proposals(body(), [source(**{field: -1})], uuid4())
    output_field = "balance" if field == "current_balance" else field
    assert proposal["body"][output_field] is None
    assert proposal["lender"] == "Funder 0" and proposal["source_refs"]
    assert any("nonnegative" in warning for warning in proposal["warnings"])


def test_payoff_amount_does_not_invent_a_principal_balance_or_select_current_payoff():
    row, = intake.document_proposals(body(), [source("payoff_or_mortgage_statement", current_balance=None, payoff_amount=1050)], uuid4())
    assert row["body"]["balance"] is None and row["body"]["payoff_amount"] == 1050
    assert row["body"]["evidence_links"][0]["role"] == "payoff"
    assert not row["body"]["evidence_links"][0]["is_current"]


def test_explicit_schedule_entity_mismatch_blocks_import_but_unknown_identity_warns():
    wrong = source()
    wrong["facts"].pop("business_name")
    wrong["facts"]["entity"] = "Different business LLC"
    proposal, = intake.document_proposals(body(), [wrong], uuid4())
    assert not proposal["eligible"] and "association" in proposal["reason"]
    wrong["facts"].pop("entity")
    unknown, = intake.document_proposals(body(), [wrong], uuid4())
    assert unknown["eligible"] and any("identity" in warning for warning in unknown["warnings"])


def test_business_association_retains_legal_entity_words_while_normalizing_punctuation():
    document = source()
    document["facts"]["business_name"] = "EXAMPLE, INC."
    assert intake.document_proposals(body(), [document], uuid4())[0]["eligible"]
    document["facts"]["business_name"] = "Example LLC"
    assert not intake.document_proposals(body(), [document], uuid4())[0]["eligible"]


def test_exact_loan_reference_combines_contract_support_but_same_lender_does_not():
    agreement = source("floorplan_mca_inventory", loan_reference="loan-A")
    payoff = source("payoff_or_mortgage_statement", loan_reference="loan-A", payoff_amount=1050)
    combined = intake.document_proposals(body(), [agreement, payoff], uuid4())
    assert len(combined) == 1 and len(combined[0]["body"]["evidence_links"]) == 2
    payoff["facts"]["debts"][0]["loan_reference"] = "loan-B"
    assert len(intake.document_proposals(body(), [agreement, payoff], uuid4())) == 2


def test_conflicting_supporting_terms_do_not_overwrite_the_first_reviewable_value():
    agreement = source("floorplan_mca_inventory", loan_reference="loan-A", monthly_payment=100)
    payoff = source("payoff_or_mortgage_statement", loan_reference="loan-A", monthly_payment=200)
    proposal, = intake.document_proposals(body(), [agreement, payoff], uuid4())
    assert proposal["body"]["monthly_payment"] == 100
    assert any("disagree" in warning for warning in proposal["warnings"])
    for item in [agreement, payoff]:
        item["facts"]["debts"][0].pop("loan_reference")
        item["facts"]["debts"][0]["account_number"] = "shared deposit account"
    assert len(intake.document_proposals(body(), [agreement, payoff], uuid4())) == 2


def test_excluded_source_does_not_return_but_a_known_distinct_loan_can_be_reviewed():
    family = uuid4()
    original = source(loan_reference="loan-A")
    proposed, = intake.document_proposals(body(), [original], family)
    excluded = deepcopy(proposed["body"])
    excluded["status"] = "dismissed"
    working = {**body(), "debts": [excluded]}
    assert not intake.document_proposals(working, [original], family)[0]["eligible"]
    replacement = source(loan_reference="loan-B")
    assert intake.document_proposals(working, [replacement], family)[0]["eligible"]


def test_same_pdf_reanalysis_changes_selection_id_when_values_or_reading_change():
    family = uuid4()
    old = source(monthly_payment=100)
    before, = intake.document_proposals(body(), [old], family)
    updated = deepcopy(old)
    updated["analysis_id"] = str(uuid4())
    updated["facts"]["debts"][0]["monthly_payment"] = 200
    after, = intake.document_proposals(body(), [updated], family)
    assert before["id"] != after["id"]
    with pytest.raises(HTTPException) as error:
        intake.apply_selected(body(), [after], [before["id"]], idempotency_key="request-1", mode="documents")
    assert error.value.status_code == 409


def test_apply_preserves_manual_and_excluded_rows_and_is_idempotent():
    existing = {**body(), "debts": [{"id": str(uuid4()), "lender": "Manual", "balance": "500", "status": "active"},
                                    {"id": str(uuid4()), "lender": "Excluded", "status": "dismissed"}]}
    before = deepcopy(existing)
    rows = intake.document_proposals(existing, [source()], uuid4())
    applied, changed = intake.apply_selected(existing, rows, [rows[0]["id"]], idempotency_key="request-1", mode="documents")
    assert changed and applied["debts"][:2] == before["debts"] and existing == before
    repeated, changed = intake.apply_selected(applied, [], [rows[0]["id"]], idempotency_key="request-1", mode="documents")
    assert not changed and repeated == applied


@pytest.mark.parametrize("working,accepted", [({"debts": [], "no_debt": True}, False),
    ({"debts": [{"lender": "Known", "balance": 100, "monthly_payment": 10}]}, False), (body(), True)])
def test_complete_and_no_debt_schedules_block_bulk_drafting(working, accepted):
    assert intake.blocked_reason(working, accepted)


@pytest.mark.asyncio
async def test_initial_seed_is_ai_only_preserves_original_pointer_and_is_idempotent():
    family = SimpleNamespace(id=uuid4(), current_version_id=uuid4())
    pointer = family.current_version_id
    draft = SimpleNamespace(body=body(), revision=0)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(source_file_id=None)))

    async def stage(_db, _profile, _family, current, updated, *, actor=None):
        assert actor is None
        current.body, current.revision = updated, current.revision + 1

    with patch.object(intake, "source_documents", AsyncMock(return_value=[source(count=7)])), \
         patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.financial_workspace.stage_draft", side_effect=stage), \
         patch("app.services.financial_workspace.publish_version", AsyncMock()) as publish:
        result = await intake.initial_seed(db, SimpleNamespace(id=uuid4()), family, draft)
        repeated = await intake.initial_seed(db, SimpleNamespace(id=uuid4()), family, draft)
    assert result["seeded"] and result["proposal_count"] == 7 and not repeated["seeded"]
    assert family.current_version_id == pointer
    assert all(row["origin"] == "ai_draft" for row in draft.body["debts"])
    assert draft.body["debt_intake_review"]["status"] == "pending_review"
    publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("working,revision", [(body(), 1), ({**body(), "no_debt": True}, 0),
    ({**body(), "debts": [{"id": str(uuid4()), "lender": "Old", "status": "dismissed"}]}, 0)])
async def test_initial_seed_never_refills_a_deliberate_clear_or_excluded_history(working, revision):
    with patch.object(intake, "source_documents", AsyncMock()) as read:
        result = await intake.initial_seed(None, None, SimpleNamespace(), SimpleNamespace(body=working, revision=revision))
    assert not result["seeded"]
    read.assert_not_called()


@pytest.mark.asyncio
async def test_banking_draft_is_explicit_and_preserves_unknown_balances_and_contract_terms():
    from app.services import financial_cockpit as cockpit

    family = SimpleNamespace(id=uuid4())
    draft = SimpleNamespace(body=body(), revision=3)
    profile = SimpleNamespace(id=uuid4(), bank_account_aliases={})
    analysis = [{"file_id": str(uuid4()), "classification": "bank_statement", "accepted": True, "key_facts": {
        "bank": "Chase", "account_last4": "1234", "statement_period": "2026-05", "recurring_debits": [
            {"payee": "Lender Funding", "occurrences": 4, "total_amount": 400, "cadence": "weekly", "kind_guess": "loan"},
            {"payee": "Payroll services", "occurrences": 4, "total_amount": 4000, "cadence": "monthly", "kind_guess": "payroll"}]}}]
    _, accounts = cockpit.bank_months(analysis)
    with patch("app.services.financial_workspace.resolve_family", AsyncMock(return_value=(family, draft))), \
         patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.financial_debt_review.source_context", AsyncMock(return_value=(analysis, accounts))):
        preview = await intake.proposals(None, profile, mode="banking", expected_revision=3)
    assert len(preview["proposals"]) == 1 and draft.body == body()
    proposed = preview["proposals"][0]["body"]
    assert proposed["balance"] is proposed["monthly_payment"] is proposed["debt_type"] is None
    assert proposed["evidence"]["observed_monthly_average"] == 400


@pytest.mark.asyncio
async def test_initial_seed_blocks_current_saved_authority_before_reading_more_sources():
    draft = SimpleNamespace(body=body(), revision=0)
    with patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=SimpleNamespace())), \
         patch.object(intake, "source_documents", AsyncMock()) as read:
        result = await intake.initial_seed(None, SimpleNamespace(id=uuid4()), SimpleNamespace(), draft)
    assert not result["seeded"]
    read.assert_not_called()


@pytest.mark.asyncio
async def test_existing_virgin_workspace_can_seed_when_documents_arrive_after_first_open():
    from app.services import financial_workspace as workspace

    stamp = workspace.now()
    family = SimpleNamespace(id=uuid4(), label="Business debt schedule", current_version_id=None)
    draft = SimpleNamespace(body=body(), revision=0, base_version_id=None, updated_at=stamp)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    profile = SimpleNamespace(id=uuid4())
    documents = AsyncMock(side_effect=[[], [source(count=7)]])

    async def stage(_db, _profile, _family, current, updated, *, actor=None):
        assert actor is None
        current.body, current.revision = updated, current.revision + 1

    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(workspace, "source_files_for_profile", AsyncMock(return_value=[])), \
         patch.object(workspace, "_families", AsyncMock(return_value=[])), \
         patch.object(intake, "source_documents", documents), \
         patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch.object(workspace, "stage_draft", side_effect=stage):
        empty = await workspace.load_workspace(db, profile, kind="debt_schedule")
        seeded = await workspace.load_workspace(db, profile, kind="debt_schedule")
        repeated = await workspace.load_workspace(db, profile, kind="debt_schedule")
    assert empty["body"]["debts"] == []
    assert len(seeded["body"]["debts"]) == len(repeated["body"]["debts"]) == 7
    assert seeded["draft"]["revision"] == repeated["draft"]["revision"] == 1
    assert documents.await_count == 2


@pytest.mark.asyncio
async def test_maintenance_entry_uses_profile_locks_and_reports_counts_without_publication():
    family = SimpleNamespace(id=uuid4(), current_version_id=uuid4())
    draft = SimpleNamespace(body=body(), revision=0)
    profile = SimpleNamespace(id=uuid4())

    async def seed(_db, _profile, _family, current):
        current.body["debts"] = [{"id": str(uuid4()), "lender": "Funder", "origin": "ai_draft"}]
        current.revision = 1
        return {"seeded": True, "proposal_count": 1}

    with patch("app.services.financial_workspace.resolve_family", AsyncMock(return_value=(family, draft))) as resolve, \
         patch.object(intake, "initial_seed", side_effect=seed):
        result = await intake.seed_initial_for_profile(None, profile, family_id=family.id)
    assert result["rows_before"] == 0 and result["rows_after"] == result["active_rows"] == 1
    assert result["original_pointer_unchanged"] and result["all_rows_ai_draft"]
    assert not result["published"] and result["emails_sent"] == 0
    assert resolve.await_args.kwargs["actor"] is None and resolve.await_args.kwargs["family_id"] == family.id
