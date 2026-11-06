"""Repeated transfer amounts require reciprocal reference matches, never greedy pairing."""

from copy import deepcopy

import pytest

from app.services.account_movements import build_account_movements

A, B, C = "chase ••1111", "chase ••2222", "chase ••3333"
ACCOUNTS = [{"key": key, "label": key, "members": [key], "counted": True} for key in (A, B, C)]


def line(direction, reference=None, *, on="2026-05-10", target=None, **values):
    target = target or ("2222" if direction == "out" else "1111")
    return {"date": on, "amount": 1500, "direction": direction, "currency": "USD", "page": 2,
            "description": f"Online transfer {'to' if direction == 'out' else 'from'} account XXXX{target}",
            "transfer_reference": reference, **values}


def statement(key, transactions, **values):
    return {"file_id": key, "file_name": f"{key}.pdf", "movement_account_key": key,
            "classification": "bank_statement", "accepted": True,
            "key_facts": {"account_holder": "Example LLC", "bank": "Chase", "account_last4": key[-4:],
                          "currency": "USD", "statement_period": "2026-05-01 to 2026-05-31",
                          "transactions_complete": True, "transactions": transactions}, **values}


def build(outgoing, incoming, **values):
    sources = [statement(A, outgoing), statement(B, incoming)]
    for index, changes in values.items():
        sources[int(index)].update(changes)
    return build_account_movements(sources, ACCOUNTS)


def supported(result):
    return [movement for movement in result["movements"] if movement["status"] == "supported"]


def test_repeated_1500_transfers_with_unique_printed_references_pair_to_their_own_credit():
    sources = [statement(A, [line("out", "T1001"), line("out", "T1002", on="2026-05-11")]),
               statement(B, [line("in", "T1001"), line("in", "T1002", on="2026-05-11")])]
    before = deepcopy(sources)
    result = build_account_movements(sources, ACCOUNTS)
    assert len(supported(result)) == 2
    assert {(movement["out_on"], movement["in_on"]) for movement in supported(result)} == {
        ("2026-05-10", "2026-05-10"), ("2026-05-11", "2026-05-11"),
    }
    assert [(link["amount"], link["transaction_count"], link["status"]) for link in result["links"]] == [(3000, 2, "supported")]
    assert sources == before


@pytest.mark.parametrize("incoming_first", [False, True])
def test_exact_reference_survives_more_than_six_amount_only_candidates_on_either_side(incoming_first):
    anonymous = [line("out" if incoming_first else "in") for _ in range(8)]
    outgoing = [*anonymous, line("out", "T1001")] if incoming_first else [line("out", "T1001")]
    incoming = [line("in", "T1001")] if incoming_first else [*anonymous, line("in", "T1001")]
    result = build(outgoing, incoming)
    assert len(supported(result)) == 1
    assert result["links"][0]["amount"] == 1500
    assert len(result["movements"]) == 9


@pytest.mark.parametrize("referenced_first", [False, True])
def test_credit_reserved_for_an_exact_reference_cannot_be_claimed_by_an_unreferenced_debit(referenced_first):
    outgoing = [line("out", "T1001"), line("out")]
    if not referenced_first:
        outgoing.reverse()
    result = build(outgoing, [line("in", "T1001")])
    assert len(supported(result)) == 1
    assert len(result["movements"]) == 2
    assert sum(len(movement["evidence"]) for movement in result["movements"]) == 3
    assert next(node for node in result["nodes"] if node["key"] == A)["periods"][0]["internal_out"] == 1500


@pytest.mark.parametrize("out_count,in_count", [(2, 2), (2, 1), (1, 2)])
def test_reused_reference_remains_ambiguous_and_never_greedily_pairs(out_count, in_count):
    result = build([line("out", "SAME1") for _ in range(out_count)], [line("in", "SAME1") for _ in range(in_count)])
    assert supported(result) == [] and result["links"] == []
    assert len(result["movements"]) == out_count + in_count
    assert all(movement["status"] == "review" for movement in result["movements"])


def test_conflicting_nonempty_references_never_become_supported_even_when_account_targets_are_exact():
    result = build([line("out", "T1001")], [line("in", "T9999")])
    assert supported(result) == []
    assert all(movement["status"] == "review" and "reference_conflict" in movement["reason_codes"] for movement in result["movements"])


def test_unknown_references_preserve_unique_account_fallback_but_repeated_amounts_stay_ambiguous():
    assert len(supported(build([line("out")], [line("in")]))) == 1
    repeated = build([line("out"), line("out", on="2026-05-11")], [line("in"), line("in", on="2026-05-11")])
    assert supported(repeated) == [] and len(repeated["movements"]) == 4


@pytest.mark.parametrize("change", ["unaccepted", "currency_unknown", "currency_mismatch", "holder_mismatch", "page_missing", "outside_window", "wrong_target"])
def test_exact_reference_never_bypasses_existing_source_account_date_and_currency_gates(change):
    sources = [statement(A, [line("out", "T1001")]), statement(B, [line("in", "T1001")])]
    row = sources[0]["key_facts"]["transactions"][0]
    if change == "unaccepted":
        sources[0]["accepted"] = False
    elif change == "currency_unknown":
        row["currency"] = None
        sources[0]["key_facts"]["currency"] = None
    elif change == "currency_mismatch":
        sources[0]["key_facts"]["currency"] = row["currency"] = "CAD"
    elif change == "holder_mismatch":
        sources[0]["key_facts"]["account_holder"] = "Other Company"
    elif change == "page_missing":
        row["page"] = None
    elif change == "outside_window":
        row["date"] = "2026-05-14"
    elif change == "wrong_target":
        row["counterparty_account_last4"] = "3333"
        row["description"] = "Online transfer to account XXXX3333"
    assert supported(build_account_movements(sources, ACCOUNTS)) == []


def test_unknown_posting_date_is_reviewable_evidence_instead_of_crashing_or_matching():
    result = build([line("out", "T1001", on=None)], [line("in", "T1001")])
    assert supported(result) == []
    assert any("date_unknown" in movement["reason_codes"] for movement in result["movements"])
