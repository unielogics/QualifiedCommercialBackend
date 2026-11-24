from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

DirectoryEligibility = Literal["all", "eligible", "ineligible", "requestable", "blocked"]
DirectoryEmail = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=320)]


class RecipientSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: Literal["all", "explicit"] = "all"
    included_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    excluded_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    included_emails: list[DirectoryEmail] = Field(default_factory=list, max_length=10000)
    excluded_emails: list[DirectoryEmail] = Field(default_factory=list, max_length=10000)
    search: str = Field("", max_length=200)
    eligibility: DirectoryEligibility = "all"

    @model_validator(mode="after")
    def normalize_ids(self):
        self.included_ids = sorted(set(self.included_ids))
        self.excluded_ids = sorted(set(self.excluded_ids))
        self.included_emails = sorted(set(self.included_emails))
        self.excluded_emails = sorted(set(self.excluded_emails))
        if sum(map(len, (self.included_ids, self.excluded_ids, self.included_emails, self.excluded_emails))) > 20000:
            raise ValueError("Use Select all matching contacts to select a larger audience.")
        return self
