"""Borrower-safe QC credit quality labels.

The exact bureau score remains internal. Staff and generated documents receive
only the QC quality classification and its corresponding score range.
"""

from __future__ import annotations

from typing import TypedDict


class CreditQualitySummary(TypedDict):
    quality_tier: str
    score_band: str


_QUALITY_BANDS: tuple[tuple[int, int, str], ...] = (
    (760, 850, "Excellent"),
    (720, 759, "Good"),
    (700, 719, "Average"),
    (680, 699, "Below average"),
    (660, 679, "Bad"),
    (300, 659, "Not fundable"),
)


def summary(score: int | None) -> CreditQualitySummary | None:
    """Return the QC classification and range without exposing the score."""
    if score is None:
        return None
    normalized = int(score)
    for lower, upper, label in _QUALITY_BANDS:
        if lower <= normalized <= upper:
            return {
                "quality_tier": label,
                "score_band": f"{lower}\u2013{upper}",
            }
    return None


def classification(score: int | None) -> str | None:
    result = summary(score)
    return result["quality_tier"] if result else None


def score_range(score: int | None) -> str | None:
    result = summary(score)
    return result["score_band"] if result else None
