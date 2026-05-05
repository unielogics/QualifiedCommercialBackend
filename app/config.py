"""Application config — loaded from .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: skip shell vars set to empty string so .env wins.
    # Without this, an exported but-blank ANTHROPIC_API_KEY shadows .env.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Database
    database_url: str = Field(default="postgresql+asyncpg://qc:qc@localhost:5432/qc")
    database_url_sync: str = Field(default="postgresql+psycopg://qc:qc@localhost:5432/qc")

    # CORS
    cors_origins: str = "http://localhost:3000,http://localhost:8081,http://localhost:19006"

    # Clerk (auth)
    clerk_secret_key: str = ""
    clerk_jwks_url: str = ""
    clerk_issuer: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model_heavy: str = "claude-sonnet-4-6"
    anthropic_model_light: str = "claude-haiku-4-5"

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    s3_bucket: str = "qc-documents-dev"

    # iSoftpull
    isoftpull_api_key: str = ""
    isoftpull_api_url: str = "https://api.isoftpull.com/v1"

    # RentCast
    rentcast_api_key: str = ""

    # FRED (Federal Reserve Economic Data) — daily index pull
    # Get a key at https://fred.stlouisfed.org/docs/api/api_key.html
    fred_api_key: str = ""
    fred_api_url: str = "https://api.stlouisfed.org/fred"

    # Gmail
    # Service-account path: when set, the Gmail client uses domain-wide
    # delegation rather than the (deferred) end-user OAuth flow. The SA must
    # have Gmail scopes authorized in Workspace admin and `gmail_delegated_user`
    # must be a real mailbox in the same workspace.
    gmail_service_account_path: str = ""
    gmail_delegated_user: str = ""
    # Legacy OAuth knobs — kept so future end-user OAuth path is still available
    gmail_oauth_client_id: str = ""
    gmail_oauth_client_secret: str = ""
    gmail_pubsub_topic: str = ""
    use_fake_inbox: bool = True

    app_env: str = "development"
    log_level: str = "INFO"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
