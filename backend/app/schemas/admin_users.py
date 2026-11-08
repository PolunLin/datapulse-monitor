"""Admin user controls expose no authentication internals or credential fields."""

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.models.user import Role
from app.schemas.user import UserRead


class AdminUserFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    q: str | None = Field(default=None, min_length=1, max_length=320)
    role: Role | None = None
    is_active: bool | None = None


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: StrictBool | None = None

    @model_validator(mode="after")
    def require_changes(self) -> "AdminUserUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide role or is_active")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Updated fields cannot be null")
        return self


class AdminUserPage(BaseModel):
    items: list[UserRead]
    total: int
    page: int
    page_size: int
