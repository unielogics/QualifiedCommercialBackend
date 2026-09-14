from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.enums import Role
from app.routers.application_profiles import (
    finalize_application_draft,
    review_extracted_fact,
)
from app.schemas.application_profile import ExtractedFactReview
from app.services.application_profiles import (
    capture_extracted_profile_facts,
    draft_analysis_status,
)
from app.services.extracted_facts import (
    canonical_field_aliases,
    canonical_field_key,
    facts_resolved_by_review,
    pending_review_group_keys,
)


def fact(
    field_key: str,
    value: str,
    *,
    status: str = "suggested",
    fact_id=None,
):
    return SimpleNamespace(
        id=fact_id or uuid4(),
        field_key=field_key,
        canonical_field_key=None,
        value={"value": value},
        normalized_value=" ".join(value.casefold().split()),
        confidence=0.9,
        source_file_id=uuid4(),
        status=status,
        extraction_method="document_ai",
        created_at=datetime.now(UTC),
        reviewed_by_user_id=None,
        reviewed_at=None,
    )


def test_canonical_field_key_uses_curated_aliases_without_fuzzy_merging() -> None:
    assert canonical_field_key(" Business Name ") == "legal_entity_name"
    assert canonical_field_key("business-activity") == "business_activity"
    assert canonical_field_key("requested_loan_amount") == "requested_amount"
    assert canonical_field_key("gross_revenue") == "gross_revenue"
    assert canonical_field_aliases("legal_entity_name") >= {
        "legal_entity_name",
        "business_name",
        "entity_name",
    }


def test_accepted_alias_hides_every_pending_suggestion_for_logical_field() -> None:
    rows = [
        fact("business_name", "Grace Auto Sales", status="accepted"),
        fact("legal_entity_name", "Grace Auto Sales and Service, Inc."),
        fact("entity_type", "Corporation"),
    ]

    assert pending_review_group_keys(rows) == {("entity_type", None)}


def test_accept_resolves_all_values_but_reject_resolves_only_repeated_value() -> None:
    selected = fact("business_name", "Grace Auto Sales")
    same = fact("legal_entity_name", "  grace auto sales  ")
    alternative = fact("legal_entity_name", "Grace Auto Sales and Service, Inc.")
    unrelated = fact("entity_type", "Corporation")
    rows = [selected, same, alternative, unrelated]

    assert facts_resolved_by_review(rows, selected, "accept") == [
        selected,
        same,
        alternative,
    ]
    assert facts_resolved_by_review(rows, selected, "reject") == [selected, same]


def test_multi_value_fields_group_repeats_without_hiding_distinct_values() -> None:
    selected = fact("tax_year", "2025")
    repeated = fact("tax_year", "2025")
    prior_year = fact("tax_year", "2024")

    assert facts_resolved_by_review(
        [selected, repeated, prior_year], selected, "accept"
    ) == [selected, repeated]
    selected.status = "accepted"
    repeated.status = "superseded"
    assert pending_review_group_keys([selected, repeated, prior_year]) == {
        ("tax_year", "2024")
    }


def test_contact_scopes_remain_distinct_fields() -> None:
    assert canonical_field_key("business_email") == "business_email"
    assert canonical_field_key("contact_email") == "contact_email"
    assert canonical_field_key("email") == "email"


@pytest.mark.asyncio
async def test_review_accepts_one_canonical_field_and_supersedes_its_siblings() -> None:
    profile = SimpleNamespace(id=uuid4(), entity_type=None, extraction_reviewed_at=None)
    selected = fact("business_entity_type", "Corporation")
    duplicate = fact("entity_type", "Corporation")
    alternative = fact("entity_structure", "LLC")
    remaining = fact("naics_code", "441110")
    rows = [selected, duplicate, alternative, remaining]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=profile),
        ),
        patch(
            "app.routers.application_profiles.profiles.log_profile_action",
            AsyncMock(),
        ) as log_action,
    ):
        response = await review_extracted_fact(
            profile.id,
            selected.id,
            ExtractedFactReview(action="accept"),
            user,
            db,
        )

    assert response.status == "accepted"
    assert response.canonical_field_key == "entity_type"
    assert selected.status == "accepted"
    assert duplicate.status == "superseded"
    assert alternative.status == "superseded"
    assert remaining.status == "suggested"
    assert profile.entity_type == "Corporation"
    assert profile.extraction_reviewed_at is None
    assert all(row.reviewed_by_user_id == user.id for row in rows[:3])
    db.commit.assert_awaited_once()
    db.refresh.assert_any_await(profile, with_for_update=True)
    log_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_does_not_reopen_an_accepted_canonical_field() -> None:
    bucket_id = uuid4()
    profile = SimpleNamespace(
        id=uuid4(),
        primary_bucket_id=bucket_id,
        extraction_reviewed_at=datetime.now(UTC),
    )

    def scalar_rows(values):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                scalar_rows([]),
                scalar_rows([profile]),
                scalar_rows([
                    fact("business_name", "Grace Auto Sales", status="accepted")
                ]),
            ]
        ),
        add=pytest.fail,
        flush=AsyncMock(),
    )
    file = SimpleNamespace(id=uuid4(), bucket_id=bucket_id, statement_period=None)
    analysis = SimpleNamespace(
        id=uuid4(),
        classification="tax_return",
        analysis={
            "profile_facts": {
                "legal_entity_name": {"value": "Grace Auto Sales and Service, Inc."}
            },
            "key_facts": {},
        },
    )

    assert await capture_extracted_profile_facts(db, file=file, analysis=analysis) == [
        profile.id
    ]
    assert profile.extraction_reviewed_at is not None
    assert db.execute.await_count == 3
    assert db.execute.await_args_list[1].args[0]._for_update_arg is not None


@pytest.mark.asyncio
async def test_finalize_locks_profile_before_rechecking_extraction_status() -> None:
    profile = SimpleNamespace(
        id=uuid4(),
        is_draft=True,
        draft_finalized_at=None,
        extraction_reviewed_at=None,
    )
    draft = SimpleNamespace(
        processing_file_count=0,
        failed_file_count=0,
        suggested_fact_count=0,
    )
    db = SimpleNamespace(
        refresh=AsyncMock(),
        commit=AsyncMock(),
    )
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    expected = SimpleNamespace(id=profile.id)

    with (
        patch(
            "app.routers.application_profiles.profiles.load_profile",
            AsyncMock(return_value=profile),
        ),
        patch(
            "app.routers.application_profiles.profiles.draft_analysis_status",
            AsyncMock(return_value=draft),
        ) as load_status,
        patch(
            "app.routers.application_profiles.profiles.log_profile_action",
            AsyncMock(),
        ),
        patch(
            "app.routers.application_profiles.profiles.profile_read",
            return_value=expected,
        ),
    ):
        response = await finalize_application_draft(profile.id, user, db)

    assert response is expected
    db.refresh.assert_any_await(profile, with_for_update=True)
    load_status.assert_awaited_once_with(db, profile)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_draft_status_counts_logical_fields_not_per_file_repeats() -> None:
    file_id = uuid4()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())

    def scalar_rows(values):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    def rows(values):
        return SimpleNamespace(all=lambda: values)

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                scalar_rows([file_id]),
                rows([(file_id, "completed")]),
                rows(
                    [
                        ("business_name", "accepted", "grace auto sales", {"value": "Grace Auto Sales"}),
                        ("legal_entity_name", "suggested", "grace auto sales and service inc", {"value": "Grace Auto Sales and Service, Inc."}),
                        ("entity_type", "suggested", "corporation", {"value": "Corporation"}),
                        ("business_entity_type", "suggested", "corporation", {"value": "Corporation"}),
                    ]
                ),
            ]
        )
    )

    result = await draft_analysis_status(db, profile)

    assert result.suggested_fact_count == 1
    assert result.reviewed_fact_count == 1
    assert result.can_finalize is False
