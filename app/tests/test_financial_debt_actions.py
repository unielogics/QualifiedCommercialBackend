from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.financial_debt_actions import active_debt, apply_debt_action


def _body():
    return {"debts": [
        {"id": str(uuid4()), "lender": "Example", "balance": "100", "monthly_payment": "10", "status": "active", "origin": "client_form"},
        {"id": str(uuid4()), "lender": "Example LLC", "balance": "100", "monthly_payment": "10", "status": "active", "origin": "admin"},
    ]}


def test_duplicate_retains_source_values_and_identity_without_changing_survivor():
    body = _body()
    before = deepcopy(body)
    first, second = [row["id"] for row in body["debts"]]
    out = apply_debt_action(body, {"op": "duplicate", "row_id": second, "target_row_id": first})
    assert body == before
    assert out["debts"][0] == before["debts"][0]
    assert out["debts"][1]["duplicate_of_id"] == first
    assert sum(float(row["balance"]) for row in out["debts"] if active_debt(row)) == 100
    restored = apply_debt_action(out, {"op": "restore", "row_id": second})
    assert active_debt(restored["debts"][1])
    assert restored["debts"][1]["origin"] == "admin"


def test_deleting_is_recoverable_and_never_removes_the_row():
    body = _body()
    row_id = body["debts"][0]["id"]
    out = apply_debt_action(body, {"op": "delete", "row_id": row_id})
    assert len(out["debts"]) == 2 and not active_debt(out["debts"][0])
    assert apply_debt_action(out, {"op": "restore", "row_id": row_id})["debts"][0]["balance"] == "100"


@pytest.mark.parametrize("target", ["self", "missing", "inactive"])
def test_duplicate_rejects_invalid_targets(target):
    body = _body()
    row_id = body["debts"][0]["id"]
    target_id = row_id if target == "self" else str(uuid4()) if target == "missing" else body["debts"][1]["id"]
    if target == "inactive":
        body["debts"][1]["status"] = "dismissed"
    with pytest.raises(HTTPException) as exc:
        apply_debt_action(body, {"op": "duplicate", "row_id": row_id, "target_row_id": target_id})
    assert exc.value.status_code == 422


def test_payoff_attachment_is_separate_from_contract_and_explicitly_selected():
    body = _body()
    row_id, file_id, old_file = body["debts"][0]["id"], str(uuid4()), str(uuid4())
    body["debts"][0]["document_id"] = str(uuid4())
    body["debts"][0]["evidence_links"] = [{"file_id": old_file, "role": "payoff", "is_current": True}]
    out = apply_debt_action(body, {"op": "link_document", "row_id": row_id, "file_id": file_id, "role": "payoff", "payoff_amount": "95.00"})
    assert "payoff_amount" not in out["debts"][0]
    out = apply_debt_action(out, {"op": "select_payoff", "row_id": row_id, "file_id": file_id, "payoff_date": "2026-11-05"})
    row = out["debts"][0]
    assert row["document_id"] == body["debts"][0]["document_id"]
    assert row["payoff_amount"] == "95.00"
    assert sum(bool(link["is_current"]) for link in row["evidence_links"]) == 1
    assert row["payoff_date"] == "2026-11-05"
    cleared = apply_debt_action(out, {"op": "select_payoff", "row_id": row_id, "file_id": file_id, "payoff_amount": None})
    assert cleared["debts"][0]["payoff_amount"] is None
    assert cleared["debts"][0]["payoff_date"] == "2026-11-05"


def test_duplicate_aliases_retarget_directly_without_chains():
    body = _body()
    third = {"id": str(uuid4()), "lender": "Other", "status": "active"}
    body["debts"].append(third)
    first, second = [row["id"] for row in body["debts"][:2]]
    out = apply_debt_action(body, {"op": "duplicate", "row_id": first, "target_row_id": second})
    out = apply_debt_action(out, {"op": "duplicate", "row_id": second, "target_row_id": third["id"]})
    assert out["debts"][0]["duplicate_of_id"] == third["id"]
    assert out["debts"][1]["duplicate_of_id"] == third["id"]


def test_deleting_survivor_keeps_duplicate_alias_group_recoverable():
    body = _body()
    first, second = [row["id"] for row in body["debts"]]
    duplicate = apply_debt_action(body, {"op": "duplicate", "row_id": second, "target_row_id": first})
    removed = apply_debt_action(duplicate, {"op": "delete", "row_id": first})
    assert all(row["status"] == "dismissed" for row in removed["debts"])
    assert removed["debts"][1]["previous_duplicate_of_id"] == first
    assert removed["debts"][1]["duplicate_of_id"] is None
