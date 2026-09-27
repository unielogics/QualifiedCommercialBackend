from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.funding_program import (
    FundingProgramCreate,
    FundingProgramPublishRequest,
    FundingProgramRetireRequest,
    FundingProgramScopePatch,
    FundingProgramScopeWrite,
    FundingProgramVersionCreate,
)
from app.services import funding_programs


def _scope(vertical: str = "dealer", scope_key: str = "default") -> dict[str, str]:
    return {"vertical": vertical, "scope_key": scope_key}


@pytest.mark.parametrize(
    ("schema", "extra"),
    [
        (
            FundingProgramCreate,
            {
                "program_key": "duplicate_scope_test",
                "public_slug": "duplicate-scope-test",
                "name": "Duplicate scope test",
            },
        ),
        (FundingProgramScopePatch, {}),
    ],
)
def test_duplicate_workspace_routing_rows_are_request_validation_errors(
    schema: type[FundingProgramCreate] | type[FundingProgramScopePatch],
    extra: dict[str, str],
) -> None:
    with pytest.raises(ValidationError) as error:
        schema.model_validate(
            {
                **extra,
                "scopes": [_scope(), _scope()],
                "reason": "Reviewed duplicate routing rows",
                "confirmed": True,
            }
        )

    issue = error.value.errors()[0]
    assert issue["loc"] == ("scopes",)
    assert "Duplicate workspace routing row: dealer / default" in issue["msg"]


def test_routing_reference_is_trimmed_before_duplicate_detection() -> None:
    with pytest.raises(ValidationError) as error:
        FundingProgramScopePatch.model_validate(
            {
                "scopes": [_scope(scope_key=" default "), _scope()],
                "reason": "Reviewed duplicate routing rows",
                "confirmed": True,
            }
        )

    assert error.value.errors()[0]["loc"] == ("scopes",)


def test_scope_normalization_preserves_naics_round_trip_values() -> None:
    scope = FundingProgramScopeWrite.model_validate(
        {
            "vertical": "main_street",
            "scope_key": " default ",
            "naics_prefixes": ["31-33", " 445 "],
            "excluded_naics_prefixes": ["5221", "524"],
        }
    )

    assert scope.scope_key == "default"
    assert scope.naics_prefixes == ["31", "32", "33", "445"]
    assert scope.excluded_naics_prefixes == ["5221", "524"]


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (
            FundingProgramScopePatch,
            {"name": "Updated name", "confirmed": True},
        ),
        (
            FundingProgramVersionCreate,
            {"confirmed": True},
        ),
        (
            FundingProgramPublishRequest,
            {"confirmed": True},
        ),
        (
            FundingProgramRetireRequest,
            {"confirmed": True},
        ),
    ],
)
def test_review_note_is_optional_and_blank_notes_normalize_to_none(
    schema: type,
    payload: dict[str, object],
) -> None:
    assert schema.model_validate(payload).reason is None
    assert schema.model_validate({**payload, "reason": "   "}).reason is None

    with pytest.raises(ValidationError):
        schema.model_validate({**payload, "reason": "x" * 2001})


@pytest.mark.asyncio
async def test_catalog_update_records_a_deterministic_reason_when_note_is_omitted() -> None:
    payload = FundingProgramScopePatch.model_validate(
        {"name": "Updated program", "confirmed": True}
    )
    program = SimpleNamespace(
        id=uuid4(),
        program_key="test_program",
        name="Test program",
        short_description=None,
        display_order=10,
    )
    db = SimpleNamespace(add=Mock())

    await funding_programs.update_program(
        db,
        program,
        payload,
        SimpleNamespace(id=uuid4()),
    )

    activity = db.add.call_args.args[0]
    assert activity.payload["reason"] == "Program details and availability updated"


def test_explicit_review_note_is_normalized_and_preserved_for_audit() -> None:
    payload = FundingProgramPublishRequest.model_validate(
        {"reason": "  Reviewed the eligibility thresholds  ", "confirmed": True}
    )

    assert payload.reason == "Reviewed the eligibility thresholds"
    assert (
        funding_programs._audit_reason(payload.reason, "Criteria version published")
        == "Reviewed the eligibility thresholds"
    )


@pytest.mark.asyncio
async def test_create_version_maps_invalid_fit_rule_to_field_addressable_422() -> None:
    payload = FundingProgramVersionCreate.model_validate(
        {
            "rules": {"fit": {"field": "made_up_field", "op": "gte", "value": 1}},
            "reason": "Reviewed eligibility rules",
            "confirmed": True,
        }
    )
    db = SimpleNamespace(execute=AsyncMock())

    with pytest.raises(HTTPException) as error:
        await funding_programs.create_version(
            db,
            SimpleNamespace(id=uuid4(), name="Test program", program_key="test_program"),
            payload,
            SimpleNamespace(id=uuid4()),
        )

    assert error.value.status_code == 422
    assert error.value.detail[0]["loc"] == ["body", "rules", "fit"]
    assert error.value.detail[0]["msg"] == "Unsupported program fit field: made_up_field"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_missing_fit_rule_returns_editor_field_guidance() -> None:
    playbook = SimpleNamespace(id=uuid4(), status="draft", rules={})
    result = SimpleNamespace(scalar_one_or_none=lambda: playbook)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    with pytest.raises(HTTPException) as error:
        await funding_programs.publish_version(
            db,
            SimpleNamespace(id=uuid4(), name="Test program", program_key="test_program"),
            playbook.id,
            "Reviewed eligibility rules",
            SimpleNamespace(id=uuid4()),
        )

    assert error.value.status_code == 422
    assert error.value.detail == [
        {
            "type": "value_error",
            "loc": ["body", "rules", "fit"],
            "msg": "Publish requires at least one validated eligibility rule",
            "input": None,
        }
    ]


@pytest.mark.asyncio
async def test_publish_invalid_fit_rule_returns_editor_field_guidance() -> None:
    playbook = SimpleNamespace(
        id=uuid4(),
        status="draft",
        rules={"fit": {"field": "annual_revenue", "op": "gte", "value": "many"}},
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: playbook)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    with pytest.raises(HTTPException) as error:
        await funding_programs.publish_version(
            db,
            SimpleNamespace(id=uuid4(), name="Test program", program_key="test_program"),
            playbook.id,
            "Reviewed eligibility rules",
            SimpleNamespace(id=uuid4()),
        )

    assert error.value.status_code == 422
    assert error.value.detail[0]["loc"] == ["body", "rules", "fit"]
    assert error.value.detail[0]["msg"] == "The gte operator requires a numeric value"
