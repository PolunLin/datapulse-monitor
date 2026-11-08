from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.exceptions import UnauthorizedError

password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    return hash_password("datapulse-dummy-password-not-a-user-credential")


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(user_id: int, token_version: int, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "ver": token_version,
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "jti": str(uuid4()),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


def decode_access_token(token: str, settings: Settings) -> tuple[int, int]:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "ver", "iat", "exp", "iss", "aud"]},
        )
        user_id = int(claims["sub"])
        version = claims["ver"]
        if user_id <= 0 or type(version) is not int or version < 0:
            raise ValueError("Invalid token claims")
        return user_id, version
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
        raise UnauthorizedError("Invalid or expired credentials") from exc
