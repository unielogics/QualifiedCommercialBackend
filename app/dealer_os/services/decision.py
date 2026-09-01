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

__all__ = ["FileDecision", "Verification", "decide", "assess_verification"]

# Ordered worst to best, so capping is a min() on the index.
_RANK = ["no_data", "not_yet", "conditional", "fundable"]


@dataclass
class Verification:
    """Whether this file has earned the right to be analysed.

    Two authorizations, both from the applicant themselves: a read-only bank
    connection and a soft credit inquiry. Until both return, everything past
    step 2 would be computed from what a rep typed into a form standing in a
    shop, and a number like that is worse than no number because it looks
    exactly like a real one.

    So this is a gate rather than a warning, and it is evaluated on the server.
    A frontend that merely hides the locked steps is one crafted request away
    from showing an underwriter a profile built on hearsay.
    """

    bank_linked: bool = False
    bank_source: str = "none"
    """plaid | upload | none — why the bank side of the gate is satisfied."""
    statement_months: list[str] = field(default_factory=list)
    missing_statement_months: list[str] = field(default_factory=list)
    statement_target: int = 6
    bank_exception_available: bool = False
    bank_exception_active: bool = False
    credit_returned: bool = False
    unlocked: bool = False
    returned: int = 0
    reason: str = ""
    """Reader-facing, matching the chip in the design: '1 of 2 authorizations
    returned' / 'Bank + credit returned'."""
    stage: str = "intake"
    """intake | verification | underwriting — what the case header shows."""
    credit_enabled: bool = True
    """Whether a bureau pull can actually run.

    Mirrors plaid_client.enabled(). Without it the failure lands on the
    applicant AFTER they have entered their details and consented, as a
    "temporarily unavailable" that is not temporary and is not their problem.
    A rep needs to know before they send, not the applicant after they
    comply."""
    ownership_total: float = 0.0
    ownership_complete: bool = False
    owner_contact_complete: bool = False
    missing_credit_contact_owner_ids: list = field(default_factory=list)
    required_credit_owner_count: int = 0
    completed_credit_owner_count: int = 0
    pending_credit_owner_ids: list = field(default_factory=list)
    pre_screen_complete: bool = False
    pre_screen_blockers: list[str] = field(default_factory=list)
    preliminary_program_fit: dict | None = None


def credit_pull_available() -> bool:
    """True when the bureau gateway has credentials to call with.

    Read at request time rather than cached: the settings come from Secrets
    Manager through the env file, so adding the keys and restarting is all it
    should take to switch this on."""
    from app.config import get_settings

    s = get_settings()
    return bool(
        (getattr(s, "isoftpull_private_key", "") or getattr(s, "isoftpull_api_key", ""))
        and getattr(s, "isoftpull_public_key", "")
    )


def assess_verification(
    *,
    bank_linked: bool,
    credit_returned: bool,
    bank_source: str | None = None,
    statement_months: list[str] | None = None,
    missing_statement_months: list[str] | None = None,
    statement_target: int = 6,
    bank_exception_available: bool = False,
    bank_exception_active: bool = False,
    ownership_total: float = 0.0,
    ownership_complete: bool = False,
    owner_contact_complete: bool = False,
    missing_credit_contact_owner_ids: list | None = None,
    required_credit_owner_count: int = 0,
    completed_credit_owner_count: int = 0,
    pending_credit_owner_ids: list | None = None,
    pre_screen_complete: bool = False,
    pre_screen_blockers: list[str] | None = None,
    preliminary_program_fit: dict | None = None,
) -> Verification:
    """PURE. Bank and credit must return after the Step 1 screen is complete.

    The bank connection is what computes the metrics, and the credit band is
    what sizes the offer. A file with one of the two is not half-analysable,
    it is a file waiting on the other one.
    """
    returned = int(bank_linked) + int(credit_returned)
    unlocked = returned == 2 and pre_screen_complete
    if not ownership_complete:
        reason = f"Ownership totals {ownership_total:.2f}% · complete 100% in step 1"
    elif not owner_contact_complete:
        reason = "Add personal email and phone for every 20%+ owner in step 1"
    elif not pre_screen_complete:
        reason = "Complete the Step 1 eligibility checkpoint before verification"
    elif required_credit_owner_count and completed_credit_owner_count < required_credit_owner_count:
        reason = (
            f"{completed_credit_owner_count} of {required_credit_owner_count} required owners completed"
        )
    elif unlocked:
        reason = (
            "Bank (3-month exception) + all required owner credit returned"
            if bank_exception_active
            else "Bank + all required owner credit returned"
        )
    elif returned == 1:
        reason = "1 of 2 authorizations returned"
    else:
        reason = "Awaiting both authorizations"
    return Verification(
        bank_linked=bank_linked,
        bank_source=bank_source or ("plaid" if bank_linked else "none"),
        statement_months=list(statement_months or []),
        missing_statement_months=list(missing_statement_months or []),
        statement_target=statement_target,
        bank_exception_available=bank_exception_available,
        bank_exception_active=bank_exception_active,
        credit_returned=credit_returned,
        unlocked=unlocked,
        returned=returned,
        reason=reason,
        stage="underwriting" if unlocked else ("verification" if returned else "intake"),
        credit_enabled=credit_pull_available(),
        ownership_total=ownership_total,
        ownership_complete=ownership_complete,
        owner_contact_complete=owner_contact_complete,
        missing_credit_contact_owner_ids=list(missing_credit_contact_owner_ids or []),
        required_credit_owner_count=required_credit_owner_count,
        completed_credit_owner_count=completed_credit_owner_count,
        pending_credit_owner_ids=list(pending_credit_owner_ids or []),
        pre_screen_complete=pre_screen_complete,
        pre_screen_blockers=list(pre_screen_blockers or []),
        preliminary_program_fit=preliminary_program_fit,
    )


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
    verification: Verification = field(default_factory=Verification)


def _cap(verdict: str, ceiling: str) -> str:
    try:
        return _RANK[min(_RANK.index(verdict), _RANK.index(ceiling))]
    except ValueError:
        return verdict


def decide(
    fundability: dict[str, Any],
    balance: BalanceHealth | None,
    verification: Verification | None = None,
) -> FileDecision:
    """Collapse the program grid, the balance rule and the verification state
    into one answer. PURE.

    Verification outranks everything. An unverified file is not "not_yet
    fundable" on its numbers, it is a file whose numbers have not been
    established, and saying "not fundable" about it would be a judgement we
    have not earned.
    """
    ver = verification or Verification()
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

    if not ver.unlocked:
        # Nothing downstream is trustworthy yet, so do not dress it up as a
        # verdict. Say what is outstanding, which is also the rep's next action.
        verdict = "no_data"
        headline = (
            "Waiting on the bank connection and the credit authorization."
            if ver.returned == 0
            else (
                "Waiting on the credit authorization."
                if ver.bank_linked
                else "Waiting on the bank connection."
            )
        )
    elif verdict == "no_data":
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
        ready_for_forms=(ver.unlocked and verdict == "fundable" and balance_passed is True),
        verification=ver,
    )
