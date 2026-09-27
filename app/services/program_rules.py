"""Validated, non-executable rules for funding-program fit."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from numbers import Real
from typing import Any

ALLOWED_OPERATORS = {"present", "eq", "in", "gte", "lte", "gt", "lt", "evidence_available"}
ALLOWED_COMBINATORS = {"all", "any", "not"}
MAX_DEPTH = 8
MAX_RULES = 100

# Every ordinary rule field must be materialized by
# application_programs.profile_fit_context. Keeping this bounded prevents a
# typo in an approved version from becoming a permanent "needs information"
# result that no evidence can ever satisfy. Evidence classifications remain
# extensible through the dedicated evidence_available operator.
SUPPORTED_FIT_FIELDS = frozenset(
    {
        "vertical",
        "intake_variant",
        "intent",
        "intent_kind",
        "funding_category",
        "entity_type",
        "industry",
        "subindustry",
        "industry_key",
        "naics_code",
        "loan_purpose",
        "requested_amount",
        "use_of_funds_total",
        "real_estate_equipment_amount",
        "real_estate_equipment_pct",
        "use_of_funds_complete",
        "business_age_years",
        "revenue",
        "annual_revenue",
        "annualized_deposits",
        "deposits",
        "bank_statement_months",
        "tax_return_years",
        "nsf_or_overdraft_count",
        "credit_score",
        "estimated_credit_score",
        "dscr",
        "cash_flow",
        "debt_burden",
        "liquid_assets",
        "tax_returns_available",
        "bank_statements_available",
        "evidence_count",
        "declared_collateral",
        "mca_obligations_present",
        "floorplan_inventory_present",
        "equipment_financing_intent",
    }
)


class ProgramRuleError(ValueError):
    pass


@dataclass(frozen=True)
class RuleEvaluation:
    matched: bool
    passed: int
    total: int
    reasons: list[str]

    @property
    def confidence(self) -> float:
        return 0.0 if self.total == 0 else round(self.passed / self.total, 4)


def validate_rules(
    rules: dict[str, Any] | None,
    *,
    enforce_supported_fields: bool = False,
) -> None:
    """Validate the reserved fit and recommendation preference trees.

    ``enforce_supported_fields`` is used when a catalog draft is created or
    published.  Evaluation deliberately leaves it off so a legacy published
    rule that used a dotted context path remains readable instead of suddenly
    becoming invalid after this validation was introduced.
    """
    if not rules:
        return
    if enforce_supported_fields:
        priority = rules.get("priority", 0)
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not -10_000 <= priority <= 10_000
        ):
            raise ProgramRuleError("Program fit priority must be a bounded integer")
    count = [0]
    if "fit" in rules:
        _validate_node(
            rules["fit"], depth=0, count=count,
            enforce_supported_fields=enforce_supported_fields,
        )
    preferences = rules.get("recommendation_preferences", [])
    if not isinstance(preferences, list) or len(preferences) > 10:
        raise ProgramRuleError("At most 10 recommendation preferences are allowed")
    keys: set[str] = set()
    preference_count = [0]
    for preference in preferences:
        if not isinstance(preference, dict) or set(preference) != {"key", "label", "when", "score"}:
            raise ProgramRuleError("Each recommendation preference requires key, label, when, and score")
        key, label, score = preference["key"], preference["label"], preference["score"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key) or key in keys:
            raise ProgramRuleError("Recommendation preference keys must be unique lowercase identifiers")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 200:
            raise ProgramRuleError("Recommendation preference labels must contain 1 to 200 characters")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 100:
            raise ProgramRuleError("Recommendation preference score must be an integer from 1 to 100")
        keys.add(key)
        # Preferences are new: unlike legacy fit rules, there are no unknown
        # historical field paths that need permissive backwards compatibility.
        _validate_node(preference["when"], depth=0, count=preference_count, enforce_supported_fields=True)


def _validate_node(
    node: Any,
    *,
    depth: int,
    count: list[int],
    enforce_supported_fields: bool,
) -> None:
    if depth > MAX_DEPTH:
        raise ProgramRuleError("Program fit rules are nested too deeply")
    count[0] += 1
    if count[0] > MAX_RULES:
        raise ProgramRuleError("Program fit rules contain too many conditions")
    if not isinstance(node, dict) or not node:
        raise ProgramRuleError("Each program fit rule must be an object")
    combinators = [key for key in ALLOWED_COMBINATORS if key in node]
    if combinators:
        if len(combinators) != 1 or len(node) != 1:
            raise ProgramRuleError("A composite rule must contain exactly one of all, any, or not")
        key = combinators[0]
        children = node[key]
        if key == "not":
            _validate_node(
                children,
                depth=depth + 1,
                count=count,
                enforce_supported_fields=enforce_supported_fields,
            )
            return
        if not isinstance(children, list) or not children:
            raise ProgramRuleError(f"{key} must contain at least one rule")
        for child in children:
            _validate_node(
                child,
                depth=depth + 1,
                count=count,
                enforce_supported_fields=enforce_supported_fields,
            )
        return
    allowed = {"field", "op", "value"}
    if set(node) - allowed:
        raise ProgramRuleError("Program fit rules contain unsupported keys")
    field = node.get("field")
    op = node.get("op")
    if not isinstance(field, str) or not field or len(field) > 120:
        raise ProgramRuleError("A program fit rule requires a valid field")
    if op not in ALLOWED_OPERATORS:
        raise ProgramRuleError("Unsupported program fit operator")
    value = node.get("value")
    if (
        enforce_supported_fields
        and op != "evidence_available"
        and field not in SUPPORTED_FIT_FIELDS
    ):
        raise ProgramRuleError(f"Unsupported program fit field: {field}")
    if enforce_supported_fields and op == "present" and "value" in node:
        raise ProgramRuleError("The present operator does not accept a value")
    if enforce_supported_fields and op == "evidence_available" and value is not None and (
        not isinstance(value, str) or not value.strip()
    ):
        raise ProgramRuleError("The evidence_available operator requires a classification string")
    if op == "in" and (
        not isinstance(value, list)
        or (enforce_supported_fields and not value)
        or len(value) > 100
    ):
        raise ProgramRuleError("The in operator requires a bounded value list")
    if op in {"gte", "lte", "gt", "lt"} and not _is_number(value):
        raise ProgramRuleError(f"The {op} operator requires a numeric value")
    if op in {"eq", "in"} and value is None:
        raise ProgramRuleError(f"The {op} operator requires a value")


def evaluate_rules(rules: dict[str, Any] | None, context: dict[str, Any]) -> RuleEvaluation:
    if not rules or "fit" not in rules:
        return RuleEvaluation(False, 0, 0, ["No published fit rule"])
    validate_rules(rules)
    return _evaluate_node(rules["fit"], context)


def evaluate_recommendation_preferences(
    rules: dict[str, Any] | None, context: dict[str, Any],
) -> tuple[int, list[str]]:
    """Rank-only signals; callers must already establish published eligibility."""
    validate_rules(rules)
    score, reasons = 0, []
    for preference in (rules or {}).get("recommendation_preferences", []):
        node = preference["when"]
        # A missing allocation/classification must not become a preference via
        # `not`, nor via one populated branch of an otherwise unknown `any`.
        if _has_unknown_preference_input(node, context):
            continue
        if _evaluate_node(node, context).matched:
            score += preference["score"]
            reasons.append(preference["label"].strip())
    return score, reasons


def _has_unknown_preference_input(node: dict[str, Any], context: dict[str, Any]) -> bool:
    if "not" in node:
        return _has_unknown_preference_input(node["not"], context)
    for key in ("all", "any"):
        if key in node:
            return any(_has_unknown_preference_input(child, context) for child in node[key])
    if node["op"] == "evidence_available":
        return "evidence_available" not in context
    return _resolve_field(context, node["field"]) in (None, "", [], {})


def _evaluate_node(node: dict[str, Any], context: dict[str, Any]) -> RuleEvaluation:
    if "all" in node:
        children = [_evaluate_node(child, context) for child in node["all"]]
        return RuleEvaluation(
            all(child.matched for child in children),
            sum(child.passed for child in children),
            sum(child.total for child in children),
            [reason for child in children for reason in child.reasons],
        )
    if "any" in node:
        children = [_evaluate_node(child, context) for child in node["any"]]
        return RuleEvaluation(
            any(child.matched for child in children),
            sum(child.passed for child in children),
            sum(child.total for child in children),
            [reason for child in children for reason in child.reasons],
        )
    if "not" in node:
        child = _evaluate_node(node["not"], context)
        matched = not child.matched
        return RuleEvaluation(matched, int(matched), 1, [f"not ({'; '.join(child.reasons)})"])

    field = str(node["field"])
    op = str(node["op"])
    expected = node.get("value")
    actual = _resolve_field(context, field)
    if op == "present":
        matched = actual not in (None, "", [], {})
    elif op == "evidence_available":
        evidence = context.get("evidence_available") or set()
        wanted = str(expected if expected is not None else field)
        matched = wanted in evidence
    elif op == "eq":
        matched = actual == expected
    elif op == "in":
        matched = actual in expected
    elif op in {"gte", "lte", "gt", "lt"}:
        matched = _numeric_compare(actual, expected, op)
    else:  # pragma: no cover - validation prevents this branch
        matched = False
    return RuleEvaluation(
        matched,
        int(matched),
        1,
        [f"{field} {op} {'matched' if matched else 'did not match'}"],
    )


def _resolve_field(context: dict[str, Any], field: str) -> Any:
    current: Any = context
    for part in field.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _is_number(value: Any) -> bool:
    if not isinstance(value, Real) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _numeric_compare(actual: Any, expected: Any, op: str) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(left) or not math.isfinite(right):
        return False
    return {
        "gte": left >= right,
        "lte": left <= right,
        "gt": left > right,
        "lt": left < right,
    }[op]
