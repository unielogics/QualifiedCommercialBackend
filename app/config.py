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
    cors_origins: str = "http://localhost:3000,http://localhost:8081,http://localhost:19006,https://qualifiedcommercial.com,https://www.qualifiedcommercial.com"

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
    # Two-key auth: public key identifies the account; private key authenticates
    # (carried as bearer / HMAC depending on what iSoftPull's contract turns out
    # to be — verify on first live call). The legacy single `isoftpull_api_key`
    # alias is preserved so existing .env files still load — when only the
    # legacy key is present the client treats it as the private key.
    isoftpull_public_key: str = ""
    isoftpull_private_key: str = ""
    isoftpull_api_key: str = ""  # legacy / fallback for private_key
    isoftpull_api_url: str = "https://api.isoftpull.com/v1"
    isoftpull_timeout_seconds: float = 15.0
    isoftpull_max_retries: int = 2
    # Server-side dashboard login — bridge until Full Feed is enabled.
    # When set, the backend logs into iSoftPull as a real user and scrapes
    # the report HTML to extract the parsed FICO from the report viewer.
    isoftpull_login_email: str = ""
    isoftpull_login_password: str = ""
    isoftpull_dashboard_url: str = "https://app.isoftpull.com"

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
    # Gmail Pub/Sub push — real-time inbound. When `gmail_pubsub_topic`
    # is set (projects/<proj>/topics/<topic>) the app registers a
    # users.watch() on the delegated mailbox's INBOX; Gmail then pushes
    # to that topic, the Pub/Sub subscription POSTs /webhooks/gmail, and
    # we run an immediate inbound poll. `gmail_push_token` is the shared
    # secret carried in the webhook URL (?token=) — pushes that don't
    # match are rejected. Empty topic => push disabled, the 60s poller
    # still runs as the fallback.
    gmail_pubsub_topic: str = ""
    gmail_push_token: str = ""
    use_fake_inbox: bool = True

    # Firebase Cloud Messaging — path to the Firebase Admin SDK
    # service-account JSON. Used by app/services/push.py to send
    # FCM HTTPv1 messages to the ANDROID app (qcmobile). When empty,
    # push.py logs a debug note and no-ops — useful for local dev
    # where you don't want to wire FCM yet.
    firebase_credentials_path: str = ""

    # Apple Push Notification service (APNs) — token-based (.p8) auth
    # for the iOS app (qcmobile-ios). push.py sends iOS device tokens
    # directly to APNs, parallel to the Android FCM path; routing is
    # by the device row's `platform` ("ios" → APNs, else → FCM).
    # When apns_key_path is empty the iOS branch no-ops silently,
    # exactly like the FCM gate above.
    #   apns_key_path    AuthKey_XXXX.p8 from the Apple Developer
    #                    portal (Keys → APNs). Gitignored.
    #   apns_key_id      the 10-char Key ID for that key
    #   apns_team_id     Apple Developer Team ID
    #   apns_bundle_id   app bundle id == APNs topic
    #   apns_use_sandbox True for dev-client builds, False for
    #                    TestFlight / App Store (production APNs).
    #                    Wrong environment = silent non-delivery.
    apns_key_path: str = ""
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_bundle_id: str = "com.qualifiedcommercial.mobile"
    apns_use_sandbox: bool = True

    app_env: str = "development"
    log_level: str = "INFO"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
