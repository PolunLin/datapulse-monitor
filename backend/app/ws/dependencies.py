"""WebSocket credentials share the REST authentication service."""

from fastapi import WebSocket

from app.core.exceptions import UnauthorizedError
from app.services.auth import AuthService, Principal
from app.services.authorization import require_sample_reader


async def authenticate_websocket(websocket: WebSocket) -> tuple[str, Principal]:
    authorization = websocket.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise UnauthorizedError("Bearer authentication is required")
    # URL tokens are deliberately unsupported: they can leak into access logs.
    token = token.strip()
    auth = AuthService(websocket.app.state.session_factory, websocket.app.state.settings)
    principal = await auth.authenticate(token)
    require_sample_reader(principal)
    return token, principal
