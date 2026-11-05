"""One vocabulary for a room's kind, a profile's funding category, and the
intake facts the desk queries.

`bucket_type` was free text that once held a partner's name; `funding_category`
held `Purchase` beside `purchase`; the intake's archive moment and last link
delivery lived only inside the audit JSON. These pin the closed vocabulary, that
every writer canonicalises, and that archive/restore stamp the new columns.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.routers import dealer_ai_intake as intake_router
from app.schemas.bucket import BucketCreate, BucketUpdate
from app.services import application_profiles
from app.services import attention_queue as aq


def test_a_room_kind_must_come_from_the_vocabulary() -> None:
    assert BucketCreate(name="Lee Motors", bucket_type="loan_file").bucket_type == "loan_file"
    assert BucketUpdate(bucket_type="general").bucket_type == "general"
    # The labels the old form sent fold into kinds (see the transition test
    # below); anything outside both vocabularies is still refused.
    with pytest.raises(ValidationError):
        BucketCreate(name="Lee Motors", bucket_type="Something Else Entirely")
    with pytest.raises(ValidationError):
        BucketUpdate(bucket_type="loan-file")


def test_the_vocabulary_keeps_the_substrings_the_vertical_is_derived_from() -> None:
    # operator_files reads main_street / mca / dealer out of the type.
    from app.models.bucket import Bucket

    (constraint,) = [c for c in Bucket.__table__.constraints if getattr(c, "name", "") == "ck_buckets_bucket_type"]
    text = str(constraint.sqltext)
    for kind in ("main_street_intake", "mca_refinance_intake", "dealer_ai_intake", "real_estate_ai_intake"):
        assert kind in text


def test_funding_category_canonicalises_known_intents_and_keeps_unknown_text() -> None:
    # The rule is lead_types.normalize_funding_intent: a known alias becomes one
    # of the eight intents; anything else is kept exactly as written, so a
    # sentence a person typed is never silently turned into a key.
    assert application_profiles.canonical_funding_category("Purchase") == "real_estate"
    assert application_profiles.canonical_funding_category("Refinance") == "debt_refinance"
    assert application_profiles.canonical_funding_category("MCA refinance") == "mca_refinance"
    assert application_profiles.canonical_funding_category("working_capital") == "working_capital"
    assert application_profiles.canonical_funding_category("Equipment financing") == "equipment"
    sentence = "General funding discussion / Not decided yet"
    assert application_profiles.canonical_funding_category(sentence) == sentence
    assert application_profiles.canonical_funding_category(None) is None
    assert application_profiles.canonical_funding_category("   ") is None


def _assignments_to(func: object, attribute: str) -> list[str]:
    """Source text of every `<something>.<attribute> = ...` in the function."""

    tree = ast.parse(inspect.getsource(func))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == attribute:
                    found.append(ast.unparse(node.value))
    return found


def test_every_profile_writer_canonicalises_the_funding_category() -> None:
    writes = _assignments_to(application_profiles.resolve_profile, "funding_category")
    assert writes, "resolve_profile no longer writes funding_category?"
    for source in writes:
        assert "_canonical_funding_category(" in source, source


def test_archiving_an_intake_stamps_the_columns_and_restoring_clears_them() -> None:
    archive = inspect.getsource(intake_router.admin_confirm_lead_deletion)
    assert "intake.archived_at = archived_at" in archive
    assert "intake.archived_by_user_id = user.id" in archive
    restore = inspect.getsource(intake_router.admin_restore_lead)
    assert "intake.archived_at = None" in restore
    assert "intake.archived_by_user_id = None" in restore


def test_link_emails_stamp_the_last_delivery_columns() -> None:
    for name in ("_record_resume_email", "_record_login_code_email"):
        source = inspect.getsource(getattr(intake_router, name))
        assert "intake.last_link_sent_at = _now()" in source, name
        assert "intake.last_link_status = result.detail" in source, name


async def test_the_queue_prefers_the_column_over_the_audit_json() -> None:
    now = datetime(2026, 10, 30, 15, tzinfo=UTC)
    intake = SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), full_name="Pat Lee", business_name=None,
        created_at=now - timedelta(days=10),
        last_link_sent_at=now - timedelta(days=2),
        intake_state={"resume_email": {"ok": True, "sent_at": "2026-10-01T10:00:00+00:00"}},
    )

    class _Result:
        def __init__(self, rows): self._rows = rows
        def all(self): return list(self._rows)
        def scalars(self): return self

    class _DB:
        def __init__(self): self.results = [[(intake, None)], [], [], [], [], [], []]
        async def execute(self, _stmt): return _Result(self.results.pop(0) if self.results else [])

    queue = await aq.load_attention_queue(_DB(), now=now)
    (item,) = queue.items
    assert "link sent 2d ago" in item.detail


def test_the_labels_the_old_form_sent_fold_into_kinds_during_the_transition() -> None:
    # A dashboard still serving the pre-vocabulary create form keeps working
    # until its own deploy lands; the mapping is the one 0244 applied to rows.
    assert BucketCreate(name="Lee Motors", bucket_type="Loan File").bucket_type == "loan_file"
    assert BucketCreate(name="Lee Motors", bucket_type="UrChoice Dealer Funding").bucket_type == "general"
    assert BucketUpdate(bucket_type="  ").bucket_type is None
    with pytest.raises(ValidationError):
        BucketCreate(name="Lee Motors", bucket_type="Something Else Entirely")
