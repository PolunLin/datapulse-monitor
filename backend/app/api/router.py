from fastapi import APIRouter

from app.api.v1 import (
    admin_system,
    admin_users,
    analytics,
    auth,
    monitoring,
    records,
    transfers,
    users,
)
from app.core.openapi import error_responses

router = APIRouter(prefix="/api/v1", responses=error_responses(500, 503))
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(records.router)
router.include_router(monitoring.router)
router.include_router(analytics.router)
router.include_router(transfers.router)
router.include_router(admin_users.router)
router.include_router(admin_system.router)
