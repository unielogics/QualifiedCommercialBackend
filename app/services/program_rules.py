"""Validated, non-executable rules for funding-program fit."""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from typing import Any

ALLOWED_OPERATORS = {"present", "eq", "in", "gte", "lte", "gt", "lt", "evidence_available"}
ALLOWED_COMBINATORS = {"all", "any", "not"}
MAX_DEPTH = 8
MAX_RULES = 100


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


def validate_rules(rules: dict[str, Any] | None) -> None:
    """Validate only the reserved ``fit`` tree; other playbook metadata is inert."""
    if not rules or "fit" not in rules:
        return
    count = [0]
    _validate_node(rules["fit"], depth=0, count=count)


def _validate_node(node: Any, *, depth: int, count: list[int]) -> None:
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
            _validate_node(children, depth=depth + 1, count=count)
            return
        if not isinstance(children, list) or not children:
            raise ProgramRuleError(f"{key} must contain at least one rule")
        for child in children:
            _validate_node(child, depth=depth + 1, count=count)
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
    if op == "in" and (not isinstance(value, list) or len(value) > 100):
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
    return isinstance(value, Real) and not isinstance(value, bool)


def _numeric_compare(actual: Any, expected: Any, op: str) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError):
        return False
    return {
        "gte": left >= right,
        "lte": left <= right,
        "gt": left > right,
        "lt": left < right,
    }[op]
