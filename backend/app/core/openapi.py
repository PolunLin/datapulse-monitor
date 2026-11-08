"""Reusable response documentation; runtime handlers keep one consistent envelope."""

from typing import Any

from pydantic import BaseModel

from app.schemas.errors import ErrorResponse

_DESCRIPTIONS = {
    401: "Missing, invalid, expired or revoked credentials",
    403: "The current user cannot perform this operation",
    404: "The requested resource does not exist",
    409: "The requested change conflicts with the current state",
    413: "The uploaded file or request exceeds the size limit",
    422: "Request validation failed or a documented query/import/export limit was exceeded",
    500: "An unexpected internal error occurred",
    503: "Database operation temporarily unavailable",
}


def error_responses(
    *status_codes: int, model: type[BaseModel] = ErrorResponse
) -> dict[int, dict[str, Any]]:
    """Create fresh metadata so per-route schemas never mutate other routes."""
    result = {}
    for code in status_codes:
        headers = {
            "X-Request-ID": {
                "description": "Request correlation ID, also returned in request_id",
                "schema": {"type": "string"},
            }
        }
        if code == 401:
            headers["WWW-Authenticate"] = {
                "description": "Bearer authentication challenge",
                "schema": {"type": "string", "enum": ["Bearer"]},
            }
        result[code] = {
            "model": model,
            "description": _DESCRIPTIONS[code],
            "headers": headers,
        }
    return result
