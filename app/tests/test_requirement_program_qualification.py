from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from app.services.document_qualification_review import check_id
from app.services.requirement_program_qualification import for_definitions
from app.tests.test_credit_requirement_sync import NOW, owner, profile, pull, requirement


def document(checks):
    return SimpleNamespace(review_checks=checks, verification_required=True,
                           completion_mode="requires_human_verify", label="Business return")


def state(checks, **extra):
    return SimpleNamespace(status="verified", verified_by_user_id=None,
        provenance={"review_checks": checks, "qualification_review": {
            "status": "pending", "blocking": True, "reasons": ["Other program condition remains open"],
            "checks": checks}, **extra})


def test_one_program_does_not_inherit_another_programs_document_conditions():
    check = {"key": "positive_income", "label": "Positive income", "instructions": "Verify income", "severity": "block"}
    shared = state([check])
    assert for_definitions(shared, [document([])])["blocking"] is False
    assert for_definitions(shared, [document([check])])["blocking"] is True


def test_grounded_multi_program_assessment_is_evaluated_per_pinned_check():
    first = {"key": "revenue", "label": "Revenue floor", "instructions": "Verify revenue", "severity": "block"}
    second = {"key": "income", "label": "Positive income", "instructions": "Verify income", "severity": "block"}
    shared = state([first, second], ai_document_review={"status": "fail", "checks": [
        {"id": check_id(first), "status": "pass", "citations": [{"file_id": "return", "page": 1, "quote": "Supported revenue"}]},
        {"id": check_id(second), "status": "fail", "reason": "Income is negative", "citations": [{"file_id": "return", "page": 1}]},
    ]})
    before = deepcopy(shared.provenance)
    assert for_definitions(shared, [document([first])])["status"] == "met"
    assert for_definitions(shared, [document([second])])["status"] == "failed"
    assert shared.provenance == before


def test_an_old_or_absent_check_result_cannot_qualify_new_program_conditions():
    first = {"key": "old", "label": "Old condition", "instructions": "Verify old facts", "severity": "block"}
    second = {"key": "new", "label": "New condition", "instructions": "Verify new facts", "severity": "block"}
    shared = state([first])
    shared.provenance["qualification_review"]["status"] = "met"
    shared.provenance["qualification_review"]["blocking"] = False
    assert for_definitions(shared, [document([second])])["blocking"] is True


def test_warning_only_program_does_not_inherit_a_shared_blocking_condition():
    blocking = {"key": "income", "label": "Positive income", "instructions": "Verify income", "severity": "block"}
    warning = {"key": "trend", "label": "Review trend", "instructions": "Review revenue trend", "severity": "review"}
    shared = state([blocking, warning])
    scoped = for_definitions(shared, [document([warning])])
    assert scoped["status"] == "pending"
    assert not scoped["blocking"]
    assert scoped["reasons"]


def test_single_bureau_collection_does_not_inherit_another_programs_score_model():
    person = owner()
    report = pull(person)
    shared = state([], credit_collection={"complete": True, "owners": [], "last_review": None})
    generic = requirement(label="Owner credit reports", review_checks=[], completion_criteria="Collect current owner reports")
    strict = requirement(label="680+ Equifax FICO 9")
    context = {"profile": profile(), "owners": [person], "pulls": {str(report.id): report}}
    assert for_definitions(shared, [generic], **context)["blocking"] is False
    assert for_definitions(shared, [strict], **context)["blocking"] is True


def test_program_credit_expiration_is_not_taken_from_the_strictest_other_program():
    person = owner()
    report = pull(person, pulled_at=NOW - timedelta(days=35), bureau_response={
        "report_import_status": "completed", "report_s3_key": "synthetic-report",
        "parsed_report": {"scores": [{"model": "FICO 9", "bureau": "Equifax", "score": 750}]},
    })
    shared = state([], credit_collection={"complete": False, "owners": [], "last_review": None})
    context = {"profile": profile(), "owners": [person], "pulls": {str(report.id): report}}
    assert for_definitions(shared, [requirement(label="680+ Equifax FICO 9", expiration_days=30, verification_required=False, completion_criteria="")], **context)["blocking"] is True
    assert for_definitions(shared, [requirement(label="680+ Equifax FICO 9", expiration_days=90, verification_required=False, completion_criteria="")], **context)["blocking"] is False


def test_invalid_explicit_credit_configuration_never_becomes_collection_only():
    shared = state([], credit_collection={"complete": True, "owners": []})
    invalid = requirement(label="Owner credit reports", review_checks=[], completion_criteria="Collect reports",
                          collection_config={"minimum_score": "invalid"})
    assert for_definitions(shared, [invalid])["blocking"] is True
