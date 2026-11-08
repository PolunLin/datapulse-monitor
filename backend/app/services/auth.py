from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)
from app.db.session import SessionFactory
from app.models.user import Role, User
from app.repositories import users
from app.schemas.auth import RegisterRequest, TokenResponse
from app.schemas.user import UserRead


@dataclass(frozen=True, slots=True)
class Principal:
    id: int
    email: str
    role: str
    is_active: bool
    token_version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "Principal":
        return cls(
            id=user.id,
            email=user.email,
            role=user.role,
            is_active=user.is_active,
            token_version=user.token_version,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )


class AuthService:
    def __init__(self, session_factory: SessionFactory, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def register(self, payload: RegisterRequest) -> UserRead:
        hashed = await run_in_threadpool(hash_password, payload.password)
        try:
            async with self.session_factory.begin() as session:
                if await users.get_by_email(session, str(payload.email)):
                    raise ConflictError("Email is already registered")
                user = await users.add(
                    session,
                    User(
                        email=str(payload.email),
                        password_hash=hashed,
                        role=Role.USER.value,
                    ),
                )
                response = UserRead.model_validate(user)
            return response
        except IntegrityError as exc:
            # The unique constraint also handles simultaneous registrations.
            raise ConflictError("Email is already registered") from exc

    async def login(self, email: str, password: str) -> TokenResponse:
        async with self.session_factory() as session:
            user = await users.get_by_email(session, email.strip().casefold())
            principal = Principal.from_user(user) if user else None
            stored_hash = user.password_hash if user else None

        if stored_hash is None:
            stored_hash = await run_in_threadpool(dummy_password_hash)
        valid = await run_in_threadpool(verify_password, password, stored_hash)
        if not valid or principal is None or not principal.is_active:
            raise UnauthorizedError("Invalid email or password")
        return TokenResponse(
            access_token=create_access_token(principal.id, principal.token_version, self.settings),
            expires_in=self.settings.access_token_expire_minutes * 60,
        )

    async def authenticate(self, token: str) -> Principal:
        user_id, version = decode_access_token(token, self.settings)
        async with self.session_factory() as session:
            user = await users.get_by_id(session, user_id)
            if user is None or not user.is_active or user.token_version != version:
                raise UnauthorizedError("Invalid or expired credentials")
            return Principal.from_user(user)

    async def logout(self, principal: Principal) -> None:
        async with self.session_factory.begin() as session:
            user = await users.get_by_id(session, principal.id, for_update=True)
            if user is None or not user.is_active:
                raise UnauthorizedError("Invalid or expired credentials")
            user.token_version += 1
