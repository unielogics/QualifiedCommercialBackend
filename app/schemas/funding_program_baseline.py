from pydantic import BaseModel, Field

from app.schemas.funding_program import FundingProgramRequirementWrite, FundingProgramScopeWrite


class FundingProgramBaselineRead(BaseModel):
    program_key: str
    name: str
    version: str
    source_urls: list[str]
    source_notes: list[str]
    rules: dict = Field(default_factory=dict)
    requirements: list[FundingProgramRequirementWrite]
    scopes: list[FundingProgramScopeWrite]
    needs_review: bool = True
