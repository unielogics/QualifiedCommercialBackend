"""Prefill a Production Arrangement from what the file already knows.

Precedence (an earlier non-empty value wins): the Dealer OS file when the
profile is dealer-backed, its application profile, the owners, the AI intake
(columns and intake_state), the AI review's key metrics, the application
profile's classification, and the acting user as relationship manager. The
sponsor block is never prefilled here — it is copied from the chosen
company by the orchestration layer.

Every prefilled value carries provenance so the editor can see where it came
from and confirm or change it; a required field that is prefilled but not
confirmed still counts as a blank for the send gate.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerApplicationProfile, DealerBusiness
from app.models.application_profile import ApplicationExtractedFact, ApplicationProfile
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.services import application_profiles as profiles
from app.services import production_arrangement as pa

SOURCE_LABELS: dict[str, str] = {
    "dealer": "Dealer file",
    "dealer_profile": "Dealer application profile",
    "owners": "Owners",
    "intake": "AI intake",
    "review": "AI review",
    "profile": "Application profile",
    "user": "Relationship manager (you)",
    "derived": "Derived",
    "sponsor": "Sponsor agreement",
    "document_extraction": "Read from an uploaded document",
}

_ENTITY_ALIASES: dict[str, str] = {
    "llc": "Limited liability company",
    "limited liability company": "Limited liability company",
    "limited_liability_company": "Limited liability company",
    "corporation": "Corporation",
    "corp": "Corporation",
    "c_corp": "Corporation",
    "c corporation": "Corporation",
    "c-corp": "Corporation",
    "inc": "Corporation",
    "s_corp": "S corporation",
    "s corporation": "S corporation",
    "s-corp": "S corporation",
    "partnership": "Limited partnership",
    "lp": "Limited partnership",
    "limited partnership": "Limited partnership",
    "llp": "Limited liability partnership",
    "limited liability partnership": "Limited liability partnership",
    "sole_proprietorship": "Sole proprietorship",
    "sole proprietorship": "Sole proprietorship",
    "sole_prop": "Sole proprietorship",
    "sole proprietor": "Sole proprietorship",
    "trust": "Trust",
}


def normalize_entity_type(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    key = text.lower().replace(".", "")
    if key in _ENTITY_ALIASES:
        return _ENTITY_ALIASES[key]
    for option in pa.ENTITY_TYPES:
        if option.lower() == key:
            return option
    return text


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for k in ("name", "legal_name", "value", "label"):
            if value.get(k):
                return str(value[k]).strip()
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for k in ("value", "amount", "monthly", "number"):
            if k in value:
                return _number(value[k])
        return None
    try:
        text = str(value).replace("$", "").replace(",", "").strip()
        if text.endswith("%"):
            text = text[:-1]
        n = float(text)
    except (TypeError, ValueError):
        return None
    return n


def compose_address(*parts: Any) -> str:
    street, city, state, zip_code = (list(parts) + [None] * 4)[:4]
    line = ", ".join(p for p in (_text(street), _text(city)) if p)
    tail = " ".join(p for p in (_text(state), _text(zip_code)) if p)
    return ", ".join(p for p in (line, tail) if p)


def trailing_twelve_months(today: date | None = None) -> tuple[str, str]:
    """Baseline window: the twelve full months before the current one."""
    today = today or date.today()
    end_year, end_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    start_year, start_month = (end_year - 1, end_month + 1) if end_month < 12 else (end_year, 1)
    start = date(start_year, start_month, 1)
    return start.isoformat(), end.isoformat()


@dataclass
class PrefillResult:
    values: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def put(self, key: str, value: Any, source: str) -> None:
        if key in self.values:
            return
        if value is None or (isinstance(value, str) and not value.strip()):
            return
        if isinstance(value, float) and value == 0:
            return
        self.values[key] = value
        self.provenance[key] = {"source": source, "label": SOURCE_LABELS.get(source, source), "confirmed": False}


def file_identity(
    dealer: DealerBusiness | None,
    dap: DealerApplicationProfile | None,
    profile: ApplicationProfile,
    intake: PublicUnderwritingIntake | None,
) -> dict[str, Any]:
    """The dealer's formation particulars, as both agreements print them.

    One definition: `build_prefill` fills the stage-one form from it and
    `load_file_context` hands the same values to the PDF builders. They used to
    read the same columns twice, which is why stage one asked for an EIN the
    file had held all along.
    """
    return {
        "legal_name": (dealer.legal_name if dealer else None) or (intake.business_name if intake else None),
        # DealerBusiness.name is the trade name; qc_master_application already
        # treats it as the DBA when the application profile has none.
        "dba": getattr(dap, "dba_name", None) or (dealer.name if dealer else None),
        "entity_type": (dealer.entity_type if dealer else None) or profile.entity_type,
        "state": getattr(dap, "state_of_formation", None) or (dealer.state if dealer else None),
        "formation_date": (dealer.started_on.isoformat() if dealer and dealer.started_on else None),
        "ein": dealer.ein if dealer else None,
        "naics": profile.naics_code or (dealer.naics_code if dealer else None),
        "address": compose_address(dealer.address, dealer.city, dealer.state, dealer.zip) if dealer else None,
        "license": None,
        "website": getattr(dap, "website", None),
    }


def file_owner_rows(owners: list[Any]) -> list[dict[str, Any]]:
    """The ownership schedule in the shape the `owners` arrangement key holds."""
    return [{
        "name": " ".join(p for p in (str(getattr(o, "first_name", "") or ""), str(getattr(o, "last_name", "") or "")) if p),
        "pct": float(getattr(o, "ownership_pct", 0) or 0), "title": "",
        "email": getattr(o, "email", None) or "", "phone": getattr(o, "phone", None) or "",
        "auth": "Yes" if getattr(o, "invite_sent_at", None) or getattr(o, "credit_pull_id", None) else "",
    } for o in owners[:pa.MAX_OWNERS]]


async def extracted_facts(db: AsyncSession, profile: ApplicationProfile) -> dict[str, str]:
    """What the AI read off the documents, best value per field.

    Every upload is analysed into `application_extracted_facts`, and nothing in
    the package has ever read them — so a file whose entity type is legible on
    an uploaded tax return still asked an operator to type it. Accepted beats
    suggested, higher confidence beats lower, and a rejected fact is never used.
    """
    rows = (
        await db.execute(
            select(ApplicationExtractedFact)
            .where(
                ApplicationExtractedFact.profile_id == profile.id,
                ApplicationExtractedFact.status != "rejected",
            )
            .order_by(ApplicationExtractedFact.created_at.desc())
        )
    ).scalars().all()
    best: dict[str, tuple[int, float, str]] = {}
    for row in rows:
        if row.status == "rejected":  # the query excludes these; so does this
            continue
        value = _text(row.normalized_value) or _text(row.value if isinstance(row.value, str) else (row.value or {}).get("value"))
        if not value:
            continue
        rank = (1 if row.status == "accepted" else 0, float(row.confidence or 0))
        if row.field_key not in best or rank > best[row.field_key][:2]:
            best[row.field_key] = (*rank, value)
    return {k: v[2] for k, v in best.items()}


def _primary_owner(owners: list[Any]) -> Any | None:
    for o in owners:
        if getattr(o, "is_primary", False):
            return o
    return owners[0] if owners else None


async def build_prefill(db: AsyncSession, profile: ApplicationProfile, actor: User | None) -> PrefillResult:
    out = PrefillResult()

    dealer: DealerBusiness | None = await db.get(DealerBusiness, profile.dealer_id) if profile.dealer_id else None
    dap: DealerApplicationProfile | None = None
    if dealer is not None:
        dap = (
            await db.execute(
                select(DealerApplicationProfile).where(DealerApplicationProfile.dealer_id == dealer.id).limit(1)
            )
        ).scalar_one_or_none()
    intake: PublicUnderwritingIntake | None = (
        await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    )
    owners = await profiles.owner_rows(db, profile)
    state = (intake.intake_state if intake is not None else None) or {}
    dealer_details = state.get("dealer_details") if isinstance(state.get("dealer_details"), dict) else {}
    entity_structure = state.get("entity_structure") if isinstance(state.get("entity_structure"), dict) else {}
    main_street = state.get("main_street_details") if isinstance(state.get("main_street_details"), dict) else {}
    primary_entity = entity_structure.get("primary_operating_entity")

    # ---- dealer identity ----
    ident = file_identity(dealer, dap, profile, intake)
    if dealer is not None:
        out.put("dealer_name", _text(dealer.legal_name) or _text(dealer.name), "dealer")
        out.put("dealer_entity", normalize_entity_type(dealer.entity_type), "dealer")
        out.put("dealer_address", compose_address(dealer.address, dealer.city, dealer.state, dealer.zip), "dealer")
    if dap is not None:
        out.put("dealer_dba", _text(dap.dba_name), "dealer_profile")
        out.put("dealer_state", _text(dap.state_of_formation), "dealer_profile")
        out.put("dealer_address", compose_address(dap.mailing_address, dap.mailing_city, dap.mailing_state, dap.mailing_zip), "dealer_profile")
        out.put("dealer_signer_title", _text(dap.signer_title), "dealer_profile")
        if dap.term_requested_months:
            out.put("term", int(dap.term_requested_months), "dealer_profile")
        out.put("debt_service", _number(dap.monthly_debt_payments), "dealer_profile")
    if intake is not None:
        # `primary_operating_entity` is the entity's NAME, not its type or its
        # state — the two readers that mined it for those were dead code that
        # would have written a company name into both fields had they run.
        out.put("dealer_name", _text(intake.business_name) or _text(primary_entity), "intake")
        out.put("dealer_entity", normalize_entity_type(main_street.get("entity_type")), "intake")
        out.put("requested", _number(intake.requested_loan_amount) or _number(dealer_details.get("requested_loan_amount")), "intake")
        out.put("debt_service", _number(dealer_details.get("stated_monthly_debt_payments")), "intake")
    out.put("dealer_entity", normalize_entity_type(profile.entity_type), "profile")
    out.put("dealer_dba", _text(ident.get("dba")), "dealer")

    # ---- what the documents already say ----
    # Below the typed columns: a human-entered value always wins over a reading.
    facts = await extracted_facts(db, profile)
    out.put("dealer_name", _text(facts.get("legal_entity_name")), "document_extraction")
    out.put("dealer_entity", normalize_entity_type(facts.get("entity_type")), "document_extraction")
    out.put("identity_naics", _text(facts.get("naics_code")), "document_extraction")

    # ---- closing identity and ownership (§9.1, §9.2) ----
    # load_file_context assembled these from the same columns, but only for
    # draft_final — so stage one asked for facts the file had all along, and the
    # Engagement printed the ownership schedule with nobody having reviewed it.
    out.put("identity_formation_date", _text(ident.get("formation_date")), "dealer")
    out.put("identity_ein", _text(ident.get("ein")), "dealer")
    out.put("identity_naics", _text(ident.get("naics")), "profile")
    out.put("identity_website", _text(ident.get("website")), "dealer_profile")
    if intake is not None:
        out.put("dealer_notice_email", _text(intake.email), "intake")
    owner_rows = file_owner_rows(owners)
    if owner_rows:
        out.put("owners", owner_rows, "owners")
    if dealer is not None:
        # Address state is a weaker proxy for state of formation; last resort.
        out.put("dealer_state", _text(dealer.state), "dealer")
    if dealer is not None and dealer.client_requested_amount:
        out.put("requested", _number(dealer.client_requested_amount), "dealer")
    if dealer is not None and dealer.funding_goal:
        out.put("requested", _number(dealer.funding_goal), "dealer")

    # ---- signer ----
    primary = _primary_owner(owners)
    if primary is not None:
        name = " ".join(p for p in (_text(primary.first_name), _text(primary.last_name)) if p)
        out.put("dealer_signer_name", name, "owners")
    if intake is not None:
        out.put("dealer_signer_name", _text(intake.full_name), "intake")

    # ---- relationship manager ----
    if actor is not None:
        out.put("rm_name", _text(getattr(actor, "name", None)), "user")
        out.put("rm_email", _text(getattr(actor, "email", None)), "user")
    out.put("rm_employer", pa.DEFAULTS["rm_employer"], "derived")

    # ---- baseline window ----
    start, end = trailing_twelve_months()
    out.put("base_from", start, "derived")
    out.put("base_through", end, "derived")

    blanks = {a["key"] for a in pa.field_attention({**pa.empty_arrangement(), **out.values}, scope="stage_one")}
    out.missing = sorted(k for k in blanks if k not in pa.SPONSOR_KEYS)
    return out


def apply_prefill(
    arrangement: dict[str, Any],
    provenance: dict[str, Any],
    result: PrefillResult,
    *,
    force: bool = False,
    fields: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
    """Write prefilled values onto blank fields (or every listed field when
    forced). Returns (arrangement, provenance, applied, skipped)."""
    base = {**pa.empty_arrangement(), **(arrangement or {})}
    prov = dict(provenance or {})
    wanted = set(fields) if fields else set(result.values)
    applied: list[str] = []
    skipped: list[str] = []
    for key in sorted(wanted):
        if key not in result.values or key not in pa.FIELD_RULES_BY_KEY:
            continue
        rule = pa.FIELD_RULES_BY_KEY[key]
        current = base.get(key)
        default = pa.DEFAULTS.get(key)
        untouched = pa.is_blank(rule, current) or (default is not None and current == default and key not in prov)
        if not force and not untouched:
            skipped.append(key)
            continue
        base[key] = result.values[key]
        prov[key] = dict(result.provenance[key])
        applied.append(key)
    return base, prov, applied, skipped
