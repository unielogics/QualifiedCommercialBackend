from app.services.math.dscr import dscr, pitia


def test_pitia_includes_taxes_insurance_hoa():
    p = pitia(
        principal=300_000,
        annual_rate=0.07,
        term_months=360,
        annual_taxes=6_000,
        annual_insurance=1_500,
        monthly_hoa=200,
    )
    # P&I ≈ 1995.91, +500 tax, +125 insurance, +200 hoa ≈ 2820.91
    assert abs(p - 2820.91) < 1.0


def test_dscr_above_one_when_rent_covers_pitia():
    ratio = dscr(
        monthly_rent=3500,
        principal=300_000,
        annual_rate=0.07,
        term_months=360,
        annual_taxes=6_000,
        annual_insurance=1_500,
    )
    assert ratio > 1.0


def test_dscr_below_one_when_rent_short():
    ratio = dscr(
        monthly_rent=1500,
        principal=300_000,
        annual_rate=0.07,
        term_months=360,
        annual_taxes=6_000,
        annual_insurance=1_500,
    )
    assert ratio < 1.0


def test_dscr_zero_pitia_returns_zero():
    ratio = dscr(
        monthly_rent=2000,
        principal=0,
        annual_rate=0.07,
        term_months=360,
        annual_taxes=0,
        annual_insurance=0,
    )
    assert ratio == 0.0


def test_dscr_at_preferred_threshold():
    # Engineer rent so DSCR ≈ 1.20
    pitia_val = pitia(300_000, 0.07, 360, 6_000, 1_500)
    target_rent = pitia_val * 1.20
    ratio = dscr(target_rent, 300_000, 0.07, 360, 6_000, 1_500)
    assert abs(ratio - 1.20) < 0.01
