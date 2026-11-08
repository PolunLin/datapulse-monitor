"""Role/status changes revoke credentials and commit with a durable audit record."""

from app.core.exceptions import ConflictError, NotFoundError, UnauthorizedError
from app.db.base import utc_now
from app.db.session import SessionFactory
from app.repositories import admin_users, users
from app.schemas.admin_users import AdminUserFilters, AdminUserPage, AdminUserUpdate
from app.schemas.user import UserRead
from app.services.auth import Principal
from app.services.authorization import require_admin
from app.services.system_logs import append_user_audit


class SelfManagementError(ConflictError):
    code = "admin_self_change"


class AdminUserService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def list(self, principal: Principal, filters: AdminUserFilters) -> AdminUserPage:
        require_admin(principal)
        async with self.session_factory.begin() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            items, total = await admin_users.list_page(session, filters)
            return AdminUserPage(
                items=[UserRead.model_validate(item) for item in items],
                total=total,
                page=filters.page,
                page_size=filters.page_size,
            )

    async def get(self, principal: Principal, user_id: int) -> UserRead:
        require_admin(principal)
        async with self.session_factory() as session:
            user = await users.get_by_id(session, user_id)
            if user is None:
                raise NotFoundError("User not found")
            return UserRead.model_validate(user)

    async def update(
        self,
        principal: Principal,
        user_id: int,
        payload: AdminUserUpdate,
        *,
        request_id: str | None = None,
    ) -> UserRead:
        require_admin(principal)
        async with self.session_factory.begin() as session:
            locked = await admin_users.lock_users(session, principal.id, user_id)
            actor = locked.get(principal.id)
            if (
                actor is None
                or not actor.is_active
                or actor.token_version != principal.token_version
            ):
                raise UnauthorizedError("Invalid or expired credentials")
            require_admin(Principal.from_user(actor))
            target = locked.get(user_id)
            if target is None:
                raise NotFoundError("User not found")
            before = {"role": target.role, "is_active": target.is_active}
            after = {**before, **payload.model_dump(mode="json", exclude_unset=True)}
            if before == after:
                return UserRead.model_validate(target)
            if target.id == actor.id:
                raise SelfManagementError("Administrators cannot change their own role or status")

            target.role = after["role"]
            target.is_active = after["is_active"]
            target.token_version += 1
            target.updated_at = utc_now()
            append_user_audit(
                session,
                actor_id=actor.id,
                target_id=target.id,
                before=before,
                after=after,
                request_id=request_id,
            )
            await session.flush()
            response = UserRead.model_validate(target)
        return response
