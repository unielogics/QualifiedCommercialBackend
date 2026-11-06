"""Persisted informational metrics must not break a client file's read path."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.capital_readiness import ApplicationCapitalReadinessSnapshot
from app.schemas.capital_readiness import ReadinessMetric, ReadinessPillar
from app.services.capital_readiness import _metric, _snapshot_read, read_snapshot


def stored_snapshot(status, value):
    return ApplicationCapitalReadinessSnapshot(
        id=uuid4(), profile_id=uuid4(), snapshot_version=2, policy_id=uuid4(),
        policy_key="qc_lending_margin_v1", policy_version=1,
        formula_version="score_v2", evidence_fingerprint="a" * 64,
        idempotency_key="persisted-informational-metric", as_of=datetime.now(UTC),
        communication_locale="en", review_status="confirmed", score=Decimal("70"),
        band="three_to_six_months", evidence_coverage_pct=Decimal("80"), confidence_pct=Decimal("90"),
        pillars=[{"key": "financial_health", "label": "Financial health", "weight": 1,
                  "score": 70, "coverage_pct": 80, "status": "acceptable"}],
        metrics=[_metric(key="monthly_debt_payments", label="Monthly debt payments", value=value,
                         unit="currency", metric_status=status, confidence=Decimal("1"),
                         source={"informational": True, "kind": "accepted_debt_schedule" if status == "accepted" else "bank_observation",
                                 "version_id": str(uuid4())})],
        strengths=[], blockers=[], phases=[], program_opportunities=[], source_manifest=[],
        created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("status", ["accepted", "observed"])
@pytest.mark.parametrize("value", [Decimal("0"), Decimal("500.25"), None])
@pytest.mark.parametrize("client_safe", [False, True])
def test_existing_informational_snapshot_serializes_without_rewriting_health_or_storage(status, value, client_safe):
    snapshot = stored_snapshot(status, value)
    original = deepcopy(snapshot.metrics)
    # The admin lead response calls _snapshot_read directly. Other scoped
    # readiness routes use its public wrapper; both must accept old snapshots.
    direct = _snapshot_read(snapshot, client_safe=client_safe)
    public = read_snapshot(snapshot, client_safe=client_safe)
    assert direct.model_dump(mode="json") == public.model_dump(mode="json")
    assert direct.metrics[0].status == status
    assert direct.metrics[0].value == (float(value) if value is not None else None)
    assert direct.metrics[0].source["informational"] is True
    assert direct.pillars[0].status == "acceptable" and direct.score == 70
    assert snapshot.metrics == original


@pytest.mark.parametrize("status", ["accepted", "observed"])
def test_informational_status_does_not_become_a_valid_scored_pillar(status):
    with pytest.raises(ValidationError):
        ReadinessPillar(key="financial_health", label="Financial health", weight=1,
                        score=None, coverage_pct=0, status=status)


def test_unknown_metric_status_is_still_rejected():
    with pytest.raises(ValidationError):
        ReadinessMetric(key="monthly_debt_payments", label="Monthly debt payments",
                        value=500, unit="currency", status="arbitrary_state")
