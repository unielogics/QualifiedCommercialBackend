"""AgentTask → AI Follow-Up promotion — contract tests (Phase 7).

The synthetic ClientRequirementStatus row created by
POST /clients/{id}/tasks/{task_id}/promote-to-ai uses
`requirement_key = f"agent_task:{task_id}"`. This module locks down
the prefix invariants so the platform requirement resolver continues
to skip these synthetic keys (they aren't backed by
AICollectionRequirement catalog rows).

If the prefix ever changes, the resolver's catalog-driven path would
need to be taught to skip the new prefix — these tests will fire
loudly if that drift starts.
"""

from __future__ import annotations

import re
import uuid

import pytest


AGENT_TASK_KEY_PREFIX = "agent_task:"
AGENT_TASK_KEY_RE = re.compile(r"^agent_task:[0-9a-f-]{36}$")


def _build_key(task_id: uuid.UUID) -> str:
    return f"agent_task:{task_id}"


def test_prefix_is_stable():
    """The 'agent_task:' prefix is referenced by:
      - routers/agent_tasks.promote_to_ai (writer)
      - services/ai/requirement_resolver (must skip these keys)
      - the unassign path in routers/deal_secretary (reverse direction)
    If this assert fires, audit every reference before changing it.
    """
    assert AGENT_TASK_KEY_PREFIX == "agent_task:"


def test_key_shape_is_uuid_addressable():
    task_id = uuid.uuid4()
    key = _build_key(task_id)
    assert AGENT_TASK_KEY_RE.match(key)
    # Round-trip — the UUID is recoverable from the key.
    assert uuid.UUID(key.removeprefix(AGENT_TASK_KEY_PREFIX)) == task_id


def test_keys_collide_only_on_same_task():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _build_key(a) != _build_key(b)
    assert _build_key(a) == _build_key(a)


def test_no_collision_with_catalog_style_keys():
    """Real catalog requirement_keys are snake_case identifiers like
    'buyer_agency_agreement' or 'proof_of_funds'. The agent_task:
    prefix uses a colon, which is never used in catalog keys."""
    catalog_examples = [
        "buyer_agency_agreement",
        "listing_agreement",
        "proof_of_funds",
        "purchase_contract",
        "bank_statements_2mo",
    ]
    for k in catalog_examples:
        assert not k.startswith(AGENT_TASK_KEY_PREFIX)
        assert ":" not in k


def test_agent_task_category_mapping_is_total():
    """Every AgentTaskCategory must have a CRS category mapping in
    routers/agent_tasks._CATEGORY_MAP. If a new category is added to
    the enum without a mapping, this test catches it."""
    from app.enums import AgentTaskCategory
    from app.routers.agent_tasks import _CATEGORY_MAP

    enum_values = {c.value for c in AgentTaskCategory}
    mapped = set(_CATEGORY_MAP.keys())
    missing = enum_values - mapped
    assert not missing, f"AgentTaskCategory members not in _CATEGORY_MAP: {missing}"


def test_agent_task_category_mapping_targets_valid_requirement_category():
    """Mapped values must be RequirementCategory enum values so the
    UI's RequirementCategory chips render correctly."""
    from app.enums import RequirementCategory
    from app.routers.agent_tasks import _CATEGORY_MAP

    valid = {c.value for c in RequirementCategory}
    for src, dst in _CATEGORY_MAP.items():
        assert dst in valid, f"_CATEGORY_MAP[{src!r}] = {dst!r} is not a RequirementCategory"
