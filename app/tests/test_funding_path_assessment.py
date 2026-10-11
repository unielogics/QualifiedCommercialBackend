from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.schemas.application_profile import ProgramFitCandidate
from app.services import application_programs as programs
from app.services.funding_path_assessment import assess_funding_paths
from app.services.program_rules import evaluate_rules


def candidate(**changes):
    return ProgramFitCandidate(**{
        "program_key": "term", "program_name": "Term", "catalog_id": uuid4(),
        "public_slug": "term", "playbook_version": 3, "eligible": False,
        "recommendation_status": "not_eligible", "criteria_status": "published",
        "scope_status": "matched", "rule_state": "fail", **changes,
    })


@pytest.mark.parametrize("node", [
    {"field": "revenue", "op": "gte", "value": 100},
    {"not": {"field": "revenue", "op": "gte", "value": 100}},
    {"not": {"field": "bank_statement", "op": "evidence_available"}},
])
def test_missing_input_cannot_pass_an_exclusion_or_become_a_known_failure(node):
    result = evaluate_rules({"fit": node}, {})
    assert result.matched is False and result.state == "unknown"


def test_all_known_fail_and_missing_input_remain_distinct_from_unknown_alternative():
    failed = {"field": "requested_amount", "op": "lte", "value": 100}
    unknown = {"field": "revenue", "op": "gte", "value": 100}
    context = {"requested_amount": 150}
    assert evaluate_rules({"fit": {"all": [failed, unknown]}}, context).state == "fail"
    assert evaluate_rules({"fit": {"any": [failed, unknown]}}, context).state == "unknown"


def test_verified_alternative_can_match_without_irrelevant_unknown_branch():
    result = evaluate_rules({"fit": {"any": [
        {"field": "requested_amount", "op": "lte", "value": 100},
        {"field": "revenue", "op": "gte", "value": 100},
    ]}}, {"requested_amount": 90})
    assert result.state == "pass" and result.matched


def test_supported_fit_is_a_path_not_approval_or_automatic_audit():
    result = assess_funding_paths([candidate(eligible=True, rule_state="pass")],
        readiness_blockers=[{"metric_key": "net_margin_pct", "impact": "high"}])
    assert result["status"] == "potential_program_fit"
    assert result["advisory_only"] is True
    assert result["eligible_programs"][0]["policy_version"] == 3


@pytest.mark.parametrize("row,expected", [
    (candidate(rule_state="unknown", evidence_gaps=["revenue"]), "needs_evidence"),
    (candidate(rule_state="unknown", requires_review=True), "needs_review"),
    (candidate(criteria_status="not_published", rule_state="unavailable"), "criteria_unavailable"),
    (candidate(scope_status="outside_scope"), "criteria_unavailable"),
])
def test_unknown_and_configuration_gaps_cannot_recommend_audit(row, expected):
    result = assess_funding_paths([row], readiness_blockers=[{"metric_key": "net_margin_pct"}])
    assert result["status"] == expected


def test_only_actual_financial_remediation_qualifies_not_identity_or_missing_docs():
    for blockers in [[], [{"key": "financial_entity_mismatch"}],
                     [{"metric_key": "net_margin_pct", "severity": "evidence_gap"}],
                     [{"metric_key": "net_margin_pct", "missing_evidence": True}]]:
        assert assess_funding_paths([candidate()], readiness_blockers=blockers)["status"] == "needs_review"
    assert assess_funding_paths([candidate()], readiness_blockers=[{
        "metric_key": "net_margin_pct", "impact": "high", "severity": "financial_finding", "missing_evidence": False,
        "verification_status": "verified",
    }])["status"] == "capital_readiness_recommended"


def test_provisional_margin_finding_requires_review_not_audit_recommendation():
    result = assess_funding_paths([candidate()], readiness_blockers=[{
        "metric_key": "net_margin_pct", "severity": "financial_finding",
        "missing_evidence": False, "verification_status": "provisional",
    }])
    assert result["status"] == "needs_review"


def fact(value, *, field="revenue", file_id=None, analysis_id=None, **changes):
    return SimpleNamespace(id=uuid4(), field_key=field, value={"value": value},
        normalized_value=None, status="accepted", reviewed_by_user_id=uuid4(),
        source_file_id=file_id, source_analysis_id=analysis_id,
        extraction_method="manual" if file_id is None else "document_ai", **changes)


def test_staff_zero_preserved_and_multiple_conflicting_sources_not_mixed():
    context = {"revenue": 999}
    source = programs._fit_fact_sources(context, [fact(0)], [], {})
    assert context["revenue"] == 0
    assert source["revenue"]["verification_status"] == "verified"
    source = programs._fit_fact_sources(context, [fact(100), fact(200)], [], {})
    assert source["revenue"]["verification_status"] == "conflicting"


@pytest.mark.parametrize("classification,hash_value,actor", [
    ("bank_statement", "current", True), ("current_p_and_l", "old", True),
    ("current_p_and_l", "current", False),
])
def test_bank_deposits_stale_bytes_or_unreviewed_facts_cannot_be_revenue(classification, hash_value, actor):
    file_id, analysis_id = uuid4(), uuid4()
    file = SimpleNamespace(id=file_id, content_hash="current")
    analysis = SimpleNamespace(id=analysis_id, status="completed", content_hash=hash_value,
                               classification=classification)
    row = fact(1000, file_id=file_id, analysis_id=analysis_id)
    row.reviewed_by_user_id = uuid4() if actor else None
    source = programs._fit_fact_sources({"revenue": 1000}, [row], [file], {file_id: analysis})
    assert source["revenue"]["verification_status"] != "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("verification,expected", [("provisional", False), ("verified", True), ("conflicting", False)])
async def test_candidate_requires_verified_rule_fact_not_populated_ai_summary(verification, expected):
    catalog = SimpleNamespace(id=uuid4(), program_key="term", name="Term", public_slug="term")
    version = SimpleNamespace(id=uuid4(), version=3,
        rules={"fit": {"field": "revenue", "op": "gte", "value": 100}})
    context = {"vertical": "main_street", "revenue": 1000,
        "fact_sources": {"revenue": {"verification_status": verification, "source_refs": [{"kind": "accepted_fact", "id": "source"}]}}}
    with (patch.object(programs, "profile_fit_context", AsyncMock(return_value=context)),
          patch.object(programs.program_catalog, "catalog_rows", AsyncMock(return_value=[catalog])),
          patch.object(programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
          patch.object(programs.program_catalog, "published_versions_by_program", AsyncMock(return_value={catalog.id: version})),
          patch.object(programs, "_catalog_scope_match", return_value=(True, []))):
        row = (await programs.published_candidates(SimpleNamespace(), SimpleNamespace()))[0]
    assert row.eligible is expected
    assert row.rule_results[0]["observed"] == 1000
    assert row.rule_results[0]["source_refs"][0]["id"] == "source"
    assert row.requires_review is (not expected)
    assert row.rule_state == ("pass" if expected else "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("verification", ["verified", "provisional"])
async def test_current_margin_override_preserves_exact_lineage_and_review_gate(verification):
    catalog = SimpleNamespace(id=uuid4(), program_key="term", name="Term", public_slug="term")
    version = SimpleNamespace(id=uuid4(), version=3,
        rules={"fit": {"field": "gross_margin_pct", "op": "gte", "value": 13}})
    source = {"verification_status": verification, "source_refs": [{
        "period_id": "p", "file_id": "f", "analysis_id": "a", "content_hash": "b" * 64,
        "fact_citations": [{"page": 1, "quote": "Revenue 100; gross profit 18"}],
    }]}
    with (patch.object(programs, "profile_fit_context", AsyncMock(return_value={"vertical": "main_street"})),
          patch.object(programs.program_catalog, "catalog_rows", AsyncMock(return_value=[catalog])),
          patch.object(programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
          patch.object(programs.program_catalog, "published_versions_by_program", AsyncMock(return_value={catalog.id: version})),
          patch.object(programs, "_catalog_scope_match", return_value=(True, []))):
        row = (await programs.published_candidates(SimpleNamespace(), SimpleNamespace(),
            readiness_metric_overrides={"gross_margin_pct": 18, "net_margin_pct": None},
            readiness_metric_sources={"gross_margin_pct": source}))[0]
    assert row.eligible is (verification == "verified")
    assert row.rule_results[0]["source_refs"] == source["source_refs"]
    assert row.rule_results[0]["observed"] == 18
