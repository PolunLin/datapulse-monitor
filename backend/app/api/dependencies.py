from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from app.core.exceptions import UnauthorizedError
from app.services.auth import AuthService, Principal
from app.services.records import RecordService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_auth_service(request: Request) -> AuthService:
    return AuthService(request.app.state.session_factory, request.app.state.settings)


def get_record_service(request: Request) -> RecordService:
    return RecordService(request.app.state.session_factory)


async def get_current_user(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> Principal:
    if token is None:
        raise UnauthorizedError("Authentication is required")
    principal = await auth_service.authenticate(token)
    request.state.actor_id = principal.id
    return principal


CurrentUser = Annotated[Principal, Depends(get_current_user)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
RecordServiceDependency = Annotated[RecordService, Depends(get_record_service)]
