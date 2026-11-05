"""One lock order, everywhere.

A deadlock is not a bug you can see in a diff. Both sides are correct on their
own; only the pair is wrong, and the failure arrives as an intermittent 500 on
whichever request Postgres picks to kill. It cost an evidence-workspace 500 in
production on 2026-10-29:

    DeadlockDetectedError: deadlock detected
    Process 5179 waits for ShareLock on transaction 352795; blocked by 5173.
    [SQL: SELECT application_profiles.id ... FOR UPDATE]

`/evidence-workspace` took the bucket row lock (via
ensure_supporting_document_group) and then the profile row lock (via
get_program_readiness). `/requirements/ai-review` took the profile lock first
(get_program_readiness, called from accept_high_confidence_ai_evidence) and the
bucket lock afterwards. The dashboard fires both at once for an open file.

The rule is BUCKET BEFORE PROFILE. These tests read the source, because there
is no assertion you can make about a single process that catches a lock cycle,
and a green suite proved nothing about it the day it broke.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

SOURCE = Path("app/services/application_programs.py")


def _function_source(name: str) -> str:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SOURCE.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"{name} not found in {SOURCE}")


def test_readiness_locks_the_bucket_before_the_profile():
    body = _function_source("get_program_readiness")
    bucket_at = body.find("select(Bucket.id)")
    profile_at = body.find("select(ApplicationProfile.id)")
    assert bucket_at != -1, "get_program_readiness no longer locks the bucket at all"
    assert profile_at != -1, "get_program_readiness no longer locks the profile"
    assert bucket_at < profile_at, (
        "get_program_readiness must take the BUCKET lock before the PROFILE lock. "
        "Reversing these re-opens the deadlock against /evidence-workspace."
    )


def test_the_bucket_lock_is_conditional_on_there_being_a_bucket():
    """A profile with no primary bucket must not blow up reaching for one."""
    body = _function_source("get_program_readiness")
    assert "if profile.primary_bucket_id is not None:" in body


@pytest.mark.parametrize(
    "func",
    ["get_program_readiness", "accept_high_confidence_ai_evidence"],
)
def test_both_sides_of_the_old_cycle_still_exist(func):
    """If either is renamed or inlined, the comment above stops describing the
    code and the next person loses the only record of why this order matters."""
    assert _function_source(func)


def test_the_rule_is_written_down_where_it_is_enforced():
    body = _function_source("get_program_readiness")
    assert "BUCKET BEFORE PROFILE" in body


def test_no_other_service_takes_the_profile_lock_then_a_bucket_lock():
    """The two paths that hold both locks are known. A third would need to
    follow the same order, and this catches one that does not."""
    offenders: list[str] = []
    for path in sorted(Path("app").rglob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "with_for_update" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            segment = ast.get_source_segment(text, node) or ""
            if "with_for_update" not in segment:
                continue
            profile_at = segment.find("ApplicationProfile.id).where")
            bucket_at = segment.find("Bucket.id).where")
            if profile_at == -1 or bucket_at == -1:
                continue
            if profile_at < bucket_at:
                offenders.append(f"{path}:{node.lineno} {node.name}")
    assert not offenders, (
        "profile lock taken before a bucket lock in: " + "; ".join(offenders)
    )


def test_inspect_agrees_with_the_parsed_source():
    """Guards the guard: if the file moves, these tests must fail loudly rather
    than silently pass against an empty string."""
    from app.services import application_programs

    live = inspect.getsource(application_programs.get_program_readiness)
    assert "BUCKET BEFORE PROFILE" in live
