from datetime import date

import pytest
from sqlalchemy import select

from app.models.application_terms import ApplicationTermSheet
from app.routers import application_terms as routes
from app.services.application_terms_pdf import render_terms_html
from app.tests.test_atomic_client_terms_plan import env as env
from app.tests.test_atomic_client_terms_plan import payload


@pytest.mark.asyncio
async def test_saved_date_version_adopts_the_same_plan_and_pdf_and_new_term_recalculates(env):
    first = await routes.put_client_terms(env.profile.id, payload(expected_funding_date="2026-01-31", term_months=1, maturity_date_mode="automatic"), env.actor, env.db)
    assert first.expected_funding_date == date(2026, 1, 31)
    assert first.maturity_date == date(2026, 2, 28)
    assert env.profile.financial_plan["new_loan_maturity_date"] == "2026-02-28"
    old_row = env.session.execute(select(ApplicationTermSheet).where(ApplicationTermSheet.version == 1)).scalar_one()
    frozen_dates = dict(old_row.deal_budget_calculation["loan_dates"])
    # An old client omitting all new date fields retains the funding estimate,
    # while a changed term recalculates the new immutable version's maturity.
    second = await routes.put_client_terms(env.profile.id, payload(expected_version=1, term_months=12), env.actor, env.db)
    assert second.expected_funding_date == first.expected_funding_date
    assert second.maturity_date == date(2027, 1, 31)
    assert old_row.deal_budget_calculation["loan_dates"] == frozen_dates
    html = render_terms_html(old_row, business_name="Synthetic Date Example")
    assert "January 31, 2026" in html and "February 28, 2026" in html
    assert "Estimated dates may change" in html


@pytest.mark.asyncio
async def test_manual_date_version_is_preserved_then_return_to_automatic_is_explicit(env):
    manual = await routes.put_client_terms(env.profile.id, payload(expected_funding_date="2026-01-31", term_months=12, maturity_date="2028-06-15", maturity_date_mode="manual"), env.actor, env.db)
    assert manual.maturity_date_mode == "manual"
    assert env.profile.financial_plan["new_loan_maturity_date"] == "2028-06-15"
    kept = await routes.put_client_terms(env.profile.id, payload(expected_version=1, term_months=24), env.actor, env.db)
    assert kept.maturity_date == manual.maturity_date
    automatic = await routes.put_client_terms(env.profile.id, payload(expected_version=2, term_months=24, maturity_date_mode="automatic"), env.actor, env.db)
    assert automatic.maturity_date == date(2028, 1, 31)
