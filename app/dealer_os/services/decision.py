"""One decision per file, with the receipts.

The problem this solves is disagreement. A file can show a green "fundable"
banner from the program grid while the balance rule says the endings fall every
month, and a rep looking at both has no way to know which one to act on. So
there is one computed result and everything on screen reads from it.

**The balance rule is a gate, not a score.** The rule is that ending balances
must not be negative and must not fall month after month. A business whose
balances are draining does not become fundable because a program's readiness
percentage happens to clear a threshold; the draining is the thing that will
get the file declined, and surfacing it as one amber note beside a green
headline buries it. So a conclusive balance failure caps the verdict, and the
reason travels with it.

**"We cannot tell yet" is not a failure.** Three months of statements is the
minimum to judge a trend. Below that the assessment is inconclusive, and a file
that has simply not been worked yet must not be reported as a business with a
problem. Inconclusive never caps anything; it just says what is missing.

Ordering matters in the headline too. What a rep needs first is what to do
next, so the blocking items come from the strongest program rather than from
the whole grid: fixing the one path that is closest is the shortest route to a
yes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .balance_health import BalanceHealth

__all__ = ["FileDecision", "decide"]

# Ordered worst to best, so capping is a min() on the index.
_RANK = ["no_data", "not_yet", "conditional", "fundable"]


@dataclass
class FileDecision:
    verdict: str
    headline: str
    """One sentence a rep can read out. Never contradicts `verdict`."""
    blocking: list[dict] = field(default_factory=list)
    """Unmet requirements of the strongest program, each with its own label."""
    balance_passed: bool | None = None
    """None when there was not enough data to judge."""
    balance_reasons: list[str] = field(default_factory=list)
    capped_by_balance: bool = False
    """True when the program grid said better than this and the balance rule
    pulled it down. Worth showing, because it explains a verdict that would
    otherwise look wrong next to the readiness percentages."""
    best_path: dict | None = None
    goal_feasible: bool | None = None
    ready_for_forms: bool = False
    """The gate for filling and signing PDFs. Deliberately strict: paperwork
    that goes out on a file that is not actually fundable wastes the owner's
    time and our credibility with the lender."""


def _cap(verdict: str, ceiling: str) -> str:
    try:
        return _RANK[min(_RANK.index(verdict), _RANK.index(ceiling))]
    except ValueError:
        return verdict


def decide(fundability: dict[str, Any], balance: BalanceHealth | None) -> FileDecision:
    """Collapse the program grid and the balance rule into one answer. PURE."""
    verdict = str(fundability.get("verdict") or "no_data")
    blocking = list(fundability.get("blocking") or [])
    best = fundability.get("best_path")
    goal_feasible = fundability.get("goal_feasible")

    balance_passed: bool | None = None
    reasons: list[str] = []
    capped = False

    if balance is not None and balance.conclusive:
        balance_passed = balance.passed
        reasons = list(balance.reasons)
        if not balance.passed:
            before = verdict
            # A draining account is not a partial yes. Capping at not_yet
            # rather than inventing a fourth state keeps every consumer of
            # this verdict working without learning a new value.
            verdict = _cap(verdict, "not_yet")
            capped = verdict != before

    if verdict == "no_data":
        headline = "Not enough in the file yet to say. Statements are what move this."
    elif capped:
        first = reasons[0] if reasons else "the ending balances are not holding up"
        headline = f"Not fundable yet: {first}"
    elif verdict == "fundable":
        name = (best or {}).get("label") or (best or {}).get("path_key") or "a program"
        headline = f"Fundable today on {name}."
        if balance_passed is None:
            headline += " Balances are not judged yet, so get three months of statements in."
    elif verdict == "conditional":
        n = len(blocking)
        headline = (
            f"Close. {n} thing{'' if n == 1 else 's'} to fix before this is fundable."
            if n
            else "Close, but the strongest program is not fully met yet."
        )
    else:
        headline = "Not fundable yet on the current numbers."

    return FileDecision(
        verdict=verdict,
        headline=headline,
        blocking=blocking,
        balance_passed=balance_passed,
        balance_reasons=reasons,
        capped_by_balance=capped,
        best_path=best,
        goal_feasible=goal_feasible,
        # Forms go out only on a clean fundable. Not "conditional", and not a
        # fundable whose balances have never been checked: a client who signs a
        # package that then gets declined on the first thing a lender looks at
        # is a client who does not come back.
        ready_for_forms=(verdict == "fundable" and balance_passed is True),
    )
