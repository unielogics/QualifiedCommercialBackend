"""The published-criteria program screen: the funding catalog's approved rules
evaluated against a file, handed to the operator thread and the review pass.

The screen is advisory knowledge for the underwriting AI (fits now / fits once
a named gap closes). It is never an approval, never quotes a rate, and the
soft credit pull is always the last step. A failure anywhere in the screen is
logged and yields an empty screen, so it can never cost a chat turn or a
review.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.services import application_programs, bucket_ai, funding_programs


class _Result:
    def __init__(self, rows=None, *, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


def _candidate(
    key: str,
    name: str,
    *,
    status: str = "recommended",
    eligible: bool = True,
    criteria_status: str = "published",
    reasons=(),
    evidence_gaps=(),
) -> SimpleNamespace:
    return SimpleNamespace(
        program_key=key,
        program_name=name,
        catalog_id=uuid4(),
        public_slug=key.replace("_", "-"),
        playbook_id=uuid4(),
        playbook_version=2,
        eligible=eligible,
        recommendation_status=status,
        criteria_status=criteria_status,
        reasons=list(reasons),
        evidence_gaps=list(evidence_gaps),
        fit_score=72.5,
        preference_score=3,
        confidence=0.7,
        rule_results=[{"field": "credit_score"}],
    )


def _selection(key: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), playbook_id=uuid4(), playbook_version=1, program_key=key, program_name=name
    )


def _definition(key: str, visibility: list[str], *, order: int = 0):
    return SimpleNamespace(
        id=uuid4(),
        requirement_key=key,
        visibility=visibility,
        display_order=order,
        ai_request_message_template=None,
        objective_text="",
        completion_criteria="",
        review_checks=[],
    )


def _state(key: str, label: str, *, status: str = "missing"):
    return SimpleNamespace(
        requirement_key=key, label=label, status=status, required_level="required", source_program_keys=[]
    )


SCREEN = [
    {
        "program_key": "ez_term",
        "program_name": "EZ Term Loan",
        "recommendation_status": "recommended",
        "eligible": True,
        "criteria_status": "published",
        "reasons": ["Time in business meets the minimum"],
        "evidence_gaps": [],
        "description": "A fixed-payment term loan.",
    },
    {
        "program_key": "sba_7a",
        "program_name": "SBA 7(a)",
        "recommendation_status": "needs_information",
        "eligible": False,
        "criteria_status": "published",
        "reasons": ["Credit score is not yet known"],
        "evidence_gaps": ["credit_score"],
        "description": None,
    },
]
SELECTIONS = [_selection("ez_term", "EZ Term Loan"), _selection("sba_7a", "SBA 7(a)")]
STATES = [_state("business_debt_schedule", "Business debt schedule", status="verified")]
DEFINITIONS = [_definition("business_debt_schedule", ["client"], order=1)]
LONG_TEXT = "x" * 1500


# --- the helper -----------------------------------------------------------------


async def test_helper_maps_candidate_fields_and_clips_the_published_description() -> None:
    eligible = _candidate(
        "ez_term",
        "EZ Term Loan",
        reasons=[f"reason {index}" for index in range(6)],
        evidence_gaps=[f"gap {index}" for index in range(6)],
    )
    pending = _candidate(
        "sba_7a",
        "SBA 7(a)",
        status="needs_information",
        eligible=False,
        reasons=["Credit score is not yet known"],
        evidence_gaps=["credit_score"],
    )
    unpublished = _candidate(
        "bridge", "Bridge", status="criteria_unavailable", eligible=False, criteria_status="not_published"
    )
    versions = {
        eligible.catalog_id: SimpleNamespace(description=LONG_TEXT),
        pending.catalog_id: SimpleNamespace(description="   "),
    }
    db = SimpleNamespace()
    profile = SimpleNamespace(id=uuid4())
    with patch.object(
        application_programs, "published_candidates", AsyncMock(return_value=[eligible, pending, unpublished])
    ) as candidates, patch.object(
        funding_programs, "published_versions_by_program", AsyncMock(return_value=versions)
    ) as loader:
        screen = await bucket_ai._published_program_screen(db, profile)

    candidates.assert_awaited_once_with(db, profile)
    loader.assert_awaited_once_with(db, [eligible.catalog_id, pending.catalog_id, unpublished.catalog_id])
    assert [entry["program_key"] for entry in screen] == ["ez_term", "sba_7a", "bridge"]
    for entry in screen:
        # Only the screen vocabulary travels: no scores, ranks, or rule internals.
        assert set(entry) == {
            "program_key",
            "program_name",
            "recommendation_status",
            "eligible",
            "criteria_status",
            "reasons",
            "evidence_gaps",
            "description",
        }
    first, second, third = screen
    assert first["program_name"] == "EZ Term Loan"
    assert first["recommendation_status"] == "recommended"
    assert first["eligible"] is True
    assert first["criteria_status"] == "published"
    assert first["reasons"] == ["reason 0", "reason 1", "reason 2", "reason 3"]
    assert first["evidence_gaps"] == ["gap 0", "gap 1", "gap 2", "gap 3"]
    assert first["description"] == "x" * 1200
    assert second == {
        "program_key": "sba_7a",
        "program_name": "SBA 7(a)",
        "recommendation_status": "needs_information",
        "eligible": False,
        "criteria_status": "published",
        "reasons": ["Credit score is not yet known"],
        "evidence_gaps": ["credit_score"],
        "description": None,
    }
    assert third["criteria_status"] == "not_published"
    assert third["description"] is None


async def test_helper_keeps_the_first_eight_best_first_candidates() -> None:
    candidates = [_candidate(f"program_{index}", f"Program {index}") for index in range(10)]
    with patch.object(
        application_programs, "published_candidates", AsyncMock(return_value=candidates)
    ), patch.object(funding_programs, "published_versions_by_program", AsyncMock(return_value={})) as loader:
        screen = await bucket_ai._published_program_screen(SimpleNamespace(), SimpleNamespace(id=uuid4()))

    assert [entry["program_key"] for entry in screen] == [f"program_{index}" for index in range(8)]
    assert loader.await_args.args[1] == [candidate.catalog_id for candidate in candidates[:8]]


async def test_helper_returns_an_empty_screen_and_logs_when_the_candidates_fail(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.bucket_ai")
    profile = SimpleNamespace(id=uuid4())
    with patch.object(
        application_programs, "published_candidates", AsyncMock(side_effect=RuntimeError("catalog down"))
    ), patch.object(funding_programs, "published_versions_by_program", AsyncMock()) as loader:
        screen = await bucket_ai._published_program_screen(SimpleNamespace(), profile)

    assert screen == []
    loader.assert_not_awaited()
    (record,) = [record for record in caplog.records if "published program screen failed" in record.getMessage()]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert str(profile.id) in record.getMessage()


async def test_helper_returns_an_empty_screen_when_the_version_lookup_fails(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.bucket_ai")
    with patch.object(
        application_programs, "published_candidates", AsyncMock(return_value=[_candidate("ez_term", "EZ Term Loan")])
    ), patch.object(
        funding_programs, "published_versions_by_program", AsyncMock(side_effect=RuntimeError("no versions"))
    ):
        screen = await bucket_ai._published_program_screen(SimpleNamespace(), SimpleNamespace(id=uuid4()))

    assert screen == []
    assert any("published program screen failed" in record.getMessage() for record in caplog.records)


# --- the operator (admin) thread --------------------------------------------------


async def _admin_context(*, selections, screen, results=None, selection_mode="manual", audience="admin"):
    profile = SimpleNamespace(id=uuid4(), program_selection_mode=selection_mode)
    db = SimpleNamespace(execute=AsyncMock(side_effect=list(results or [])))
    with patch.object(
        application_programs, "profile_for_chat_scope", AsyncMock(return_value=profile)
    ), patch.object(
        application_programs, "active_selections", AsyncMock(return_value=selections)
    ), patch.object(bucket_ai, "_published_program_screen", AsyncMock(return_value=screen)) as helper:
        context = await bucket_ai._program_context_for_chat(
            db,
            bucket_id=uuid4(),
            intake_id=uuid4(),
            upload_link_id=None,
            audience=audience,
        )
    return context, db, helper, profile


async def test_admin_without_a_selection_carries_the_screen_and_the_screen_wording() -> None:
    context, db, helper, profile = await _admin_context(selections=[], screen=SCREEN)

    helper.assert_awaited_once_with(db, profile)
    assert context["selection_mode"] == "manual"
    assert context["selected_programs"] == []
    assert context["shared_requirements"] == []
    assert context["published_program_screen"] == SCREEN
    assert context["instructions"] == bucket_ai.ADMIN_PROGRAM_SCREEN_INSTRUCTION
    db.execute.assert_not_awaited()


async def test_admin_without_a_selection_keeps_the_old_wording_when_the_screen_is_empty() -> None:
    context, _, _, _ = await _admin_context(selections=[], screen=[])

    assert context["published_program_screen"] == []
    assert context["instructions"] == bucket_ai.ADMIN_NO_SELECTION_INSTRUCTION
    assert context["instructions"].startswith("No approved published program is selected.")
    assert "Do not use legacy fit data" in context["instructions"]


async def test_admin_with_a_selection_carries_the_screen_and_each_programs_published_text() -> None:
    playbooks = [
        SimpleNamespace(id=SELECTIONS[0].playbook_id, description=LONG_TEXT),
        SimpleNamespace(id=uuid4(), description="A stranger's playbook"),
    ]
    context, db, helper, profile = await _admin_context(
        selections=SELECTIONS,
        screen=SCREEN,
        results=[_Result(STATES), _Result(DEFINITIONS), _Result([]), _Result(playbooks)],
    )

    helper.assert_awaited_once_with(db, profile)
    assert context["published_program_screen"] == SCREEN
    assert context["instructions"] == bucket_ai.ADMIN_PROGRAM_SCREEN_INSTRUCTION
    assert context["selected_programs"] == [
        {
            "program_key": "ez_term",
            "program_name": "EZ Term Loan",
            "playbook_version": 1,
            "description": "x" * 1200,
        },
        {"program_key": "sba_7a", "program_name": "SBA 7(a)", "playbook_version": 1, "description": None},
    ]
    assert [item["requirement_key"] for item in context["shared_requirements"]] == ["business_debt_schedule"]
    # The published text is read once, keyed on the selected playbook versions.
    assert db.execute.await_count == 4
    playbook_sql = str(db.execute.await_args_list[3].args[0])
    assert "ai_playbook_templates" in playbook_sql
    assert "ai_playbook_templates.id IN" in playbook_sql


async def test_admin_with_a_selection_and_an_empty_screen_adds_no_instruction() -> None:
    context, _, _, _ = await _admin_context(
        selections=SELECTIONS,
        screen=[],
        results=[_Result(STATES), _Result(DEFINITIONS), _Result([]), _Result([])],
    )

    assert context["published_program_screen"] == []
    assert "instructions" not in context
    assert [item["description"] for item in context["selected_programs"]] == [None, None]


@pytest.mark.parametrize("selection_mode", ["auto", "manual"])
async def test_the_borrower_thread_never_receives_the_screen(selection_mode) -> None:
    from app.tests.test_client_program_context import CATALOG

    results = [_Result(STATES), _Result(DEFINITIONS), _Result([])]
    if selection_mode == "manual":
        results.append(_Result(CATALOG))
    context, _, helper, _ = await _admin_context(
        selections=SELECTIONS, screen=SCREEN, results=results, selection_mode=selection_mode, audience="uploader"
    )

    helper.assert_not_awaited()
    assert "published_program_screen" not in context
    for program in context["selected_programs"]:
        assert "description" not in program
        assert "program_key" not in program


async def test_a_screen_failure_does_not_fail_the_operator_thread(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.bucket_ai")
    profile = SimpleNamespace(id=uuid4(), program_selection_mode="auto")
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("no query expected")))
    with patch.object(
        application_programs, "profile_for_chat_scope", AsyncMock(return_value=profile)
    ), patch.object(application_programs, "active_selections", AsyncMock(return_value=[])), patch.object(
        application_programs, "published_candidates", AsyncMock(side_effect=RuntimeError("catalog down"))
    ):
        context = await bucket_ai._program_context_for_chat(
            db, bucket_id=uuid4(), intake_id=None, upload_link_id=None, audience="admin"
        )

    assert context["published_program_screen"] == []
    assert context["instructions"] == bucket_ai.ADMIN_NO_SELECTION_INSTRUCTION
    assert any("published program screen failed" in record.getMessage() for record in caplog.records)


def test_admin_instruction_constants_match_the_contract() -> None:
    instruction = bucket_ai.ADMIN_PROGRAM_SCREEN_INSTRUCTION
    assert instruction.startswith("published_program_screen is the approved-criteria screen for this file.")
    assert "which programs the file fits now and which it would fit once the named evidence gaps close" in instruction
    assert "It is a screen, never an approval; never quote a rate" in instruction
    assert "a missing credit score is a pending step, not a disqualifier" in instruction
    assert instruction.endswith("Do not use legacy fit data to recommend a program.")
    assert bucket_ai.ADMIN_NO_SELECTION_INSTRUCTION == (
        "No approved published program is selected. Do not use legacy fit data "
        "to recommend a program; report that published criteria are unavailable "
        "or that more information is required."
    )


# --- the review pass ---------------------------------------------------------------


def _review_fixture(*, with_intake: bool):
    bucket = SimpleNamespace(
        id=uuid4(),
        name="Operating file",
        client_name="Fixture Client",
        purpose="Working capital",
        bucket_type="underwriting",
        description="Main Street file",
        ai_context={"review_type": "main_street_v1", "loan_program_fit": ["legacy"]},
        files=[],
        requested_documents=[],
    )
    intake = (
        SimpleNamespace(
            id=uuid4(),
            variant="main_street_v1",
            loan_purpose=None,
            intake_state={},
            status="collecting",
            requested_loan_amount=None,
            bucket=bucket,
            bucket_id=bucket.id,
        )
        if with_intake
        else None
    )
    review = SimpleNamespace(
        id=uuid4(),
        bucket=bucket,
        bucket_id=bucket.id,
        status="queued",
        context_snapshot={"review_type": "main_street_v1"},
        result=None,
    )
    db = Mock(
        execute=AsyncMock(
            side_effect=[
                Mock(scalar_one_or_none=Mock(return_value=review)),
                Mock(scalar_one_or_none=Mock(return_value=intake)),
            ]
        ),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )
    return db, review, bucket, intake


async def _run_review(*, with_intake: bool, profile):
    db, review, bucket, intake = _review_fixture(with_intake=with_intake)
    with patch.object(bucket_ai, "log_bucket_ai_activity", AsyncMock()), patch(
        "app.services.operator_file_links.selected_files_for_intake", AsyncMock(return_value=[])
    ), patch(
        "app.services.document_qualification_review.review_bucket_checks", AsyncMock()
    ), patch.object(
        application_programs, "profile_for_chat_scope", AsyncMock(return_value=profile)
    ) as resolver, patch.object(
        bucket_ai, "_published_program_screen", AsyncMock(return_value=SCREEN)
    ) as helper, patch.object(bucket_ai, "get_client", Mock(return_value=object())), patch.object(
        bucket_ai, "model_light", Mock(return_value="model-light")
    ), patch.object(
        bucket_ai, "tracked_messages_create", AsyncMock(side_effect=RuntimeError("stop before the model"))
    ) as model, patch.object(bucket_ai, "_persist_review_failure", AsyncMock(return_value=review)) as persist:
        result = await bucket_ai.run_bucket_ai_review(db, review.id)

    assert result is review
    persist.assert_awaited_once()
    model.assert_awaited_once()
    payload = json.loads(model.await_args.kwargs["messages"][0]["content"][0]["text"])
    return payload, model.await_args.kwargs, resolver, helper, db, bucket, intake


@pytest.mark.asyncio
async def test_the_review_context_carries_the_screen_when_a_profile_resolves() -> None:
    profile = SimpleNamespace(id=uuid4())
    payload, call, resolver, helper, db, bucket, intake = await _run_review(with_intake=True, profile=profile)

    resolver.assert_awaited_once_with(db, bucket_id=bucket.id, intake_id=intake.id, upload_link_id=None)
    helper.assert_awaited_once_with(db, profile)
    assert payload["published_program_screen"] == SCREEN
    # The screen sits next to ai_context, and nothing else in the context moved.
    keys = list(payload)
    assert keys.index("published_program_screen") == keys.index("ai_context") + 1
    assert keys == [
        "bucket",
        "ai_context",
        "published_program_screen",
        "requested_documents",
        "uploaded_files",
        "instruction",
    ]
    assert payload["ai_context"] == {"review_type": "main_street_v1"}
    assert payload["bucket"]["name"] == "Operating file"
    assert bucket_ai.REVIEW_PROGRAM_SCREEN_RULES in call["system"]


@pytest.mark.asyncio
async def test_the_review_context_omits_the_screen_when_no_profile_resolves() -> None:
    payload, _, resolver, helper, db, bucket, _ = await _run_review(with_intake=False, profile=None)

    resolver.assert_awaited_once_with(db, bucket_id=bucket.id, intake_id=None, upload_link_id=None)
    helper.assert_not_awaited()
    assert "published_program_screen" not in payload
    assert list(payload) == ["bucket", "ai_context", "requested_documents", "uploaded_files", "instruction"]


@pytest.mark.asyncio
async def test_a_failed_profile_lookup_does_not_fail_the_review(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.bucket_ai")
    db, review, bucket, _ = _review_fixture(with_intake=False)
    with patch.object(bucket_ai, "log_bucket_ai_activity", AsyncMock()), patch(
        "app.services.document_qualification_review.review_bucket_checks", AsyncMock()
    ), patch.object(
        application_programs, "profile_for_chat_scope", AsyncMock(side_effect=RuntimeError("lineage broken"))
    ), patch.object(bucket_ai, "_published_program_screen", AsyncMock()) as helper, patch.object(
        bucket_ai, "get_client", Mock(return_value=object())
    ), patch.object(bucket_ai, "model_light", Mock(return_value="model-light")), patch.object(
        bucket_ai, "tracked_messages_create", AsyncMock(side_effect=RuntimeError("stop before the model"))
    ) as model, patch.object(bucket_ai, "_persist_review_failure", AsyncMock(return_value=review)):
        await bucket_ai.run_bucket_ai_review(db, review.id)

    helper.assert_not_awaited()
    payload = json.loads(model.await_args.kwargs["messages"][0]["content"][0]["text"])
    assert "published_program_screen" not in payload
    assert any("program screen profile lookup failed" in record.getMessage() for record in caplog.records)


# --- prompt text ---------------------------------------------------------------------

PERSONA_RULES = {
    "dealer_gatekeeper_v1": bucket_ai.DEALER_REVIEW_RULES,
    "real_estate_dscr_v1": bucket_ai.RE_REVIEW_RULES,
    "main_street_v1": bucket_ai.MAIN_STREET_REVIEW_RULES,
    "mca_refi_v1": bucket_ai.MCA_REVIEW_RULES,
}


@pytest.mark.parametrize(
    "review_type",
    ["dealer_gatekeeper_v1", "real_estate_dscr_v1", "main_street_v1", "mca_refi_v1", None, "", "some_future_vertical_v9"],
)
def test_every_review_persona_carries_the_screen_rules_after_its_rules_and_before_the_limits(review_type) -> None:
    system = bucket_ai.build_review_system(review_type)
    rules = bucket_ai.REVIEW_PROGRAM_SCREEN_RULES

    assert system.count(rules) == 1
    assert system.index(bucket_ai.REVIEW_PREAMBLE) < system.index(rules) < system.index(bucket_ai.REVIEW_LIMITS)
    persona = PERSONA_RULES.get(review_type)
    if persona is not None:
        assert system.index(persona) < system.index(rules)


def test_review_screen_rules_match_the_contract_and_leave_the_result_shape_alone() -> None:
    rules = bucket_ai.REVIEW_PROGRAM_SCREEN_RULES
    assert rules.startswith("context.published_program_screen lists approved programs")
    assert "say in executive_summary that the file fits the published criteria for that program by name" in rules
    assert "keep one_next_step on the lender's remaining items" in rules
    assert "name in recommended_next_document_requests the single missing item that would unlock it" in rules
    assert rules.endswith(
        "Never call a fit an approval, never quote a rate, and never treat a missing credit score as a "
        "failure: the soft credit pull is the last step."
    )
    # The rules steer existing fields only; the JSON shape in the preamble gains nothing.
    assert "published_program_screen" not in bucket_ai.REVIEW_PREAMBLE
    assert "published_program_screen" not in bucket_ai._ensure_review_result_shape({})
