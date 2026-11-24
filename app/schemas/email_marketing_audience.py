from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecipientSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: Literal["all", "explicit"] = "all"
    included_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    excluded_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    search: str = Field("", max_length=200)

    @model_validator(mode="after")
    def normalize_ids(self):
        self.included_ids = sorted(set(self.included_ids))
        self.excluded_ids = sorted(set(self.excluded_ids))
        return self
