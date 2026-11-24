from pydantic import field_validator, model_validator
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
    # a PostgreSQL URL (e.g. a Neon PostgreSQL DATABASE_URL per ADR-019) for
    # durable, shared production storage.
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

    # ---- ADR-017 / ADR-018 migration configuration (foundation only) ----
    # This block defines the configuration contract for the PostgreSQL
    # migration lease and the dedicated migration identity. Nothing here
    # connects to a database, and no enforcement is wired up in this change:
    # the fail-closed behaviour (missing or identical migration identity on
    # PostgreSQL is refused) is implemented by the lease module and startup
    # guard slice. There is deliberately no single-identity fallback and a
    # missing STRIKENOVA_MIGRATION_DATABASE_URL never implies permission to
    # reuse the runtime identity.
    #
    # Provider-neutral environment selector ("production", "staging",
    # "development", ...). "production" (case-insensitive) is THE production
    # signal, so it works identically on any host (Neon, Render, a VM,
    # a disposable rehearsal cluster).
    STRIKENOVA_ENV: str = ""

    # ADR-017/018: the URL for the dedicated migration identity. Required
    # whenever DATABASE_URL is PostgreSQL; it must resolve to a different
    # database identity than DATABASE_URL. SQLite keeps its historical
    # single-process behaviour and ignores this setting.
    STRIKENOVA_MIGRATION_DATABASE_URL: str | None = None

    # ADR-018 target-identity proof: explicit equivalence declarations for the
    # rare case where one logical database is reachable through two hostnames
    # (for example a Neon pooled route and its direct route). Comma-separated
    # ``hostA=hostB`` pairs, literal hosts only. Empty (the default) means a
    # different host is a different database.
    STRIKENOVA_MIGRATION_TARGET_ALIASES: str = ""

    # ADR-017 (migration lease): the transactional lease that guarantees only
    # one process executes the migration chain (and the startup backfill) at a
    # time. There is no renewal thread; long work refreshes the lease at every
    # transaction boundary, so the TTL must exceed the slowest single protected
    # transaction (one DDL transaction or one per-user backfill transaction).
    #
    # Defaults per ADR-017 §9 and the disposable PostgreSQL 16.16 measurements
    # recorded in the design document: the whole Alembic chain and each
    # per-user backfill transaction complete in seconds, so 300 s leaves a wide
    # margin while keeping crash recovery bounded. The statement timeout is
    # half the TTL so a runaway user transaction fails (57014) before
    # ownership can expire.
    MIGRATION_LEASE_TTL_SECONDS: float = 300.0
    #: Bounded wait for the admission lock/acquisition retries.
    MIGRATION_LEASE_WAIT_SECONDS: float = 180.0
    #: Per-call lock_timeout handed to the lease functions (1..600000 ms).
    MIGRATION_LEASE_LOCK_TIMEOUT_MS: int = 10000
    #: statement_timeout for each per-user backfill transaction (< TTL).
    MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS: int = 150000

    # ADR-018 identity names. The runtime identity must hold zero access to
    # migration_internal. STRIKENOVA_OWNER_ROLE is empty for Topology A (the
    # migrator owns what Alembic creates) and is set only for Topology B (a
    # separate NOLOGIN owner reached through an explicit SET ROLE window).
    # Staging and preview environments must set their actual role names
    # explicitly instead of relying on the production defaults.
    STRIKENOVA_MIGRATOR_ROLE: str = "strikenova_prod_migrator"
    STRIKENOVA_RUNTIME_ROLE: str = "strikenova_production_app"
    STRIKENOVA_OWNER_ROLE: str = ""

    # Optional per-run nonce used only to correlate a rehearsal's lease
    # tokens. It is never published and never logged.
    STRIKENOVA_LEASE_NONCE: str = ""

    @field_validator("MIGRATION_LEASE_TTL_SECONDS")
    @classmethod
    def _validate_lease_ttl(cls, v: float) -> float:
        if not (0 < v <= 86400):
            raise ValueError(
                "MIGRATION_LEASE_TTL_SECONDS must be > 0 and <= 86400: the "
                "database rejects any other TTL with SQLSTATE 45004"
            )
        return v

    @field_validator("MIGRATION_LEASE_WAIT_SECONDS")
    @classmethod
    def _validate_lease_wait(cls, v: float) -> float:
        if not (v > 0):
            raise ValueError(
                "MIGRATION_LEASE_WAIT_SECONDS must be positive: a zero budget "
                "would fail every attempt without waiting for a peer to finish"
            )
        return v

    @field_validator("MIGRATION_LEASE_LOCK_TIMEOUT_MS")
    @classmethod
    def _validate_lease_lock_timeout(cls, v: int) -> int:
        # PostgreSQL treats lock_timeout = 0 as "wait forever"; the lease
        # functions reject it. Fail fast at the configuration boundary.
        if not (1 <= v <= 600000):
            raise ValueError(
                "MIGRATION_LEASE_LOCK_TIMEOUT_MS must be between 1 and 600000; "
                "0 means wait forever in PostgreSQL and is refused"
            )
        return v

    @field_validator("MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS")
    @classmethod
    def _validate_backfill_statement_timeout(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                "MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS must be positive"
            )
        return v

    @model_validator(mode="after")
    def _validate_statement_timeout_below_lease_ttl(self) -> "Settings":
        """A per-user backfill transaction must fail before ownership can expire.

        The rule is strict: the statement timeout must be *below* the lease
        TTL, so equality is rejected as well. The defaults (300 s TTL,
        150 000 ms timeout) satisfy it with a wide margin.
        """
        ttl_ms = self.MIGRATION_LEASE_TTL_SECONDS * 1000.0
        if self.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS >= ttl_ms:
            raise ValueError(
                "MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS "
                f"({self.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS} ms) must be "
                "strictly below MIGRATION_LEASE_TTL_SECONDS * 1000 "
                f"({ttl_ms:.0f} ms): a backfill transaction that outlives the "
                "lease would let ownership expire mid-work"
            )
        return self

    @property
    def IS_PRODUCTION(self) -> bool:
        """Detect production environments.

        Primary signal is the provider-neutral ``STRIKENOVA_ENV=production``
        (case-insensitive). The legacy Railway-era indicators are retained for
        backward compatibility with deployments that predate the neutral
        marker.
        """
        import os as _os

        if self.STRIKENOVA_ENV.strip().lower() == "production":
            return True
        return bool(
            _os.environ.get("RAILWAY_ENVIRONMENT")
            or _os.environ.get("RAILWAY_SERVICE_NAME")
            or _os.environ.get("PRODUCTION")
        )

    @property
    def FRONTEND_ORIGIN(self) -> str:
        """Primary frontend URL for OAuth redirects.
        Returns the first URL when FRONTEND_URL is comma-separated.
        """
        url = self.FRONTEND_URL.strip()
        if "," in url:
            url = url.split(",")[0].strip()
        return url

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
