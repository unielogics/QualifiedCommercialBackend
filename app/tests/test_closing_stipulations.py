"""Stipulations to close: seeded from the terms, kept by the desk, never dropped."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.closing_stipulations import (
    conditions_text_lines,
    merge_items,
    read_block,
    seed_items,
)


def test_the_terms_conditions_seed_the_list_once_each() -> None:
    first = seed_items([], ["Proof of insurance", "Payoff letter from OnDeck"], by="u1")
    assert [i["text"] for i in first] == ["Proof of insurance", "Payoff letter from OnDeck"]
    assert {i["source"] for i in first} == {"term_sheet"} and {i["status"] for i in first} == {
        "open"
    }
    again = seed_items(first, ["proof of insurance", "Voided check"], by="u1")
    assert [i["text"] for i in again] == [
        "Proof of insurance",
        "Payoff letter from OnDeck",
        "Voided check",
    ]
    assert conditions_text_lines("- Proof of insurance\n• Payoff letter\n\n") == [
        "Proof of insurance",
        "Payoff letter",
    ]
    assert conditions_text_lines(["a", "", None, " b "]) == ["a", "b"]


def test_the_desk_marks_received_adds_its_own_and_an_item_left_out_is_waived_not_dropped() -> None:
    current = seed_items([], ["Proof of insurance", "Payoff letter"], by="u1")
    ids = [i["id"] for i in current]
    merged = merge_items(
        current,
        [
            {
                "id": ids[0],
                "text": "Proof of insurance",
                "status": "received",
                "note": "Received Nov 4",
            },
            {"text": "Landlord waiver", "status": "open"},
        ],
        by="u2",
    )
    by_text = {i["text"]: i for i in merged}
    assert (
        by_text["Proof of insurance"]["status"] == "received"
        and by_text["Proof of insurance"]["updated_by"] == "u2"
    )
    assert (
        by_text["Proof of insurance"]["id"] == ids[0]
        and by_text["Proof of insurance"]["source"] == "term_sheet"
    )
    assert (
        by_text["Landlord waiver"]["source"] == "desk"
        and by_text["Landlord waiver"]["status"] == "open"
    )
    assert by_text["Payoff letter"]["status"] == "waived"  # left out of the list, kept as waived
    assert merge_items(
        merged, [{"id": "nope", "text": "   "}], by="u2"
    )  # blank text is ignored, the rest kept


def test_the_block_says_whether_the_file_is_ready_to_close() -> None:
    profile = SimpleNamespace(
        id="p1",
        closing_stipulations=[],
        underwriting_accepted_amount=None,
        estimated_close_date=None,
    )
    block = read_block(profile, ["Proof of insurance"])
    assert (
        block["accepted"] is False
        and block["ready_to_close"] is False
        and block["terms_conditions"] == ["Proof of insurance"]
    )
    profile.underwriting_accepted_amount = 200000
    profile.closing_stipulations = seed_items([], ["Proof of insurance"], by=None)
    assert (
        read_block(profile, [])["ready_to_close"] is False
        and read_block(profile, [])["open_count"] == 1
    )
    profile.closing_stipulations = merge_items(
        profile.closing_stipulations,
        [{**profile.closing_stipulations[0], "status": "received"}],
        by=None,
    )
    block = read_block(profile, [])
    assert (
        block["ready_to_close"] is True
        and block["received_count"] == 1
        and block["accepted_amount"] == 200000.0
    )
