"""Explainable Product Finder screening for Field Desk conversations.

EZ Term and MicroCap are the only products with deterministic hard gates.
Every other catalog product remains advisory until verified evidence and a
human underwriter confirm fit. Canonical NAICS codes drive industry rules.
"""

from __future__ import annotations

from typing import Any

QUESTIONS: tuple[dict[str, Any], ...] = (
    {"key": "use_of_funds", "kind": "text", "en": "Describe exactly how the funds will be used.", "es": "Describa exactamente como se usaran los fondos."},
    {"key": "years_in_business", "kind": "number", "en": "How many years has the business operated?", "es": "¿Cuantos anos lleva operando el negocio?"},
    {"key": "annual_revenue", "kind": "money", "en": "What is the approximate annual revenue?", "es": "¿Cual es el ingreso anual aproximado?"},
    {"key": "citizen_or_lpr", "kind": "boolean", "en": "Is the primary owner a U.S. citizen or legal permanent resident?", "es": "¿El propietario principal es ciudadano estadounidense o residente permanente legal?"},
    {"key": "primary_owner_credit_660_or_higher", "kind": "boolean", "en": "Is the primary owner's estimated credit 660 or higher?", "es": "¿El credito estimado del propietario principal es 660 o mayor?"},
    {"key": "bankruptcy_timing", "kind": "select", "options": ["none", "within_3_years", "4_to_7_years", "more_than_7_years"], "en": "When was the primary owner's most recent bankruptcy?", "es": "¿Cuando fue la bancarrota mas reciente del propietario principal?"},
    {"key": "foreclosure_within_3_years", "kind": "boolean", "en": "Has the primary owner had a foreclosure in the last 3 years?", "es": "¿El propietario principal tuvo una ejecucion hipotecaria en los ultimos 3 anos?"},
    {"key": "felony_timing", "kind": "select", "options": ["none", "within_10_years", "more_than_10_years"], "en": "When was the primary owner's most recent felony conviction?", "es": "¿Cuando fue la condena por delito grave mas reciente del propietario principal?"},
    {"key": "owner_count", "kind": "number", "en": "How many individual owners are there?", "es": "¿Cuantos propietarios individuales hay?"},
    {"key": "debt_refinance", "kind": "boolean", "en": "Will any portion of the funds refinance existing debt?", "es": "¿Se usara alguna parte de los fondos para refinanciar deuda existente?"},
    {"key": "mca_count", "kind": "number", "en": "How many MCA or SBA balances are outstanding?", "es": "¿Cuantos saldos MCA o SBA estan pendientes?"},
    {"key": "youngest_mca_days", "kind": "number", "en": "How many days ago was the newest MCA or SBA funded?", "es": "¿Hace cuantos dias se financio el MCA o SBA mas reciente?"},
    {"key": "active_ucc_count", "kind": "number", "en": "How many active UCC filings are outstanding?", "es": "¿Cuantos registros UCC activos estan pendientes?"},
    {"key": "positive_month_end_count", "kind": "number", "en": "How many recent months ended with a positive bank balance?", "es": "¿Cuantos meses recientes terminaron con saldo bancario positivo?"},
    {"key": "nsf_count", "kind": "number", "en": "How many NSF charges appear in the review period?", "es": "¿Cuantos cargos NSF aparecen en el periodo revisado?"},
    {"key": "negative_balance_days", "kind": "number", "en": "How many negative-balance days occurred?", "es": "¿Cuantos dias hubo saldo negativo?"},
    {"key": "official_bank_statements", "kind": "boolean", "en": "Can the business provide official downloaded bank statements?", "es": "¿Puede el negocio proporcionar estados bancarios oficiales descargados?"},
    {"key": "business_dscr", "kind": "number", "en": "What is the estimated business DSCR, if known?", "es": "¿Cual es el DSCR comercial estimado, si se conoce?"},
    {"key": "misdemeanor_5y", "kind": "boolean", "en": "Has the primary owner had a misdemeanor in the last 5 years?", "es": "¿El propietario principal tuvo un delito menor en los ultimos 5 anos?"},
    {"key": "open_tax_liens_or_judgments", "kind": "boolean", "en": "Are there open tax liens or judgments?", "es": "¿Hay gravamenes fiscales o sentencias abiertas?"},
    {"key": "ofac_match", "kind": "boolean", "en": "Is the primary owner aware of an OFAC sanctions match?", "es": "¿El propietario principal conoce una coincidencia con sanciones de OFAC?"},
    {"key": "active_legal_charges", "kind": "boolean", "en": "Are there active criminal or disqualifying legal charges?", "es": "¿Hay cargos penales activos u otros cargos legales descalificadores?"},
    {"key": "real_estate_involved", "kind": "boolean", "en": "Does the request involve purchasing, refinancing, improving, or leveraging real estate?", "es": "¿La solicitud implica comprar, refinanciar, mejorar o aprovechar bienes raices?"},
    {"key": "real_estate_purpose", "kind": "select", "options": ["purchase", "refinance", "cash_out", "construction", "other"], "en": "What is the real-estate purpose?", "es": "¿Cual es el proposito relacionado con los bienes raices?"},
    {"key": "owned_real_estate_available", "kind": "boolean", "en": "Can owned real estate support the request as collateral?", "es": "¿Puede un inmueble propio respaldar la solicitud como garantia?"},
)

ADVISORY_PROGRAMS: tuple[tuple[str, str, str], ...] = (
    ("line_of_credit", "Business Line of Credit", "Linea de credito comercial"),
    ("term_loan_loc_hybrid", "Hybrid Term / LOC", "Hibrido de plazo y linea"),
    ("equipment_financing", "Equipment Financing", "Financiamiento de equipo"),
    ("jumbo_term_loan", "Jumbo Term Loan", "Prestamo jumbo a plazo"),
    ("transportation_finance", "Transportation Finance", "Financiamiento de transporte"),
    ("sba", "SBA 7(a)", "SBA 7(a)"),
    ("sba_grocery", "SBA Grocery", "SBA para supermercados"),
    ("sba_made_in_america", "SBA Made in America", "SBA Hecho en EE. UU."),
)


def _number(answers: dict[str, Any], key: str) -> float | None:
    try:
        value = answers.get(key)
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _reason(en: str, es: str, locale: str) -> str:
    return es if locale == "es" else en


def _credit_meets_660(answers: dict[str, Any]) -> bool | None:
    direct = answers.get("primary_owner_credit_660_or_higher")
    if isinstance(direct, bool):
        return direct
    value = answers.get("credit_tier")
    if value in (None, ""):
        return None
    normalized = str(value).lower().replace(" ", "_")
    if normalized in {"strong", "strong_credit", "720+", "good", "excellent"}:
        return True
    if normalized in {"below_threshold", "below", "under_620", "poor"}:
        return False
    try:
        return float(value) >= 660
    except (TypeError, ValueError):
        return None


def _canonical_naics(answers: dict[str, Any]) -> str | None:
    # Pending community contributions remain advisory until admin-approved.
    if str(answers.get("taxonomy_status") or "official") == "pending":
        return None
    digits = "".join(char for char in str(answers.get("naics_code") or "") if char.isdigit())
    return digits[:6] or None


def _industry_excluded(naics: str | None, *, program: str) -> bool:
    if not naics:
        return False
    sector = naics[:2]
    if program == "ez":
        return sector in {"48", "49"} or naics.startswith(
            ("441110", "441120", "441210", "441222", "621511")
        )
    return sector in {"48", "49"} or naics.startswith(
        ("441110", "441120", "441210", "441222", "7225")
    )


def _program_result(
    key: str,
    name: str,
    maximum: float,
    blocks: list[str],
    unresolved: list[str],
    strengths: list[str],
) -> dict[str, Any]:
    status = "blocked" if blocks else "potential" if unresolved else "recommended"
    return {
        "program_key": key,
        "name": name,
        "status": status,
        "decision_type": "deterministic",
        "borrower_safe_reasons": blocks,
        "unresolved": unresolved,
        "strengths": strengths,
        "estimated_max_amount": maximum,
        "verification": "Self-reported and unverified",
    }


def _properties(answers: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_value = 0.0
    total_debt = 0.0
    raw_rows = answers.get("properties")
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, dict):
            continue
        value = _safe_number(raw.get("estimated_value"))
        debt = _safe_number(raw.get("amount_owed"))
        equity = value - debt if value is not None and debt is not None else None
        ltv = debt / value if value and debt is not None else None
        total_value += value or 0
        total_debt += debt or 0
        rows.append({
            "address": raw.get("address"),
            "property_type": raw.get("property_type"),
            "estimated_value": value,
            "amount_owed": debt,
            "stated_equity": round(equity, 2) if equity is not None else None,
            "current_ltv": round(ltv, 4) if ltv is not None else None,
            "verification": "Self-reported and unverified",
        })
    return {
        "properties": rows,
        "property_count": len(rows),
        "total_estimated_value": round(total_value, 2) if rows else None,
        "total_amount_owed": round(total_debt, 2) if rows else None,
        "total_stated_equity": round(total_value - total_debt, 2) if rows else None,
        "portfolio_ltv": round(total_debt / total_value, 4) if total_value else None,
        "verification": "Self-reported and unverified",
    }


def _advisory_results(
    answers: dict[str, Any], locale: str, naics: str | None, real_estate: dict[str, Any]
) -> list[dict[str, Any]]:
    requested = _number(answers, "requested_amount") or 0
    use = str(answers.get("use_of_funds") or "").lower()
    real_estate_signal = bool(
        answers.get("real_estate_involved")
        or answers.get("owned_real_estate_available")
        or real_estate["property_count"]
    )
    output: list[dict[str, Any]] = []
    for key, en_name, es_name in ADVISORY_PROGRAMS:
        why: str | None = None
        if key == "line_of_credit" and any(word in use for word in ("working capital", "inventory", "seasonal")):
            why = _reason("Use of funds may fit revolving working capital.", "El uso de fondos puede ajustarse a capital rotativo.", locale)
        elif key == "term_loan_loc_hybrid" and requested >= 100_000:
            why = _reason("The request size may support a blended term and revolving structure.", "El monto puede admitir una estructura combinada.", locale)
        elif key == "equipment_financing" and any(word in use for word in ("equipment", "machinery", "vehicle")):
            why = _reason("The stated use includes financeable equipment.", "El uso declarado incluye equipo financiable.", locale)
        elif key == "jumbo_term_loan" and requested >= 1_000_000:
            why = _reason("The stated request is in the larger-capital range.", "La solicitud esta en el rango de capital mayor.", locale)
        elif key == "transportation_finance" and naics and naics[:2] in {"48", "49"}:
            why = _reason("The canonical NAICS sector indicates transportation or warehousing.", "El sector NAICS indica transporte o almacenamiento.", locale)
        elif key == "sba" and (real_estate_signal or requested > 500_000):
            why = _reason("Real estate or request size may justify an SBA review.", "Los bienes raices o el monto pueden justificar una revision SBA.", locale)
        elif key == "sba_grocery" and naics and naics.startswith("445"):
            why = _reason("The canonical NAICS code indicates a grocery or food retailer.", "El codigo NAICS indica un minorista de alimentos.", locale)
        elif key == "sba_made_in_america" and naics and naics[:2] in {"31", "32", "33"}:
            why = _reason("The canonical NAICS sector indicates manufacturing.", "El sector NAICS indica manufactura.", locale)
        if why:
            output.append({
                "program_key": key,
                "name": es_name if locale == "es" else en_name,
                "status": "advisory",
                "decision_type": "advisory",
                "borrower_safe_reasons": [],
                "unresolved": [_reason("Program terms require underwriter review.", "Los terminos requieren revision del suscriptor.", locale)],
                "strengths": [why],
                "estimated_max_amount": None,
                "verification": _reason("Advisory, self-reported, and unverified", "Orientativo, autodeclarado y sin verificar", locale),
            })
    return output


def _common_owner_gates(
    answers: dict[str, Any], locale: str
) -> tuple[list[str], list[str], list[str], list[str]]:
    ez_blocks: list[str] = []
    ez_open: list[str] = []
    micro_blocks: list[str] = []
    micro_open: list[str] = []
    credit_ok = _credit_meets_660(answers)
    if credit_ok is None:
        message = _reason("Confirm the primary owner's estimated 660 credit threshold.", "Confirmar el umbral estimado de credito de 660.", locale)
        ez_open.append(message); micro_open.append(message)
    elif not credit_ok:
        message = _reason("The primary owner's estimated credit is below the 660 threshold.", "El credito estimado esta por debajo del umbral de 660.", locale)
        ez_blocks.append(message); micro_blocks.append(message)
    if answers.get("citizen_or_lpr") is None:
        message = _reason("Confirm U.S. citizen or permanent-resident status.", "Confirmar ciudadania o residencia permanente.", locale)
        ez_open.append(message); micro_open.append(message)
    elif answers.get("citizen_or_lpr") is False:
        message = _reason("The disclosed residency status does not meet the program requirement.", "El estatus migratorio no cumple el requisito.", locale)
        ez_blocks.append(message); micro_blocks.append(message)
    bankruptcy = str(answers.get("bankruptcy_timing") or "")
    if bankruptcy in {"within_3_years", "4_to_7_years"} or answers.get("bankruptcy_7y") is True:
        ez_blocks.append(_reason("A bankruptcy in the last 7 years blocks EZ Term.", "Una bancarrota en los ultimos 7 anos bloquea EZ Term.", locale))
    if bankruptcy == "within_3_years" or answers.get("bankruptcy_or_foreclosure_3y") is True:
        micro_blocks.append(_reason("A bankruptcy in the last 3 years blocks MicroCap.", "Una bancarrota en los ultimos 3 anos bloquea MicroCap.", locale))
    if answers.get("foreclosure_within_3_years") is True:
        micro_blocks.append(_reason("A foreclosure in the last 3 years blocks MicroCap.", "Una ejecucion hipotecaria en los ultimos 3 anos bloquea MicroCap.", locale))
    felony = str(answers.get("felony_timing") or "")
    if felony == "within_10_years" or answers.get("felony_10y") is True:
        ez_blocks.append(_reason("A felony conviction in the last 10 years blocks EZ Term.", "Un delito grave en los ultimos 10 anos bloquea EZ Term.", locale))
    if felony in {"within_10_years", "more_than_10_years"} or answers.get("any_felony") is True:
        micro_blocks.append(_reason("The disclosed felony history blocks MicroCap.", "Los antecedentes de delito grave bloquean MicroCap.", locale))
    return ez_blocks, ez_open, micro_blocks, micro_open


def screen_products(answers: dict[str, Any], locale: str = "en") -> dict[str, Any]:
    """Apply exact EZ/Micro gates and advisory signals for other products."""
    locale = "es" if locale == "es" else "en"
    requested = _number(answers, "requested_amount") or 0
    tib = _number(answers, "years_in_business")
    revenue = _number(answers, "annual_revenue")
    owners = _number(answers, "owner_count")
    mcas = _number(answers, "mca_count")
    mca_age = _number(answers, "youngest_mca_days")
    positive = _number(answers, "positive_month_end_count")
    nsfs = _number(answers, "nsf_count")
    negative_days = _number(answers, "negative_balance_days")
    dscr = _number(answers, "business_dscr")
    naics = _canonical_naics(answers)
    ez_blocks, ez_open, micro_blocks, micro_open = _common_owner_gates(answers, locale)
    ez_strengths: list[str] = []
    micro_strengths: list[str] = []

    if requested and not 25_000 <= requested <= 500_000:
        ez_blocks.append(_reason("The requested amount is outside the $25,000-$500,000 range.", "El monto esta fuera del rango de $25,000-$500,000.", locale))
    if requested and not 15_000 <= requested <= 50_000:
        micro_blocks.append(_reason("The requested amount is outside the $15,000-$50,000 range.", "El monto esta fuera del rango de $15,000-$50,000.", locale))
    if answers.get("debt_refinance") is True:
        micro_blocks.append(_reason("MicroCap is working-capital only and cannot refinance debt.", "MicroCap es solo para capital de trabajo y no refinancia deuda.", locale))

    if tib is None:
        message = _reason("Confirm at least 2 years in business.", "Confirmar al menos 2 anos en operacion.", locale)
        ez_open.append(message); micro_open.append(message)
    elif tib < 2:
        message = _reason("At least 2 years in business are required.", "Se requieren al menos 2 anos en operacion.", locale)
        ez_blocks.append(message); micro_blocks.append(message)
    else:
        ez_strengths.append(_reason("Time in business meets the stated minimum.", "El tiempo en operacion cumple el minimo.", locale))
    if revenue is None:
        ez_open.append(_reason("Confirm at least $50,000 in annual revenue.", "Confirmar al menos $50,000 en ingresos anuales.", locale))
    elif revenue < 50_000:
        ez_blocks.append(_reason("Annual revenue is below the EZ Term minimum.", "Los ingresos estan por debajo del minimo de EZ Term.", locale))
    if mcas is None:
        message = _reason("Confirm outstanding MCA/SBA exposure.", "Confirmar la exposicion MCA/SBA.", locale)
        ez_open.append(message); micro_open.append(message)
    else:
        if mcas > 1:
            ez_blocks.append(_reason("EZ Term permits no more than one outstanding MCA.", "EZ Term permite no mas de un MCA pendiente.", locale))
        if mcas > 2:
            micro_blocks.append(_reason("MicroCap permits no more than two outstanding MCA/SBA balances.", "MicroCap permite no mas de dos saldos MCA/SBA.", locale))
        elif mcas > 0 and (mca_age is None or mca_age <= 90):
            micro_open.append(_reason("Confirm every MCA/SBA was funded more than 90 days ago.", "Confirmar que cada MCA/SBA fue financiado hace mas de 90 dias.", locale))
    if positive is None:
        ez_open.append(_reason("Confirm three positive month-end balances.", "Confirmar tres saldos positivos al cierre de mes.", locale))
    elif positive < 3:
        ez_blocks.append(_reason("EZ Term requires three positive month-end balances.", "EZ Term requiere tres saldos positivos al cierre de mes.", locale))
    if dscr is None:
        micro_open.append(_reason("Confirm business DSCR of at least 1.10x.", "Confirmar un DSCR de al menos 1.10x.", locale))
    elif dscr < 1.1:
        micro_blocks.append(_reason("Business DSCR is below 1.10x.", "El DSCR esta por debajo de 1.10x.", locale))
    else:
        micro_strengths.append(_reason("Estimated DSCR meets the minimum.", "El DSCR estimado cumple el minimo.", locale))
    if owners is None:
        micro_open.append(_reason("Confirm the number of owners.", "Confirmar el numero de propietarios.", locale))
    elif owners > 5:
        micro_blocks.append(_reason("MicroCap supports no more than five owners.", "MicroCap admite no mas de cinco propietarios.", locale))
    if nsfs is None:
        micro_open.append(_reason("Confirm NSF activity.", "Confirmar la actividad NSF.", locale))
    elif nsfs > 2:
        micro_blocks.append(_reason("NSF activity exceeds the MicroCap limit.", "La actividad NSF supera el limite de MicroCap.", locale))
    if negative_days is None:
        micro_open.append(_reason("Confirm negative-balance days.", "Confirmar los dias con saldo negativo.", locale))
    elif negative_days > 5:
        micro_blocks.append(_reason("Negative-balance days exceed the MicroCap limit.", "Los dias con saldo negativo superan el limite de MicroCap.", locale))
    if answers.get("official_bank_statements") is None:
        ez_open.append(_reason("Confirm official downloaded bank statements are available.", "Confirmar que hay estados bancarios oficiales.", locale))
    elif answers.get("official_bank_statements") is False:
        ez_blocks.append(_reason("EZ Term requires official downloaded bank statements.", "EZ Term requiere estados bancarios oficiales.", locale))
    if answers.get("misdemeanor_5y") is True:
        micro_blocks.append(_reason("The disclosed recent background history is outside MicroCap guidelines.", "Los antecedentes recientes estan fuera de las pautas de MicroCap.", locale))
    if answers.get("open_tax_liens_or_judgments") is True:
        micro_blocks.append(_reason("Open tax liens or judgments block MicroCap.", "Los gravamenes fiscales o sentencias abiertas bloquean MicroCap.", locale))
    if answers.get("ofac_match") is True or answers.get("active_legal_charges") is True:
        ez_blocks.append(_reason("The disclosed compliance issue requires review outside EZ Term.", "El asunto de cumplimiento requiere revision fuera de EZ Term.", locale))
    uccs = _number(answers, "active_ucc_count")
    if uccs is None:
        micro_open.append(_reason("Confirm active UCC filings.", "Confirmar los registros UCC activos.", locale))
    elif uccs > 4:
        micro_blocks.append(_reason("Active UCC filings exceed the MicroCap limit.", "Los registros UCC superan el limite de MicroCap.", locale))
    if _industry_excluded(naics, program="ez"):
        ez_blocks.append(_reason("The selected business activity is excluded from EZ Term.", "La actividad seleccionada esta excluida de EZ Term.", locale))
    if _industry_excluded(naics, program="micro"):
        micro_blocks.append(_reason("The selected business activity is excluded from MicroCap.", "La actividad seleccionada esta excluida de MicroCap.", locale))

    exact = [
        _program_result("term_loan_3_5_year", _reason("EZ Term Loan", "Prestamo EZ a plazo", locale), 500_000, ez_blocks, ez_open, ez_strengths),
        _program_result("term_loan_10_year", _reason("MicroCap Working Capital", "Capital de trabajo MicroCap", locale), 50_000, micro_blocks, micro_open, micro_strengths),
    ]
    real_estate = _properties(answers)
    advisory = _advisory_results(answers, locale, naics, real_estate)
    viable_max = max((row["estimated_max_amount"] for row in exact if row["status"] != "blocked"), default=0)
    recommended_amount = min(requested, viable_max) if requested and viable_max else viable_max or None
    unanswered = []
    for question in QUESTIONS:
        key = question["key"]
        if key in {"real_estate_purpose", "owned_real_estate_available"} and answers.get("real_estate_involved") is False:
            continue
        if key == "youngest_mca_days" and _number(answers, "mca_count") == 0:
            continue
        if answers.get(key) in (None, ""):
            unanswered.append(question)
    return {
        "source": "self_reported",
        "screening_scope": "primary_owner",
        "verification": _reason("Primary-owner, self-reported, and unverified", "Propietario principal, autodeclarado y sin verificar", locale),
        "client_requested_amount": requested or None,
        "recommended_amount": recommended_amount,
        "amount_adjustment_required": bool(requested and viable_max and requested > viable_max),
        "recommended": [row for row in exact if row["status"] == "recommended"],
        "potential": [row for row in exact if row["status"] == "potential"],
        "blocked": [row for row in exact if row["status"] == "blocked"],
        "advisory": advisory,
        "real_estate_analysis": real_estate,
        "canonical_naics_code": naics,
        "next_question": unanswered[0] if unanswered else None,
        "evaluated_programs": exact + advisory,
    }
