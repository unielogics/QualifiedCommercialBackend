"""Current operating earnings, hypothetical refinance, and published policy agree."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import application_profiles, application_programs, bucket_ai, financial_cockpit
from app.services.ai import context as loan_context
from app.tests.test_financial_metric_review import build, correction


def test_financial_check_explains_published_limit_and_verified_source_without_staff_audit():
    source = {"currency": "USD", "period": "Jan–Jun 2026", "basis": "accrual", "annualization_factor": 2,
              "source_refs": [{"file_id": "pnl", "currency": "USD", "period_start": "2026-01-01", "period_end": "2026-06-30", "staff_reason": "PRIVATE"}],
              "actor_name": "PRIVATE", "history": ["PRIVATE"]}
    candidate = SimpleNamespace(fact_sources={"annual_ebitda": source}, rule_results=[
        {"field": "annual_ebitda", "operator": "gte", "required": 50000, "observed": 52000, "status": "pass", "verification_status": "verified"},
        {"field": "annual_ebitda", "operator": "lt", "required": 0, "observed": 52000, "status": "fail", "negated": True},
        {"field": "unrelated_private_fact", "observed": "PRIVATE"},
    ])
    checks = bucket_ai._program_financial_checks(candidate)
    assert checks[0]["required"] == 50000 and checks[0]["observed"] == 52000
    assert checks[0]["period"] == "Jan–Jun 2026" and checks[0]["currency"] == "USD"
    assert checks[0]["source_refs"][0]["file_id"] == "pnl"
    assert checks[1]["negated"] is True and checks[1]["status"] == "fail"
    assert len(checks) == 2 and "PRIVATE" not in json.dumps(checks)


@pytest.mark.asyncio
async def test_failed_current_read_drops_cached_earnings_and_hypothetical_plan():
    data = {"financial_calculation_review": {"outdated": True}, "desk_financial_plan": {"outdated": True}}
    with patch.object(financial_cockpit, "load_cockpit", AsyncMock(side_effect=RuntimeError("temporarily unavailable"))):
        await bucket_ai._attach_current_financial_context(None, SimpleNamespace(id=uuid4()), data)
    assert not data


@pytest.mark.asyncio
async def test_loan_ai_receives_corrected_current_earnings_and_saved_refinance_separately():
    cockpit = build(state=correction(build()), plan={"new_payment_monthly": 500})
    screen = [{"program_key": "example", "criteria_version": 3, "eligible": True,
               "financial_checks": [{"field": "annual_ebitda", "required": 60000, "observed": 72000}]}]
    with patch.object(application_profiles, "find_profile", AsyncMock(return_value=SimpleNamespace(id=uuid4()))), \
         patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
         patch.object(bucket_ai, "_published_program_screen", AsyncMock(return_value=screen)):
        result = await loan_context._financial_calculation_block(None, SimpleNamespace(id=uuid4()), audience="super_admin")
    data = json.loads(result.rsplit("\n", 1)[1])
    assert data["financial_calculation_review"]["validated_annual"]["ebitda_annual"] == 72000
    assert data["desk_financial_plan"]["active"] and data["published_program_screen"] == screen
    assert "does not increase EBITDA" in bucket_ai.REVIEW_PLAN_RULES
    assert "hypothetical" in result and "not EBITDA" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["admin", "uploader"])
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_bucket_chat_attaches_current_earnings_only_to_exact_internal_scope(audience, ambiguous):
    cockpit = build(state=correction(build()), plan={"new_payment_monthly": 500})
    profile = SimpleNamespace(id=uuid4())
    bucket = SimpleNamespace(id=uuid4(), name="Example", client_name="Example", purpose="Refinance",
                             bucket_type="underwriting", ai_context={}, files=[], requested_documents=[], notes=[])
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(execute=AsyncMock(return_value=empty))
    with patch.object(application_programs, "profile_for_chat_scope", AsyncMock(return_value=None if ambiguous else profile)) as resolve, \
         patch.object(bucket_ai, "_program_context_for_chat", AsyncMock(return_value={})), \
         patch.object(bucket_ai, "latest_review", AsyncMock(return_value=None)), \
         patch.object(bucket_ai, "visible_action_items", AsyncMock(return_value=[])), \
         patch("app.services.capital_readiness.latest_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.financial_publication.current_version_context", AsyncMock(return_value={})), \
         patch("app.services.financial_publication.latest_publication_at", AsyncMock(return_value=None)), \
         patch("app.services.financial_publication.snapshot_matches_publication", return_value=True), \
         patch("app.services.financial_workspace.filter_current_files", AsyncMock(return_value=[])), \
         patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)) as read:
        data = await bucket_ai._chat_context(db, bucket=bucket, audience=audience,
                    upload_link=SimpleNamespace(id=uuid4(), recipient_name="Borrower") if audience == "uploader" else None,
                    share=None, vendor_access=None)
    if audience == "admin" and not ambiguous:
        assert data["financial_calculation_review"]["validated_annual"]["ebitda_annual"] == 72000
        assert data["desk_financial_plan"]["active"]
        read.assert_awaited_once_with(db, profile)
        resolve.assert_awaited_once_with(db, bucket_id=bucket.id, intake_id=None, upload_link_id=None)
    else:
        assert "financial_calculation_review" not in data and "desk_financial_plan" not in data
        read.assert_not_awaited()


def test_admin_rules_explain_ebitda_qualification_without_changing_borrower_prompt():
    admin = bucket_ai.build_chat_system("main_street_v1", audience="admin")
    borrower = bucket_ai.build_chat_system("main_street_v1", audience="uploader")
    assert bucket_ai.EBITDA_REASONING_RULES in admin
    assert bucket_ai.EBITDA_REASONING_RULES not in borrower
    assert "criteria_version" in admin and "all/any/negated" in admin
