from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.funding_program import FundingProgramRequirementWrite
from app.services.application_programs import (
    _automatic_evidence_decision,
    _evidence_decision_context_key,
    _merged_review_checks,
    _requires_human_verification,
)
from app.services.document_qualification_review import (
    assessment_key,
    check_id,
    current_assessment,
    validate_result,
)

CHECK = {"key": "net_income_not_declining", "label": "No earnings decline",
         "instructions": "Compare both complete filed years.", "severity": "block"}
PAGES = {("2024", 2): "2024 Ordinary business net income: 100,000",
         ("2025", 3): "2025 Ordinary business net income: 120,000"}


def result(status="pass", **changes):
    row = {"id": check_id(CHECK), "status": status, "confidence": "high",
           "reason": "Income increased from 100,000 to 120,000.", "citations": [
               {"file_id": "2024", "page": 2, "quote": PAGES[("2024", 2)]},
               {"file_id": "2025", "page": 3, "quote": PAGES[("2025", 3)]},
           ], "observations": [
               {"period": "2024", "measure": "ordinary_business_income", "value": 100000, "file_id": "2024", "page": 2},
               {"period": "2025", "measure": "ordinary_business_income", "value": 120000, "file_id": "2025", "page": 3},
           ]}
    return {"checks": [{**row, **changes}]}


def requirement(**changes):
    return SimpleNamespace(**{"requirement_key": "business_tax_returns_2_years",
        "label": "Tax returns", "category": "financials", "verification_required": False,
        "completion_mode": "ai_can_complete", "review_checks": [CHECK], **changes})


def decision(assessment=None, **changes):
    return _automatic_evidence_decision(
        requirement=requirement(**changes),
        file=SimpleNamespace(file_name="Tax return 2025.pdf", content_hash="hash", statement_period=None),
        analysis=SimpleNamespace(status="completed", content_hash="hash", confidence="high",
            classification="tax_return", analysis={"key_facts": {"tax_year": "2025"}}),
        expected_entity=None, duplicate_content=False, check_assessment=assessment)


def test_verified_quotes_from_both_periods_can_pass_document_check():
    assessment = validate_result(result(), [CHECK], PAGES)
    assert assessment["status"] == "pass"
    assert len(assessment["checks"][0]["citations"]) == 2
    assert decision(assessment)[:2] == ("accepted", "document_checks_passed")
    assert "not financing approval" in decision(assessment)[2]


@pytest.mark.parametrize("change", [
    {"citations": [{"file_id": "unrelated", "page": 2, "quote": "Net income is positive"}]},
    {"citations": [{"file_id": "2024", "page": 3, "quote": PAGES[("2024", 2)]}]},
    {"citations": [{"file_id": "2024", "page": 2, "quote": "Invented source quotation"}]},
    {"citations": [{"file_id": "2024", "page": True, "quote": PAGES[("2024", 2)]}]},
    {"citations": []}, {"confidence": "medium"}, {"reason": ""}, {"status": "unknown"},
    {"id": "not-a-policy-check"},
    {"citations": [{"file_id": "2024", "page": 2, "quote": " " * 20},
                   {"file_id": "2025", "page": 3, "quote": " " * 20}]},
    {"observations": []},
])
def test_unreliable_or_unscoped_results_cannot_pass(change):
    assessment = validate_result(result(**change), [CHECK], PAGES)
    assert assessment["status"] == "unknown"
    assert decision(assessment)[0] == "needs_more"


def test_one_good_year_cannot_establish_a_two_year_trend():
    assessment = validate_result(result(citations=[result()["checks"][0]["citations"][1]]), [CHECK], PAGES)
    assert assessment["status"] == "unknown"


def test_another_checks_citation_does_not_establish_trend_coverage():
    second = {**CHECK, "key": "custom_identity", "label": "Entity identity"}
    rows = [result(citations=[result()["checks"][0]["citations"][1]])["checks"][0],
            result(id=check_id(second))["checks"][0]]
    assert validate_result({"checks": rows}, [CHECK, second], PAGES)["status"] == "unknown"


def test_merged_pdf_does_not_make_one_period_sufficient_for_trend():
    pages = {("merged", page): text for (_, page), text in PAGES.items()}
    row = result()["checks"][0]
    row["citations"] = [{"file_id": "merged", "page": 3, "quote": PAGES[("2025", 3)]}]
    row["observations"] = [{**o, "file_id": "merged"} for o in row["observations"]]
    assert validate_result({"checks": [row]}, [CHECK], pages)["status"] == "unknown"


@pytest.mark.parametrize("observations", [
    [{"period": "2024", "measure": "net_income", "value": 100000, "file_id": "2024", "page": 2},
     {"period": "2025", "measure": "revenue", "value": 120000, "file_id": "2025", "page": 3}],
    [{"period": "2024", "measure": "net_income", "value": 100000, "file_id": "2024", "page": 2},
     {"period": "2025", "measure": "net_income", "value": 999999, "file_id": "2025", "page": 3}],
])
def test_trend_uses_same_measure_and_printed_not_invented_numbers(observations):
    assert validate_result(result(observations=observations), [CHECK], PAGES)["status"] == "unknown"


@pytest.mark.parametrize("mistake", ["other_metric", "year_as_value", "uncited_number"])
def test_trend_amount_is_bound_to_the_printed_metric_in_its_own_quote(mistake):
    payload = result()
    pages = dict(PAGES)
    if mistake == "other_metric":
        pages[("2025", 3)] = "2025 Gross revenue: 120,000. Ordinary business net income: 80,000"
        payload["checks"][0]["citations"][1]["quote"] = pages[("2025", 3)]
    elif mistake == "year_as_value":
        payload["checks"][0]["observations"][1]["value"] = 2025
    else:
        pages[("2025", 3)] += ". Assets: 150,000"
        payload["checks"][0]["observations"][1]["value"] = 150000
    assert validate_result(payload, [CHECK], pages)["status"] == "unknown"


@pytest.mark.parametrize("amount", ["−80,000", "- 80,000", "80,000-", "(80,000)"])
def test_negative_accounting_notation_cannot_be_misread_as_positive(amount):
    payload = result()
    pages = dict(PAGES)
    pages[("2025", 3)] = f"2025 Ordinary business net income: {amount}"
    payload["checks"][0]["citations"][1]["quote"] = pages[("2025", 3)]
    payload["checks"][0]["observations"][1]["value"] = 80000
    assert validate_result(payload, [CHECK], pages)["status"] == "unknown"


def test_nonnegative_income_check_cannot_pass_a_grounded_negative_value():
    check = {**CHECK, "key": "net_income_nonnegative"}
    payload = result(id=check_id(check))
    pages = dict(PAGES)
    pages[("2025", 3)] = "2025 Ordinary business net income: -80,000"
    payload["checks"][0]["citations"][1]["quote"] = pages[("2025", 3)]
    payload["checks"][0]["observations"][1]["value"] = -80000
    assert validate_result(payload, [check], pages)["status"] == "unknown"


def test_another_year_mentioned_elsewhere_does_not_ground_second_period():
    pages = {("merged", 1): "2024 Net income: 50,000. 2025 tax return not available."}
    payload = result(citations=[{"file_id": "merged", "page": 1, "quote": "2024 Net income: 50,000"}],
        observations=[{"period": year, "measure": "net_income", "value": 50000,
                       "file_id": "merged", "page": 1} for year in ("2024", "2025")])
    assert validate_result(payload, [CHECK], pages)["status"] == "unknown"


def test_every_policy_check_required_and_duplicate_results_rejected():
    second = {**CHECK, "key": "custom_consistency", "label": "Consistency"}
    assert validate_result(result(), [CHECK, second], PAGES)["status"] == "unknown"
    assert validate_result({"checks": result()["checks"] * 2}, [CHECK, second], PAGES)["status"] == "unknown"


def test_adverse_block_and_review_findings_do_not_complete_requirement():
    blocked = validate_result(result(status="fail"), [CHECK], PAGES)
    assert blocked["status"] == "blocked"
    assert decision(blocked)[:2] == ("rejected", "document_check_failed")
    review = {**CHECK, "severity": "review"}
    assessment = validate_result(result(status="fail", id=check_id(review)), [review], PAGES)
    assert assessment["status"] == "review"
    assert decision(assessment)[0] == "needs_more"


@pytest.mark.parametrize("human", [{"completion_mode": "requires_human_verify"}, {"verification_required": True}])
def test_explicit_staff_requirements_cannot_be_accepted_by_ai(human):
    assert _requires_human_verification(requirement(**human)) is True
    assert decision(validate_result(result(), [CHECK], PAGES), **human)[0] == "needs_more"


def test_a_staff_requirement_in_any_selected_policy_preserves_the_human_gate():
    definitions = [requirement(), requirement(verification_required=True)]
    merged_human = any(_requires_human_verification(row) for row in definitions)
    verdict = _automatic_evidence_decision(
        requirement=definitions[0], requires_human=merged_human,
        file=SimpleNamespace(file_name="Tax return 2025.pdf", content_hash="hash", statement_period=None),
        analysis=SimpleNamespace(status="completed", content_hash="hash", confidence="high",
            classification="tax_return", analysis={"key_facts": {"tax_year": "2025"}}),
        expected_entity=None, duplicate_content=False,
        check_assessment=validate_result(result(), [CHECK], PAGES),
    )
    assert verdict[0] == "needs_more" and "staff" in verdict[2]


def test_ai_mode_does_not_accept_readability_without_check_result():
    assert _requires_human_verification(requirement()) is False
    assert decision()[0] == "needs_more"


def test_objective_and_completion_instructions_are_checks_not_ignored_prose():
    checks = _merged_review_checks([requirement(
        objective_text="Confirm entity and review both tax years.",
        completion_criteria="Reconcile the same income measure for both years.")])
    assert len(checks) == 3
    assert {row["label"] for row in checks} >= {"Document review objective", "Document completion requirements"}
    checks = _merged_review_checks([requirement(completion_criteria="x" * 4000)])
    assert all(len(row["instructions"]) <= 2000 for row in checks)


def test_content_addition_removal_replacement_and_policy_changes_invalidate_pass():
    files = [SimpleNamespace(id=uuid4(), content_hash="a"), SimpleNamespace(id=uuid4(), content_hash="b")]
    assessment = {"key": assessment_key([CHECK], files), "status": "pass"}
    assert current_assessment(assessment, [CHECK], files) == assessment
    assert current_assessment(assessment, [CHECK], files[:1]) is None
    assert current_assessment(assessment, [{**CHECK, "instructions": "Compare three years"}], files) is None
    files[0].content_hash = "replacement"
    assert current_assessment(assessment, [CHECK], files) is None
    files[0].content_hash = ""
    assert current_assessment(assessment, [CHECK], files) is None


def test_authority_and_grounded_result_are_in_immutable_decision_identity():
    args = dict(link_id="link", content_hash="hash", analysis_version=3, criteria_version=2,
                analyzed_at="2026-09-01", expected_entity=None, duplicate_content=False,
                corroborated_entities=set(), review_checks=[CHECK])
    assert _evidence_decision_context_key(**args) != _evidence_decision_context_key(**args, requires_human=True)
    assert _evidence_decision_context_key(**args) != _evidence_decision_context_key(**args, check_assessment={"status": "pass"})


def test_client_cannot_self_attest_document_checks():
    with pytest.raises(ValidationError, match="self-attestation"):
        FundingProgramRequirementWrite(requirement_key="tax_returns", label="Tax returns",
            completion_mode="borrower_self_attest", review_checks=[CHECK])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "human", "policy", "content", "provider_failure", "prior_adverse", "concurrent_adverse"])
async def test_worker_releases_locks_rechecks_and_persists_audit_before_return(change):
    from app.services.document_qualification_review import review_profile_checks

    files = [SimpleNamespace(id="2024", content_hash="one"), SimpleNamespace(id="2025", content_hash="two")]
    adverse = {"key": assessment_key([CHECK], files), "status": "blocked", "checks": [], "reason": "Negative earnings"}
    policy = SimpleNamespace(model_dump=lambda: dict(CHECK))
    initial = SimpleNamespace(requirement_key="business_tax_returns_2_years", label="Tax returns",
        review_checks=[policy], verification_required=False, coverage_complete=True,
        status="received_unverified", evidence_files=[SimpleNamespace(file_id=f.id) for f in files],
        provenance={"ai_document_review": adverse} if change in {"provider_failure", "prior_adverse"} else {})
    fresh = SimpleNamespace(**initial.__dict__)
    if change == "human":
        fresh.verification_required = True
    if change == "policy":
        fresh.review_checks = [SimpleNamespace(model_dump=lambda: {**CHECK, "instructions": "New checks"})]
    fresh_files = [SimpleNamespace(**f.__dict__) for f in files]
    if change == "content":
        fresh_files[0].content_hash = "changed"
    state = SimpleNamespace(verification_required=False, provenance=dict(initial.provenance))
    if change == "concurrent_adverse":
        state.provenance = {"ai_document_review": adverse}
    active_write_lock = True
    events = []

    async def commit():
        nonlocal active_write_lock
        events.append("commit")
        active_write_lock = False

    async def readiness(*_args):
        nonlocal active_write_lock
        active_write_lock = True
        events.append("prepare")
        return SimpleNamespace(requirements=[initial if len(events) == 1 else fresh])

    async def provider(*_args, **_kwargs):
        assert active_write_lock is False
        events.append("provider")
        if change == "provider_failure":
            raise TimeoutError("provider timeout")
        return SimpleNamespace()

    def rows(items):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))

    responses = [rows(files), rows(fresh_files), SimpleNamespace(scalar_one_or_none=lambda: state)]
    db = SimpleNamespace(execute=AsyncMock(side_effect=responses), commit=AsyncMock(side_effect=commit), add=Mock())
    with (patch("app.services.application_programs.get_program_readiness", side_effect=readiness),
          patch("app.services.document_qualification_review._source_pages", return_value=PAGES),
          patch("app.services.ai.bedrock_client.get_client", return_value=Mock()),
          patch("app.services.ai.bedrock_client.model_heavy", return_value="test-model"),
          patch("app.services.ai.usage.tracked_messages_create", side_effect=provider),
          patch("app.services.ai.structured_output.require_complete_response"),
          patch("app.services.bucket_ai._text_from_response", return_value="{}"),
          patch("app.services.bucket_ai._json_or_fallback", return_value=result())):
        count = await review_profile_checks(db, SimpleNamespace(id=uuid4(), loan_id=None))

    if change in {"human", "policy", "content"}:
        assert count == 0
        db.add.assert_not_called()
        assert "ai_document_review" not in state.provenance
    else:
        assert count == 1
        assert state.provenance["ai_document_review"]["status"] == ("blocked" if change in {"provider_failure", "prior_adverse", "concurrent_adverse"} else "pass")
        audit = db.add.call_args.args[0]
        assert audit.payload["assessment"]["status"] == ("unknown" if change == "provider_failure" else "pass")
        assert audit.payload["retained_prior_adverse"] is (change in {"provider_failure", "prior_adverse", "concurrent_adverse"})
        if change in {"prior_adverse", "concurrent_adverse"}:
            assert "conflict" in state.provenance["ai_document_review"]["reason"]
    assert events.index("commit") < events.index("provider")
    assert events[-1] == "commit"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_legacy_review_endpoint_queues_or_reuses_durable_job_without_provider(existing):
    from app.services.application_programs import accept_high_confidence_ai_evidence

    record = SimpleNamespace(requirement_key="business_tax_returns_2_years", review_checks=[CHECK],
        verification_required=False, status="received_unverified", evidence_files=[])
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    bucket = SimpleNamespace(id=profile.primary_bucket_id, ai_context={})
    queued = SimpleNamespace(id=uuid4()) if existing else None
    db = SimpleNamespace(get=AsyncMock(return_value=bucket),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: queued)),
        add=Mock(), flush=AsyncMock())
    async def flush():
        if db.add.called:
            db.add.call_args.args[0].id = uuid4()
    db.flush.side_effect = flush
    with patch("app.services.application_programs.get_program_readiness", return_value=SimpleNamespace(requirements=[record])):
        output = await accept_high_confidence_ai_evidence(db, profile, [], SimpleNamespace(id=uuid4()))
    assert output["queued_review_id"] is not None
    assert "queued" in output["review_message"]
    assert db.add.call_count == (0 if existing else 1)
    assert db.get.call_args.kwargs["with_for_update"] is True
