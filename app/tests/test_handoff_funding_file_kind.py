"""funding_file_kind derivation — pure-logic test (Phase 4)."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.handoff import _derive_funding_file_kind


@dataclass
class FakeDeal:
    deal_type: str


def test_buyer_purchase_maps_to_dscr_purchase():
    assert _derive_funding_file_kind(FakeDeal("buyer"), "purchase") == "dscr_purchase"


def test_buyer_refinance_maps_to_dscr_refi():
    assert _derive_funding_file_kind(FakeDeal("buyer"), "refinance") == "dscr_refi"


def test_buyer_default_is_dscr_purchase():
    assert _derive_funding_file_kind(FakeDeal("buyer"), None) == "dscr_purchase"


def test_borrower_defaults_to_bridge():
    assert _derive_funding_file_kind(FakeDeal("borrower"), None) == "bridge"


def test_investor_purchase_maps_to_dscr_purchase():
    assert _derive_funding_file_kind(FakeDeal("investor"), "purchase") == "dscr_purchase"


def test_investor_refinance_maps_to_dscr_refi():
    assert _derive_funding_file_kind(FakeDeal("investor"), "refinance") == "dscr_refi"


def test_seller_default_is_other():
    assert _derive_funding_file_kind(FakeDeal("seller"), None) == "other"


def test_unknown_deal_type_falls_through_to_other():
    assert _derive_funding_file_kind(FakeDeal("unknown_kind"), None) == "other"


def test_unknown_purpose_uses_deal_type_default():
    # Unknown override_purpose should fall through to the deal-type default
    # (the second lookup in _KIND_MAP with purpose=None).
    assert (
        _derive_funding_file_kind(FakeDeal("buyer"), "construction_perm")
        == "dscr_purchase"
    )
