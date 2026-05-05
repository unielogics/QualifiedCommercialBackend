from app.enums import LoanType, PropertyType
from app.services.hud_template import build_hud_draft


def test_hud_draft_includes_origination():
    draft = build_hud_draft(
        loan_amount=500_000,
        property_type=PropertyType.SFR,
        loan_type=LoanType.DSCR,
        broker_origination_dollars=7500,
    )
    codes = [item.code for item in draft.items]
    assert "orig" in codes
    assert "uw" in codes
    assert "proc" in codes
    assert "appr" in codes
    assert "title" in codes
    assert "rec" in codes


def test_hud_draft_appraisal_higher_for_2_4_units():
    sfr = build_hud_draft(500_000, PropertyType.SFR, LoanType.DSCR, 7500)
    multi = build_hud_draft(500_000, PropertyType.UNITS_2_4, LoanType.DSCR, 7500)
    sfr_appr = next(i for i in sfr.items if i.code == "appr").amount
    multi_appr = next(i for i in multi.items if i.code == "appr").amount
    assert multi_appr > sfr_appr


def test_hud_draft_includes_inspection_for_flips():
    flip = build_hud_draft(500_000, PropertyType.SFR, LoanType.FIX_AND_FLIP, 7500)
    dscr = build_hud_draft(500_000, PropertyType.SFR, LoanType.DSCR, 7500)
    assert any(i.code == "insp" for i in flip.items)
    assert not any(i.code == "insp" for i in dscr.items)


def test_hud_draft_title_is_half_pct_of_loan():
    draft = build_hud_draft(1_000_000, PropertyType.SFR, LoanType.DSCR, 15000)
    title = next(i for i in draft.items if i.code == "title")
    assert title.amount == 5000.0  # 0.5% × 1,000,000


def test_hud_draft_total_sums_items():
    draft = build_hud_draft(500_000, PropertyType.SFR, LoanType.DSCR, 7500)
    expected = sum(i.amount for i in draft.items)
    assert draft.total == round(expected, 2)
