from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import financial_workspace as router
from app.services import financial_cockpit as cockpit
from app.services import financial_debt_review as review


def sources(last4="1111", payee="Example funding", kind="loan", **values):
    return [{"file_id": str(uuid4()), "classification": "bank_statement", "accepted": True,
             "content_hash": "current", "key_facts": {"bank": "Chase", "account_last4": last4,
                 "statement_period": "2026-05", "recurring_debits": [{"payee": payee, "kind_guess": kind,
                     "occurrences": 4, "total_amount": 400, "cadence": "weekly", **values}]}}]


def candidates(body=None, analyses=None):
    analyses = analyses or sources()
    _, accounts = cockpit.bank_months(analyses)
    return review.build_candidates(body or {"debts": []}, analyses, accounts)


def test_candidates_never_merge_same_vendor_across_distinct_accounts():
    rows = candidates(analyses=sources() + sources("2222"))
    assert len(rows) == 2 and rows[0]["key"] != rows[1]["key"]
    assert all(row["status"] == "unreviewed" and not row["in_debt_service"] for row in rows)
    assert len({row["vendor_key"] for row in rows}) == 1


@pytest.mark.parametrize("kind", ["loan", "payroll", "transfer", "other", "rent"])
def test_classification_does_not_prevent_staff_from_reviewing_a_misclassified_funder(kind):
    row, = candidates(analyses=sources(payee="Example", kind=kind))
    assert row["eligible"] and row["status"] == "unreviewed"
    assert "does not establish debt" in row["reason"]


def test_explicit_add_keeps_financial_values_unknown_and_preserves_separate_same_lender_loan():
    candidate, = candidates()
    manual = {"id": str(uuid4()), "lender": candidate["payee"], "balance": "1000", "monthly_payment": "100", "status": "active"}
    body = {"debts": [manual]}
    before = deepcopy(body)
    added, changed = review.apply_candidate(body, candidate, action="include", family_id="family", idempotency_key="request-1")
    assert changed and body == before and added["debts"][0] == manual
    new = added["debts"][1]
    assert new["balance"] is None and new["monthly_payment"] is None and new.get("debt_type") is None
    assert new["evidence_links"][0]["file_id"] == candidate["file_ids"][0]
    repeated, changed = review.apply_candidate(added, candidate, action="include", family_id="family", idempotency_key="request-1")
    assert not changed and repeated == added


def test_disable_only_excludes_candidate_owned_row_and_can_be_reviewed_again():
    analysis = sources()
    candidate, = candidates(analyses=analysis)
    added, _ = review.apply_candidate({"debts": []}, candidate, action="include", family_id="family", idempotency_key="request-1")
    included, = candidates(added, analysis)
    assert included["status"] == "included"
    disabled, _ = review.apply_candidate(added, included, action="disable", family_id="family", idempotency_key="request-2")
    excluded, = candidates(disabled, analysis)
    assert excluded["status"] == "disabled" and disabled["debts"][0]["status"] == "dismissed"
    reopened, _ = review.apply_candidate(disabled, excluded, action="restore", family_id="family", idempotency_key="request-3")
    restored, = candidates(reopened, analysis)
    assert restored["status"] == "unreviewed" and reopened["debts"][0]["status"] == "dismissed"
    again, _ = review.apply_candidate(reopened, restored, action="include", family_id="family", idempotency_key="request-4")
    assert len(again["debts"]) == 1 and again["debts"][0]["status"] == "active"


def test_existing_duplicate_is_never_silently_restored():
    candidate, = candidates()
    candidate["row_id"] = str(uuid4())
    body = {"debts": [{"id": candidate["row_id"], "status": "duplicate", "duplicate_of_id": str(uuid4())}]}
    with pytest.raises(HTTPException) as error:
        review.apply_candidate(body, candidate, action="include", family_id="family", idempotency_key="request-1")
    assert error.value.status_code == 409


def test_reused_request_key_cannot_mutate_a_different_candidate_or_action():
    candidate, = candidates()
    body, _ = review.apply_candidate({"debts": []}, candidate, action="disable", family_id="family", idempotency_key="request-1")
    with pytest.raises(HTTPException) as error:
        review.apply_candidate(body, candidate, action="include", family_id="family", idempotency_key="request-1")
    assert error.value.status_code == 409


def test_linking_existing_row_preserves_both_separate_loans_and_disable_never_removes_them():
    analysis = sources()
    candidate, = candidates(analyses=analysis)
    rows = [{"id": str(uuid4()), "lender": candidate["payee"], "status": "active", "balance": str(value), "monthly_payment": "10"} for value in (100, 200)]
    body = {"debts": rows}
    candidate, = candidates(body, analysis)
    assert candidate["row_id"] is None and len(candidate["possible_row_ids"]) == 2
    linked, _ = review.apply_candidate(body, candidate, action="link_existing", target_row_id=rows[1]["id"], family_id="family", idempotency_key="request-1")
    assert linked["debts"] == rows
    recognized, = candidates(linked, analysis)
    assert recognized["status"] == "included" and recognized["binding"] == "existing" and recognized["row_id"] == rows[1]["id"]
    disabled, _ = review.apply_candidate(linked, recognized, action="disable", family_id="family", idempotency_key="request-2")
    assert disabled["debts"] == rows and candidates(disabled, analysis)[0]["status"] == "disabled"


def test_distinct_account_candidates_can_link_one_existing_obligation_without_double_counting():
    analysis = sources() + sources("2222")
    row = {"id": str(uuid4()), "lender": "Existing loan", "status": "active", "balance": "100", "monthly_payment": "10"}
    body = {"debts": [row]}
    for index, candidate in enumerate(candidates(body, analysis)):
        body, _ = review.apply_candidate(body, candidate, action="link_existing", target_row_id=row["id"], family_id="family", idempotency_key=f"request-{index}")
    assert body["debts"] == [row]
    assert {item["row_id"] for item in candidates(body, analysis)} == {row["id"]}


def test_disabling_second_account_link_does_not_remove_first_accounts_created_obligation():
    analysis = sources() + sources("2222")
    first, second = candidates(analyses=analysis)
    body, _ = review.apply_candidate({"debts": []}, first, action="include", family_id="family", idempotency_key="request-1")
    row_id = body["debts"][0]["id"]
    second = next(item for item in candidates(body, analysis) if item["key"] == second["key"])
    body, _ = review.apply_candidate(body, second, action="link_existing", target_row_id=row_id, family_id="family", idempotency_key="request-2")
    second = next(item for item in candidates(body, analysis) if item["key"] == second["key"])
    assert second["binding"] == "existing"
    disabled, _ = review.apply_candidate(body, second, action="disable", family_id="family", idempotency_key="request-3")
    assert disabled["debts"][0]["status"] == "active"
    statuses = {item["key"]: item["status"] for item in candidates(disabled, analysis)}
    assert statuses[first["key"]] == "included" and statuses[second["key"]] == "disabled"


def test_link_existing_requires_an_active_row_in_the_same_working_body():
    candidate, = candidates()
    with pytest.raises(HTTPException) as error:
        review.apply_candidate({"debts": []}, candidate, action="link_existing", target_row_id=str(uuid4()), family_id="family", idempotency_key="request-1")
    assert error.value.status_code == 422


def test_disabled_candidate_stays_disabled_after_reanalysis_and_replacement_evidence():
    analysis = sources()
    candidate, = candidates(analyses=analysis)
    body, _ = review.apply_candidate({"debts": []}, candidate, action="disable", family_id="family", idempotency_key="request-1")
    replacement = sources()
    reread, = candidates(body, replacement)
    assert reread["key"] == candidate["key"] and reread["status"] == "disabled"


def test_repeated_statement_copies_do_not_double_observed_amounts():
    analysis = sources()
    duplicate = deepcopy(analysis[0])
    duplicate["file_id"] = str(uuid4())
    candidate, = candidates(analyses=analysis + [duplicate])
    assert candidate["monthly_average"] == 400


def test_complete_automated_draft_is_not_mistaken_for_staff_curation():
    row = {"id": str(uuid4()), "lender": "Unreviewed", "origin": "ai_draft", "balance": 100, "monthly_payment": 10}
    result = cockpit.build_cockpit(sources(), [row], working_schedule={"body": {"debts": [row]}})
    assert not result["debt"]["service"]["working_schedule_complete"]


def test_working_edits_do_not_change_complete_explicit_legacy_baseline():
    row = {"id": str(uuid4()), "lender": "Disclosed", "origin": "admin", "balance": 100, "monthly_payment": 10}
    complete = cockpit.build_cockpit(sources(), [row], working_schedule={"body": {"debts": [{**row, "monthly_payment": 99}]}})
    incomplete = cockpit.build_cockpit(sources(), [row], working_schedule={"body": {"debts": [{**row, "monthly_payment": None}]}})
    assert complete["debt"]["service"]["monthly"] == incomplete["debt"]["service"]["monthly"] == 10
    assert not incomplete["debt"]["service"]["working_schedule_complete"]


def test_complete_human_preview_cannot_replace_an_incomplete_or_automated_baseline():
    legacy = {"id": str(uuid4()), "lender": "Unreviewed", "origin": "ai_draft", "balance": 100, "monthly_payment": 10}
    baseline = cockpit.build_cockpit(sources(), [legacy])
    preview = cockpit.build_cockpit(sources(), [legacy], working_schedule={"body": {"debts": [{**legacy, "origin": "admin", "monthly_payment": 99}]}})
    assert baseline["debt"]["service"]["monthly"] == preview["debt"]["service"]["monthly"]
    assert preview["debt"]["review"]["working"]["monthly"] == 99
    assert not preview["debt"]["service"]["observed_additions_suppressed"]


def test_one_human_row_does_not_confirm_untouched_automated_working_rows():
    human = {"id": str(uuid4()), "lender": "Staff", "origin": "admin", "balance": 100, "monthly_payment": 10}
    automated = {**human, "id": str(uuid4()), "origin": "ai_draft"}
    result = cockpit.build_cockpit(sources(), [], working_schedule={"body": {"debts": [human, automated]}})
    assert not result["debt"]["service"]["working_schedule_complete"]


def test_all_payees_remain_reviewable_beyond_the_existing_summary_limit():
    analysis = sources()
    entries = [{"payee": f"DifferentSupplier{chr(65 + index // 26)}{chr(65 + index % 26)}Funding", "kind_guess": "other", "cadence": "irregular", "occurrences": 2, "total_amount": 100 + index} for index in range(45)]
    entries.append({"payee": "Rare unmatched funder", "kind_guess": "other", "cadence": "irregular", "occurrences": 2, "total_amount": 1})
    analysis[0]["key_facts"]["recurring_debits"] = entries
    assert len(cockpit.recurring_payments(analysis)["payees"]) == 40
    reviewed = candidates(analyses=analysis)
    assert len(reviewed) == 46 and any(row["payee"] == "Rare unmatched funder" for row in reviewed)


def test_combined_and_replacement_pdfs_use_canonical_monthly_readings_once():
    old = sources()[0]
    entry = old["key_facts"].pop("recurring_debits")[0]
    old["key_facts"]["months"] = [{"statement_period": month, "recurring_debits": [{**entry, "total_amount": 100}]} for month in ("2026-01", "2026-02")]
    new = sources()[0]
    new["key_facts"].update(statement_period="2026-01", total_deposits_and_credits=1000)
    new["key_facts"]["recurring_debits"][0]["total_amount"] = 200
    result, = candidates(analyses=[old, new])
    assert result["monthly"] == {"2026-01": 200, "2026-02": 100}
    assert result["monthly_average"] == 150


def test_relinking_active_candidate_created_row_requires_explicit_cleanup_first():
    candidate, = candidates()
    body, _ = review.apply_candidate({"debts": []}, candidate, action="include", family_id="family", idempotency_key="request-1")
    other = {"id": str(uuid4()), "lender": "Existing separate loan", "status": "active"}
    body["debts"].append(other)
    current, = candidates(body)
    with pytest.raises(HTTPException) as error:
        review.apply_candidate(body, current, action="link_existing", target_row_id=other["id"], family_id="family", idempotency_key="request-2")
    assert error.value.status_code == 409


def test_old_unconfirmed_payoff_selections_are_warned_even_without_a_workspace():
    result = cockpit.build_cockpit(sources(), [], plan={"retire": ["payee:old", "doc:0"]})
    assert result["debt"]["review"] is None
    assert result["plan"]["unresolved_previous_selections"] == ["payee:old", "doc:0"]
    assert any("saved payoff selections" in note for note in result["notes"])
    assert result["plan"]["record"]["retire"] == ["payee:old", "doc:0"]


def test_complete_working_draft_stops_unreviewed_extras_without_publishing_its_new_values():
    row_id = str(uuid4())
    legacy = [{"id": row_id, "lender": "Disclosed funder", "balance": 100, "monthly_payment": 10, "origin": "admin"}]
    draft = {"body": {"debts": [{**legacy[0], "monthly_payment": 99}]}}
    result = cockpit.build_cockpit(sources(), legacy, working_schedule=draft)
    assert result["debt"]["service"]["monthly"] == 10
    assert result["debt"]["service"]["source"] == "debt_schedule_rows"
    assert result["debt"]["review"]["working"]["monthly"] == 99
    assert result["debt"]["review"]["candidates"][0]["status"] == "unreviewed"
    assert result["debt"]["review"]["published"]["version_id"] is None


def test_empty_workspace_does_not_suppress_existing_provisional_estimate():
    baseline = cockpit.build_cockpit(sources(), [])
    opened = cockpit.build_cockpit(sources(), [], working_schedule={"body": {"debts": []}})
    assert baseline["debt"]["service"]["monthly"] == opened["debt"]["service"]["monthly"]
    assert opened["debt"]["service"]["basis_status"] == "provisional"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER, Role.LENDER, Role.VENDOR])
async def test_candidate_endpoints_preserve_staff_only_access(role):
    with pytest.raises(HTTPException) as error:
        await router._profile(None, uuid4(), SimpleNamespace(role=role))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_stale_draft_revision_refuses_candidate_action_before_any_save():
    snapshot = {"workspace": {"body": {}, "draft": {"revision": 4}}, "candidates": []}
    with patch.object(review, "read_review", AsyncMock(return_value=snapshot)), patch("app.services.financial_workspace.save_draft", AsyncMock()) as save:
        with pytest.raises(HTTPException) as error:
            await review.act(None, None, expected_revision=3, candidate_key="unknown", action="include", idempotency_key="request-1")
    assert error.value.status_code == 409
    save.assert_not_called()
