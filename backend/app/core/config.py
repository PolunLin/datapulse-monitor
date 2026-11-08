from decimal import Decimal
from functools import lru_cache

from pydantic import EmailStr, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    db_host: str = "db"
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_name: str = "datapulse"
    db_user: str = "datapulse"
    db_password: SecretStr | None = None
    database_url: SecretStr | None = None
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout: int = Field(default=30, ge=1)
    db_pool_recycle: int = Field(default=1800, ge=1)

    jwt_secret: SecretStr = Field(min_length=32)
    jwt_issuer: str = "datapulse"
    jwt_audience: str = "datapulse-api"
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    bootstrap_admin_email: EmailStr | None = None
    bootstrap_admin_password: SecretStr | None = None
    log_level: str = "INFO"

    # The local Compose stack enables simulation explicitly; tests/library users opt in.
    simulator_enabled: bool = False
    simulator_interval_seconds: float = Field(default=1.0, gt=0, le=3600, allow_inf_nan=False)
    simulator_category: str = Field(default="temperature", min_length=1, max_length=100)
    simulator_threshold: Decimal = Field(
        default=Decimal("80"), max_digits=18, decimal_places=6, allow_inf_nan=False
    )
    realtime_batch_size: int = Field(default=100, ge=1, le=10000)
    realtime_flush_interval_seconds: float = Field(default=5.0, gt=0, le=3600, allow_inf_nan=False)
    realtime_queue_capacity: int = Field(default=1000, ge=1, le=100000)
    realtime_retry_initial_seconds: float = Field(default=0.1, gt=0, le=60, allow_inf_nan=False)
    realtime_retry_max_seconds: float = Field(default=5.0, gt=0, le=60, allow_inf_nan=False)
    realtime_shutdown_timeout_seconds: float = Field(default=10.0, gt=0, le=20, allow_inf_nan=False)
    ws_enabled: bool = True
    ws_queue_capacity: int = Field(default=100, ge=1, le=10000)
    ws_send_timeout_seconds: float = Field(default=2.0, gt=0, le=10, allow_inf_nan=False)
    ws_auth_check_interval_seconds: float = Field(default=1.0, gt=0, le=30, allow_inf_nan=False)
    ws_max_connections: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        if not self.database_url and not self.db_password:
            raise ValueError("DB_PASSWORD or DATABASE_URL is required")
        if self.database_url:
            url = make_url(self.database_url.get_secret_value())
            if url.drivername != "mysql+asyncmy":
                raise ValueError("DATABASE_URL must use the mysql+asyncmy driver")
        if bool(self.bootstrap_admin_email) != bool(self.bootstrap_admin_password):
            raise ValueError(
                "BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD must be set together"
            )
        if self.bootstrap_admin_password:
            length = len(self.bootstrap_admin_password.get_secret_value())
            if not 8 <= length <= 128:
                raise ValueError("BOOTSTRAP_ADMIN_PASSWORD must contain 8 to 128 characters")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")
        self.simulator_category = self.simulator_category.strip()
        if not self.simulator_category:
            raise ValueError("SIMULATOR_CATEGORY cannot be blank")
        if self.realtime_retry_max_seconds < self.realtime_retry_initial_seconds:
            raise ValueError("Retry maximum must be at least the initial retry delay")
        return self

    @property
    def sqlalchemy_url(self) -> URL:
        if self.database_url:
            return make_url(self.database_url.get_secret_value())
        return URL.create(
            "mysql+asyncmy",
            username=self.db_user,
            password=self.db_password.get_secret_value() if self.db_password else None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
