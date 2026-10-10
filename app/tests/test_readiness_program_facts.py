import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.services import application_programs
from app.services.application_programs import reviewed_profitability_facts
from app.services.program_rules import evaluate_rules, validate_rules


def _snapshot(*, review_status="confirmed", source=None):
    return SimpleNamespace(review_status=review_status, metrics=[{"key": "gross_margin_pct", "value": 13, "status": "healthy", "source": source or {"review_statuses": ["confirmed"]}}])


def test_program_margin_rule_requires_explicit_published_criterion():
    rules = {"fit": {"field": "gross_margin_pct", "op": "gte", "value": 13}}
    validate_rules(rules, enforce_supported_fields=True)
    assert evaluate_rules(rules, reviewed_profitability_facts(_snapshot())).matched


def test_provisional_and_self_reported_results_never_satisfy_program_rules():
    for snapshot in [
        None,
        _snapshot(review_status="provisional"),
        _snapshot(source={"review_statuses": ["submitted"]}),
        _snapshot(source={"review_statuses": ["confirmed"], "verification_status": "self_reported_unverified"}),
        _snapshot(source={"review_statuses": ["confirmed"], "needs_reconciliation_review": True}),
        _snapshot(source={"review_statuses": ["confirmed"], "not_applicable": True}),
    ]:
        assert reviewed_profitability_facts(snapshot) == {"gross_margin_pct": None, "net_margin_pct": None}


def test_recalculation_does_not_route_using_an_old_confirmed_margin():
    program = SimpleNamespace(id=uuid4(), program_key="term", name="Term", public_slug="term")
    version = SimpleNamespace(id=uuid4(), version=1, rules={"fit": {"field": "gross_margin_pct", "op": "gte", "value": 13}})
    with (
        patch.object(application_programs, "profile_fit_context", AsyncMock(return_value={"gross_margin_pct": 18})),
        patch.object(application_programs.program_catalog, "catalog_rows", AsyncMock(return_value=[program])),
        patch.object(application_programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
        patch.object(application_programs.program_catalog, "published_versions_by_program", AsyncMock(return_value={program.id: version})),
        patch.object(application_programs, "_catalog_scope_match", return_value=(True, [])),
    ):
        result = asyncio.run(application_programs.published_candidates(SimpleNamespace(), SimpleNamespace(), readiness_metric_overrides={"gross_margin_pct": None, "net_margin_pct": None}))
    assert result[0].eligible is False
    assert result[0].recommendation_status == "needs_information"
