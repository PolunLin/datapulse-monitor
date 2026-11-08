from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.dependencies import AuthServiceDependency, CurrentUser
from app.core.openapi import error_responses
from app.schemas.auth import RegisterRequest, TokenResponse
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(409, 422),
)
async def register(payload: RegisterRequest, service: AuthServiceDependency) -> UserRead:
    return await service.register(payload)


@router.post("/login", response_model=TokenResponse, responses=error_responses(401, 422))
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], service: AuthServiceDependency
) -> TokenResponse:
    """Use an email address in the OAuth2 `username` field."""
    return await service.login(form.username, form.password)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, responses=error_responses(401))
async def logout(principal: CurrentUser, service: AuthServiceDependency) -> Response:
    """Revoke all currently issued access tokens for the signed-in user."""
    await service.logout(principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
