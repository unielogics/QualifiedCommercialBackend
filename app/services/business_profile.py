"""What the client says their business is, asked on the first screen of every AI application.

Two answers: the business's website (or "N/A" when it has none) and a short
description of what it does. They are the context a person or the AI would
otherwise have to dig out of documents: a description is often all it takes to
see which industry rules apply, and a website is the fastest way to check the
business is real.

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

from pydantic import BaseModel, Field, field_validator

MAX_WEBSITE = 255
MIN_DESCRIPTION = 10
MAX_DESCRIPTION = 1000

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


class BusinessProfileFields(BaseModel):
    """The two answers, as an application's start form posts them.

    Optional on the wire so an older page or an API caller still starts a file;
    the forms themselves require both, and whatever arrives is validated.
    """

    business_website: str | None = Field(default=None, max_length=MAX_WEBSITE)
    business_description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)

    @field_validator("business_website", mode="before")
    @classmethod
    def _check_website(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        normalize_website(value)
        return value

    @field_validator("business_description", mode="before")
    @classmethod
    def _check_description(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        return clean_description(value)


def record(payload: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """What goes in `intake_state["business_profile"]`, or None when neither was given."""
    website, none = normalize_website(getattr(payload, "business_website", None))
    description = clean_description(getattr(payload, "business_description", None))
    if website is None and not none and description is None:
        return None
    return {
        "website": website,
        "website_none": none,
        "description": description,
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
        "website": website or None,
        "website_none": none and not website,
        "description": (description or None),
    }
