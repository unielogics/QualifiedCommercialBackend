"""Staff-authored file requests, sharing the normal evidence/collection lifecycle."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FileRequestItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    document_type: str = Field(default="other", min_length=1, max_length=80)
    name: str = Field(min_length=2, max_length=180)
    quantity: int = Field(default=1, ge=1, le=100)
    unit: Literal["files", "months", "years"] = "files"
    subject: str = Field(default="business", min_length=1, max_length=160)
    period: str = Field(default="current", min_length=1, max_length=160)
    instructions: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def bounded_period(self):
        maximum = {"files": 100, "months": 60, "years": 10}[self.unit]
        if self.quantity > maximum:
            raise ValueError(f"Request at most {maximum} {self.unit}")
        return self


class FileRequestBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    items: list[FileRequestItem] = Field(min_length=1, max_length=30)
    notify: bool = True


class FileRequestRetry(BaseModel):
    retry_id: UUID
