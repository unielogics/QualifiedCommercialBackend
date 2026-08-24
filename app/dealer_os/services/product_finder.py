"""Deterministic, self-reported product screening for the Field Desk.

This module deliberately does not participate in the application wizard. It
screens a prospect conversation and returns explainable, per-program results.
"""

from __future__ import annotations

from typing import Any


QUESTIONS: tuple[dict[str, Any], ...] = (
    {"key": "requested_amount", "kind": "money", "en": "How much funding are you looking for?", "es": "¿Cuánto financiamiento busca?"},
    {"key": "use_of_funds", "kind": "text", "en": "How will the funds be used?", "es": "¿Cómo se usarán los fondos?"},
    {"key": "industry", "kind": "select", "en": "What industry is the business in?", "es": "¿En qué industria opera el negocio?"},
    {"key": "years_in_business", "kind": "number", "en": "How many years has the business operated?", "es": "¿Cuántos años lleva operando el negocio?"},
    {"key": "annual_revenue", "kind": "money", "en": "What is the approximate annual revenue?", "es": "¿Cuál es el ingreso anual aproximado?"},
    {"key": "credit_tier", "kind": "select", "en": "What is the estimated credit tier?", "es": "¿Cuál es el nivel de crédito estimado?"},
    {"key": "owner_count", "kind": "number", "en": "How many individual owners are there?", "es": "¿Cuántos propietarios individuales hay?"},
    {"key": "mca_count", "kind": "number", "en": "How many MCA or SBA balances are outstanding?", "es": "¿Cuántos saldos MCA o SBA están pendientes?"},
    {"key": "youngest_mca_days", "kind": "number", "en": "How many days ago was the newest MCA or SBA funded?", "es": "¿Hace cuántos días se financió el MCA o SBA más reciente?"},
    {"key": "positive_month_end_count", "kind": "number", "en": "How many recent months ended with a positive bank balance?", "es": "¿Cuántos meses recientes terminaron con saldo bancario positivo?"},
    {"key": "nsf_count", "kind": "number", "en": "How many NSF charges appear in the review period?", "es": "¿Cuántos cargos NSF aparecen en el período revisado?"},
    {"key": "negative_balance_days", "kind": "number", "en": "How many negative-balance days occurred?", "es": "¿Cuántos días hubo saldo negativo?"},
    {"key": "business_dscr", "kind": "number", "en": "What is the estimated business DSCR, if known?", "es": "¿Cuál es el DSCR comercial estimado, si se conoce?"},
    {"key": "citizen_or_lpr", "kind": "boolean", "en": "Is the primary owner a U.S. citizen or legal permanent resident?", "es": "¿El propietario principal es ciudadano estadounidense o residente permanente legal?"},
    {"key": "bankruptcy_7y", "kind": "boolean", "en": "Has any required owner had a bankruptcy in the last 7 years?", "es": "¿Algún propietario requerido tuvo una bancarrota en los últimos 7 años?"},
    {"key": "bankruptcy_or_foreclosure_3y", "kind": "boolean", "en": "Has any required owner had a bankruptcy or foreclosure in the last 3 years?", "es": "¿Algún propietario requerido tuvo bancarrota o ejecución hipotecaria en los últimos 3 años?"},
    {"key": "felony_10y", "kind": "boolean", "en": "Has any required owner had a felony conviction in the last 10 years?", "es": "¿Algún propietario requerido tuvo una condena por delito grave en los últimos 10 años?"},
    {"key": "misdemeanor_5y", "kind": "boolean", "en": "Has any required owner had a misdemeanor in the last 5 years?", "es": "¿Algún propietario requerido tuvo un delito menor en los últimos 5 años?"},
    {"key": "open_tax_liens_or_judgments", "kind": "boolean", "en": "Are there open tax liens or judgments?", "es": "¿Hay gravámenes fiscales o sentencias abiertas?"},
    {"key": "ofac_match", "kind": "boolean", "en": "Is any required owner aware of an OFAC sanctions match?", "es": "¿Algún propietario requerido conoce una coincidencia con sanciones de OFAC?"},
    {"key": "active_legal_charges", "kind": "boolean", "en": "Are there active criminal or disqualifying legal charges?", "es": "¿Hay cargos penales activos u otros cargos legales descalificadores?"},
    {"key": "any_felony", "kind": "boolean", "en": "Has any required owner ever had a felony conviction?", "es": "¿Algún propietario requerido ha tenido una condena por delito grave?"},
    {"key": "active_ucc_count", "kind": "number", "en": "How many active UCC filings are outstanding?", "es": "¿Cuántos registros UCC activos están pendientes?"},
    {"key": "loan_to_revenue_pct", "kind": "number", "en": "What percentage of annual revenue is the requested amount?", "es": "¿Qué porcentaje del ingreso anual representa el monto solicitado?"},
    {"key": "official_bank_statements", "kind": "boolean", "en": "Can the business provide official downloaded bank statements?", "es": "¿Puede el negocio proporcionar estados bancarios oficiales descargados?"},
)


def _number(answers: dict[str, Any], key: str) -> float | None:
    value = answers.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reason(en: str, es: str, locale: str) -> str:
    return es if locale == "es" else en


def _credit_meets_660(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    normalized = str(value).lower().replace(" ", "_")
    if normalized in {"strong", "strong_credit", "720+", "good", "excellent"}:
        return True
    if normalized in {"below_threshold", "below", "under_620", "poor"}:
        return False
    if normalized in {"mid", "mid_credit", "620-719", "fair"}:
        return None
    try:
        return float(value) >= 660
    except (TypeError, ValueError):
        return None


def _result(program_key: str, name: str, amount_max: float, blocks: list[str], unresolved: list[str], strengths: list[str]) -> dict[str, Any]:
    status = "blocked" if blocks else "potential" if unresolved else "recommended"
    return {
        "program_key": program_key,
        "name": name,
        "status": status,
        "borrower_safe_reasons": blocks,
        "unresolved": unresolved,
        "strengths": strengths,
        "estimated_max_amount": amount_max,
        "verification": "Self-reported and unverified",
    }


def screen_products(answers: dict[str, Any], locale: str = "en") -> dict[str, Any]:
    """Evaluate exact v1 Quidity gates and leave the rest catalog-visible."""
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
    credit_ok = _credit_meets_660(answers.get("credit_tier"))
    industry = str(answers.get("industry") or "").lower()

    ez_blocks: list[str] = []
    ez_open: list[str] = []
    ez_strengths: list[str] = []
    if tib is None: ez_open.append(_reason("Confirm at least 2 years in business.", "Confirmar al menos 2 años en operación.", locale))
    elif tib < 2: ez_blocks.append(_reason("This program requires at least 2 years in business.", "Este programa requiere al menos 2 años en operación.", locale))
    else: ez_strengths.append(_reason("Time in business meets the stated minimum.", "El tiempo en operación cumple el mínimo indicado.", locale))
    if revenue is None: ez_open.append(_reason("Confirm at least $50,000 in annual revenue.", "Confirmar al menos $50,000 en ingresos anuales.", locale))
    elif revenue < 50000: ez_blocks.append(_reason("Annual revenue is below this program's stated minimum.", "Los ingresos anuales están por debajo del mínimo del programa.", locale))
    if credit_ok is None: ez_open.append(_reason("Verify the 660 minimum credit requirement.", "Verificar el requisito mínimo de crédito de 660.", locale))
    elif not credit_ok: ez_blocks.append(_reason("The estimated credit profile is below this program's requirement.", "El perfil de crédito estimado está por debajo del requisito del programa.", locale))
    if mcas is None: ez_open.append(_reason("Confirm outstanding MCA exposure.", "Confirmar la exposición MCA pendiente.", locale))
    elif mcas > 1: ez_blocks.append(_reason("This program permits no more than one outstanding MCA.", "Este programa permite no más de un MCA pendiente.", locale))
    if positive is None: ez_open.append(_reason("Confirm three positive month-end bank balances.", "Confirmar tres saldos bancarios positivos al cierre de mes.", locale))
    elif positive < 3: ez_blocks.append(_reason("Three positive month-end balances are required.", "Se requieren tres saldos positivos al cierre de mes.", locale))
    for key, en, es in (
        ("citizen_or_lpr", "Confirm U.S. citizen or permanent-resident status.", "Confirmar ciudadanía estadounidense o residencia permanente."),
        ("official_bank_statements", "Official downloaded bank statements are required.", "Se requieren estados bancarios oficiales descargados."),
    ):
        if answers.get(key) is None: ez_open.append(_reason(en, es, locale))
        elif answers.get(key) is False: ez_blocks.append(_reason(en, es, locale))
    if answers.get("bankruptcy_7y") is True: ez_blocks.append(_reason("A bankruptcy in the last 7 years blocks this program.", "Una bancarrota en los últimos 7 años bloquea este programa.", locale))
    if answers.get("felony_10y") is True: ez_blocks.append(_reason("A felony conviction in the last 10 years blocks this program.", "Una condena por delito grave en los últimos 10 años bloquea este programa.", locale))
    if answers.get("ofac_match") is None: ez_open.append(_reason("Confirm sanctions screening.", "Confirmar la revisión de sanciones.", locale))
    elif answers.get("ofac_match") is True: ez_blocks.append(_reason("A sanctions match blocks this program pending compliance review.", "Una coincidencia de sanciones bloquea este programa hasta revisión de cumplimiento.", locale))
    if answers.get("active_legal_charges") is None: ez_open.append(_reason("Confirm there are no active disqualifying legal charges.", "Confirmar que no haya cargos legales activos descalificadores.", locale))
    elif answers.get("active_legal_charges") is True: ez_blocks.append(_reason("Active disqualifying legal charges block this program.", "Los cargos legales activos descalificadores bloquean este programa.", locale))
    if any(term in industry for term in ("trucking", "warehouse", "auto dealer", "boat dealer", "medical lab", "speculative")):
        ez_blocks.append(_reason("The stated industry is excluded from this program.", "La industria indicada está excluida de este programa.", locale))

    micro_blocks: list[str] = []
    micro_open: list[str] = []
    micro_strengths: list[str] = []
    if tib is None: micro_open.append(_reason("Confirm at least 2 years in business.", "Confirmar al menos 2 años en operación.", locale))
    elif tib < 2: micro_blocks.append(_reason("This program requires at least 2 years in business.", "Este programa requiere al menos 2 años en operación.", locale))
    if credit_ok is None: micro_open.append(_reason("Verify the 660 minimum credit requirement.", "Verificar el requisito mínimo de crédito de 660.", locale))
    elif not credit_ok: micro_blocks.append(_reason("The estimated credit profile is below this program's requirement.", "El perfil de crédito estimado está por debajo del requisito del programa.", locale))
    if dscr is None: micro_open.append(_reason("Confirm business DSCR of at least 1.10x.", "Confirmar un DSCR comercial de al menos 1.10x.", locale))
    elif dscr < 1.1: micro_blocks.append(_reason("Business DSCR is below the 1.10x requirement.", "El DSCR comercial está por debajo del requisito de 1.10x.", locale))
    else: micro_strengths.append(_reason("Estimated DSCR meets the stated minimum.", "El DSCR estimado cumple el mínimo indicado.", locale))
    if owners is None: micro_open.append(_reason("Confirm the number of individual owners.", "Confirmar el número de propietarios individuales.", locale))
    elif owners > 5: micro_blocks.append(_reason("This program supports no more than five individual owners.", "Este programa admite no más de cinco propietarios individuales.", locale))
    if nsfs is None: micro_open.append(_reason("Confirm NSF activity.", "Confirmar la actividad NSF.", locale))
    elif nsfs > 2: micro_blocks.append(_reason("NSF activity exceeds this program's limit.", "La actividad NSF supera el límite del programa.", locale))
    if negative_days is None: micro_open.append(_reason("Confirm negative-balance days.", "Confirmar los días con saldo negativo.", locale))
    elif negative_days > 5: micro_blocks.append(_reason("Negative-balance days exceed this program's limit.", "Los días con saldo negativo superan el límite del programa.", locale))
    if mcas is None: micro_open.append(_reason("Confirm outstanding MCA or SBA exposure.", "Confirmar la exposición MCA o SBA pendiente.", locale))
    elif mcas > 2: micro_blocks.append(_reason("This program permits no more than two outstanding MCA or SBA balances.", "Este programa permite no más de dos saldos MCA o SBA pendientes.", locale))
    elif mcas > 0 and (mca_age is None or mca_age <= 90): micro_open.append(_reason("Confirm every outstanding MCA or SBA was funded more than 90 days ago.", "Confirmar que cada MCA o SBA pendiente fue financiado hace más de 90 días.", locale))
    if answers.get("bankruptcy_or_foreclosure_3y") is True: micro_blocks.append(_reason("A bankruptcy or foreclosure in the last 3 years blocks this program.", "Una bancarrota o ejecución hipotecaria en los últimos 3 años bloquea este programa.", locale))
    if answers.get("any_felony") is None: micro_open.append(_reason("Confirm required-owner felony history.", "Confirmar antecedentes de delitos graves de los propietarios requeridos.", locale))
    elif answers.get("any_felony") is True: micro_blocks.append(_reason("The disclosed felony history is outside this program's guidelines.", "Los antecedentes de delito grave informados están fuera de las pautas del programa.", locale))
    if answers.get("misdemeanor_5y") is True: micro_blocks.append(_reason("The disclosed recent background history is outside this program's guidelines.", "Los antecedentes recientes informados están fuera de las pautas del programa.", locale))
    if answers.get("open_tax_liens_or_judgments") is True: micro_blocks.append(_reason("Open tax liens or judgments block this program.", "Los gravámenes fiscales o sentencias abiertas bloquean este programa.", locale))
    ucc_count = _number(answers, "active_ucc_count")
    if ucc_count is None: micro_open.append(_reason("Confirm active UCC filings.", "Confirmar los registros UCC activos.", locale))
    elif ucc_count > 4: micro_blocks.append(_reason("Active UCC filings exceed this program's limit.", "Los registros UCC activos superan el límite del programa.", locale))
    loan_to_revenue = _number(answers, "loan_to_revenue_pct")
    if loan_to_revenue is None: micro_open.append(_reason("Confirm requested amount relative to annual revenue.", "Confirmar el monto solicitado en relación con el ingreso anual.", locale))
    elif loan_to_revenue > 50: micro_blocks.append(_reason("The requested amount is too high relative to stated annual revenue for this program.", "El monto solicitado es demasiado alto en relación con el ingreso anual indicado para este programa.", locale))
    if any(term in industry for term in ("trucking", "logistics", "auto dealer", "rv dealer", "boat dealer", "restaurant", "food service")):
        micro_blocks.append(_reason("The stated industry is excluded from this program.", "La industria indicada está excluida de este programa.", locale))

    programs = [
        _result("term_loan_3_5_year", _reason("EZ Term Loan", "Préstamo EZ a plazo", locale), 500000, ez_blocks, ez_open, ez_strengths),
        _result("term_loan_10_year", _reason("MicroCap Working Capital", "Capital de trabajo MicroCap", locale), 50000, micro_blocks, micro_open, micro_strengths),
    ]
    viable_max = max((p["estimated_max_amount"] for p in programs if p["status"] != "blocked"), default=0)
    recommended_amount = min(requested, viable_max) if requested and viable_max else viable_max or None
    unanswered = [q for q in QUESTIONS if answers.get(q["key"]) in (None, "")]
    return {
        "source": "self_reported",
        "verification": _reason("Preliminary and unverified", "Preliminar y sin verificar", locale),
        "client_requested_amount": requested or None,
        "recommended_amount": recommended_amount,
        "amount_adjustment_required": bool(requested and viable_max and requested > viable_max),
        "recommended": [p for p in programs if p["status"] == "recommended"],
        "potential": [p for p in programs if p["status"] == "potential"],
        "blocked": [p for p in programs if p["status"] == "blocked"],
        "next_question": unanswered[0] if unanswered else None,
        "evaluated_programs": programs,
    }
