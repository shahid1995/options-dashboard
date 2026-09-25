from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DEPRECATED in Phase 10.2B-2: per-user credentials in broker_connections.
    # Kept for backward compatibility during migration.  Remove in 10.2B-6.
    # New code MUST use resolve_user_credentials() instead.
    UPSTOX_API_KEY: str = ""
    UPSTOX_API_SECRET: str = ""
    # Phase 10.2B-6: UPSTOX_REDIRECT_URI is now optional.
    # If empty, auto-derived from RAILWAY_PUBLIC_DOMAIN or BACKEND_URL.
    UPSTOX_REDIRECT_URI: str = ""
    # Primary frontend URL used for OAuth redirects and CORS.
    # Supports comma-separated origins for multiple Vercel preview deployments.
    FRONTEND_URL: str = "http://localhost:3000"
    # Additional CORS origins (comma-separated) for Vercel preview branches.
    # Example: "https://options-dashboard-git-branch.vercel.app,https://options-dashboard-ruddy.vercel.app"
    ADDITIONAL_CORS_ORIGINS: str = ""
    # Phase 9C: CORS — set ALLOW_LOCALHOST_CORS=True only in development
    ALLOW_LOCALHOST_CORS: bool = False
    DEBUG: bool = False
    # Paper trading journal database. Defaults to a local SQLite file; point at
    # a PostgreSQL URL (e.g. a Railway Postgres DATABASE_URL) for durable,
    # shared production storage.
    DATABASE_URL: str | None = None
    # Historical IV foundation (Phase 4.1): the persistence model and
    # repository exist, but collection is DISABLED by default. A future phase
    # that turns it on must honour these bounds to avoid uncontrolled
    # database growth (sampling interval, retention, per-key caps).
    IV_HISTORY_ENABLED: bool = False
    IV_HISTORY_SAMPLE_SECONDS: int = 300
    IV_HISTORY_RETENTION_DAYS: int = 90
    # Historical GEX snapshots (Phase 7.3): persistence model and repository.
    # Collection is DISABLED by default.  A future phase that enables it must
    # honour these bounds to avoid uncontrolled database growth.
    GEX_HISTORY_ENABLED: bool = False
    GEX_HISTORY_SAMPLE_SECONDS: int = 60   # 1-minute live GEX snapshot interval
    GEX_HISTORY_RETENTION_DAYS: int = 90   # matches IV history retention
    # Historical NIFTY candles (Phase 7.7 research)
    CANDLE_RETENTION_DAYS: int = 365
    CANDLE_INTERVAL: str = "3min"
    CANDLE_BACKFILL_ENABLED: bool = False

    # Phase 10.2B-1: Broker credential encryption key.
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    # Changing this key invalidates all existing encrypted ciphertext —
    # rotation requires re-encrypting every row in broker_connections
    # and broker_tokens before deployment.
    TOKEN_ENCRYPTION_KEY: str = ""

    # Phase A: Google OAuth

    # GEX capture configuration:
    # - GEX_HISTORY_ENABLED: controls UI display of historical GEX data
    # - GEX_CAPTURE_ENABLED: controls background capture loop (requires GEX_USER_ID)
    # Customer Analytics Tokens must NEVER be used as implicit platform credentials.
    # GEX_CAPTURE_ENABLED=True + GEX_USER_ID enables capture for that specific user.
    GEX_CAPTURE_ENABLED: bool = False
    GEX_USER_ID: str = ""

    GOOGLE_CLIENT_ID: str = ""

    # Phase 10.2B-6: Optional backend URL for auto-deriving UPSTOX_REDIRECT_URI
    BACKEND_URL: str = ""

    # ---- Account security (2026-09-16 plan Task 2) ----
    # Token lifecycles are TTL-bounded; recent-authentication gates sensitive
    # account changes server-side. Email settings feed the provider-neutral
    # EmailSender (Task 3); credentials, when a real provider is configured,
    # live ONLY here in backend environment configuration.
    EMAIL_VERIFICATION_TTL_MINUTES: int = 60 * 24
    PASSWORD_RESET_TTL_MINUTES: int = 60
    RECENT_AUTH_TTL_MINUTES: int = 15
    EMAIL_FROM_ADDRESS: str = "StrikeNova <no-reply@strikenova.local>"
    EMAIL_BASE_URL: str = "http://localhost:3000"
    # Provider-neutral email transport: selection is EXPLICIT via
    # EMAIL_PROVIDER (inmemory|brevo). The default ``inmemory`` deterministic
    # test sender never performs network calls. Provider credentials live
    # ONLY in backend environment configuration — never in the frontend,
    # logs, or security events. Selecting ``brevo`` without BREVO_API_KEY
    # fails fast (no silent fallback).
    EMAIL_PROVIDER: str = "inmemory"
    EMAIL_API_URL: str = ""
    EMAIL_API_KEY: str = ""
    BREVO_API_KEY: str = ""
    BREVO_API_URL: str = "https://api.brevo.com/v3/smtp/email"

    @property
    def FRONTEND_ORIGIN(self) -> str:
        """Primary frontend URL for OAuth redirects.
        Returns the first URL when FRONTEND_URL is comma-separated.
        """
        url = self.FRONTEND_URL.strip()
        if "," in url:
            url = url.split(",")[0].strip()
        return url

    # Provider-neutral environment selector (e.g. "production", "staging",
    # "development"). "production" (case-insensitive) is THE production
    # signal: it requires DATABASE_URL pointing at PostgreSQL/CockroachDB
    # and fails startup closed otherwise. Unlike the legacy Railway-era
    # indicators below, it works identically on any host (Render, Railway,
    # a VM, local rehearsal).
    STRIKENOVA_ENV: str = ""

    # ADR-016 (optional identity separation): when set, Alembic migrations
    # run under this dedicated higher-privilege identity instead of the
    # runtime DATABASE_URL. The runtime identity can then be restricted to
    # DML (no schema ownership/DDL). Leave unset for single-identity
    # deployments (behavior identical to the historical model).
    STRIKENOVA_MIGRATION_DATABASE_URL: str | None = None

    @property
    def IS_PRODUCTION(self) -> bool:
        """Detect production environments.

        Primary signal is the provider-neutral ``STRIKENOVA_ENV=production``
        (case-insensitive). The legacy Railway-era indicators are kept for
        backward compatibility with deployments that predate the neutral
        marker; new deployments must set STRIKENOVA_ENV instead.
        """
        import os as _os

        if self.STRIKENOVA_ENV.strip().lower() == "production":
            return True
        return bool(
            _os.environ.get("RAILWAY_ENVIRONMENT")
            or _os.environ.get("RAILWAY_SERVICE_NAME")
            or _os.environ.get("PRODUCTION")
        )

    class Config:
        env_file = ".env"


settings = Settings()

# Phase 10.2B-6: Auto-derive UPSTOX_REDIRECT_URI if not set.
# Priority: explicit env var > BACKEND_URL > RAILWAY_PUBLIC_DOMAIN
if not settings.UPSTOX_REDIRECT_URI:
    import os as _os
    _backend_url = (
        settings.BACKEND_URL
        or _os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        or _os.environ.get("BACKEND_URL", "")
    )
    if _backend_url:
        _backend_url = _backend_url.rstrip("/")
        if not _backend_url.startswith("http"):
            _backend_url = f"https://{_backend_url}"
        settings.UPSTOX_REDIRECT_URI = f"{_backend_url}/auth/callback"
