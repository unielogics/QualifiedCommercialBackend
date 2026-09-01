"""Application config — loaded from .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: skip shell vars set to empty string so .env wins.
    # Without this, an exported but-blank provider key shadows .env.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Database
    database_url: str = Field(default="postgresql+asyncpg://qc:qc@localhost:5432/qc")
    database_url_sync: str = Field(default="postgresql+psycopg://qc:qc@localhost:5432/qc")

    # CORS
    cors_origins: str = (
        "http://localhost:3000,http://localhost:8081,http://localhost:19006,"
        "https://qualifiedcommercial.com,https://www.qualifiedcommercial.com,"
        "https://app.qualifiedcommercial.com,https://agreement.qualifiedcommercial.com,"
        "https://audit.qualifiedcommercial.com,https://rep.qualifiedcommercial.com"
    )
    frontend_app_url: str = "https://app.qualifiedcommercial.com"
    rep_app_url: str = "https://rep.qualifiedcommercial.com"
    public_api_url: str = "https://api.qualifiedcommercial.com"

    # Clerk (auth)
    clerk_secret_key: str = ""
    # When true, a Clerk session that still owes its two-step-verification
    # setup is rejected. Ships OFF so every account can enrol without being
    # locked out; flip it in Secrets Manager once `two_factor_enabled` is true
    # for everyone. Reversible in one env var.
    require_mfa: bool = False
    clerk_jwks_url: str = ""
    clerk_issuer: str = ""

    # AWS Bedrock AI
    bedrock_enabled: bool = False
    aws_bearer_token_bedrock: str = ""
    bedrock_region: str = ""
    bedrock_model_heavy: str = "us.anthropic.claude-sonnet-4-6"
    bedrock_model_light: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # Estimated Bedrock Claude prices per 1M tokens, used for local cost
    # attribution. Alert thresholds live in AppSettings.ai_spend so
    # super-admins can tune them from the dashboard.
    ai_pricing_light_input_per_mtok: float = 0.80
    ai_pricing_light_output_per_mtok: float = 4.00
    ai_pricing_heavy_input_per_mtok: float = 3.00
    ai_pricing_heavy_output_per_mtok: float = 15.00

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    s3_bucket: str = "qc-documents-dev"
    buckets_s3_prefix: str = "buckets/prod"
    buckets_kms_key_id: str = ""

    # iSoftpull
    # Two-key auth: public key identifies the account; private key authenticates
    # (carried as bearer / HMAC depending on what iSoftPull's contract turns out
    # to be — verify on first live call). The legacy single `isoftpull_api_key`
    # alias is preserved so existing .env files still load — when only the
    # legacy key is present the client treats it as the private key.
    isoftpull_public_key: str = ""
    isoftpull_private_key: str = ""
    isoftpull_api_key: str = ""  # legacy / fallback for private_key
    isoftpull_api_url: str = "https://app.isoftpull.com/api/v2"
    isoftpull_timeout_seconds: float = 15.0
    isoftpull_max_retries: int = 2
    # Server-side dashboard login — bridge until Full Feed is enabled.
    # When set, the backend logs into iSoftPull as a real user and scrapes
    # the report HTML to extract the parsed FICO from the report viewer.
    isoftpull_login_email: str = ""
    isoftpull_login_password: str = ""
    isoftpull_dashboard_url: str = "https://app.isoftpull.com"

    # Stripe — client payment authorization. Card entry must happen through
    # Stripe-hosted elements/SDKs; the backend only stores reusable tokens and
    # non-sensitive card metadata.
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    primary_super_admin_email: str = "franco@qualifiedcommercial.com"
    primary_super_admin_emails: str = ""

    # RentCast
    rentcast_api_key: str = ""

    # Address providers. These credentials are deployment-managed and are
    # never accepted from or returned to a frontend. QCDashboard only selects
    # which configured provider is active.
    geoapify_api_key: str = ""
    google_server_api_key: str = ""

    # Provider secrets
    # Super-admin managed provider keys are encrypted before they are stored
    # in Postgres. In production, set provider_secrets_kms_key_id so EC2 IAM
    # can use AWS KMS. Local/dev falls back to Fernet with this key or a
    # deterministic dev key derived from existing app secrets.
    provider_secrets_kms_key_id: str = ""
    provider_secrets_encryption_key: str = ""

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
    # End-user Google OAuth (per-user Gmail/Calendar/Drive connect). The same
    # Web-application client is used for all three services; scopes are requested
    # incrementally per feature. Redirect URI must be registered in the GCP
    # console and point at GET /api/v1/google/oauth/callback.
    gmail_oauth_client_id: str = ""
    gmail_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""  # e.g. https://api.qualifiedcommercial.com/api/v1/google/oauth/callback
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
    # Phase 4 — per-message Workspace-mailbox inbox sync (the isolated inbox).
    # Ships dormant: even with Gmail DWD configured, the inbox sync only runs when
    # this is explicitly enabled, so the code can deploy before the feature is live.
    user_inbox_sync_enabled: bool = False

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

    # AWS SES — used for AI re-engagement email (auto-send, nurture-grade
    # deliverability), distinct from the Gmail transport used for
    # operational lender mail. Auth is the EC2 instance role (no keys);
    # the role needs ses:SendEmail. When ses_from_address is empty the
    # SES path no-ops silently — the re-engagement engine still runs,
    # the email rung just logs "dormant".
    # Transactional SMS. The selected provider is explicit and there is no
    # automatic fallback: a failed Twilio send must not unexpectedly leave by
    # AWS (or vice versa). Keep both providers configured so switching is one
    # environment change plus a service restart.
    sms_provider: str = "aws"  # aws | twilio
    sms_origination_number: str = ""
    sms_production: bool = False
    sms_webhook_token: str = ""
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_from_number: str = ""
    twilio_validate_signatures: bool = True

    # QCRelay — the SMS + WhatsApp relay on this box (/home/ubuntu/QCRelay).
    # Tailnet-only; it holds no consent state and decides nothing about who may
    # be contacted. Prefer a MagicDNS name over a raw 100.x address. When set,
    # SMS_PROVIDER may also be "android": the message leaves a physical
    # handset's SIM over Tailscale. That path is deliberately NOT gated on
    # sms_production — that flag means "AWS granted production access" and is
    # forced false on every service start by the A2P pause drop-in, which would
    # strand the tablet for an unrelated reason.
    relay_sms_url: str = ""
    relay_auth_token: str = ""

    # AI re-engagement auto-send, per channel. OFF by default and meant to stay
    # that way until someone deliberately turns it on: this is the one path that
    # texts a borrower with no human in the loop, so shipping it enabled would
    # start messaging people the moment it deployed. With it off, the engine
    # still runs and still records the composed draft for review.
    reengagement_autosend_sms: bool = False

    ses_region: str = "us-east-1"
    ses_from_address: str = ""
    ses_configuration_set: str = ""

    app_env: str = "development"
    log_level: str = "INFO"

    @field_validator("twilio_validate_signatures", mode="before")
    @classmethod
    def fail_closed_on_invalid_twilio_signature_flag(cls, value: object) -> bool:
        """A malformed secret must never disable verification or stop startup."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return True

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def bedrock_runtime_region(self) -> str:
        return self.bedrock_region or self.aws_region

    @property
    def ai_provider_enabled(self) -> bool:
        return self.bedrock_enabled or bool(self.aws_bearer_token_bedrock)


@lru_cache
def get_settings() -> Settings:
    return Settings()
