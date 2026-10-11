"""Exact lineage and advisory paths strengthen existing reads, never decisions."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers.application_profiles import get_application_evidence_file_url
from app.schemas.application_profile import ProgramFitCandidate
from app.services import application_programs, capital_readiness
from app.services.financial_fact_citations import build_financial_citation_manifest
from app.tests.test_numbers_first_readiness import computation as _base_computation


@pytest.fixture
def computation(monkeypatch):
    return _base_computation.__wrapped__(monkeypatch)


def cite_period(period):
    file_id, analysis_id = uuid4(), uuid4()
    period.source_file_id, period.source_analysis_id = file_id, analysis_id
    period.idempotency_key = f"statement-analysis:{analysis_id}"
    facts = {
        "entity_name": period.entity_name, "basis": period.accounting_basis,
        "currency": period.currency, "period_start": period.period_start.isoformat(),
        "period_end": period.period_end.isoformat(),
    }
    quotes = {}
    for field, label in (("revenue", "Total revenue"), ("cogs", "Cost of goods sold"),
                         ("gross_profit", "Gross profit"), ("net_income", "Net income")):
        facts[field] = str(getattr(period, field))
        quote = f"{label} {facts[field]}"
        quotes[field] = {"page": 2, "line_label": label, "quote": quote,
                         "value": facts[field], "measure": field}
    facts["fact_citations"] = quotes
    analysis = SimpleNamespace(id=analysis_id, bucket_file_id=file_id,
                               content_hash=period.content_hash, status="completed",
                               analysis={"key_facts": facts})
    manifest = build_financial_citation_manifest(
        analysis, source_pages={2: "\n".join(item["quote"] for item in quotes.values())},
        source_kind="pdf_searchable", source_hash=period.content_hash,
    )
    analysis.analysis["financial_fact_citations"] = manifest
    file = SimpleNamespace(id=file_id, file_name="2025 P&L.pdf", content_hash=period.content_hash)
    capital_readiness.attach_financial_period_citations([period], files={file_id: file},
                                                       newest_analyses={file_id: analysis})
    return file, analysis


def candidate(**changes):
    values = dict(program_key="working_capital", program_name="Working Capital",
                  catalog_id=uuid4(), public_slug="working-capital", playbook_version=3,
                  eligible=True, recommendation_status="recommended", scope_status="matched",
                  rule_state="pass", rule_results=[{"field": "revenue", "operator": "gte",
                  "required": 100000, "observed": 1000000, "status": "pass", "source_refs": []}])
    values.update(changes)
    return ProgramFitCandidate(**values)


def finalize(snapshot):
    snapshot.created_at = datetime.now(UTC)
    return snapshot


@pytest.mark.asyncio
async def test_exact_source_reaches_metric_period_ai_and_program_lineage(computation):
    db, profile, period = computation
    file, analysis = cite_period(period)
    period.review_status = "confirmed"
    snapshot = finalize(await capital_readiness.recompute(db, profile, idempotency_key="cited-integration",
                                                        expected_snapshot_version=None))
    metric = snapshot.metrics[0]
    assert metric["value"] == 18 and metric["numerator"] == 180000
    assert metric["source"]["citation_status"] == "verified"
    assert {row["field"] for row in metric["source"]["fact_citations"]} == {"revenue", "gross_profit"}
    assert all(row["page"] == 2 and row["file_name"] == "2025 P&L.pdf" for row in metric["source"]["fact_citations"])
    assert capital_readiness.period_read(period).fact_citation_status["revenue"]["status"] == "verified"
    kwargs = application_programs.published_candidates.call_args.kwargs
    assert kwargs["readiness_metric_overrides"] == {"gross_margin_pct": 18, "net_margin_pct": 5}
    source = kwargs["readiness_metric_sources"]["gross_margin_pct"]
    assert source["source_refs"][0]["file_id"] == str(file.id)
    assert source["source_refs"][0]["analysis_id"] == str(analysis.id)
    context = capital_readiness.build_ai_context(snapshot)
    assert context["metrics"][0]["fact_citations"][0]["page"] == 2
    assert context["funding_path"]["status"] == "criteria_unavailable"


@pytest.mark.asyncio
async def test_legacy_numbers_remain_visible_but_not_verified_program_facts(computation):
    db, profile, period = computation
    period.review_status = "confirmed"
    snapshot = await capital_readiness.recompute(db, profile, idempotency_key="legacy-citations",
                                                expected_snapshot_version=None)
    assert snapshot.metrics[0]["value"] == 18
    assert snapshot.metrics[0]["source"]["citation_status"] == "unverified"
    assert snapshot.metrics[0]["source"]["fact_citations"] == []
    assert application_programs.published_candidates.call_args.kwargs["readiness_metric_overrides"] == {
        "gross_margin_pct": None, "net_margin_pct": None}


def test_retired_or_cross_file_sources_keep_values_without_current_links(computation):
    _, _, period = computation
    file, analysis = cite_period(period)
    assert period.fact_citations
    capital_readiness.attach_financial_period_citations([period], files={}, newest_analyses={})
    assert capital_readiness.period_read(period).revenue == 1000000
    assert period.fact_citations == {}
    foreign = SimpleNamespace(id=uuid4(), bucket_file_id=uuid4(), content_hash=period.content_hash)
    capital_readiness.attach_financial_period_citations([period], files={file.id: file},
                                                       newest_analyses={file.id: foreign})
    assert period.fact_citations == {}


def test_partial_and_derived_lineage_never_invents_a_printed_total(computation):
    _, _, period = computation
    file, analysis = cite_period(period)
    period.fact_citation_status["gross_profit"] = {"status": "unverified", "reason": "citation_not_provided"}
    period.fact_citations.pop("gross_profit")
    source = capital_readiness.metric_citation_metadata([period], ("revenue", "gross_profit"))
    assert source["citation_status"] == "partial"
    assert source["fact_citations"][0]["field"] == "revenue"
    period.fact_citations["gross_profit"] = [period.fact_citations["revenue"][0], period.fact_citations["cogs"][0]]
    period.fact_citation_status["gross_profit"] = {"status": "verified", "reason": "derived_from_verified_inputs"}
    source = capital_readiness.metric_citation_metadata([period], ("revenue", "gross_profit"))
    assert source["citation_status"] == "verified"
    assert {row["field"] for row in source["fact_citations"]} == {"revenue", "cogs"}


@pytest.mark.asyncio
async def test_funding_path_is_immutable_advisory_with_exact_rule_results(computation, monkeypatch):
    db, profile, period = computation
    monkeypatch.setattr(application_programs, "published_candidates", AsyncMock(return_value=[candidate()]))
    snapshot = finalize(await capital_readiness.recompute(db, profile, idempotency_key="funding-path",
                                                        expected_snapshot_version=None))
    read = capital_readiness._snapshot_read(snapshot)
    assert read.funding_path["status"] == "potential_program_fit"
    assert read.funding_path["advisory_only"] is True
    assert read.funding_path["eligible_programs"][0]["policy_version"] == 3
    assert read.program_opportunities[0]["rule_results"][0]["required"] == 100000
    assert profile.vertical == "main_street"
    assert not hasattr(profile, "decision")
    db.add.assert_called()  # only financial assessment/snapshot, never stage/decision write


@pytest.mark.asyncio
async def test_client_reads_and_context_hide_internal_quotes_and_fact_ids(computation):
    db, profile, period = computation
    cite_period(period)
    profile.communication_locale = "es"
    snapshot = finalize(await capital_readiness.recompute(db, profile, idempotency_key="redacted-citations",
                                                        expected_snapshot_version=None))
    original = deepcopy(snapshot.metrics)
    read = capital_readiness._snapshot_read(snapshot, client_safe=True)
    assert "fact_citations" not in read.metrics[0].source
    assert "citation_reason" not in read.metrics[0].source
    assert "source_period_ids" not in read.metrics[0].source
    assert read.source_manifest == []
    assert "program_results" not in read.funding_path
    assert "No hay criterios" in read.funding_path["reasons"][0]
    context = capital_readiness.build_ai_context(snapshot, client_safe=True)
    assert context["metrics"][0]["fact_citations"] == []
    assert "source_period_ids" not in context["metrics"][0]
    assert snapshot.metrics == original


@pytest.mark.asyncio
async def test_missing_evidence_not_a_capital_readiness_financial_failure(computation, monkeypatch):
    db, profile, period = computation
    for field in ("revenue", "cogs", "gross_profit", "net_income"):
        setattr(period, field, None)
    monkeypatch.setattr(application_programs, "published_candidates", AsyncMock(return_value=[candidate(
        eligible=False, recommendation_status="needs_information", rule_state="unknown", evidence_gaps=["revenue"])]))
    snapshot = finalize(await capital_readiness.recompute(db, profile, idempotency_key="missing-facts",
                                                        expected_snapshot_version=None))
    assert capital_readiness._snapshot_read(snapshot).funding_path["status"] == "needs_evidence"
    assert all(row["missing_evidence"] for row in snapshot.blockers if row.get("metric_key") in {"gross_margin_pct", "net_margin_pct"})


@pytest.mark.asyncio
async def test_private_review_notes_never_enter_client_copy_or_ai_context(computation):
    db, profile, period = computation
    period.reconciliation_warnings = [
        {"code": "review_note", "detail": "PRIVATE INTERNAL REVIEW NOTE"},
        {"code": "financial_currency_unknown", "severity": "evidence_gap", "detail": "UNTRUSTED DETAIL"},
    ]
    profile.communication_locale = "es"
    snapshot = finalize(await capital_readiness.recompute(db, profile, idempotency_key="private-warnings",
                                                        expected_snapshot_version=None))
    for context in (capital_readiness.build_ai_context(snapshot),
                    capital_readiness.build_ai_context(snapshot, client_safe=True)):
        warnings = context["metrics"][0]["context_warnings"]
        assert warnings == [{"code": "financial_currency_unknown", "severity": "evidence_gap",
                             "detail": "La moneda es desconocida; no combine estos importes con otra fuente."}]
    warnings = capital_readiness._snapshot_read(snapshot, client_safe=True).metrics[0].source["context_warnings"]
    assert warnings[0]["code"] == "financial_currency_unknown" and "source_period_id" not in warnings[0]
    assert len(warnings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("current_hash,expected_hash,allowed", [("a" * 64, "a" * 64, True),
                                                               ("a" * 64, "A" * 64, True),
                                                               ("b" * 64, "a" * 64, False),
                                                               (None, "a" * 64, False)])
async def test_citation_preview_does_not_sign_replacement_bytes(current_hash, expected_hash, allowed):
    profile_id, file_id = uuid4(), uuid4()
    file = SimpleNamespace(id=file_id, file_name="source.pdf", content_hash=current_hash,
                           s3_key="private/source.pdf", content_type="application/pdf", status="uploaded", deleted_at=None)
    db = SimpleNamespace(get=AsyncMock(return_value=file), commit=AsyncMock())
    with (patch("app.routers.application_profiles.profiles.load_profile", AsyncMock(return_value=SimpleNamespace(id=profile_id))),
          patch("app.routers.application_profiles.profiles.evidence_state", AsyncMock(return_value=SimpleNamespace(files=[file]))),
          patch("app.routers.application_profiles.profiles.log_profile_action", AsyncMock()),
          patch("app.routers.buckets._download_url", return_value="https://storage.example/signed") as sign):
        if allowed:
            result = await get_application_evidence_file_url(profile_id, file_id, SimpleNamespace(role=Role.LOAN_EXEC), db,
                                                           expected_content_hash=expected_hash)
            assert result["expires_in"] == 900
            sign.assert_called_once()
        else:
            with pytest.raises(HTTPException) as error:
                await get_application_evidence_file_url(profile_id, file_id, SimpleNamespace(role=Role.LOAN_EXEC), db,
                                                       expected_content_hash=expected_hash)
            assert error.value.status_code == 409
            assert error.value.detail["code"] == "evidence_source_changed"
            sign.assert_not_called()
            db.commit.assert_not_awaited()
