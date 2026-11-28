import math

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

    # ADR-017/018 (mandatory identity separation on PostgreSQL): the URL for
    # the dedicated migration identity. It is REQUIRED whenever DATABASE_URL
    # is PostgreSQL and must resolve to a different database identity; a
    # missing value or one equal to DATABASE_URL fails closed with SQLSTATE
    # 45004 (app.db.assert_guarded_migration_configuration). There is no
    # single-identity PostgreSQL fallback. SQLite keeps its historical
    # single-process behaviour and ignores this setting.
    STRIKENOVA_MIGRATION_DATABASE_URL: str | None = None

    # ADR-018 target-identity proof: explicit equivalence declarations for the
    # rare case where one logical database is reachable through two hostnames
    # (for example a Neon pooled route and its direct route). Comma-separated
    # ``hostA=hostB`` pairs, literal hosts only. Empty (the default) means a
    # different host is a different database and is refused with SQLSTATE
    # 45004. A declaration never bypasses the connection-time proof that both
    # URLs reach one cluster: app.db._verify_logical_target compares the
    # pg_control_system() system_identifier, the database name and the
    # pg_database OID seen by each (all restart-stable).
    STRIKENOVA_MIGRATION_TARGET_ALIASES: str = ""

    # ADR-017 (migration lease): the transactional lease that guarantees only
    # one process executes the migration chain (and the startup backfill) at a
    # time. There is no renewal thread; long work refreshes the lease at every
    # transaction boundary, so the TTL must exceed the slowest single protected
    # transaction (one DDL transaction or one per-user backfill transaction).
    #
    # The defaults below are derived from the disposable PostgreSQL 16.16
    # measurement recorded in
    # docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md: the measured per-user
    # backfill p99 was well under a second, and the whole Alembic chain
    # completed in a few seconds. 300 s leaves a wide margin over both while
    # keeping crash recovery bounded. The statement timeout is per-statement
    # only, not cumulative across the transaction, and does not by itself bound
    # the total transaction duration. A transaction can run several statements
    # each below the statement_timeout while still holding its lease row lock
    # beyond the lease TTL. The pre-commit enter check then refuses an expired
    # owner, rolling back work that held locks longer than the TTL.
    MIGRATION_LEASE_TTL_SECONDS: float = 300.0
    #: Bounded wait for the admission lock/acquisition retries (ADR §6.7).
    MIGRATION_LEASE_WAIT_SECONDS: float = 180.0
    #: Per-call lock_timeout handed to the lease functions (1..600000 ms).
    MIGRATION_LEASE_LOCK_TIMEOUT_MS: int = 10000
    #: Per-statement execution bound applied to each backfill transaction
    #: call. A single statement must complete within this timeout; it does not
    #: accumulate across statements within the same transaction. The default
    #: 150000 ms (2.5 min) is well below the 300 s lease TTL, providing
    #: margin for a runaway statement to fail (57014) before the lease can
    #: expire at pre-commit enter.
    MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS: int = 150000

    # ADR-018 identity names. The runtime identity must hold zero access to
    # migration_internal; the owner role is only used inside an explicit
    # SET ROLE window around Alembic DDL (Topology B).
    STRIKENOVA_MIGRATOR_ROLE: str = "strikenova_prod_migrator"
    STRIKENOVA_RUNTIME_ROLE: str = "strikenova_production_app"
    STRIKENOVA_OWNER_ROLE: str = ""

    # Optional per-run nonce used only to correlate a rehearsal's lease
    # tokens. It is never published and never logged.
    STRIKENOVA_LEASE_NONCE: str = ""

    @field_validator("MIGRATION_LEASE_TTL_SECONDS")
    @classmethod
    def _validate_lease_ttl(cls, v: float) -> float:
        # ``math.isfinite`` is explicit: NaN compares false against every
        # bound, so an ordering test alone is easy to get wrong, and infinity
        # would otherwise be the only value the upper bound catches.
        if not math.isfinite(v) or not (0 < v <= 86400):
            raise ValueError(
                "MIGRATION_LEASE_TTL_SECONDS must be finite, > 0 and <= 86400: "
                "the database rejects any other TTL with SQLSTATE 45004"
            )
        return v

    @field_validator("MIGRATION_LEASE_WAIT_SECONDS")
    @classmethod
    def _validate_lease_wait(cls, v: float) -> float:
        # A NaN or infinite wait would make the monotonic admission deadline
        # meaningless (``now + nan`` is never exceeded), so both are refused
        # at the configuration boundary rather than inside the retry loop.
        if not math.isfinite(v) or not (0 < v <= 86400):
            raise ValueError(
                "MIGRATION_LEASE_WAIT_SECONDS must be finite, > 0 and <= 86400: "
                "an unbounded or undefined admission wait is refused"
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
        """Require the per-statement timeout to be below the lease TTL.

        ``statement_timeout`` bounds each individual SQL statement; it does not
        accumulate across the statements of one transaction. A multi-statement
        backfill transaction can therefore keep holding its lease-row lock
        beyond the TTL. The TTL is an expiry/takeover threshold, not a hard
        duration limit on an already-running transaction: the mandatory
        pre-commit ``enter`` ownership check rejects expired ownership, so the
        protected transaction rolls back instead of committing work it no
        longer owns.

        The runbook rule is strict: the statement timeout must be *below* the
        lease TTL, so equality is rejected as well. The defaults (300 s TTL,
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
