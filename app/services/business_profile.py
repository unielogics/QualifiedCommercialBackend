"""Business address, website, declared industry and activity from AI intake.

The answers live on the intake (`intake_state["business_profile"]`), beside the
other things the client declared. A desk edit lands in `desk_facts` and wins on
read, the same precedence `years_in_business` already has, so correcting a
website never rewrites what the client typed.

"N/A" is an answer, not a blank: the form requires one, and a business with no
site says so. It is stored as `website_none` so the desk can tell "has no
website" from "was never asked" (files that predate the question).

The dashboard mirrors the website rule in `src/lib/businessProfile.ts`. Change both.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, create_model, field_validator

from app.services.main_street_programs import MAIN_STREET_INDUSTRIES

MAX_WEBSITE = 255
MIN_DESCRIPTION = 10
MAX_DESCRIPTION = 1000
ADDRESS_FIELDS = ("business_street", "business_city", "business_state", "business_zip")
PROFILE_FIELDS = (*ADDRESS_FIELDS, "business_website", "business_description", "industry")
_STATES = frozenset("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI AA AE AP".split())

WEBSITE_MESSAGE = (
    "Enter your website address (like example.com), or N/A if the business does not have one."
)
DESCRIPTION_MESSAGE = (
    f"Describe the business in a sentence or two (at least {MIN_DESCRIPTION} characters)."
)

#: Ways people say "no website". Compared case-insensitively after trimming
#: spaces and trailing periods.
NONE_WORDS = frozenset(
    {
        "n/a",
        "na",
        "n.a",
        "n a",
        "none",
        "no",
        "nope",
        "not applicable",
        "no website",
        "no site",
        "none yet",
        "-",
        "--",
        "dont have one",
        "don't have one",
        "do not have one",
        "no tengo",
        "ninguno",
        "ninguna",
        "no aplica",
        "n/a.",
    }
)

#: A host with at least one dot and a letters-only ending, then any path.
#: Deliberately narrower than "anything URL-shaped": the desk renders this as a
#: link, so a scheme other than http(s) must never get through.
_HOST = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?::\d{2,5})?$", re.IGNORECASE
)
_SCHEME = re.compile(r"^(https?)://", re.IGNORECASE)


def _squash(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_website(raw: Any) -> tuple[str | None, bool]:
    """(website, none): the address as a link, whether the client said they have none.

    ``("https://example.com/shop", False)`` for an address, ``(None, True)`` for
    N/A and its relatives, ``(None, False)`` for blank. Anything else is not a
    website and raises ``ValueError`` with the sentence to show.
    """
    text = _squash(raw)
    if not text:
        return None, False
    if text.casefold().strip(" .") in NONE_WORDS or text.casefold() in NONE_WORDS:
        return None, True
    scheme = "https"
    match = _SCHEME.match(text)
    rest = text[match.end() :] if match else text
    if match:
        scheme = match.group(1).lower()
    if " " in rest or len(text) > MAX_WEBSITE:
        raise ValueError(WEBSITE_MESSAGE)
    host, slash, path = rest.partition("/")
    if not _HOST.match(host):
        raise ValueError(WEBSITE_MESSAGE)
    path = path.rstrip("/") if not path.strip("/") else path
    return f"{scheme}://{host.lower()}{slash if path else ''}{path}", False


def clean_description(raw: Any) -> str | None:
    text = _squash(raw)
    if not text:
        return None
    if len(text) < MIN_DESCRIPTION or len(text) > MAX_DESCRIPTION:
        raise ValueError(DESCRIPTION_MESSAGE)
    return text


def clean_address(field: str, raw: Any) -> str | None:
    """Validate a supplied business address component, without filling missing history."""
    text = _squash(raw)
    if not text:
        return None
    if field == "business_state":
        text = text.upper()
        if text not in _STATES:
            raise ValueError("Choose a valid two-letter US state or territory.")
    elif field == "business_zip" and not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", text):
        raise ValueError("Enter a five-digit ZIP code or ZIP+4.")
    return text


def clean_industry(raw: Any) -> str | None:
    text = _squash(raw)
    if not text:
        return None
    if text not in MAIN_STREET_INDUSTRIES:
        raise ValueError("Choose an industry, or explicitly select Other operating business.")
    return text


class BusinessProfileFields(BaseModel):
    """Optional historical/partial-edit shape and additive rollout contract."""

    business_phone: str | None = Field(default=None, max_length=32)
    employee_count: int | None = Field(default=None, ge=0, le=2147483647)
    business_website: str | None = Field(default=None, max_length=MAX_WEBSITE)
    business_description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    business_street: str | None = Field(default=None, max_length=255)
    business_city: str | None = Field(default=None, max_length=120)
    business_state: str | None = Field(default=None, max_length=2)
    business_zip: str | None = Field(default=None, max_length=10)
    industry: str | None = Field(default=None, max_length=64)

    @field_validator(*ADDRESS_FIELDS, mode="before")
    @classmethod
    def _check_address(cls, value: Any, info: Any) -> Any:
        return clean_address(info.field_name, value)

    @field_validator("industry", mode="before")
    @classmethod
    def _check_industry(cls, value: Any) -> Any:
        return clean_industry(value)

    @field_validator("business_website", mode="before")
    @classmethod
    def _check_website(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        website, none = normalize_website(value)
        return value if website or none else None

    @field_validator("business_description", mode="before")
    @classmethod
    def _check_description(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        return clean_description(value)


class RequiredBusinessProfileFields(BusinessProfileFields):
    """New application boundary; historical reads and partial edits stay optional."""

    business_street: str = Field(max_length=255)
    business_city: str = Field(max_length=120)
    business_state: str = Field(max_length=2)
    business_zip: str = Field(max_length=10)
    business_website: str = Field(max_length=MAX_WEBSITE)
    business_description: str = Field(min_length=MIN_DESCRIPTION, max_length=MAX_DESCRIPTION)
    industry: str = Field(max_length=64)


def payload_fields(payload: Any) -> dict[str, Any]:
    """Pass the same validated answers through internal create adapters."""
    return {key: getattr(payload, key, None) for key in (*PROFILE_FIELDS, "employee_count", "business_phone")}


def _legacy_adapter(model: type[BaseModel], **values: Any) -> BaseModel:
    """Private adoption of an existing CRM/appointment record, never a public DTO.

    Those records may predate the questions. Validate all other fields normally,
    preserving missing business facts instead of inventing a website or industry.
    New public/staff creation endpoints must continue to use the required model.
    """
    optional: dict[str, Any] = {}
    # Inherit the shared validators and constraints without inheriting required
    # defaults. Limits remain enforced by the optional shared field definitions.
    for key in PROFILE_FIELDS:
        field = BusinessProfileFields.model_fields[key]
        optional[key] = (str | None, field)
    adapter = create_model(f"_Legacy{model.__name__}", __base__=model, **optional)
    return adapter.model_validate(values, context={"existing_source": True})


def existing_source_fields(source: Any) -> dict[str, Any]:
    """Copy only recognizable existing business facts; a property is not its business."""
    values: dict[str, Any] = {}
    for key, names in {
        "business_street": ("business_street", "street", "address"),
        "business_city": ("business_city", "city"),
        "business_state": ("business_state", "state"),
        "business_zip": ("business_zip", "zip"),
        "business_website": ("business_website", "website"),
        "business_description": ("business_description",),
        "industry": ("industry",),
    }.items():
        raw = next((getattr(source, name, None) for name in names if getattr(source, name, None)), None)
        if raw is None:
            continue
        try:
            checked = BusinessProfileFields.model_validate({key: raw})
        except ValueError:
            continue
        values[key] = getattr(checked, key)
    return values


async def rearm_unclassified_suggestion(db: Any, intake: Any) -> None:
    """Let the existing bounded worker reconsider new words, preserving its audit."""
    from sqlalchemy import select

    from app.models.application_profile import ApplicationProfile

    profile = (await db.execute(select(ApplicationProfile).where(
        ApplicationProfile.intake_id == intake.id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if profile is None or profile.naics_code or profile.classified_by_user_id:
        return
    previous = dict(profile.classification_provenance or {})
    if previous.get("suggestion_outcome") not in {"no_activity_fact", "no_candidates"}:
        return
    attempts = list(previous.get("suggestion_history") or [])
    attempts.append({key: value for key, value in previous.items() if key != "suggestion_history"})
    profile.classification_provenance = {
        **previous,
        "suggestion_history": attempts,
        "suggestion_attempted_at": None,
        "suggestion_rearmed_at": datetime.now(UTC).isoformat(),
    }


def record(payload: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The original declared answers, or None when this older file has none."""
    website, none = normalize_website(getattr(payload, "business_website", None))
    description = clean_description(getattr(payload, "business_description", None))
    address = {
        key.removeprefix("business_"): clean_address(key, getattr(payload, key, None))
        for key in ADDRESS_FIELDS
    }
    industry = clean_industry(getattr(payload, "industry", None))
    if website is None and not none and description is None and not any(address.values()) and not industry:
        return None
    return {
        "website": website,
        "website_none": none,
        "description": description,
        **address,
        "industry": industry,
        "business_phone": getattr(payload, "business_phone", None),
        "employee_count": getattr(payload, "employee_count", None),
        "captured_at": (now or datetime.now(UTC)).isoformat(),
    }


def state_entry(payload: Any) -> dict[str, Any]:
    """The same, shaped to be splatted into an intake_state dict."""
    entry = record(payload)
    return {"business_profile": entry} if entry else {}


def of_intake(intake: Any) -> dict[str, Any]:
    """The business profile as the file knows it: the desk's correction when
    there is one, else what the client typed. Keys are always present."""
    state = getattr(intake, "intake_state", None) or {}
    declared = (
        state.get("business_profile") if isinstance(state.get("business_profile"), dict) else {}
    )
    desk = state.get("desk_facts") if isinstance(state.get("desk_facts"), dict) else {}
    website = declared.get("website")
    none = bool(declared.get("website_none"))
    if "business_website" in desk or "business_website_none" in desk:
        website = desk.get("business_website") or None
        none = bool(desk.get("business_website_none"))
    description = (
        desk["business_description"]
        if "business_description" in desk
        else declared.get("description")
    )
    return {
        "business_phone": declared.get("business_phone"),
        "employee_count": declared.get("employee_count"),
        "website": website or None,
        "website_none": none and not website,
        "description": (description or None),
        **{
            key.removeprefix("business_"): (desk[key] if key in desk else declared.get(key.removeprefix("business_"))) or None
            for key in ADDRESS_FIELDS
        },
        "industry": (
            desk["industry"] if "industry" in desk else declared.get("industry")
            or ((state.get("main_street_details") or {}).get("industry") if isinstance(state.get("main_street_details"), dict) else None)
        ) or None,
    }


def apply_read(read: Any, intake: Any) -> Any:
    """Use the same desk-first projection in detail and client-list responses."""
    declared = of_intake(intake)
    read.business_phone = declared["business_phone"]
    read.employee_count = declared["employee_count"]
    read.business_website = declared["website"]
    read.business_website_none = declared["website_none"]
    read.business_description = declared["description"]
    for key in ("street", "city", "state", "zip"):
        setattr(read, f"business_{key}", declared[key])
    read.industry = declared["industry"]
    return read


def routing_industry(profile: Any, declared: dict[str, Any]) -> str | None:
    """Keep broad declared routing distinct from a catalog label.

    A suggestion cannot erase the original broad selector. A staff-confirmed
    canonical path takes precedence without guessing a slug from its label.
    """
    canonical = getattr(profile, "industry", None)
    if getattr(profile, "classified_by_user_id", None):
        return canonical
    return declared.get("industry") or canonical
