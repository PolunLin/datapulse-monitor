from typing import Literal

from pydantic import BaseModel, Field


class ImportIssue(BaseModel):
    row: int = Field(ge=1)
    field: Literal["file", "row", "title", "value", "category", "timestamp"]
    code: Literal["missing", "invalid", "extra", "format", "limit", "empty", "duplicate"]
    message: str


class ImportIssueDetails(BaseModel):
    errors: list[ImportIssue]
    total_errors: int
    errors_truncated: bool


class ImportErrorBody(BaseModel):
    code: str
    message: str
    details: ImportIssueDetails


class ImportErrorResponse(BaseModel):
    error: ImportErrorBody
    request_id: str


class ImportResult(BaseModel):
    imported_count: int = Field(ge=1, le=5000)
