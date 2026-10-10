from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.capital_readiness import CapitalReadinessPolicyVersion
from app.routers.capital_readiness_policies import (
    PolicyDraftCreate,
    PolicyPublishRequest,
    _create_policy_draft,
    _publish_policy,
)


def _draft_payload(**overrides) -> dict:
    payload = {
        "policy_key": "qc_lending_margin_v1",
        "minimum_coverage_pct": 60,
        "pillar_weights": {
            "revenue_earnings": 20,
            "debt_capital": 25,
            "liquidity_banking": 20,
            "bookkeeping_tax": 15,
            "credit_collateral": 10,
            "transaction_use": 10,
        },
        "metric_thresholds": {
            "gross_margin_pct": {
                "acceptable": 10,
                "healthy": 13,
                "very_strong": 18,
            },
            "net_margin_pct": {
                "acceptable": 2,
                "healthy": 3,
                "very_strong": 5,
            },
            "dscr": {"acceptable": 1, "healthy": 1.25, "very_strong": 1.5},
        },
        "confirmed": True,
    }
    payload.update(overrides)
    return payload


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = list(rows or [])

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows


@pytest.mark.parametrize(
    "change",
    [
        {"minimum_coverage_pct": 59.99},
        {
            "pillar_weights": {
                "revenue_earnings": 20,
                "debt_capital": 25,
                "liquidity_banking": 20,
                "bookkeeping_tax": 15,
                "credit_collateral": 10,
                "transaction_use": 9,
            }
        },
        {
            "pillar_weights": {
                "revenue_earnings": 20,
                "debt_capital": 25,
                "liquidity_banking": 20,
                "bookkeeping_tax": 15,
                "credit_collateral": 10,
                "unexpected": 10,
            }
        },
        {
            "metric_thresholds": {
                "gross_margin_pct": {
                    "acceptable": 13,
                    "healthy": 10,
                    "very_strong": 18,
                },
                "net_margin_pct": {
                    "acceptable": 2,
                    "healthy": 3,
                    "very_strong": 5,
                },
                "dscr": {
                    "acceptable": 1,
                    "healthy": 1.25,
                    "very_strong": 1.5,
                },
            }
        },
    ],
)
def test_policy_draft_rejects_invalid_governance_values(change: dict) -> None:
    with pytest.raises(ValidationError):
        PolicyDraftCreate.model_validate(_draft_payload(**change))


def test_policy_draft_accepts_the_complete_canonical_shape() -> None:
    payload = PolicyDraftCreate.model_validate(
        _draft_payload(policy_key="  QC_LENDING_MARGIN_V1  ")
    )

    assert payload.policy_key == "qc_lending_margin_v1"
    assert payload.minimum_coverage_pct == 60


@pytest.mark.parametrize(
    "policy_key,scope",
    [
        ("regional_override", None),
        ("regional_override", {"kind": "firm"}),
        (
            "qc_lending_margin_v1",
            {"kind": "vertical", "key": "main_street"},
        ),
        ("industry_override", {"kind": "industry", "key": "  "}),
        ("vertical_override", {"kind": "vertical", "key": "restaurant"}),
        ("naics_override", {"kind": "naics_prefix", "key": "72A"}),
        ("naics_override", {"kind": "naics_prefix", "key": "7"}),
        ("naics_override", {"kind": "naics_prefix", "key": "7225119"}),
    ],
)
def test_policy_draft_rejects_invalid_or_ambiguous_scope(
    policy_key: str, scope: dict | None
) -> None:
    thresholds = _draft_payload()["metric_thresholds"]
    if scope is not None:
        thresholds["scope"] = scope
    with pytest.raises(ValidationError):
        PolicyDraftCreate.model_validate(
            _draft_payload(policy_key=policy_key, metric_thresholds=thresholds)
        )


def test_policy_draft_normalizes_explicit_scope_key() -> None:
    thresholds = _draft_payload()["metric_thresholds"]
    thresholds["scope"] = {"kind": " INDUSTRY ", "key": " Full-Service Restaurants "}
    payload = PolicyDraftCreate.model_validate(
        _draft_payload(policy_key="restaurant_override", metric_thresholds=thresholds)
    )

    assert payload.metric_thresholds.scope is not None
    assert payload.metric_thresholds.scope.kind == "industry"
    assert payload.metric_thresholds.scope.key == "full-service restaurants"


@pytest.mark.parametrize(
    "path,value",
    [
        (("minimum_coverage_pct",), "60"),
        (("pillar_weights", "revenue_earnings"), True),
        (("metric_thresholds", "gross_margin_pct", "acceptable"), "10"),
    ],
)
def test_policy_draft_rejects_coerced_numeric_inputs(
    path: tuple[str, ...], value: object
) -> None:
    payload = _draft_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        PolicyDraftCreate.model_validate(payload)


@pytest.mark.asyncio
async def test_create_draft_locks_policy_family_before_assigning_next_version() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(), _Result(scalar=7)]),
        add=Mock(),
        flush=AsyncMock(),
    )
    payload = PolicyDraftCreate.model_validate(_draft_payload())

    row = await _create_policy_draft(db, payload, SimpleNamespace(id=uuid4()))

    assert row.version == 8
    assert row.status == "draft"
    assert "pg_advisory_xact_lock" in str(db.execute.await_args_list[0].args[0])
    assert "max(capital_readiness_policy_versions.version)" in str(
        db.execute.await_args_list[1].args[0]
    )


@pytest.mark.asyncio
async def test_publish_retires_previous_version_before_promoting_draft() -> None:
    actor_id = uuid4()
    candidate = CapitalReadinessPolicyVersion(
        id=uuid4(),
        policy_key="qc_lending_margin_v1",
        version=2,
        status="draft",
        minimum_coverage_pct=60,
        pillar_weights=_draft_payload()["pillar_weights"],
        metric_thresholds=_draft_payload()["metric_thresholds"],
    )
    previous = CapitalReadinessPolicyVersion(
        id=uuid4(),
        policy_key=candidate.policy_key,
        version=1,
        status="published",
        minimum_coverage_pct=60,
        pillar_weights=_draft_payload()["pillar_weights"],
        metric_thresholds=_draft_payload()["metric_thresholds"],
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=candidate),
                _Result(),
                _Result(scalar=candidate),
                _Result(rows=[previous]),
            ]
        ),
        add=Mock(),
        flush=AsyncMock(),
    )

    result = await _publish_policy(
        db,
        candidate.id,
        PolicyPublishRequest(confirmed=True, reason="Reviewed thresholds"),
        SimpleNamespace(id=actor_id),
    )

    assert result is candidate
    assert previous.status == "retired"
    assert candidate.status == "published"
    assert candidate.published_by_user_id == actor_id
    assert candidate.published_at is not None
    assert db.flush.await_count == 2
    assert "pg_advisory_xact_lock" in str(db.execute.await_args_list[1].args[0])


@pytest.mark.asyncio
async def test_publish_rejects_legacy_nondefault_draft_without_scope() -> None:
    candidate = CapitalReadinessPolicyVersion(
        id=uuid4(),
        policy_key="unscoped_override",
        version=1,
        status="draft",
        minimum_coverage_pct=60,
        pillar_weights=_draft_payload()["pillar_weights"],
        metric_thresholds=_draft_payload()["metric_thresholds"],
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[_Result(scalar=candidate), _Result(), _Result(scalar=candidate)]
        ),
        add=Mock(),
        flush=AsyncMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _publish_policy(
            db,
            candidate.id,
            PolicyPublishRequest(confirmed=True),
            SimpleNamespace(id=uuid4()),
        )

    assert exc_info.value.status_code == 422
    db.flush.assert_not_awaited()
