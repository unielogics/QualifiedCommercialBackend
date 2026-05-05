from app.enums import LoanPurpose, LoanType
from app.services.lender_matrix import validate_dscr, validate_fix_flip, validate_loan


def test_dscr_clean_inputs_no_warnings():
    out = validate_dscr(ltv=0.75, purpose=LoanPurpose.PURCHASE, fico=720, dscr_ratio=1.30)
    assert out == []


def test_dscr_ltv_above_purchase_cap_blocks():
    out = validate_dscr(ltv=0.85, purpose=LoanPurpose.PURCHASE, fico=720, dscr_ratio=1.30)
    assert any(w.code == "ltv_above_cap" and w.severity == "block" for w in out)


def test_dscr_ltv_above_cashout_cap_blocks():
    out = validate_dscr(ltv=0.78, purpose=LoanPurpose.CASH_OUT_REFI, fico=720, dscr_ratio=1.30)
    assert any(w.code == "ltv_above_cap" for w in out)


def test_dscr_fico_below_minimum_blocks():
    out = validate_dscr(ltv=0.75, purpose=LoanPurpose.PURCHASE, fico=650, dscr_ratio=1.30)
    assert any(w.code == "fico_below_min" for w in out)


def test_dscr_ratio_below_one_blocks():
    out = validate_dscr(ltv=0.75, purpose=LoanPurpose.PURCHASE, fico=720, dscr_ratio=0.95)
    assert any(w.code == "dscr_below_min" for w in out)


def test_fix_flip_clean_inputs_no_warnings():
    out = validate_fix_flip(ltc=0.80, arv_ltv=0.65, term_months=12)
    assert out == []


def test_fix_flip_ltc_too_high():
    out = validate_fix_flip(ltc=0.90, arv_ltv=0.65, term_months=12)
    assert any(w.code == "ltc_above_cap" for w in out)


def test_fix_flip_arv_too_high():
    out = validate_fix_flip(ltc=0.80, arv_ltv=0.75, term_months=12)
    assert any(w.code == "arv_ltv_above_cap" for w in out)


def test_fix_flip_term_out_of_range():
    out = validate_fix_flip(ltc=0.80, arv_ltv=0.65, term_months=24)
    assert any(w.code == "term_out_of_range" for w in out)


def test_validate_loan_dispatches_dscr():
    out = validate_loan(
        loan_type=LoanType.DSCR,
        ltv=0.85,
        purpose=LoanPurpose.PURCHASE,
        fico=720,
        dscr_ratio=1.30,
    )
    assert any(w.code == "ltv_above_cap" for w in out)


def test_validate_loan_dispatches_flip():
    out = validate_loan(
        loan_type=LoanType.FIX_AND_FLIP,
        ltc=0.90,
        arv_ltv=0.65,
        term_months=12,
    )
    assert any(w.code == "ltc_above_cap" for w in out)
