"""Module 10 — Pro-Forma HUD-1 Fee Map.

Auto-populates a draft HUD/Settlement Statement with standard CRE fees.
Fixed fees are placeholder estimates; broker can override via the HUD-1 tab.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.constants import (
    HUD_APPRAISAL_FEE_2_4_UNITS,
    HUD_APPRAISAL_FEE_SFR,
    HUD_INSPECTION_FEE_FLIPS,
    HUD_LENDER_UNDERWRITING_FEE,
    HUD_PROCESSING_FEE,
    HUD_RECORDING_FEES,
    HUD_TITLE_INSURANCE_RATE,
)
from app.enums import LoanType, PropertyType


@dataclass(frozen=True)
class HudLineItem:
    code: str
    label: str
    amount: float
    category: Literal["fixed", "variable"]
    editable: bool = True


@dataclass
class HudDraft:
    items: list[HudLineItem] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(i.amount for i in self.items), 2)


def appraisal_fee(property_type: PropertyType) -> int:
    """Higher fee for 2-4 unit properties."""
    if property_type == PropertyType.UNITS_2_4:
        return HUD_APPRAISAL_FEE_2_4_UNITS
    return HUD_APPRAISAL_FEE_SFR


def is_flip_or_bridge(loan_type: LoanType) -> bool:
    return loan_type in {LoanType.FIX_AND_FLIP, LoanType.BRIDGE, LoanType.GROUND_UP}


def build_hud_draft(
    loan_amount: float,
    property_type: PropertyType,
    loan_type: LoanType,
    broker_origination_dollars: float,
) -> HudDraft:
    """Compose a standard HUD-1 draft from the fee map."""
    items: list[HudLineItem] = [
        HudLineItem("orig", "Origination Points", round(broker_origination_dollars, 2), "variable"),
        HudLineItem("uw", "Lender Underwriting Fee", float(HUD_LENDER_UNDERWRITING_FEE), "fixed"),
        HudLineItem("proc", "Processing Fee", float(HUD_PROCESSING_FEE), "fixed"),
        HudLineItem("appr", "Appraisal Fee", float(appraisal_fee(property_type)), "fixed"),
        HudLineItem(
            "title",
            "Title Insurance Estimate",
            round(loan_amount * HUD_TITLE_INSURANCE_RATE, 2),
            "variable",
        ),
        HudLineItem("rec", "Recording Fees", float(HUD_RECORDING_FEES), "fixed"),
    ]
    if is_flip_or_bridge(loan_type):
        items.append(
            HudLineItem(
                "insp",
                "Environmental / Draw Inspection",
                float(HUD_INSPECTION_FEE_FLIPS),
                "fixed",
            )
        )
    return HudDraft(items=items)
