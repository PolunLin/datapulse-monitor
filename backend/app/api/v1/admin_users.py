from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from app.api.dependencies import CurrentUser
from app.core.openapi import error_responses
from app.schemas.admin_users import AdminUserFilters, AdminUserPage, AdminUserUpdate
from app.schemas.user import UserRead
from app.services.admin_users import AdminUserService

router = APIRouter(prefix="/admin/users", tags=["Admin users"], responses=error_responses(401, 403))
UserId = Annotated[int, Path(ge=1)]


@router.get("", response_model=AdminUserPage, responses=error_responses(422))
async def list_users(
    request: Request, principal: CurrentUser, filters: Annotated[AdminUserFilters, Query()]
) -> AdminUserPage:
    """Admin-only user search; stable ID order, at most 100 users per page."""
    return await AdminUserService(request.app.state.session_factory).list(principal, filters)


@router.get("/{user_id}", response_model=UserRead, responses=error_responses(404, 422))
async def get_user(request: Request, principal: CurrentUser, user_id: UserId) -> UserRead:
    return await AdminUserService(request.app.state.session_factory).get(principal, user_id)


@router.patch("/{user_id}", response_model=UserRead, responses=error_responses(404, 409, 422))
async def update_user(
    request: Request, principal: CurrentUser, user_id: UserId, payload: AdminUserUpdate
) -> UserRead:
    """Change another user's role/status, revoke existing tokens and audit atomically.

    Self role/status changes return 409. Repeating an unchanged state is a no-op.
    Accounts are deactivated instead of deleted, preserving record ownership.
    """
    return await AdminUserService(request.app.state.session_factory).update(
        principal, user_id, payload, request_id=getattr(request.state, "request_id", None)
    )
