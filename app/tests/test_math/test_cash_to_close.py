from app.enums import LoanPurpose
from app.services.math.cash_to_close import borrower_equity_required, total_cash_to_close
from app.services.math.sizing import SizingResult


def test_purchase_cash_to_close_includes_equity_gap_hud_and_discount():
    equity = borrower_equity_required(
        sizing=SizingResult(
            loan_amount=500_000,
            max_allowed=500_000,
            binding_constraint="ltv",
            clamped=False,
            ltv=0.7692,
        ),
        purpose=LoanPurpose.PURCHASE,
        amount=500_000,
        arv=650_000,
    )

    assert equity == 150_000
    assert total_cash_to_close(
        borrower_equity=equity,
        hud_total=12_990,
        discount_dollars=5_000,
    ) == 167_990


def test_refi_cash_to_close_uses_payoff_gap_only_when_short():
    equity = borrower_equity_required(
        sizing=SizingResult(
            loan_amount=500_000,
            max_allowed=500_000,
            binding_constraint="refi-cap",
            clamped=False,
            cash_to_borrower=-25_000,
        ),
        purpose=LoanPurpose.CASH_OUT_REFI,
        amount=500_000,
        arv=700_000,
    )

    assert equity == 25_000
