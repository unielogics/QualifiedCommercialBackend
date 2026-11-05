"""Transfer review never turns summaries, aliases or guesses into cash facts."""

from copy import deepcopy

import pytest

from app.services.account_movements import build_account_movements
from app.services.financial_cockpit import build_cockpit

A, B, C = "chase ••1111", "chase ••2222", "chase ••3333"


def account(key, **values):
    return {"key": key, "label": key.title(), "counted": True, "members": [key], **values}


def line(direction="out", *, amount=100, on="2026-05-10", target="2222", **values):
    return {"date": on, "amount": amount, "direction": direction,
            "description": f"Transfer {'to' if direction == 'out' else 'from'} XXXX{target}", "page": 2, **values}


def statement(key, lines=None, *, file_id=None, currency="USD", accepted=True, field="transactions", **values):
    facts = {"bank": "Chase", "account_last4": key[-4:], "account_holder": "Example LLC", "currency": currency,
             "statement_period": "2026-05-01 to 2026-05-31", "beginning_balance": 1000, "ending_balance": 900,
             "transactions_complete": True, **values}
    if lines is not None:
        facts[field] = lines
    return {"file_id": file_id or key, "file_name": f"{file_id or key}.pdf", "movement_account_key": key,
            "classification": "bank_statement", "accepted": accepted, "key_facts": facts}


def pair(**values):
    return [statement(A, [line()], **values), statement(B, [line("in", target="1111")], **values)]


def build(analyses, **values):
    return build_account_movements(analyses, [account(A), account(B), account(C)], **values)


def test_unique_accepted_located_cross_account_lines_support_pair_and_balance_bridge():
    source = pair()
    source[1]["key_facts"].update(beginning_balance=200, ending_balance=300)
    before = deepcopy(source)
    result = build(source)
    movement, = result["movements"]
    assert movement["status"] == "supported" and movement["from_account"] == A and movement["to_account"] == B
    assert movement["amount"] == 100 and movement["currency"] == "USD" and movement["lag_days"] == 0
    assert movement["evidence"][0]["file_id"] == A and movement["evidence"][0]["page"] == 2
    outgoing = next(node for node in result["nodes"] if node["key"] == A)["periods"][0]
    incoming = next(node for node in result["nodes"] if node["key"] == B)["periods"][0]
    assert outgoing["observed_net"] == outgoing["internal_net"] == outgoing["observed_balance_change"] == -100
    assert incoming["observed_net"] == incoming["internal_net"] == incoming["observed_balance_change"] == 100
    assert outgoing["unexplained_change"] == incoming["unexplained_change"] == 0
    assert source == before


def test_month_end_pair_is_discoverable_in_both_periods_without_doubled_account_flows():
    outgoing = statement(A, [line(on="2026-05-31")])
    incoming = statement(B, [line("in", target="1111", on="2026-06-01")], statement_period="2026-06-01 to 2026-06-30")
    result = build([outgoing, incoming])
    movement, = result["movements"]
    assert movement["periods"] == ["2026-05", "2026-06"] and movement["lag_days"] == 1
    assert {link["period"] for link in result["links"]} == {"2026-05", "2026-06"}
    assert all(link["periods"] == [link["period"]] for link in result["links"])
    assert next(node for node in result["nodes"] if node["key"] == A)["periods"][0]["internal_out"] == 100
    assert next(node for node in result["nodes"] if node["key"] == B)["periods"][0]["internal_in"] == 100


def test_cross_month_graph_amount_does_not_include_other_outgoing_month_payments():
    outgoing = statement(A, [line(on="2026-05-10"), line(on="2026-05-31")])
    incoming = statement(B, [line("in", target="1111", on="2026-05-10"), line("in", target="1111", on="2026-06-01")],
                         statement_period="2026-05-01 to 2026-06-30")
    result = build([outgoing, incoming])
    assert {link["period"]: link["amount"] for link in result["links"]} == {"2026-05": 200, "2026-06": 100}
    assert next(link for link in result["links"] if link["period"] == "2026-06")["transaction_count"] == 1


def test_recurring_summaries_and_totals_never_become_actual_transactions():
    source = statement(A, None, recurring_debits=[{"payee": "Transfer to2222", "occurrences": 4, "total_amount": 400}],
        total_withdrawals_and_debits=400)
    result = build([source])
    assert result["status"] == "no_transaction_evidence" and result["movements"] == []
    assert result["coverage"]["ignored_summary_records"] == 1
    node = next(node for node in result["nodes"] if node["key"] == A)["periods"][0]
    assert node["observed_in"] is node["observed_net"] is node["explained_change"] is None
    assert node["observed_balance_change"] == -100 and node["coverage_status"] == "missing"


def test_unmatched_side_keeps_source_and_missing_counterpart_coverage():
    result = build([statement(A, [line()]), statement(B, None)])
    row, = result["movements"]
    assert row["status"] == "unmatched" and row["to_account"] is None
    assert "counterpart_not_observed" in row["reason_codes"] and row["evidence"][0]["page"] == 2
    assert result["coverage"]["files_missing_detail"] == [B]


@pytest.mark.parametrize("change,reason", [("unaccepted", "source_not_accepted"), ("currency", "currency_unknown"),
                                         ("location", "source_location_missing"), ("holder", "account_holder_conflict")])
def test_provisional_or_unknown_pair_stays_review(change, reason):
    source = pair()
    if change == "unaccepted":
        source[0]["accepted"] = False
    elif change == "currency":
        source[0]["key_facts"]["currency"] = None
    elif change == "location":
        source[0]["key_facts"]["transactions"][0].pop("page")
    else:
        source[0]["key_facts"]["account_holder"] = "Unrelated Company"
    row, = build(source)["movements"]
    assert row["status"] == "review" and reason in row["reason_codes"]


def test_known_different_currencies_or_dates_outside_source_period_cannot_pair():
    source = pair()
    source[1]["key_facts"]["currency"] = "CAD"
    assert build(source)["links"] == []
    source = pair()
    source[0]["key_facts"]["transactions"][0]["date"] = "2026-06-10"
    result = build(source)
    assert result["links"] == []
    assert any("outside_statement_period" in row["reason_codes"] for row in result["movements"])


def test_same_amount_unrelated_payments_never_form_transfer_link():
    source = pair()
    source[0]["key_facts"]["transactions"][0]["description"] = "Utility bill"
    source[1]["key_facts"]["transactions"][0]["description"] = "Customer settlement"
    assert build(source)["movements"] == []


def test_repeated_same_amount_payments_are_ambiguous_not_greedily_paired_or_deduped():
    source = pair()
    source[0]["key_facts"]["transactions"].append(deepcopy(source[0]["key_facts"]["transactions"][0]))
    result = build(source)
    assert result["links"] == [] and len(result["movements"]) == 3
    assert all(row["status"] == "review" for row in result["movements"])
    assert all("ambiguous_repeated_amount" in row["reason_codes"] or "ambiguous_counterpart" in row["reason_codes"] for row in result["movements"])
    assert next(node for node in result["nodes"] if node["key"] == A)["periods"][0]["observed_out"] == 200


def test_same_account_aliases_cannot_create_internal_transfer():
    accounts = [account(A, members=[A, B])]
    result = build_account_movements(pair(), accounts)
    assert len(result["nodes"]) == 1 and result["links"] == []


def test_duplicate_pdf_content_and_pdf_plus_export_do_not_double_cash_flow():
    source = pair()
    duplicate = deepcopy(source[0])
    duplicate["file_id"] = "duplicate-pdf"
    source[0]["content_hash"] = duplicate["content_hash"] = "samehash"
    result = build([*source, duplicate])
    assert len(result["movements"]) == 1 and result["links"][0]["transaction_count"] == 1
    export = deepcopy(source[0])
    export.update(file_id="export", analyzed_at="2026-06-01T00:00:00Z")
    export["key_facts"]["source"] = "transactions_export"
    result = build([*source, export])
    assert result["coverage"]["transaction_records"] == 2
    assert next(node for node in result["nodes"] if node["key"] == A)["periods"][0]["observed_out"] == 100


def test_revised_export_uses_newest_reading_not_the_sum_of_both_versions():
    old = statement(A, [line(amount=100), line(amount=50)], source="transactions_export", file_id="old")
    old["analyzed_at"] = "2026-06-01T00:00:00Z"
    new = statement(A, [line(amount=80)], source="transactions_export", file_id="new")
    new["analyzed_at"] = "2026-06-02T00:00:00Z"
    result = build([old, new])
    assert result["coverage"]["transaction_records"] == 1 and result["coverage"]["superseded_detail_file_ids"] == ["old"]
    assert next(node for node in result["nodes"] if node["key"] == A)["periods"][0]["observed_out"] == 80


@pytest.mark.parametrize("extra", [{"debit": 100, "direction": "in"}, {"debit": 200}, {"debit": 100, "credit": 100}])
def test_contradictory_direction_or_amount_never_supports_pair(extra):
    source = pair()
    source[0]["key_facts"]["transactions"][0].update(extra)
    result = build(source)
    assert result["links"] == [] and all(row["status"] != "supported" for row in result["movements"])


def test_export_computed_balances_are_never_presented_as_observed_closing_balances():
    result = build([statement(A, [line()], source="transactions_export")])
    period = next(node for node in result["nodes"] if node["key"] == A)["periods"][0]
    assert period["beginning_balance"] is period["ending_balance"] is period["observed_balance_change"] is None
    assert "export_balance_is_computed" in period["reason_codes"]


def test_selective_lines_have_partial_coverage_even_when_pair_is_supported():
    result = build(pair(field="transfer_lines"))
    assert result["movements"][0]["status"] == "supported" and result["status"] == "partial"
    assert all(period["coverage_status"] == "partial" for node in result["nodes"] for period in node["periods"])


def test_unknown_direction_does_not_invent_from_or_to_account():
    raw = line(direction=None)
    raw.pop("direction")
    result = build([statement(A, [raw])])
    row, = result["movements"]
    assert row["direction"] is row["from_account"] is row["to_account"] is None
    assert "direction_unknown" in row["reason_codes"]


def test_ambiguous_mixed_account_export_is_review_only():
    source = pair()
    source[0]["key_facts"].update(source="transactions_export", account_identity_ambiguous=True)
    result = build(source)
    assert result["links"] == []
    row = next(row for row in result["movements"] if row["account_key"] == A)
    assert row["status"] == "review" and "account_identity_ambiguous" in row["reason_codes"]
    assert next(node for node in result["nodes"] if node["key"] == A)["periods"][0]["observed_net"] is None


def test_legacy_text_period_and_canonical_balances_remain_visible_without_currency_guess():
    source = statement(A, None, currency=None, statement_period="May 2026")
    result = build_cockpit([source], [])["bank"]["account_movements"]
    node, = result["nodes"]
    period, = node["periods"]
    assert period["period"] == "2026-05" and period["currency"] is None
    assert period["ending_balance"] == 900 and period["observed_net"] is None
    assert period["coverage_status"] == "missing" and "currency_unknown" in period["reason_codes"]


def test_preview_and_link_samples_are_bounded_and_stable():
    source = []
    for day in range(1, 21):
        on = f"2026-05-{day:02d}"
        source.extend([statement(A, [line(on=on, amount=day)], file_id=f"a{day}"),
                       statement(B, [line("in", on=on, amount=day, target="1111")], file_id=f"b{day}")])
    # Use one file per account to preserve all distinct posted lines.
    a, b = source[:2]
    a["key_facts"]["transactions"] = [item["key_facts"]["transactions"][0] for item in source[::2]]
    b["key_facts"]["transactions"] = [item["key_facts"]["transactions"][0] for item in source[1::2]]
    result = build([a, b], limit=5)
    assert result["pagination"] == {"offset": 0, "limit": 5, "total": 20, "next_offset": 5}
    assert len(result["movements"]) == 5 and len(result["links"][0]["sample_movements"]) == 3
    assert result["coverage"]["details_truncated"]
    assert build([a, b], limit=5)["movements"] == result["movements"]


def test_cockpit_exposes_movements_without_changing_existing_metrics_or_source_dicts():
    sources = pair()
    for item in sources:
        item["key_facts"].update(total_deposits_and_credits=1000, total_withdrawals_and_debits=500)
    original = deepcopy(sources)
    result = build_cockpit(sources, [])
    without_lines = deepcopy(sources)
    for item in without_lines:
        item["key_facts"].pop("transactions")
    baseline = build_cockpit(without_lines, [])
    assert result["bank"]["summary"] == baseline["bank"]["summary"]
    assert result["dscr"] == baseline["dscr"] and result["net_profit"] == baseline["net_profit"]
    assert result["bank"]["account_movements"]["movements"][0]["status"] == "supported"
    assert sources == original
