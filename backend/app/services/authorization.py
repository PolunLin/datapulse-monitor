from app.core.exceptions import ForbiddenError
from app.models.user import Role
from app.services.auth import Principal


def require_admin(principal: Principal) -> None:
    if not principal.is_active or principal.role != Role.ADMIN.value:
        raise ForbiddenError("Administrator access is required")


def require_sample_reader(principal: Principal) -> None:
    if not principal.is_active or principal.role not in {role.value for role in Role}:
        raise ForbiddenError("This role cannot subscribe to monitoring data")


def require_record_writer(principal: Principal) -> None:
    if principal.role not in {Role.USER.value, Role.ADMIN.value}:
        raise ForbiddenError("This role cannot modify records")


def require_record_owner_or_admin(principal: Principal, created_by: int) -> None:
    require_record_writer(principal)
    if principal.role != Role.ADMIN.value and principal.id != created_by:
        raise ForbiddenError("Only the creator or an administrator can modify this record")
