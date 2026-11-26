import os
import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Database path / URL resolution
# ---------------------------------------------------------------------------
#
# SQLite remains the default for local development and for the current
# production environment until the explicit PostgreSQL switchover phase.
# When DATABASE_URL points at PostgreSQL, normalize bare postgres URLs to the
# installed psycopg 3 SQLAlchemy dialect.
# ---------------------------------------------------------------------------

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB_PATH = os.path.join(_BACKEND_DIR, "paper_journal.db")


def normalize_database_url(url: str) -> str:
    """Normalize database URLs to dialects supported by this application.

    Railway may provide either ``postgres://`` or ``postgresql://`` style
    URLs. The application uses psycopg 3, so bare PostgreSQL URLs are mapped
    to ``postgresql+psycopg://``. Explicit driver URLs are preserved.
    SQLite and other SQLAlchemy URLs are returned unchanged.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


#: Query parameters psycopg merges into the connection arguments *after* the
#: URL authority and path. ``?user=`` or ``?dbname=`` can therefore make a URL
#: authenticate as, or connect to, something other than what the URL appears to
#: name. No URL whose identity is used as evidence may carry one of these.
_FORBIDDEN_CONNECTION_QUERY_KEYS = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "dbname",
        "database",
        "user",
        "password",
        "service",
        "servicefile",
        "passfile",
    }
)

#: Neon's documented native connection-string grammar:
#: ``<endpoint-id>[-pooler].<region>.<cloud>.neon.tech``. The first label is the
#: *compute endpoint* identifier (e.g. ``ep-cool-darkness-123456``) — never a
#: branch or project identifier — and the pooled route for one endpoint differs
#: from its direct route only by the documented ``-pooler`` suffix. Neon states
#: that a different compute or branch has a different host.
_NEON_DOMAIN = "neon.tech"
_NEON_ENDPOINT_ID_RE = re.compile(r"^ep-[a-z0-9]+(?:-[a-z0-9]+)*$")
_NEON_HOST_RE = re.compile(
    r"^(?P<endpoint>ep-[a-z0-9]+(?:-[a-z0-9]+)*?)"
    r"(?P<pooler>-pooler)?"
    r"\.(?P<region>[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"\.(?P<cloud>aws|azure)"
    r"\.neon\.tech$"
)

#: Connection-option names Neon documents for naming the compute endpoint when
#: the client cannot send SNI: ``options=endpoint=<id>`` and its deprecated
#: former name ``project=<id>``. The option routes the connection, so it must
#: agree with the hostname before a URL may be used as identity evidence.
_NEON_ENDPOINT_OPTIONS = frozenset({"endpoint", "project"})

#: A libpq ``dbname`` value is expanded into connection parameters when it
#: contains ``=`` or begins with a URI scheme. Neon documents
#: ``dbname=neondb options=endpoint=<id>`` as an alternative way to name the
#: compute, so a database component that would be expanded must not be able to
#: name a different one.
_NEON_DBNAME_URI_PREFIXES = ("postgresql://", "postgres://")
_NEON_DBNAME_FORBIDDEN_KEYS = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "user",
        "password",
        "service",
        "servicefile",
        "passfile",
    }
)

#: Neon's last-resort password form, ``endpoint=<id>;<password>`` (or ``$``).
#: Neon's proxy interprets the prefix as a compute selector, and Neon documents
#: that the form downgrades authentication from SCRAM-SHA-256 to password;
#: libpq and psycopg pass the value through untouched, so the application can
#: never see which compute it would select.
_NEON_PASSWORD_PREFIX_RE = re.compile(
    r"^\s*(?:endpoint|project)\s*=", re.IGNORECASE
)

#: The server's own view of a runtime connection: the authenticated login
#: (``session_user``), the role it currently runs as (``current_user``, which a
#: ``SET ROLE`` or an inherited membership changes) and that login's attributes.
_RUNTIME_IDENTITY_SQL = (
    "SELECT session_user, current_user, r.rolcanlogin, r.rolsuper, "
    "r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, r.rolreplication "
    "FROM pg_roles r WHERE r.rolname = session_user"
)

_RUNTIME_FORBIDDEN_ATTRIBUTES: tuple[tuple[int, str], ...] = (
    (3, "SUPERUSER"),
    (4, "CREATEROLE"),
    (5, "CREATEDB"),
    (6, "BYPASSRLS"),
    (7, "REPLICATION"),
)


def _raise_config_error(message: str) -> None:
    from app._migration_lease import LeaseError, SQLSTATE_CONFIG

    raise LeaseError(SQLSTATE_CONFIG, message)


def _runtime_identity_problems(
    observed,
    runtime_role: str,
    migrator_role: str,
    *,
    stage: str,
) -> list[str]:
    """Problems with one live runtime connection's identity (none == verified).

    The URL's declared username is not evidence: the check reads the principal
    PostgreSQL authenticated and the role the session currently runs as, so a
    ``SET ROLE``, an inherited membership or a libpq query parameter cannot
    make the runtime look like a different, safer identity.
    """
    if observed is None:
        return [
            f"{stage}: the runtime connection's login is not a role in pg_roles, "
            "so its identity cannot be proven"
        ]
    session_user, current_user = observed[0], observed[1]
    problems: list[str] = []
    if runtime_role == migrator_role:
        problems.append(
            "the configured runtime identity and migration identity are the same "
            f"role ({runtime_role!r}); ADR-018 requires two distinct logins"
        )
    if session_user != runtime_role:
        problems.append(
            f"{stage}: session_user is {session_user!r}, not the configured "
            f"runtime identity {runtime_role!r}"
        )
    if current_user != runtime_role:
        problems.append(
            f"{stage}: current_user is {current_user!r}, not the configured "
            f"runtime identity {runtime_role!r} (unexpected role elevation)"
        )
    if not observed[2]:
        problems.append(
            f"{stage}: runtime identity {runtime_role!r} is not a LOGIN role"
        )
    for index, name in _RUNTIME_FORBIDDEN_ATTRIBUTES:
        if observed[index]:
            problems.append(
                f"{stage}: runtime identity {runtime_role!r} has {name}"
            )
    return problems


#: The restart-stable cluster/database identity proven by startup preflight.
#: The runtime engine is created at import time, before any connection can be
#: proven, so the listener reads this shared holder on every new connection:
#: empty means "not proven yet" (the principal is still checked), set means
#: every later runtime connection must reach exactly that target. A PostgreSQL
#: restart or Neon compute suspend/wake does not change this value, so a
#: reconnection after either is accepted; a different cluster or database is
#: not.
_RUNTIME_TARGET_SIGNATURE: list = [None]


def _install_runtime_identity_guard(
    engine_, runtime_role: str, migrator_role: str
) -> None:
    """Verify the real principal of EVERY runtime DBAPI connection.

    Sampling one connection would leave the rest of the pool unproven, and a
    pool member that authenticated as another login (for example through a
    ``?user=`` query parameter) would then serve application traffic while the
    privilege verifier still inspected the configured runtime role.
    """
    from sqlalchemy import event

    @event.listens_for(engine_, "connect")
    def _verify_runtime_identity(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        signature = None
        try:
            cursor.execute(_RUNTIME_IDENTITY_SQL)
            observed = cursor.fetchone()
            expected = _RUNTIME_TARGET_SIGNATURE[0]
            if expected is not None:
                signature = _dbapi_logical_identity(cursor)
        finally:
            cursor.close()
            dbapi_connection.rollback()
        problems = _runtime_identity_problems(
            observed,
            runtime_role,
            migrator_role,
            stage="the runtime engine's new connection",
        )
        if signature is not None and signature != _RUNTIME_TARGET_SIGNATURE[0]:
            problems.append(
                "the runtime engine's new connection does not reach the same "
                "PostgreSQL cluster/database identity proven during startup "
                f"preflight (observed database {signature[0]!r}); refusing to "
                "serve application queries on an unproven target"
            )
        if problems:
            # Never embed the URL or password: only the observed identity and
            # the configured role names appear here.
            _raise_config_error("; ".join(problems))


def _create_runtime_engine(url: str, **kwargs):
    """Build a runtime engine whose every new connection is identity-checked.

    ``STRIKENOVA_RUNTIME_ROLE`` is mandatory on PostgreSQL: the whole point of
    the guard is that the authenticated principal is the named runtime
    identity, so an unnamed identity fails closed instead of skipping the
    check.
    """
    runtime_role = (getattr(settings, "STRIKENOVA_RUNTIME_ROLE", "") or "").strip()
    migrator_role = (getattr(settings, "STRIKENOVA_MIGRATOR_ROLE", "") or "").strip()
    if not runtime_role or not migrator_role:
        _raise_config_error(
            "STRIKENOVA_RUNTIME_ROLE and STRIKENOVA_MIGRATOR_ROLE must both be "
            "configured on PostgreSQL so the authenticated runtime principal "
            "can be proven distinct from the migration identity"
        )
    eng = create_engine(url, **kwargs)
    _install_runtime_identity_guard(eng, runtime_role, migrator_role)
    return eng


def _engine():
    if settings.DATABASE_URL:
        url = normalize_database_url(settings.DATABASE_URL)
    else:
        url = f"sqlite:///{_DEFAULT_DB_PATH}"

    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        eng = create_engine(url, connect_args=connect_args)

        # SQLite-only crash/concurrency tuning. Never register these hooks
        # against PostgreSQL or another SQLAlchemy dialect.
        @event.listens_for(eng, "connect")
        def _set_wal(dbapi_conn, _rec):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA synchronous=NORMAL")
    else:
        # PostgreSQL production/staging configuration. The runtime identity
        # guard is attached here so every pooled connection is verified. It is
        # PostgreSQL-only: a legacy CockroachDB URL keeps its historical
        # engine, and CockroachDB is retired for migrations by ADR-019.
        options = dict(
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
        # Inline scheme check: ``_engine()`` runs at import time, before the
        # later ADR helpers are defined. ``cockroachdb+psycopg`` is not
        # PostgreSQL here and keeps its historical engine.
        if url.split(":", 1)[0].startswith("postgres"):
            eng = _create_runtime_engine(url, **options)
        else:
            eng = create_engine(url, **options)

    return eng


# ---------------------------------------------------------------------------
# Production safety validation (ADR-014, fail-closed)
# ---------------------------------------------------------------------------


def validate_production_config() -> None:
    """Fail closed when production database configuration is unsafe.

    Called at module import time BEFORE any engine is constructed. When the
    production signal is active (``STRIKENOVA_ENV=production`` —
    provider-neutral — or the legacy Railway-era indicators), the application
    MUST NOT be able to start on SQLite:

    - missing ``DATABASE_URL``       -> RuntimeError (no silent fallback)
    - SQLite ``DATABASE_URL``        -> RuntimeError (scheme match is
      case-insensitive: ``sqlite:``, ``SQLITE:``, and mixed case all refused)
    - PostgreSQL/CockroachDB ``DATABASE_URL`` -> accepted

    Non-production environments are untouched: intentional local SQLite
    development/test behavior is preserved.

    Failure messages never embed the connection string (no credentials in
    logs or exception text).
    """
    import logging

    logger = logging.getLogger(__name__)

    if not settings.IS_PRODUCTION:
        return

    if not settings.DATABASE_URL:
        logger.error(
            "Production environment detected but DATABASE_URL is not set. "
            "Refusing to start: the application would silently fall back to "
            "local SQLite, which is unsuitable for production. Set "
            "DATABASE_URL to a PostgreSQL/CockroachDB connection string."
        )
        raise RuntimeError(
            "production database configuration is required: DATABASE_URL is "
            "not set while production mode is enabled. The application "
            "refuses to silently fall back to SQLite."
        )

    normalized = normalize_database_url(settings.DATABASE_URL)
    if normalized.lower().startswith("sqlite"):
        logger.error(
            "Production environment detected but DATABASE_URL points to "
            "SQLite (connection string masked). Refusing to start: "
            "production must use PostgreSQL/CockroachDB."
        )
        raise RuntimeError(
            "production database configuration is required: DATABASE_URL "
            "points to SQLite while production mode is enabled. Set "
            "DATABASE_URL to a PostgreSQL/CockroachDB connection string."
        )

    # Explicit allowlist of the only dialect families production supports
    # (PostgreSQL/CockroachDB after normalization). A malformed or unknown
    # scheme (e.g. ``unknown://``, ``postgres+nosuchdriver://``) must fail
    # through THIS error contract — not with a raw SQLAlchemy
    # ``NoSuchModuleError`` from engine construction. Credential text is
    # never included: only the scheme family is echoed.
    #
    # CockroachDB schemes are REQUIRED: CockroachDB Cloud is the mandated
    # production database, the SQLAlchemy psycopg dialect cannot parse
    # CockroachDB's server version string (startup aborts with
    # "Could not determine version from string 'CockroachDB CCL ...'"), and
    # the project's declared dependency sqlalchemy-cockroachdb provides the
    # working ``cockroachdb[+psycopg]://`` dialects. Staging's production
    # configuration uses ``cockroachdb+psycopg://``. Rejecting these schemes
    # would make it impossible to boot against the intended production
    # database while the plain-PostgreSQL schemes cannot actually run on it.
    allowed_prefixes = (
        "postgresql+psycopg://",  # normalize_database_url() target
        "postgresql://",          # accepted pre-normalization form
        "postgres://",            # legacy pre-normalization form
        "cockroachdb+psycopg://",  # sqlalchemy-cockroachdb (psycopg 3) — CRDB
        "cockroachdb://",          # sqlalchemy-cockroachdb dialect — CRDB
    )
    if not normalized.startswith(allowed_prefixes):
        scheme = normalized.split(":", 1)[0]
        logger.error(
            "Production environment detected but DATABASE_URL uses an "
            "unsupported scheme (connection string masked). Refusing to "
            "start: production must use PostgreSQL/CockroachDB."
        )
        raise RuntimeError(
            "production database configuration is required: DATABASE_URL "
            f"uses unsupported scheme '{scheme}' while production mode is "
            "enabled. Set DATABASE_URL to a PostgreSQL/CockroachDB "
            "connection string."
        )


# Validation MUST run before engine construction: a malformed SQLite URL
# could otherwise fail inside create_engine() with a dialect error before the
# required production-configuration error is raised.
validate_production_config()


engine = _engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ---------------------------------------------------------------------------
# Migration-state validation (Day 5)
# ---------------------------------------------------------------------------


def validate_migration_state() -> dict:
    """Validate Alembic migration state against the expected head.

    Returns a dict with:
    - "status": "current" | "behind" | "uninitialised" | "error"
    - "expected_head": the single expected Alembic head revision
    - "actual_revision": the revision stamped in the database (or None)
    - "alembic_heads": list of heads from the migration script directory
    - "error": error message if validation failed

    Never raises — always returns a result dict.
    Credentials are never included in the output.
    """
    import logging

    logger = logging.getLogger(__name__)
    result: dict = {
        "status": "error",
        "expected_head": None,
        "actual_revision": None,
        "alembic_heads": [],
        "error": None,
    }

    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext

        alembic_cfg = AlembicConfig("alembic.ini")
        # This path genuinely needs the INI option (no supplied connection),
        # and ConfigParser treats ``%`` as interpolation: a URL-encoded
        # credential character such as ``%40`` must be escaped.
        alembic_cfg.set_main_option(
            "sqlalchemy.url", str(engine.url).replace("%", "%%")
        )
        script = ScriptDirectory.from_config(alembic_cfg)
        heads = script.get_heads()
        result["alembic_heads"] = list(heads)

        if len(heads) != 1:
            result["status"] = "error"
            result["error"] = (
                f"Expected exactly 1 Alembic head, got {len(heads)}: {heads}"
            )
            return result

        result["expected_head"] = heads[0]

        # Collect every revision ID in the migration graph.  This lets us
        # distinguish "behind" (revision is in the graph but not the head)
        # from "unknown" (revision is not in the graph at all).
        #
        # Uses Alembic's full-graph walk: a manual down_revision walk that
        # picks downs[0] at merge points silently drops sibling parents and
        # misclassifies real chain revisions as "unknown" (Day41 fix — the
        # graph has merge points since Day38).
        all_revisions: set = set()
        try:
            all_revisions = {r.revision for r in script.walk_revisions()}
        except Exception:
            # If graph walking fails, fall back to checking only against head
            all_revisions = set(heads)

        with engine.connect() as conn:
            mc = MigrationContext.configure(conn)
            current_rev = mc.get_current_revision()
            result["actual_revision"] = current_rev

        if current_rev is None:
            result["status"] = "uninitialised"
            result["error"] = "No alembic_version record found"
        elif current_rev == heads[0]:
            result["status"] = "current"
        elif current_rev in all_revisions:
            result["status"] = "behind"
            result["error"] = (
                f"Database revision {current_rev} != expected head {heads[0]}"
            )
        else:
            result["status"] = "unknown"
            result["error"] = (
                f"Database revision {current_rev} is not present in the "
                f"migration graph (expected one of: {sorted(all_revisions)})"
            )

    except Exception as e:
        result["status"] = "error"
        # Mask any potential credentials in the error message
        error_str = str(e)
        for sensitive in ["password", "secret", "token"]:
            if sensitive in error_str.lower():
                error_str = "Database connection error (details masked)"
                break
        result["error"] = error_str
        logger.warning("Migration state validation failed: %s", result["error"])

    return result



def get_database_path() -> str:
    """Return the configured database URL or local SQLite path."""
    if settings.DATABASE_URL:
        return normalize_database_url(settings.DATABASE_URL)
    return _DEFAULT_DB_PATH


def get_db():
    """FastAPI dependency: yields a database session, closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def resolve_migration_database_url(explicit_url: str | None = None) -> str:
    """Resolve the candidate database URL for an Alembic invocation.

    Precedence (PR #107 review contract):

    1. ``explicit_url`` — an operator-supplied ``sqlalchemy.url`` (the
       alembic.ini / CLI-configured value passed in by ``alembic/env.py``);
       the highest-priority, intentional operator override.
    2. ``STRIKENOVA_MIGRATION_DATABASE_URL`` — the dedicated migration
       identity.
    3. ``DATABASE_URL`` — the runtime URL.
    4. The local SQLite file when nothing is configured.

    This resolver only *proposes* a URL; it is not the policy boundary.
    :func:`assert_guarded_migration_configuration` is, and it fails closed
    (``45004``) when ``DATABASE_URL`` is PostgreSQL and
    ``STRIKENOVA_MIGRATION_DATABASE_URL`` is missing or resolves to the same
    database identity. The fallback in step 3 therefore cannot produce an
    unguarded PostgreSQL migration: it only serves SQLite, which keeps its
    historical single-process, single-identity behaviour.

    Blank/whitespace values are treated as unset at every level, and every
    resolved URL passes through :func:`normalize_database_url` exactly like
    the startup migration path (``cockroachdb+psycopg://`` and other
    explicit driver URLs are preserved). This resolver never changes which
    identity serves application traffic — the runtime engine keeps using
    ``DATABASE_URL`` (ADR-014's production guard semantics are untouched).
    """
    normalized_explicit = (explicit_url or "").strip()
    if normalized_explicit and not normalized_explicit.startswith("driver://"):
        return normalize_database_url(normalized_explicit)
    migration_url = getattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", None)
    if migration_url and migration_url.strip():
        return normalize_database_url(migration_url.strip())
    runtime_url = settings.DATABASE_URL
    if runtime_url and runtime_url.strip():
        return normalize_database_url(runtime_url.strip())
    return f"sqlite:///{_DEFAULT_DB_PATH}"


def _migration_engine_url() -> str:
    """Resolve the URL used by Alembic migrations.

    Identity separation (ADR-017/018): on PostgreSQL every migration runs
    under the dedicated migration identity named by
    ``STRIKENOVA_MIGRATION_DATABASE_URL``. Single-identity deployment is no
    longer supported for PostgreSQL: a missing or identical migration
    identity is refused by :func:`assert_guarded_migration_configuration`
    with SQLSTATE ``45004`` rather than silently reusing the runtime URL.
    SQLite keeps the historical single-process path.

    Delegates to :func:`resolve_migration_database_url` so the startup path
    and the standalone Alembic CLI resolve URLs identically.
    """
    return resolve_migration_database_url()


# ---------------------------------------------------------------------------
# ADR-017 migration lease / ADR-018 identity separation
# ---------------------------------------------------------------------------


def _runtime_url_from_settings() -> str | None:
    value = (settings.DATABASE_URL or "").strip()
    return normalize_database_url(value) if value else None


def _is_postgres_url(url: str) -> bool:
    return url.split(":", 1)[0].startswith(("postgres", "cockroachdb"))


@dataclass(frozen=True)
class _NeonComputeEndpoint:
    """The Neon compute endpoint a native Neon hostname names.

    ``endpoint`` is the compute endpoint identifier in the hostname's first
    label. Its documented identity is textual only: a different Neon compute or
    branch has a different host, and the pooled and direct routes for one
    compute differ only by the ``-pooler`` suffix. It is a *compute endpoint*
    identifier — not a branch identifier and not a project identifier — and it
    is neither provider-verified nor evidence about provider-side ownership.
    """

    endpoint: str
    region: str
    cloud: str
    pooled: bool

    @property
    def identity(self) -> tuple[str, str, str]:
        """Everything that distinguishes a compute, except the pooler variant."""
        return (self.endpoint, self.region, self.cloud)


@dataclass(frozen=True)
class _MigrationTarget:
    """One parsed PostgreSQL endpoint: role, host, port and database.

    Passwords are deliberately absent: they are not identity attributes, a
    different password is not a different identity, and no value of this
    dataclass may ever appear in a log or exception.
    """

    backend: str
    role: str
    host: str
    port: int
    database: str
    neon: _NeonComputeEndpoint | None = None

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self.host, self.port)


def _config_error(message: str):
    from app._migration_lease import LeaseError, SQLSTATE_CONFIG

    return LeaseError(SQLSTATE_CONFIG, message)


def _neon_compute_endpoint(
    host: str, *, label: str
) -> _NeonComputeEndpoint | None:
    """Read the compute endpoint from a native Neon hostname.

    Returns ``None`` for a host outside ``neon.tech``: generic PostgreSQL
    providers are never assumed to share Neon's grammar. A host *under*
    ``neon.tech`` that does not match the documented grammar is refused rather
    than handed to the generic alias policy, because an unreadable Neon compute
    identity is exactly the case an alias could otherwise paper over.
    """
    if host != _NEON_DOMAIN and not host.endswith("." + _NEON_DOMAIN):
        return None
    match = _NEON_HOST_RE.match(host)
    if match is None:
        raise _config_error(
            f"{label} names a host under neon.tech that does not match Neon's "
            "documented '<endpoint-id>[-pooler].<region>.<cloud>.neon.tech' "
            "grammar; a Neon host whose compute endpoint cannot be read is "
            "refused rather than treated as a generic host"
        )
    return _NeonComputeEndpoint(
        endpoint=match.group("endpoint"),
        region=match.group("region"),
        cloud=match.group("cloud"),
        pooled=bool(match.group("pooler")),
    )


def _declared_compute_endpoint(query, *, label: str) -> str | None:
    """Read ``options=endpoint=<id>`` (or deprecated ``project=<id>``) from a URL.

    Neon documents these connection options as the way a client without SNI
    names the compute it wants, so the option *routes* the connection: a value
    that disagrees with the hostname would reach a compute the URL does not
    name. Ordinary runtime parameters carried in the same ``options`` string
    (``-c name=value``) cannot change the endpoint and are ignored.
    """
    declared: set[str] = set()
    for key, value in (query or {}).items():
        if str(key).lower() != "options":
            continue
        for token in str(value).split():
            name, separator, raw = token.partition("=")
            if not separator or name.lower() not in _NEON_ENDPOINT_OPTIONS:
                continue
            endpoint = raw.strip().strip("'\"")
            if not _NEON_ENDPOINT_ID_RE.match(endpoint):
                raise _config_error(
                    f"{label} carries a connection option naming a malformed "
                    "Neon compute endpoint; refusing an endpoint identity that "
                    "cannot be read"
                )
            declared.add(endpoint)
    if len(declared) > 1:
        raise _config_error(
            f"{label} carries connection options naming more than one Neon "
            "compute endpoint; refusing an ambiguous endpoint identity"
        )
    return declared.pop() if declared else None


def _assert_endpoint_option_matches(
    neon: _NeonComputeEndpoint | None,
    declared: str | None,
    *,
    label: str,
    source: str,
) -> None:
    """Require a documented endpoint option to agree with the hostname."""
    if declared is None:
        return
    if neon is None:
        raise _config_error(
            f"{label} carries a Neon compute-endpoint {source} while its host "
            "is not a native Neon host; that option has no documented meaning "
            "for another server, so the pair is refused rather than assumed"
        )
    if declared != neon.endpoint:
        raise _config_error(
            f"{label} carries a Neon compute-endpoint {source} naming "
            f"{declared!r} while its host names compute endpoint "
            f"{neon.endpoint!r}; the option routes the connection, so the "
            "hostname and the option must name one compute"
        )


def _assert_no_neon_dbname_routing(
    database: str, neon: _NeonComputeEndpoint | None, *, label: str
) -> None:
    """Refuse a Neon database component that libpq would expand as a conninfo string.

    Neon documents ``dbname=neondb options=endpoint=<id>`` as an alternative way
    to name the compute when the client cannot send SNI. libpq only expands such
    a value when it contains an equal sign or begins with a URI scheme, so only
    those values are parsed — with libpq's own parser, never a substring guess —
    and only on a native Neon host. A value carrying ``host``, ``hostaddr``,
    ``port``, ``user``, ``password``, ``service``, ``servicefile`` or
    ``passfile`` is refused because it can retarget the connection or change
    the principal, and an endpoint option must agree with the hostname.

    Ordinary database names, including dotted, dashed and underscored ones, are
    never parsed. This is a static check only: it runs before any engine or
    connection exists, and it does not depend on the driver choosing to expand
    the value (with psycopg 3 the value is passed through verbatim).
    """
    if neon is None:
        return
    if "=" not in database and not database.lower().startswith(
        _NEON_DBNAME_URI_PREFIXES
    ):
        return
    from psycopg.conninfo import conninfo_to_dict

    try:
        expanded = conninfo_to_dict(database)
    except Exception as exc:  # noqa: BLE001 - never echo the value
        raise _config_error(
            f"{label} uses a database component that libpq would treat as a "
            "connection string and that cannot be parsed "
            f"({type(exc).__name__}); refusing an ambiguous Neon target"
        ) from None
    forbidden = sorted(set(expanded) & _NEON_DBNAME_FORBIDDEN_KEYS)
    if forbidden:
        raise _config_error(
            f"{label} uses a database component that expands into connection "
            f"parameter(s) {forbidden} capable of overriding the host, login "
            "or password this check proves; refusing a Neon database value "
            "that carries connection parameters"
        )
    options = expanded.get("options")
    _assert_endpoint_option_matches(
        neon,
        _declared_compute_endpoint({"options": options}, label=label)
        if options
        else None,
        label=label,
        source="database-component option",
    )


def _assert_no_neon_password_routing(
    password: str | None, neon: _NeonComputeEndpoint | None, *, label: str
) -> None:
    """Refuse Neon's endpoint-prefixed password form on a native Neon host.

    ``endpoint=<id>;<password>`` (or ``endpoint=<id>$<password>``) is a
    documented workaround for clients without SNI: Neon's proxy reads the
    prefix as a compute selector, and Neon documents that it downgrades
    authentication from SCRAM-SHA-256 to password authentication. libpq and
    psycopg pass the password through verbatim, so the application cannot tell
    which compute the proxy would choose, and the hostname check would be
    bypassed by a value that is not even a hostname. The form is therefore
    refused outright — even when the prefix happens to name the hostname's
    endpoint — rather than supported with weaker authentication. The message
    never contains the password.
    """
    if neon is None or not password:
        return
    if _NEON_PASSWORD_PREFIX_RE.match(password):
        raise _config_error(
            f"{label} uses Neon's endpoint-prefixed password form, which Neon's "
            "proxy interprets as a compute selector and which downgrades "
            "authentication from SCRAM-SHA-256 to password; use a plain "
            "password and select the compute with the hostname"
        )


def _parse_migration_target(url: str, *, label: str) -> _MigrationTarget:
    """Parse a URL into a :class:`_MigrationTarget`, failing closed.

    A malformed, blank, partial or non-PostgreSQL URL is refused with SQLSTATE
    45004. The raised message names only the setting and the failing attribute:
    the URL itself (which may carry a password) is never embedded.
    """
    from sqlalchemy.engine import make_url

    value = (url or "").strip()
    if not value:
        raise _config_error(f"{label} is not configured")
    try:
        parsed = make_url(value)
    except Exception as exc:  # noqa: BLE001 - never echo the URL in the error
        raise _config_error(
            f"{label} is not a usable SQLAlchemy database URL ({type(exc).__name__})"
        ) from None

    # libpq merges query parameters into the connect arguments after the URL
    # authority/path, so ``?user=`` or ``?dbname=`` can change the principal or
    # database this URL really uses. Such a URL cannot serve as identity
    # evidence; refuse it before anything is derived from it.
    query_keys = {str(key).lower() for key in (parsed.query or {})}
    overrides = query_keys & _FORBIDDEN_CONNECTION_QUERY_KEYS
    if overrides:
        raise _config_error(
            f"{label} carries connection query parameter(s) {sorted(overrides)} "
            "that can override the host, database or login this check proves"
        )
    # A comma-separated host list is a libpq failover list: a later host may
    # serve a cluster this check never proved. Refuse it rather than guess.
    if "," in (parsed.host or ""):
        raise _config_error(
            f"{label} names multiple hosts; a failover host list cannot be "
            "proven to reach the one target the migration lease protects"
        )

    backend = parsed.get_backend_name()
    if backend in ("postgres", "postgresql"):
        backend = "postgresql"
    if backend != "postgresql":
        # SQLite/CockroachDB and every other dialect are not alternative
        # PostgreSQL migration targets (ADR-018; CockroachDB is retired).
        raise _config_error(
            f"{label} targets the {backend!r} dialect; the ADR-017 lease and the "
            "ADR-018 identity separation are PostgreSQL-only"
        )

    role = (parsed.username or "").strip()
    host = (parsed.host or "").strip().rstrip(".").lower()
    database = (parsed.database or "").strip()
    try:
        port = int(parsed.port or 5432)
    except (TypeError, ValueError):
        raise _config_error(f"{label} has a non-numeric port") from None

    if not host:
        raise _config_error(f"{label} does not name a PostgreSQL host")
    if not database:
        raise _config_error(f"{label} does not name a PostgreSQL database")
    if not role:
        raise _config_error(
            f"{label} does not name a PostgreSQL login; an anonymous or "
            "implicit identity cannot be proven distinct"
        )
    if not (0 < port <= 65535):
        raise _config_error(f"{label} has an out-of-range port")

    # Neon compute-endpoint identity, read from the hostname and required to
    # agree with any ``options=endpoint=``/``project=`` routing option. This is
    # static evidence: it is compared before an engine or a connection exists.
    neon = _neon_compute_endpoint(host, label=label)
    # Every documented way of naming a Neon compute must agree with the
    # hostname: the query parameter, the database component, and the password
    # prefix (which is refused outright). All of this is static evidence — it is
    # decided before an engine, a connection, a lease or any DDL exists.
    _assert_endpoint_option_matches(
        neon,
        _declared_compute_endpoint(parsed.query, label=label),
        label=label,
        source="connection query option",
    )
    _assert_no_neon_dbname_routing(parsed.database or "", neon, label=label)
    _assert_no_neon_password_routing(parsed.password, neon, label=label)
    return _MigrationTarget(backend, role, host, port, database, neon)


def _declared_endpoint_aliases() -> dict[str, set[str]]:
    """Parse ``STRIKENOVA_MIGRATION_TARGET_ALIASES`` into equivalence sets.

    The setting is a comma-separated list of explicit ``hostA=hostB`` pairs,
    e.g. a Neon pooled route and its direct route for the *same* endpoint. It
    is an explicit operator declaration, never an implicit suffix heuristic:
    without a declaration, different host strings are different targets.

    Each pair is validated (both hosts present, distinct, no empty entries)
    and malformed configuration fails closed with SQLSTATE 45004 rather than
    being ignored.
    """
    raw = (getattr(settings, "STRIKENOVA_MIGRATION_TARGET_ALIASES", "") or "").strip()
    groups: dict[str, set[str]] = {}
    if not raw:
        return groups

    parent: dict[str, str] = {}

    def find(host: str) -> str:
        parent.setdefault(host, host)
        while parent[host] != host:
            parent[host] = parent[parent[host]]
            host = parent[host]
        return host

    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if item.count("=") != 1:
            raise _config_error(
                "STRIKENOVA_MIGRATION_TARGET_ALIASES entries must be 'hostA=hostB' pairs"
            )
        left, right = (part.strip().rstrip(".").lower() for part in item.split("="))
        if not left or not right or left == right:
            raise _config_error(
                "STRIKENOVA_MIGRATION_TARGET_ALIASES must declare two distinct, "
                "non-empty hosts per entry"
            )
        if any(ch in left + right for ch in "*/ ") or not left or not right:
            raise _config_error(
                "STRIKENOVA_MIGRATION_TARGET_ALIASES must name literal hosts, "
                "not patterns or wildcards"
            )
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for host in parent:
        groups.setdefault(find(host), set()).add(host)
    return groups


def _assert_same_neon_compute_endpoint(
    runtime: _MigrationTarget, migration: _MigrationTarget
) -> None:
    """Refuse two different Neon compute endpoints (and mixed Neon/non-Neon pairs).

    Neon's documented grammar makes the compute endpoint identity textual: a
    different compute or branch is a different host, and the pooled and direct
    routes for one compute differ only by ``-pooler``. Two different endpoint
    identifiers are therefore two different targets whatever signature the
    servers report — a branch is a copy-on-write fork, so it shares its
    parent's ``system_identifier`` and database OIDs and the server-side
    comparison cannot separate them. An explicit alias asserts that two
    *hostnames* for one endpoint are one route; it is never evidence that two
    endpoints are one target, so it cannot override this refusal.

    A Neon host paired with a host that carries no Neon compute endpoint is
    refused too: no documented equivalence between the two exists, so the pair
    is not assumed to be one target.
    """
    if runtime.neon is None and migration.neon is None:
        return
    if runtime.neon is None or migration.neon is None:
        raise _config_error(
            "the runtime and migration URLs mix a native Neon host with a host "
            "that carries no Neon compute endpoint; no documented equivalence "
            "between them exists, so the pair is refused"
        )
    if runtime.neon.identity != migration.neon.identity:
        raise _config_error(
            "the runtime and migration URLs name different Neon compute "
            f"endpoints ({runtime.neon.endpoint} in "
            f"{runtime.neon.region}.{runtime.neon.cloud} vs "
            f"{migration.neon.endpoint} in "
            f"{migration.neon.region}.{migration.neon.cloud}); a different "
            "compute or branch is a different host, and "
            "STRIKENOVA_MIGRATION_TARGET_ALIASES cannot declare two different "
            "compute endpoints to be one target"
        )


def _same_endpoint(runtime: _MigrationTarget, migration: _MigrationTarget) -> bool:
    """Prove the two URLs address the same logical PostgreSQL endpoint.

    Identical ``host:port`` is accepted directly. A different host is accepted
    only when it is declared equivalent through
    ``STRIKENOVA_MIGRATION_TARGET_ALIASES``; the declaration is checked for a
    matching port, and the connection-time check in
    :func:`_verify_logical_target` then proves both routes reach one cluster.

    Native Neon hosts must additionally name the same compute endpoint: the
    caller raises the specific refusal in
    :func:`_assert_same_neon_compute_endpoint`, and the comparison here refuses
    the same cases so no later caller can let an alias join two Neon computes.
    """
    if runtime.endpoint == migration.endpoint:
        return True
    if runtime.neon is not None or migration.neon is not None:
        if runtime.neon is None or migration.neon is None:
            return False
        if runtime.neon.identity != migration.neon.identity:
            return False
    for members in _declared_endpoint_aliases().values():
        if runtime.host in members and migration.host in members:
            return runtime.port == migration.port
    return False


def _configured_migration_role() -> str:
    """The configured dedicated migration identity (fail closed when unnamed)."""
    role = (getattr(settings, "STRIKENOVA_MIGRATOR_ROLE", "") or "").strip()
    if not role:
        raise _config_error(
            "STRIKENOVA_MIGRATOR_ROLE is not configured: the migration login "
            "cannot be proven to be the intended identity without a role name"
        )
    return role


def _assert_configured_identities(
    runtime: _MigrationTarget, migration: _MigrationTarget
) -> None:
    """The URL logins must be exactly the identities the settings name.

    ADR-018 separates two *named* identities. "Differs from the runtime login"
    is a weaker contract: an unrelated login, a shared admin login or a
    superuser that merely differs from the runtime role satisfies separation
    while executing migrations as the wrong principal. The URLs are therefore
    bound to ``STRIKENOVA_MIGRATOR_ROLE`` (always) and to
    ``STRIKENOVA_RUNTIME_ROLE`` (when configured) here, before an engine, a
    connection, a lease or any DDL exists.
    """
    migrator_role = _configured_migration_role()
    if migration.role != migrator_role:
        raise _config_error(
            "STRIKENOVA_MIGRATION_DATABASE_URL connects as "
            f"{migration.role!r}, not the configured migration identity "
            f"{migrator_role!r}"
        )
    runtime_role = (getattr(settings, "STRIKENOVA_RUNTIME_ROLE", "") or "").strip()
    if runtime_role and runtime.role != runtime_role:
        raise _config_error(
            f"DATABASE_URL connects as {runtime.role!r}, not the configured "
            f"runtime identity {runtime_role!r}"
        )


def assert_guarded_migration_configuration(
    runtime_url: str | None = None,
    migration_url: str | None = None,
) -> str:
    """Fail closed unless the migration login serves the runtime database.

    ADR-018 §8.1 requires two *distinct* identities for *one* logical
    database. The stored ``DATABASE_URL`` and ``STRIKENOVA_MIGRATION_DATABASE_URL``
    are therefore checked on four independent properties:

    1. both are PostgreSQL (SQLite/CockroachDB are never migration targets);
    2. the two logins differ — a different password for the same login is not
       separation;
    3. the database name is identical;
    4. the endpoint (``host:port``) is identical, or is declared equivalent in
       ``STRIKENOVA_MIGRATION_TARGET_ALIASES`` — and on native Neon hosts both
       URLs must name one compute endpoint, which no alias can override;
    5. the migration login *is* ``STRIKENOVA_MIGRATOR_ROLE`` and the runtime
       login *is* ``STRIKENOVA_RUNTIME_ROLE`` where that is configured — being
       different from each other is not the same as being the named identities.

    A URL that cannot be parsed into all attributes fails here, before an
    engine, a connection, a lease or any DDL exists. This proves what the
    configuration *asks for*; :func:`_assert_migration_session_identity` and
    the engine connection guard prove what the server actually authenticated.
    SQLite keeps its historical single-process, single-identity behaviour.
    """
    runtime_url = runtime_url or _runtime_url_from_settings()
    migration_url = migration_url or _migration_engine_url()

    if not runtime_url or not _is_postgres_url(runtime_url):
        if _is_postgres_url(migration_url or ""):
            raise _config_error(
                "STRIKENOVA_MIGRATION_DATABASE_URL is PostgreSQL while "
                "DATABASE_URL is not: a migration identity without a PostgreSQL "
                "runtime database has no proven target"
            )
        return _assert_sqlite_migration_target(runtime_url, migration_url)

    explicit = (
        getattr(settings, "STRIKENOVA_MIGRATION_DATABASE_URL", None) or ""
    ).strip()
    if not explicit:
        raise _config_error(
            "STRIKENOVA_MIGRATION_DATABASE_URL is required when DATABASE_URL is "
            "PostgreSQL: the runtime identity must never execute migrations"
        )

    runtime = _parse_migration_target(runtime_url, label="DATABASE_URL")
    migration = _parse_migration_target(
        normalize_database_url(explicit), label="STRIKENOVA_MIGRATION_DATABASE_URL"
    )
    # Validate any declared equivalence eagerly, even when the endpoints
    # already match: a malformed declaration is a configuration error, not
    # something to ignore until it happens to matter.
    _declared_endpoint_aliases()

    if migration.role == runtime.role:
        raise _config_error(
            "the migration and runtime URLs name the same PostgreSQL login "
            f"({migration.role!r}); a different password is not identity "
            "separation"
        )
    if migration.database != runtime.database:
        raise _config_error(
            "the migration and runtime URLs name different PostgreSQL databases "
            f"({migration.database!r} vs {runtime.database!r}); the migration "
            "login must serve the runtime database"
        )
    # Neon compute-endpoint identity is part of the static evidence and is
    # checked before the alias policy, so an alias can never join two computes.
    _assert_same_neon_compute_endpoint(runtime, migration)
    if not _same_endpoint(runtime, migration):
        raise _config_error(
            "the migration and runtime URLs address different PostgreSQL "
            f"endpoints (ports {migration.port} vs {runtime.port}); declare a "
            "proven equivalent route in STRIKENOVA_MIGRATION_TARGET_ALIASES "
            "rather than assuming one"
        )
    _assert_configured_identities(runtime, migration)
    return migration_url


#: SQLSTATEs that mean "this installation does not let the connected role read
#: ``pg_control_system()``": ``insufficient_privilege`` when the function's
#: EXECUTE has been revoked (the documented hardening) and
#: ``undefined_function`` when the server does not provide it at all. They are
#: classified so the refusal names the exact remedy — never so the check can
#: downgrade to evidence that a restart would invalidate.
_IDENTITY_UNAVAILABLE_SQLSTATES = frozenset({"42501", "42883"})

#: The whole privilege the restart-stable identity needs. ``pg_control_system()``
#: is created by ``initdb`` with the default function ACL, which grants EXECUTE
#: to ``PUBLIC`` (stock PostgreSQL 16 needs no grant at all); a hardened
#: installation that revoked it can restore exactly this one function instead
#: of granting administrative rights.
_IDENTITY_GRANT_HINT = (
    "grant EXECUTE on pg_control_system() to the runtime and migration "
    "logins (stock PostgreSQL 16 grants it to PUBLIC)"
)


#: SQLite opening modes libpq-style URI parameters may request. ``rwc``,
#: ``rw`` and ``ro`` all address the same on-disk database; ``memory`` names a
#: distinct in-memory database even when it reuses a file-like name.
_SQLITE_MODES = frozenset({"rwc", "rw", "ro", "memory"})
_SQLITE_CACHES = frozenset({"shared", "private"})


def _sqlite_target_identity(url: str | None) -> tuple[str, str, str | None]:
    """Canonical identity of the SQLite database a URL really opens.

    Path equality alone is not target equality: ``?mode=memory`` turns a
    file-shaped name into a *shared in-memory* database, ``?mode=rwc`` is the
    ordinary file mode, and a ``file:`` name is only a URI when ``?uri=true``
    is present. The returned identity distinguishes those cases. Ambiguous or
    unsupported combinations are refused instead of being treated as the same
    target, and two ordinary file URLs for the same path still compare equal.
    """
    from sqlalchemy.engine import make_url

    if not url:
        return (os.path.realpath(_DEFAULT_DB_PATH), "file", None)
    try:
        parsed = make_url(url)
    except Exception as exc:  # noqa: BLE001 - never echo the URL
        raise _config_error(
            f"the SQLite URL cannot be parsed ({type(exc).__name__})"
        ) from None
    if parsed.get_backend_name() != "sqlite":
        raise _config_error(
            f"expected a SQLite URL, got the {parsed.get_backend_name()!r} dialect"
        )
    query = {str(key).lower(): str(value) for key, value in (parsed.query or {}).items()}
    database = parsed.database or ""
    uri_value = query.get("uri")
    if uri_value is None:
        uri = False
    elif uri_value.lower() in {"true", "1", "yes"}:
        uri = True
    elif uri_value.lower() in {"false", "0", "no"}:
        uri = False
    else:
        raise _config_error("SQLite ?uri= must be true or false")
    if uri and not database.startswith("file:"):
        raise _config_error(
            "SQLite ?uri=true requires a file: database name; refusing an "
            "ambiguous target"
        )
    if database.startswith("file:") and not uri:
        raise _config_error(
            "a SQLite file: database name requires ?uri=true; refusing an "
            "ambiguous target"
        )
    if uri:
        database = database[len("file:") :]
    mode = query.get("mode", "memory" if database in ("", ":memory:") else "rwc")
    if mode not in _SQLITE_MODES:
        raise _config_error(f"unsupported SQLite mode {mode!r}")
    cache = query.get("cache")
    if cache is not None and cache not in _SQLITE_CACHES:
        raise _config_error(f"unsupported SQLite cache {cache!r}")
    if mode == "memory":
        return (database or ":memory:", "memory", cache)
    if not database:
        raise _config_error("a file-mode SQLite URL must name a database file")
    return (os.path.realpath(database), "file", None)


def _assert_sqlite_migration_target(
    runtime_url: str | None, migration_url: str
) -> str:
    """Refuse a separate SQLite migration URL (ADR-018 local single identity).

    The SQLite path hands Alembic the global runtime engine, so a migration URL
    naming a *different* SQLite file would silently migrate the runtime file
    while the configured target stayed untouched. SQLite keeps the historical
    single-identity behaviour, which means one database only.
    """
    if not (migration_url or "").lower().startswith("sqlite"):
        return migration_url
    if _sqlite_target_identity(runtime_url) != _sqlite_target_identity(migration_url):
        raise _config_error(
            "a separate SQLite migration URL is not supported: Alembic is given "
            "the runtime SQLite engine, so the configured migration target must "
            "be the runtime SQLite database (single-identity local behavior)"
        )
    return migration_url


def _cluster_system_identifier(conn) -> str:
    """Read the cluster's ``system_identifier``, the deterministic evidence.

    ``pg_control_system()`` is created with the default function ACL
    (``=X/<owner>``, i.e. PUBLIC EXECUTE) on stock PostgreSQL 16, so an
    ordinary LOGIN role reads it without superuser. PostgreSQL documents that
    a hardened installation may revoke it.

    That revocation (and a server without the function) **fails closed**. The
    only weaker evidence available is the postmaster start time, and a restart
    or a compute suspend/wake changes it without changing the cluster; using it
    as identity either rejects a legitimate reconnection or accepts a different
    cluster. A target that cannot be identified in a restart-stable way is
    therefore refused with the exact grant that restores the evidence, never
    silently accepted.

    Every other failure — a lost connection, a timeout, a permissions error on
    another object, a malformed query — raises as well.
    """
    from sqlalchemy.exc import DBAPIError

    from app._migration_lease import sqlstate_of

    try:
        value = conn.exec_driver_sql(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).scalar()
    except DBAPIError as exc:
        state = sqlstate_of(exc)
        if state in _IDENTITY_UNAVAILABLE_SQLSTATES:
            raise _config_error(
                "cannot read pg_control_system().system_identifier (SQLSTATE "
                f"{state}); without it the target cannot be identified across "
                "a PostgreSQL restart, so the connection is refused instead "
                f"of being accepted on restart-unsafe evidence: "
                f"{_IDENTITY_GRANT_HINT}"
            ) from None
        raise _config_error(
            "cannot read pg_control_system().system_identifier from one of the "
            f"connections (SQLSTATE {state or 'none'}); refusing to prove the "
            "target on an unexpected database error"
        ) from None
    if not value:
        raise _config_error(
            "pg_control_system() returned no system_identifier; refusing an "
            "unproven target"
        )
    return str(value)


#: Canonical, timezone-independent postmaster start time in integer
#: microseconds since the Unix epoch. ``pg_postmaster_start_time()::text``
#: renders in the session's TimeZone, so two connections to the *same* cluster
#: could disagree purely because they set different time zones.
#:
#: This value is **diagnostic only**. It changes on every restart, so it is
#: never compared as identity: doing so would reject a legitimate reconnection
#: after a PostgreSQL restart or a Neon compute suspend/wake.
_POSTMASTER_START_SQL = (
    "(extract(epoch from pg_postmaster_start_time()) * 1000000)::bigint::text"
)


#: Fields of :func:`_logical_target_identity`, named for diagnostics.
_TARGET_IDENTITY_FIELDS = ("database", "database_oid", "system_identifier")


def _postmaster_start_epoch_us(conn) -> int:
    """Read the server's start time as canonical epoch microseconds.

    Diagnostic evidence only (see ``_POSTMASTER_START_SQL``): it proves that a
    restart happened, and is what the restart regression test asserts on. It
    is deliberately absent from :func:`_logical_target_identity`.
    """
    return int(conn.exec_driver_sql(f"SELECT {_POSTMASTER_START_SQL}").scalar())


def _logical_target_identity(conn) -> tuple[str, int, str]:
    """Read the restart-stable identity of the cluster/database a connection sees.

    The tuple is ``(current_database(), pg_database.oid, system_identifier)``.
    Every element lives in the data directory rather than in the server
    process, so all three survive a restart, a crash recovery and a Neon
    compute suspend/wake. What the tuple establishes is *equality of an
    observed signature*: a cluster initialized independently has its own
    ``system_identifier`` (generated by ``initdb``) and a different database in
    one cluster has a different name and OID. It does not identify a physical
    data directory, because a physical copy or a copy-on-write fork (a Neon
    branch, for example) preserves both the identifier and the database OIDs.
    It is also not a Neon project, branch or compute-endpoint identifier: that
    identity comes from the hostname check in :func:`_parse_migration_target`,
    not from this query.

    ``pg_postmaster_start_time()`` is intentionally not part of the tuple: it
    changes on every restart without changing the target, so it could only be
    used as identity by rejecting legitimate reconnections. It is read by
    :func:`_postmaster_start_epoch_us` for diagnostics.
    """
    row = conn.exec_driver_sql(
        "SELECT current_database(), "
        "(SELECT oid FROM pg_database WHERE datname = current_database())"
    ).one()
    if row[1] is None:
        raise _config_error(
            "the connected database has no pg_database row; refusing an "
            "unproven target"
        )
    identifier = _cluster_system_identifier(conn)
    if not identifier:
        raise _config_error(
            "the cluster identifier is empty; a target that cannot be "
            "identified across a restart is refused rather than accepted"
        )
    return (str(row[0]), int(row[1]), identifier)


def _verify_logical_target(
    migration_engine, signature_holder: list | None = None
) -> None:
    """Prove, from the server, that both URLs reach one cluster and database.

    The static check compares what the URLs *say*. This check compares what the
    server *reports*: two connections that present the same
    ``system_identifier``, database name and ``pg_database`` OID present one
    observed signature. All three live in the data directory, so the comparison
    survives a PostgreSQL restart or a Neon compute suspend/wake, and a
    reconnect after either is accepted. Equality of that signature is not proof
    that one physical cluster serves both connections — a physical copy or a
    copy-on-write fork preserves it — and it says nothing about provider
    project, branch or compute-endpoint identity, which the static hostname
    check carries instead. The postmaster start time is read for diagnostics and
    never compared. This runs after the static check and before the engine is
    returned, so no caller can acquire the lease, run DDL or bootstrap on a
    target the static check refused.

    Errors raised here never include a URL: only the failing attribute and the
    observation are reported.
    """
    from sqlalchemy.pool import NullPool

    runtime_url = _runtime_url_from_settings()
    if not runtime_url or not _is_postgres_url(runtime_url):
        raise _config_error(
            "cannot prove the migration target: a PostgreSQL DATABASE_URL is "
            "required to compare the runtime and migration connections"
        )
    expected_database = _parse_migration_target(
        runtime_url, label="DATABASE_URL"
    ).database

    # A previous proof must never authorize this one: clear the runtime
    # reference while the target is re-proven, then publish the new evidence.
    _RUNTIME_TARGET_SIGNATURE[0] = None
    with migration_engine.connect() as conn:
        observed = _logical_target_identity(conn)
        migration_started = _postmaster_start_epoch_us(conn)
        conn.rollback()

    runtime_engine = _create_runtime_engine(
        runtime_url, poolclass=NullPool, pool_pre_ping=True
    )
    try:
        with runtime_engine.connect() as conn:
            runtime_observed = _logical_target_identity(conn)
            runtime_started = _postmaster_start_epoch_us(conn)
            conn.rollback()
    finally:
        runtime_engine.dispose()

    import logging

    logger = logging.getLogger(__name__)

    if observed[0] != expected_database:
        raise _config_error(
            "the migration connection reports database "
            f"{observed[0]!r}, not the runtime database {expected_database!r}"
        )

    # Diagnostics only. A restart between the two preflight connections changes
    # the server's start time, not the logical target, so it must not change
    # the verdict — it is recorded so the restart is visible in the logs.
    if migration_started != runtime_started:
        logger.info(
            "the PostgreSQL server restarted between the migration and runtime "
            "preflight connections; the target identity is restart-stable, so "
            "the same logical target is still accepted"
        )

    # Deterministic, restart-stable evidence: two connections opened by one
    # running server report the same initdb-generated system_identifier, and an
    # independently initialized cluster reports a different one. Equality is an
    # observed-signature match, not proof of one physical cluster: a physical
    # copy or a copy-on-write fork (a Neon branch) preserves the value, which is
    # why the endpoint identity above is checked statically from the hostname.
    if observed[2] != runtime_observed[2]:
        raise _config_error(
            "the migration and runtime connections report different "
            "PostgreSQL cluster identifiers (pg_control_system()."
            "system_identifier differs); refusing to migrate a different "
            "cluster"
        )

    # Same cluster, different database: the name and the pg_database OID that
    # lives in the data directory both have to agree.
    if observed[:2] != runtime_observed[:2]:
        raise _config_error(
            "the migration and runtime connections report different databases "
            "in one PostgreSQL cluster (database name or pg_database OID "
            "differs); refusing to migrate a different target"
        )

    # Only a fully proven target becomes the per-connection reference. Every
    # later connection on this engine re-reads and must match it.
    if signature_holder is not None:
        signature_holder[0] = observed
    # The runtime engine's guard reads the same proof on every new connection,
    # including pool reconnects, restarts and provider failover.
    _RUNTIME_TARGET_SIGNATURE[0] = runtime_observed


def _verified_runtime_role() -> str:
    """Read the authenticated runtime principal from a live runtime connection.

    The connect listener already refuses a wrong identity; this returns the
    server-verified ``session_user`` so the §8.3 privilege matrix is evaluated
    against the principal that actually serves application traffic — not the
    username the URL happens to declare.
    """
    runtime_url = _runtime_url_from_settings()
    if not runtime_url or not _is_postgres_url(runtime_url):
        raise _config_error(
            "cannot verify the runtime privilege matrix: PostgreSQL DATABASE_URL "
            "is required"
        )
    from sqlalchemy.pool import NullPool

    engine_ = _create_runtime_engine(runtime_url, poolclass=NullPool)
    try:
        with engine_.connect() as conn:
            session_user, current_user = conn.exec_driver_sql(
                "SELECT session_user, current_user"
            ).one()
            conn.rollback()
    finally:
        engine_.dispose()
    expected = (getattr(settings, "STRIKENOVA_RUNTIME_ROLE", "") or "").strip()
    if session_user != expected or current_user != expected:
        raise _config_error(
            "the verified runtime principal is "
            f"{session_user!r}/{current_user!r}, not the configured runtime "
            f"identity {expected!r}"
        )
    if session_user == _configured_migration_role():
        raise _config_error(
            "the runtime connection authenticated as the migration identity; "
            "ADR-018 requires distinct runtime and migration principals"
        )
    return str(session_user)


#: Identity evidence read from every migration connection: the login the
#: server authenticated (``session_user``), the role the session currently runs
#: as (``current_user``, which ``SET ROLE`` and inherited memberships change),
#: and that login's role attributes. A URL login proves what the configuration
#: requests; only this proves what the connection actually is.
_MIGRATION_IDENTITY_SQL = (
    "SELECT session_user, current_user, r.rolcanlogin, r.rolsuper, "
    "r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, r.rolreplication "
    "FROM pg_roles r WHERE r.rolname = session_user"
)

#: Role attributes a migration identity must never carry (ADR-018 §8.1).
_MIGRATION_FORBIDDEN_ATTRIBUTES: tuple[tuple[int, str], ...] = (
    (3, "SUPERUSER"),
    (4, "CREATEROLE"),
    (5, "CREATEDB"),
    (6, "BYPASSRLS"),
    (7, "REPLICATION"),
)


def _migration_identity_problems(observed, migrator_role: str, *, stage: str) -> list[str]:
    """Problems with one live connection's identity evidence (none == proven).

    ``observed`` is one :data:`_MIGRATION_IDENTITY_SQL` row: the server's
    ``session_user``/``current_user`` and the session login's role attributes.
    Topology B may elevate ``current_user`` to the NOLOGIN owner inside the
    approved DDL window, so callers check this outside that window; the
    session login itself must be the configured migrator at every point.
    """
    if observed is None:
        message = (
            f"{stage}: the connection's session login is not a role in pg_roles, "
            "so its identity cannot be proven"
        )
        return [message]
    session_user, current_user = observed[0], observed[1]
    problems: list[str] = []
    if session_user != migrator_role:
        problems.append(
            f"{stage}: the connection's session_user is {session_user!r}, not the "
            f"configured migration identity {migrator_role!r}"
        )
    if current_user != migrator_role:
        problems.append(
            f"{stage}: the connection is operating as current_user "
            f"{current_user!r}, not the configured migration identity "
            f"{migrator_role!r}"
        )
    if not observed[2]:
        problems.append(
            f"{stage}: the migration identity {migrator_role!r} is not a LOGIN role"
        )
    for index, name in _MIGRATION_FORBIDDEN_ATTRIBUTES:
        if observed[index]:
            problems.append(
                f"{stage}: the migration identity {migrator_role!r} has {name}"
            )
    return problems


def _assert_migration_session_identity(conn, *, stage: str) -> None:
    """Fail closed unless this live connection is the configured migrator.

    Called before the lease is acquired (via the engine guard), after
    ``RESET ROLE`` and before the §8.3 verification, and before the startup
    backfill writes. It never repairs or connects elsewhere: it reads the
    server's own view of the session and refuses with SQLSTATE ``45004``.
    """
    observed = conn.exec_driver_sql(_MIGRATION_IDENTITY_SQL).one_or_none()
    problems = _migration_identity_problems(
        observed, _configured_migration_role(), stage=stage
    )
    if problems:
        raise _config_error("; ".join(problems))


def _dbapi_logical_identity(cursor) -> tuple[str, int, str]:
    """Restart-stable cluster/database identity from a raw DBAPI cursor.

    Used inside the migration engine's ``connect`` listener, before the
    connection can run any application statement, so a reconnect that fails
    over to another cluster — or to another database in the same cluster — is
    rejected rather than trusted. The postmaster start time is not read here:
    it changes on every restart without changing the target.

    An unreadable ``system_identifier`` fails closed with the exact grant that
    restores the evidence; it never degrades to a restart-unsafe comparison.
    """
    cursor.execute(
        "SELECT current_database(), "
        "(SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    row = cursor.fetchone()
    system_identifier = None
    try:
        cursor.execute("SELECT system_identifier::text FROM pg_control_system()")
        system_identifier = cursor.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - classified immediately below
        state = getattr(exc, "sqlstate", None)
        if state in _IDENTITY_UNAVAILABLE_SQLSTATES:
            raise _config_error(
                "cannot read pg_control_system().system_identifier from a new "
                f"migration connection (SQLSTATE {state}); without it the "
                "target cannot be identified across a PostgreSQL restart, so "
                "the connection is refused: " + _IDENTITY_GRANT_HINT
            ) from None
        raise _config_error(
            "cannot read pg_control_system().system_identifier from a new "
            f"migration connection (SQLSTATE {state or 'none'}); refusing to "
            "accept an unproven target"
        ) from None
    if not system_identifier or row[1] is None:
        raise _config_error(
            "a new migration connection reported an incomplete target identity; "
            "refusing to accept an unproven target"
        )
    return (str(row[0]), int(row[1]), str(system_identifier))


def _install_migration_identity_guard(
    engine_, expected_signature: list | None = None
) -> None:
    """Verify every DBAPI connection the migration engine opens.

    Sampling one connection would let a different pooled connection run the
    lease, the DDL or a backfill transaction under another identity. The
    listener is registered before the engine's first connection and raises
    SQLSTATE ``45004`` while the connection is still being created, so a
    wrong identity can never execute a statement on the migration path.

    Once ``expected_signature[0]`` is set by :func:`_verify_logical_target`,
    every later connection must also report the same database, database OID and
    ``system_identifier`` — evidence that survives a restart. A postmaster
    start time is not part of it, so reconnecting after a restart or a compute
    suspend/wake is accepted for the same target. That is what turns the
    one-time preflight into a per-connection proof for reconnects and failover.
    """
    from sqlalchemy import event

    migrator_role = _configured_migration_role()

    @event.listens_for(engine_, "connect")
    def _verify_connected_identity(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(_MIGRATION_IDENTITY_SQL)
            observed = cursor.fetchone()
            signature = None
            if expected_signature is not None and expected_signature[0] is not None:
                signature = _dbapi_logical_identity(cursor)
        finally:
            cursor.close()
            dbapi_connection.rollback()
        problems = _migration_identity_problems(
            observed,
            migrator_role,
            stage="the migration engine's new connection",
        )
        if signature is not None and signature != expected_signature[0]:
            problems.append(
                "the migration engine's new connection does not reach the same "
                "PostgreSQL cluster/database identity proven during preflight "
                f"(observed database {signature[0]!r}); refusing to run lease "
                "or DDL on an unproven target"
            )
        if problems:
            raise _config_error("; ".join(problems))


def _migration_engine():
    """Build, verify and return the engine for the dedicated migration identity."""
    url = assert_guarded_migration_configuration()
    # Each acquisition attempt composes three independent finite bounds: pool
    # checkout (``pool_timeout``), TCP/authentication (``connect_timeout``)
    # and the lease SQL's lock wait (``lock_timeout``, further capped by the
    # remaining admission budget). This is not a strict wall-clock cap on the
    # whole retry loop; the loop itself stops starting attempts at the deadline.
    attempt_bound_seconds = max(
        1, int(min(30, settings.MIGRATION_LEASE_WAIT_SECONDS))
    )
    engine_ = create_engine(
        url,
        poolclass=None,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        pool_timeout=attempt_bound_seconds,
        connect_args={"connect_timeout": attempt_bound_seconds},
    )
    # The reference is empty during preflight (the first connection is what
    # proves the target) and set before the engine is returned, so every
    # connection the lease/DDL/backfill path opens afterwards is re-proven.
    signature_holder: list = [None]
    try:
        if not url.startswith("sqlite"):
            _install_migration_identity_guard(engine_, signature_holder)
            _verify_logical_target(engine_, signature_holder)
    except BaseException:
        engine_.dispose()
        raise
    return engine_


def _quote_role(role: str) -> str:
    return '"' + role.replace('"', '""') + '"'


def _set_owner_role(conn) -> None:
    """ADR §8.3 Topology B: explicit owner-role window around Alembic DDL."""
    owner_role = (getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip()
    if not owner_role:
        return
    conn.exec_driver_sql(f"SET ROLE {_quote_role(owner_role)}")
    current = conn.exec_driver_sql("SELECT current_user").scalar()
    if current != owner_role:
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            f"SET ROLE {owner_role!r} did not take effect (current_user={current!r})",
        )


def _reset_owner_role(conn) -> None:
    owner_role = (getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip()
    if not owner_role:
        return
    conn.exec_driver_sql("RESET ROLE")
    current = conn.exec_driver_sql("SELECT current_user").scalar()
    migrator_role = settings.STRIKENOVA_MIGRATOR_ROLE
    if current != migrator_role:
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            f"RESET ROLE did not restore the migration identity "
            f"(current_user={current!r}, expected {migrator_role!r})",
        )


#: ADR §8.3 Topology A: the ordinary privileges the migration identity must
#: NOT keep on the application objects the startup backfill touches, once the
#: Alembic chain has created them. The migrator owns those objects, and
#: PostgreSQL 16 lets an owner revoke its own ordinary privileges — the
#: revoke is effective and a later INSERT/UPDATE/DELETE fails ``42501`` even
#: for the owner. Ownership still permits ``ALTER``/``DROP``: those are
#: inherent ownership powers, not ordinary privileges, and no revoke can
#: remove them (ADR §8.4 and D-2 therefore govern who owns the tables).
_TOPOLOGY_A_RESTRICTED_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("positions", ("INSERT", "UPDATE", "DELETE", "TRUNCATE")),
    ("paper_orders", ("INSERT", "UPDATE", "DELETE", "TRUNCATE")),
    ("strategy_leg_exposures", ("UPDATE", "DELETE", "TRUNCATE")),
)

#: ADR §8.3 Topology A: tables whose ``id`` sequence is restricted to
#: ``USAGE`` only (``REVOKE ALL`` then ``GRANT USAGE``), so ``nextval`` works
#: for ``INSERT … RETURNING id`` while ``SELECT`` on the sequence does not.
_TOPOLOGY_A_RESTRICTED_SEQUENCES: tuple[str, ...] = ("strategy_leg_exposures",)


def apply_migrator_privilege_restrictions(conn) -> int:
    """Apply the ADR §8.3 Topology A restrictions to the migration identity.

    Called inside the protected DDL transaction, after Alembic has created (or
    re-created) the application objects and before the protected backfill, so
    the restriction is committed together with the schema and is re-applied by
    every later ``upgrade head`` instead of silently disappearing. Topology B
    does not use this path: its objects are owned by the separate owner role
    and its privileges come from the §8.3 grants.

    Returns the number of objects whose privileges were restricted. Only
    ordinary table/sequence privileges are revoked (see
    ``_TOPOLOGY_A_RESTRICTED_TABLES``), together with ``REFERENCES`` and
    ``TRIGGER``: the migrator keeps its inherent
    ownership powers to ``ALTER``/``DROP`` those objects, and it could grant
    the privileges back to itself — these revokes bound accident, not intent.
    """
    owner_role = (getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip()
    if owner_role:
        return 0
    migrator_role = (settings.STRIKENOVA_MIGRATOR_ROLE or "").strip()
    if not migrator_role:
        return 0
    migrator = _quote_role(migrator_role)
    restricted = 0
    for table, privileges in _TOPOLOGY_A_RESTRICTED_TABLES:
        relation = f"public.{_quote_role(table)}"
        if conn.exec_driver_sql(f"SELECT to_regclass('{relation}')").scalar() is None:
            continue
        conn.exec_driver_sql(
            f"REVOKE {', '.join(privileges)} ON TABLE {relation} FROM {migrator}"
        )
        # REFERENCES and TRIGGER are not in the §8.3 matrix either: the first
        # lets the backfill identity point a foreign key at the table, the
        # second lets it attach a trigger. The verifier examines both, so the
        # restriction must cover them as well.
        conn.exec_driver_sql(
            f"REVOKE REFERENCES, TRIGGER ON TABLE {relation} FROM {migrator}"
        )
        restricted += 1
    for table in _TOPOLOGY_A_RESTRICTED_SEQUENCES:
        sequence = conn.exec_driver_sql(
            f"SELECT pg_get_serial_sequence('public.{table}', 'id')"
        ).scalar()
        if not sequence:
            continue
        conn.exec_driver_sql(f"REVOKE ALL ON SEQUENCE {sequence} FROM {migrator}")
        conn.exec_driver_sql(f"GRANT USAGE ON SEQUENCE {sequence} TO {migrator}")
        restricted += 1
    return restricted


def apply_migrator_backfill_grants(conn) -> int:
    """ADR §8.3 Topology B: install the migrator's four application grants.

    Runs inside the verified ``SET ROLE`` owner window, because only the owner
    may grant on its own objects. ``GRANT`` is idempotent, so every later
    ``upgrade head`` converges on the same matrix instead of relying on
    out-of-band provisioning. In Topology A this is a no-op (returns 0): the
    migrator already owns the objects and the §8.3 revokes bound them.

    Returns the number of statements executed. This is the migration writing
    the intended privileges, not a verifier repairing a bad installation.
    """
    owner_role = (getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip()
    migrator_role = (settings.STRIKENOVA_MIGRATOR_ROLE or "").strip()
    if not owner_role or not migrator_role:
        return 0
    current = conn.exec_driver_sql("SELECT current_user").scalar()
    if current != owner_role:
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            f"the §8.3 Topology B grants require the owner window "
            f"(current_user={current!r}, expected {owner_role!r})",
        )
    migrator = _quote_role(migrator_role)
    statements = (
        f"GRANT SELECT ON TABLE public.\"positions\" TO {migrator}",
        f"GRANT SELECT ON TABLE public.\"paper_orders\" TO {migrator}",
        f"GRANT SELECT, INSERT ON TABLE public.\"strategy_leg_exposures\" TO {migrator}",
        f"GRANT USAGE ON SEQUENCE public.\"strategy_leg_exposures_id_seq\" TO {migrator}",
    )
    for statement in statements:
        conn.exec_driver_sql(statement)
    return len(statements)


def _verify_application_privileges(conn) -> None:
    """Run the read-only ADR §8.3 application privilege verifier."""
    from app._migration_lease import verify_application_privileges

    # Topology B's owner window must already be closed here: the DDL run calls
    # this after RESET ROLE, and the backfill run calls it before its first
    # write. Proving the session and current role again is what shows the
    # elevation did not outlive the window and that the verification (and the
    # backfill that follows it) executes as the configured migrator.
    _assert_migration_session_identity(
        conn, stage="before the §8.3 application privilege verification"
    )
    # The runtime matrix must be evaluated for the principal PostgreSQL
    # actually authenticated, not the username DATABASE_URL declares.
    runtime_role = _verified_runtime_role()
    migrator_role = _configured_migration_role()
    if runtime_role == migrator_role:
        raise _config_error(
            "the verified runtime identity is the migration identity; ADR-018 "
            "requires two distinct principals"
        )
    verify_application_privileges(
        conn,
        migrator_role=migrator_role,
        runtime_role=runtime_role,
        owner_role=(getattr(settings, "STRIKENOVA_OWNER_ROLE", "") or "").strip() or None,
    )


def _run_guarded_migrations(
    alembic_cfg,
    command,
    logger,
    post_migration_callback: Callable[["MigrationWork"], None] | None = None,
) -> None:
    """Run Alembic DDL inside the ADR-017 lease transaction (ADR §7.1, §13).

    The lease row lock, the advisory guard and the DDL share one physical
    connection and one transaction. ``enter`` is the first statement of that
    transaction and again the last statement before ``COMMIT``.
    """
    from app._migration_lease import MigrationLease, preflight

    migration_engine = _migration_engine()
    lease = MigrationLease(migration_engine)
    # Ownership is tracked explicitly so cleanup can distinguish "failed before
    # acquisition" (nothing to release) from "failed after acquisition"
    # (release exactly once). Both paths dispose the engine.
    acquired = False
    primary: BaseException | None = None
    try:
        preflight(migration_engine)
        lease.acquire()
        acquired = True
        with migration_engine.connect() as conn:
            # Session-level bounded lock wait, before the transaction begins.
            conn.exec_driver_sql(
                f"SET lock_timeout = '{settings.MIGRATION_LEASE_LOCK_TIMEOUT_MS}ms'"
            )
            conn.commit()
            with conn.begin():
                lease.enter(conn)  # FIRST statement of the protected transaction
                alembic_cfg.attributes["connection"] = conn
                alembic_cfg.attributes["lease_guard"] = lease
                _set_owner_role(conn)
                command.upgrade(alembic_cfg, "head")
                # Only reached on success: an exception rolls the whole
                # transaction back, so the elevation cannot outlive it.
                # Topology B installs the migrator's four §8.3 grants while
                # the owner window is still open (only the owner may grant).
                granted = apply_migrator_backfill_grants(conn)
                if granted:
                    logger.info(
                        "ADR §8.3 Topology B: installed %d migrator grant(s)",
                        granted,
                    )
                _reset_owner_role(conn)
                # ADR §8.3 Topology A: bound the migration identity's ordinary
                # privileges before the backfill runs, in the same transaction
                # as the DDL and on every upgrade (never just the first one).
                restricted = apply_migrator_privilege_restrictions(conn)
                if restricted:
                    logger.info(
                        "ADR §8.3 Topology A: restricted %d application object(s) "
                        "for the migration identity",
                        restricted,
                    )
                # The privilege topology is ENFORCED, not merely tested: the
                # effective matrix is read back from the catalogs after the
                # DDL and the topology step, inside the same transaction, and
                # before the protected transaction may commit or the backfill
                # may start. A mismatch aborts the whole migration.
                _verify_application_privileges(conn)
                lease.enter(conn)  # refresh immediately before COMMIT
        logger.info("Alembic migrations applied successfully under the lease")

        if post_migration_callback is not None:
            post_migration_callback(MigrationWork(engine=migration_engine, lease=lease))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        # Nested so disposal is unconditional: a release failure must neither
        # skip engine disposal nor hide an earlier exception.
        try:
            if acquired:
                try:
                    lease.release()
                except Exception:  # noqa: BLE001 - cleanup must never mask the cause
                    logger.exception(
                        "migration lease cleanup failed (fingerprint=%s)",
                        lease.fingerprint,
                    )
                    if primary is None:
                        raise
        finally:
            migration_engine.dispose()


def _run_alembic_migrations(
    post_migration_callback: Callable[["MigrationWork"], None] | None = None,
) -> None:
    """Run Alembic migrations against the migration identity.

    On SQLite the historical single-process path is preserved. On PostgreSQL
    the ADR-017 lease guards the DDL transaction and the post-migration
    backfill (``post_migration_callback``), and the runtime identity never
    executes DDL.
    """
    import logging
    from alembic.config import Config
    from alembic import command

    logger = logging.getLogger(__name__)
    alembic_cfg = Config("alembic.ini")
    migration_url = assert_guarded_migration_configuration()
    # Only the SQLite path still needs the ConfigParser-stored URL (it has no
    # supplied connection). The guarded PostgreSQL path passes a live
    # connection to env.py, so a credential-bearing URL must not go through
    # ConfigParser, which treats ``%`` as interpolation (``%40`` would raise).
    if migration_url.startswith("sqlite"):
        alembic_cfg.set_main_option(
            "sqlalchemy.url", migration_url.replace("%", "%%")
        )

    if migration_url.startswith("sqlite"):
        # Single-process by construction: no distributed lease is needed.
        logger.info("SQLite target: migration lease skipped (single-process)")
        alembic_cfg.attributes["connectable"] = engine
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations applied successfully")
        if post_migration_callback is not None:
            post_migration_callback(MigrationWork(engine=engine, lease=None))
        return

    if migration_url.split(":", 1)[0].startswith("cockroachdb"):
        from app._migration_lease import LeaseError, SQLSTATE_CONFIG

        raise LeaseError(
            SQLSTATE_CONFIG,
            "CockroachDB is retired (ADR-019); the ADR-017 lease is "
            "PostgreSQL-only and no unleased CockroachDB migration path exists",
        )

    _run_guarded_migrations(alembic_cfg, command, logger, post_migration_callback)


@dataclass
class MigrationWork:
    """Context handed to post-migration work (the startup exposure backfill)."""

    engine: object
    lease: object | None


def run_startup_backfill(work: MigrationWork) -> None:
    """Run the exposure backfill as the migration identity (ADR §7.2–§7.3).

    SQLite keeps one session and one implicit transaction. PostgreSQL
    enumerates users in a short committed transaction, then processes one
    user per protected transaction whose FIRST statement is ``enter`` and
    whose last statement before ``COMMIT`` is a second ``enter``.
    """
    import logging

    from sqlalchemy.orm import Session

    from app.services.leg_exposure import backfill_all_exposures, backfill_exposures

    logger = logging.getLogger(__name__)
    if work.lease is None:
        session = sessionmaker(bind=work.engine)()
        try:
            backfill_all_exposures(session)
        finally:
            session.close()
        return

    with work.engine.connect() as conn:
        # Fail closed before the first backfill write: the effective matrix is
        # re-read from the catalogs, never assumed from the DDL transaction.
        _verify_application_privileges(conn)
        conn.rollback()
        user_ids = [
            row[0]
            for row in conn.execute(
                text("SELECT DISTINCT user_id FROM positions ORDER BY user_id")
            )
        ]
        # Enumeration is its own short transaction (ADR §7.2).
        conn.commit()

    logger.info("startup backfill: %d user(s) to process", len(user_ids))
    statement_timeout_ms = settings.MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS
    for user_id in user_ids:
        with work.engine.connect() as conn:
            with conn.begin():
                work.lease.enter(conn)  # FIRST statement of the user transaction
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"
                )
                session = Session(
                    bind=conn,
                    autoflush=False,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                try:
                    backfill_exposures(session, user_id)
                    session.commit()  # release the savepoint, not the outer tx
                except BaseException:
                    session.rollback()
                    raise
                finally:
                    session.close()
                work.lease.enter(conn)  # MANDATORY pre-commit refresh


# ---------------------------------------------------------------------------
# DATABASE SCHEMA ARCHITECTURE (Phase 10.1B — final)
# ---------------------------------------------------------------------------
#
# Alembic is the SOLE authoritative schema management mechanism.
#
# Startup sequence:
#   1. Alembic upgrade head       — versioned, authoritative schema DDL
#   2. Data backfill (idempotent) — strategy-leg attribution backfill
#   3. Composite indexes           — SQLite-only pipeline query indexes
#
# CLI tools (candle_backfill, run_backfill, run_daily, etc.) may still
# call Base.metadata.create_all() for their own database setup — those
# are separate from the web application startup path.
#
# greeks_checkpoint remains CLI-owned raw SQL, intentionally outside
# Base.metadata and the Alembic baseline.
# ---------------------------------------------------------------------------


def init_db():
    """Initialize database on application startup.

    Alembic owns the authoritative schema. This function:
      1. Runs ``alembic upgrade head`` (schema DDL)
      2. Runs idempotent data backfills
      3. Creates composite indexes (SQLite only)

    Called ONCE from the FastAPI lifespan handler. Never called during
    request processing.
    """
    import logging
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    logger = logging.getLogger(__name__)

    # Conservative one-time backfill for pre-existing, provably unambiguous
    # executions. Rows already present are never duplicated. On PostgreSQL it
    # runs as the migration identity inside the ADR-017 lease, one protected
    # transaction per user (ADR §7.2–§7.3); on SQLite it runs as before.
    _run_alembic_migrations(post_migration_callback=run_startup_backfill)

    # These indexes are intentionally SQLite-only and are not part of the
    # cross-dialect Alembic schema.
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            for stmt in [
                "CREATE INDEX IF NOT EXISTS ix_ingestion_log_operation_status ON ingestion_log (operation, status)",
                "CREATE INDEX IF NOT EXISTS ix_ingestion_log_completed_at ON ingestion_log (completed_at)",
                "CREATE INDEX IF NOT EXISTS ix_data_completeness_status ON data_completeness (status)",
                "CREATE INDEX IF NOT EXISTS ix_ingestion_checkpoint_status ON ingestion_checkpoint (pipeline, status)",
            ]:
                conn.execute(text(stmt))


# ---------------------------------------------------------------------------
# Database health check (Phase 7.21)
# ---------------------------------------------------------------------------

_HISTORICAL_TABLES = [
    "nifty_candles",
    "contract_specs",
    "option_candles",
    "option_greeks",
]


def check_database_health() -> dict:
    """Return a diagnostic snapshot of the active database.

    Day 4: report includes the active dialect name and conditionally
    includes file-specific information only for SQLite databases.
    PostgreSQL reports omit file_exists/file_size_bytes since they are
    meaningless for client-server databases.
    """
    from sqlalchemy import inspect as sa_inspect, func, select

    dialect_name = engine.dialect.name
    db_path = get_database_path()
    report: dict = {
        "database_path": db_path,
        "dialect": dialect_name,
        "accessible": False,
        "tables_present": [],
        "tables_missing": [],
        "row_counts": {},
        "oldest_record": None,
        "newest_record": None,
    }

    # File-specific fields are only meaningful for SQLite.
    if dialect_name == "sqlite":
        report["file_exists"] = False
        report["file_size_bytes"] = 0
        if os.path.isfile(db_path):
            report["file_exists"] = True
            report["file_size_bytes"] = os.path.getsize(db_path)

    try:
        insp = sa_inspect(engine)
        existing_tables = set(insp.get_table_names())
        report["tables_present"] = sorted(
            t for t in _HISTORICAL_TABLES if t in existing_tables
        )
        report["tables_missing"] = sorted(
            t for t in _HISTORICAL_TABLES if t not in existing_tables
        )
    except Exception as e:
        report["schema_error"] = str(e)
        return report

    db = SessionLocal()
    try:
        from app.models import NiftyCandle, ContractSpec, OptionCandle, OptionGreeks

        for label, model in [
            ("nifty_candles", NiftyCandle),
            ("contract_specs", ContractSpec),
            ("option_candles", OptionCandle),
            ("option_greeks", OptionGreeks),
        ]:
            count = db.scalar(select(func.count(model.id))) or 0
            report["row_counts"][label] = count

        nifty_oldest = db.scalar(
            select(NiftyCandle.open_time).order_by(NiftyCandle.open_time.asc()).limit(1)
        )
        nifty_newest = db.scalar(
            select(NiftyCandle.open_time).order_by(NiftyCandle.open_time.desc()).limit(1)
        )
        if nifty_oldest:
            report["oldest_record"] = str(nifty_oldest)
        if nifty_newest:
            report["newest_record"] = str(nifty_newest)

        report["accessible"] = True
    except Exception as e:
        report["access_error"] = str(e)
    finally:
        db.close()

    return report
