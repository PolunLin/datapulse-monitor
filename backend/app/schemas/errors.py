"""Public HTTP error contract emitted by the application exception handlers."""

from typing import Any

from pydantic import BaseModel


class ValidationIssue(BaseModel):
    loc: list[str | int]
    type: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ValidationIssue] | dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str
