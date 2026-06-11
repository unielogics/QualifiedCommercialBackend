from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.property_intelligence import PropertyIntelligenceSnapshot
from app.services.ai.anthropic_client import get_client, model_light
from app.services.ai.usage import tracked_messages_create
from app.services.provider_secrets import runtime_settings


def _num(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
        try:
            if value is not None and str(value).strip() != "":
                return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _range_from_estimate(raw: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    if not raw:
        return None
    value = _num(raw.get(key), raw.get("price"), raw.get("rent"))
    low = _num(raw.get(f"{key}RangeLow"), raw.get("priceRangeLow"), raw.get("rentRangeLow"))
    high = _num(raw.get(f"{key}RangeHigh"), raw.get("priceRangeHigh"), raw.get("rentRangeHigh"))
    if value is None and low is None and high is None:
        return None
    return {"estimate": value, "low": low, "high": high}


def build_deterministic_report(
    *,
    product: str,
    inputs: dict[str, Any],
    calculator_output: dict[str, Any] | None,
    snapshot: PropertyIntelligenceSnapshot | None,
) -> dict[str, Any]:
    calc = calculator_output or {}
    value_range = _range_from_estimate(snapshot.rentcast_value if snapshot else None, "value")
    rent_range = _range_from_estimate(snapshot.rentcast_rent if snapshot else None, "rent")
    requested = _num(inputs.get("requested_loan_amount"), inputs.get("loan_amount"), calc.get("loan_amount"))
    purchase = _num(inputs.get("purchase_price"), inputs.get("market_value"), inputs.get("arv"), inputs.get("property_value"))
    rent = _num(inputs.get("monthly_rent"), calc.get("monthly_rent"), (rent_range or {}).get("estimate"))
    dscr = _num(calc.get("dscr"), inputs.get("dscr"))
    rate = _num(calc.get("final_rate"), inputs.get("rate"), inputs.get("base_rate"))
    ltv = requested / purchase if requested and purchase else _num(calc.get("ltv"), inputs.get("ltv"))
    flood = (snapshot.fema_flood or {}).get("primary") if snapshot and snapshot.fema_flood else None
    strengths: list[str] = []
    weaknesses: list[str] = []
    if rent and requested:
        strengths.append("Rent and loan request are available for DSCR screening.")
    if value_range:
        strengths.append("Automated value support is available from property data.")
    if rent_range:
        strengths.append("Automated rent support is available from rental comps.")
    if dscr is not None and dscr >= 1.1:
        strengths.append(f"Modeled DSCR is {dscr:.2f}x.")
    if dscr is not None and dscr < 1.0:
        weaknesses.append(f"Modeled DSCR is below 1.00x at {dscr:.2f}x.")
    if ltv is not None and ltv > 0.8:
        weaknesses.append(f"Leverage is high at {ltv * 100:.1f}% LTV.")
    if flood and flood.get("SFHA_TF") == "T":
        weaknesses.append(f"FEMA flood lookup indicates special flood hazard zone {flood.get('FLD_ZONE')}.")
    status = "Likely eligible" if dscr is not None and dscr >= 1.0 and (ltv is None or ltv <= 0.8) else "Needs review"
    confidence = "Medium-High" if value_range and rent_range else "Medium" if value_range or rent_range else "Low"
    return {
        "status": status,
        "product": product,
        "estimated_market_value": value_range,
        "estimated_rent": rent_range,
        "recommended_underwritten_rent": rent,
        "requested_loan_amount": requested,
        "purchase_or_value": purchase,
        "ltv": ltv,
        "rate": rate,
        "dscr": dscr,
        "flood_zone": flood,
        "strengths": strengths[:6],
        "weaknesses": weaknesses[:6],
        "confidence": confidence,
        "narrative": (
            "Preliminary analysis based on borrower inputs, calculator output, and available third-party "
            "property intelligence. This is not an appraisal, rate lock, commitment to lend, or final underwriting decision."
        ),
    }


def sanitize_report(report: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "product",
        "estimated_market_value",
        "estimated_rent",
        "recommended_underwritten_rent",
        "requested_loan_amount",
        "purchase_or_value",
        "ltv",
        "rate",
        "dscr",
        "flood_zone",
        "strengths",
        "weaknesses",
        "confidence",
        "narrative",
    }
    out = {k: v for k, v in report.items() if k in allowed}
    out["disclaimer"] = (
        "Estimate only. Not an appraisal, not a rate lock, not a commitment to lend, "
        "and subject to appraisal, title, insurance, income/rent verification, lender review, and final underwriting."
    )
    return out


async def generate_analysis_report(
    db: AsyncSession,
    *,
    product: str,
    inputs: dict[str, Any],
    calculator_output: dict[str, Any] | None,
    snapshot: PropertyIntelligenceSnapshot | None,
    user,
    client_id=None,
    loan_id=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = build_deterministic_report(
        product=product,
        inputs=inputs,
        calculator_output=calculator_output,
        snapshot=snapshot,
    )
    runtime = await runtime_settings(db)
    settings = get_settings()
    if not runtime.property_analysis_ai_enabled or not settings.anthropic_api_key:
        return base, sanitize_report(base)
    prompt_payload = {
        "property": snapshot.address if snapshot else {},
        "rent_estimate": snapshot.rentcast_rent if snapshot else None,
        "value_estimate": snapshot.rentcast_value if snapshot else None,
        "sales_comps": (snapshot.rentcast_value or {}).get("comparables") if snapshot and snapshot.rentcast_value else [],
        "rental_comps": (snapshot.rentcast_rent or {}).get("comparables") if snapshot and snapshot.rentcast_rent else [],
        "fema_flood": snapshot.fema_flood if snapshot else None,
        "loan_request": inputs,
        "calculator_output": calculator_output,
        "base_report": base,
    }
    try:
        resp = await tracked_messages_create(
            db,
            feature="property_analysis",
            client=get_client(),
            model=model_light(),
            max_tokens=1200,
            temperature=0.1,
            user_id=getattr(user, "id", None),
            broker_id=getattr(getattr(user, "broker", None), "id", None),
            client_id=client_id,
            loan_id=loan_id,
            system=(
                "You are a commercial mortgage analyst. Return strict JSON only. "
                "Do not make a lending commitment. Use cautious ranges and cite uncertainty. "
                "Output keys: status, estimated_market_value, estimated_rent, "
                "recommended_underwritten_rent, strengths, weaknesses, confidence, narrative."
            ),
            messages=[{"role": "user", "content": json.dumps(prompt_payload, default=str)[:18000]}],
        )
        text = "".join(getattr(block, "text", "") for block in getattr(resp, "content", [])).strip()
        ai = json.loads(text)
        if isinstance(ai, dict):
            merged = {**base, **ai, "product": product}
            return merged, sanitize_report(merged)
    except Exception:
        pass
    return base, sanitize_report(base)
