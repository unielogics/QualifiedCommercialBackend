"""Balance health over the trailing months: the desk's first screen on a file.

Two questions, both answered from statement balances alone, and both cheap
enough to run before anything expensive happens:

  1. Did the account ever go negative? A negative ending or starting balance is
     an overdraft, and it is disqualifying on its own regardless of how healthy
     the averages look.
  2. Is the balance trending down? A file that ends three consecutive months at
     30, then 20, then 10 is not a business with a $20k average balance. It is a
     business running out of money, and averaging the three hides exactly the
     thing that matters. Flat or growing passes; a consistent slide does not.

Pure and deterministic: takes period dicts, returns a verdict. No IO, no model
call, no clock.

Note on starting balances: statements carry an ending balance per month, and a
month's starting balance is the prior month's ending balance. So a run of N
months yields N ending balances and N-1 derivable starting balances, and the
first month's opening is unknown rather than assumed to be zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BalanceHealth", "assess_balance_health", "DEFAULT_MONTHS"]

# The desk looks at three months for this screen. Longer windows let an old bad
# month keep punishing a business that has since recovered, which is the
# opposite of what a trend check is for.
DEFAULT_MONTHS = 3

# A month-over-month move smaller than this is noise, not direction. Without it,
# 20,000 -> 19,980 reads as a decline.
_FLAT_TOLERANCE = 0.01  # 1% of the earlier balance


@dataclass
class BalanceHealth:
    months_used: int
    passed: bool
    """True only when every check passed AND there was enough data to judge."""
    conclusive: bool
    """False when there are too few months to say anything. Distinct from a
    failure: 'we cannot tell yet' must never be reported as 'they failed'."""
    negative_months: list[str] = field(default_factory=list)
    declining: bool = False
    trend_pct: float | None = None
    """Change from the first ending balance to the last, as a fraction."""
    endings: list[float] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    """Plain-language failures, ready to show a rep without rewording."""


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _label(period) -> str:
    try:
        return period.strftime("%b %Y")
    except AttributeError:
        return str(period)


def assess_balance_health(periods: list[dict], months: int = DEFAULT_MONTHS) -> BalanceHealth:
    """Judge the trailing `months` of balances.

    `periods` is the engine's period-dict list, most-recent-first, each with
    `period` and `ending_balance`. Months without an ending balance are skipped
    rather than treated as zero, because a missing statement is missing data,
    not an empty account.
    """
    usable = [p for p in periods if _f(p.get("ending_balance")) is not None][:months]
    # Oldest first, so the trend reads left to right the way a person would.
    usable = list(reversed(usable))

    endings = [_f(p["ending_balance"]) for p in usable]
    result = BalanceHealth(
        months_used=len(usable),
        passed=False,
        conclusive=len(usable) >= 2,
        endings=[round(e, 2) for e in endings],
    )

    if not usable:
        result.reasons.append("No statement balances on file yet.")
        return result

    # --- 1. Negative balances -------------------------------------------------
    # A month is flagged if it ENDED negative, or if it STARTED negative, which
    # is knowable because the prior month's ending balance is this month's open.
    for i, p in enumerate(usable):
        if endings[i] < 0:
            result.negative_months.append(_label(p.get("period")))
        elif i > 0 and endings[i - 1] < 0:
            # Opened negative even though it closed positive: still an overdraft
            # the desk needs to see, so record the month it carried into.
            label = _label(p.get("period"))
            if label not in result.negative_months:
                result.negative_months.append(label)

    if result.negative_months:
        result.reasons.append(
            "Balance went negative in " + ", ".join(result.negative_months) + "."
        )

    # --- 2. Trend -------------------------------------------------------------
    if len(endings) >= 2:
        first, last = endings[0], endings[-1]
        if first != 0:
            result.trend_pct = round((last - first) / abs(first), 4)

        # "Declining" means every step went down, not merely that the last month
        # is below the first. One dip inside an otherwise rising run is normal
        # business, and failing it would reject seasonal operators.
        steps_down = 0
        for a, b in zip(endings, endings[1:]):
            tolerance = abs(a) * _FLAT_TOLERANCE
            if b < a - tolerance:
                steps_down += 1
        result.declining = steps_down == len(endings) - 1

        if result.declining:
            span = f"{result.endings[0]:,.0f} to {result.endings[-1]:,.0f}"
            result.reasons.append(
                f"Ending balance fell every month ({span}). The desk needs flat or "
                "growing balances."
            )

    if not result.conclusive:
        result.reasons.append(
            f"Only {len(usable)} month of balances on file; {months} are needed to read a trend."
        )
        return result

    result.passed = not result.negative_months and not result.declining
    return result
