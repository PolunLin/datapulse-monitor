from fastapi import APIRouter

from app.api.dependencies import CurrentUser
from app.core.openapi import error_responses
from app.schemas.user import UserRead

router = APIRouter(prefix="/users", tags=["Users"], responses=error_responses(401))


@router.get("/me", response_model=UserRead)
async def get_me(principal: CurrentUser) -> UserRead:
    return UserRead.model_validate(principal)
