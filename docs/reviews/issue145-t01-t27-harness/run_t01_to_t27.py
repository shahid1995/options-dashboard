#!/usr/bin/env python3
"""
Issue #145 — T-01…T-27 Lease Protocol Harness

Validated against ADR-017/018 (pinned commit ae2226ef592dc48d5e9619bd2198de33285911df).

All SQL below is copied verbatim from that document: §6.1 (tables),
§6.3–§6.6 (the four functions), §10.2 (revoke set) and §9.2.1 (first
installation). No invented substitute protocol, and no literal backslash-n
sequences: every SQL string uses real line breaks.

Design rules enforced here:
  * `run_sql()` never commits or rolls back. Transaction ownership stays with
    the scenario, which uses explicit `conn.transaction()` blocks.
  * Every harness connection is opened with `autocommit=True`, so a standalone
    statement is its own committed transaction and each `conn.transaction()`
    block is a real BEGIN/COMMIT instead of a savepoint inside an accidental
    implicit transaction.
  * A destructive-run gate refuses to connect unless the operator sets
    `ALLOW_DESTRUCTIVE_TESTS=1`, names a dedicated disposable cluster in
    `HARNESS_CLUSTER_LABEL`, and supplies the cluster's independently observed
    `system_identifier` in `HARNESS_TARGET_SYSTEM_IDENTIFIER`. The label is a
    declaration, not proof: identity is established from `pg_control_system()`,
    and an existing database counts as harness-owned only when its catalog
    comment carries the matching marker. Existing roles and memberships are
    validated against exact expected attributes and options, never altered.
  * Success results are the documented function results (3-column rows for
    acquire/enter, a boolean for release, five columns for protocol info).
  * Errors are PostgreSQL exceptions; SQLSTATE is read from the driver's
    `sqlstate` attribute, never parsed from the message and never modelled as
    a query row.
  * Every scenario runs against its own freshly created database, created and
    dropped through the administrative connection.
  * The runner probes the actual server version. If PostgreSQL 16.16 is not
    available, every scenario is reported NOT RUN and the process exits
    cleanly; nothing raises before result generation.

This harness performs no runtime verification in this environment because
PostgreSQL 16.16 is unavailable. It reports NOT RUN, it does not claim PASS.

`python3 run_t01_to_t27.py --self-test` runs the harness's own DB-free
regression checks against an injected fake driver. It opens no connection,
reads no credentials and contacts no server.
"""

import datetime
import inspect
import os
import sys
import time

try:  # psycopg is needed to run scenarios, not to run the built-in self-test.
    import psycopg
    from psycopg import sql
    PSYCOPG_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - only without psycopg installed
    PSYCOPG_AVAILABLE = False

    class _StubError(Exception):
        sqlstate = None

    class _StubSQL(str):
        def format(self, *args, **kwargs):
            rendered = str(self)
            for arg in args:
                rendered = rendered.replace("{}", str(arg), 1)
            return _StubSQL(rendered)

    def _stub_identifier(name):
        return _StubSQL('"' + str(name).replace('"', '""') + '"')

    def _stub_literal(value):
        return _StubSQL("'" + str(value).replace("'", "''") + "'")

    class _StubSQLModule:
        SQL = _StubSQL
        Identifier = staticmethod(_stub_identifier)
        Literal = staticmethod(_stub_literal)

    class _StubPsycopgModule:
        Error = _StubError
        sql = _StubSQLModule()

        @staticmethod
        def connect(**kwargs):
            raise RuntimeError(
                "psycopg is not installed; install 'psycopg[binary]' to run scenarios")

    psycopg = _StubPsycopgModule()
    sql = _StubSQLModule()

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
LEASE_TTL_SECONDS = int(os.getenv("LEASE_TTL_SECONDS", "500"))
LEASE_LOCK_TIMEOUT_MS = int(os.getenv("LEASE_LOCK_TIMEOUT_MS", "1000"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "55432"))

# Separate administrative and migration identities (disposable cluster only).
ADMIN_DB_USER = os.getenv("ADMIN_DB_USER", DB_USER)
ADMIN_DB_PASSWORD = os.getenv("ADMIN_DB_PASSWORD", DB_PASSWORD)
MIGRATOR_DB_USER = os.getenv("MIGRATOR_DB_USER", "strikenova_prod_migrator")
MIGRATOR_DB_PASSWORD = os.getenv("MIGRATOR_DB_PASSWORD", "")

# ---------------------------------------------------------------------------
# Destructive-run safety gate
# ---------------------------------------------------------------------------
# This harness drops fixed-name databases and mutates cluster-level role
# membership. It must never be run casually against a live environment, so it
# refuses to connect at all unless the operator opts in explicitly *and* names
# a dedicated disposable cluster. Neither condition is a default.
ALLOW_DESTRUCTIVE_TESTS = os.getenv("ALLOW_DESTRUCTIVE_TESTS", "")
HARNESS_CLUSTER_LABEL = os.getenv("HARNESS_CLUSTER_LABEL", "")
REQUIRED_CLUSTER_LABEL = "issue145-disposable"

# The label above is an operator declaration, not evidence of cluster identity.
# Identity is established independently: `pg_control_system().system_identifier`
# is read from pg_control and compared with a value the operator obtained and
# confirmed separately while provisioning the disposable cluster.
HARNESS_TARGET_SYSTEM_IDENTIFIER = os.getenv("HARNESS_TARGET_SYSTEM_IDENTIFIER", "")

# Marker written into the catalog comment of every database this harness
# creates. An existing database counts as harness-owned only when its comment
# equals this exact value, which binds it to the verified cluster identity.
HARNESS_DB_MARKER_PREFIX = "strikenova-issue145-harness-disposable:"

SAFETY_REFUSAL_EXIT_CODE = 2


class SafetyRefusal(Exception):
    """The destructive-run gate refused the configured target."""

MIGRATOR_ROLE = "strikenova_prod_migrator"
OWNER_ROLE = "strikenova_prod_owner"
APP_ROLE = "strikenova_production_app"

# Only the PostgreSQL 16 major version is supported, and only minor 16 or later
# within it. A lower bound alone would silently accept PostgreSQL 17, whose
# catalog rendering this harness was not written or approved against.
REQUIRED_SERVER_MAJOR = 16
REQUIRED_SERVER_MINOR = 16
ADR_SOURCE_COMMIT = "ae2226ef592dc48d5e9619bd2198de33285911df"
ADR_SOURCE_PATH = "docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md"

# Raw tokens are 64 hex characters. They are never logged or written to files;
# only the 12-character fingerprint is ever compared or reported.
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64

# ---------------------------------------------------------------------------
# SQL from the pinned ADR (verbatim; real line breaks only)
# ---------------------------------------------------------------------------

# ADR §6.1 — migration_lease table
LEASE_TABLE_SCHEMA = """CREATE TABLE migration_internal.migration_lease (
    lease_name        text        PRIMARY KEY,
    protocol_version  integer     NOT NULL,
    owner_token_hash  text,                       -- sha256 hex of the raw token; NULL = unowned
    owner_fingerprint text,                       -- first 12 hex chars; diagnostics only
    owner_role        name,                       -- session_user at acquisition; diagnostics only
    acquired_at       timestamptz,
    expires_at        timestamptz,
    released_at       timestamptz,
    generation        bigint      NOT NULL DEFAULT 0,   -- diagnostics only, never proof
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT migration_lease_singleton_ck CHECK (lease_name = 'migration-lease'),
    CONSTRAINT migration_lease_generation_ck CHECK (generation >= 0),
    CONSTRAINT migration_lease_owner_shape_ck CHECK (
        (owner_token_hash IS NULL AND owner_fingerprint IS NULL)
        OR (owner_token_hash IS NOT NULL AND owner_fingerprint IS NOT NULL)
    ),
    CONSTRAINT migration_lease_owned_expiry_ck CHECK (
        owner_token_hash IS NULL OR expires_at IS NOT NULL
    )
);"""

# ADR §6.1 — lease_protocol control table
PROTOCOL_TABLE_SCHEMA = """CREATE TABLE migration_internal.lease_protocol (
    singleton             boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    installed_version     integer NOT NULL,
    min_supported_version integer NOT NULL,
    max_supported_version integer NOT NULL,
    barrier_state         text NOT NULL CHECK (barrier_state IN ('open', 'closed')),
    bootstrap_state       text NOT NULL CHECK (bootstrap_state IN ('ready', 'installing')),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            name NOT NULL DEFAULT session_user,
    note                  text
);"""

# ADR §6.3 — acquire
LEASE_ACQUIRE_V1_SQL = """CREATE FUNCTION migration_internal.lease_acquire_v1(
    p_protocol integer,
    p_token text,
    p_ttl_seconds double precision,
    p_lock_timeout_ms integer
) RETURNS TABLE (out_owner_fingerprint text, out_expires_at timestamptz, out_generation bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_hash  text;
    v_fp    text;
    v_row   migration_internal.migration_lease%ROWTYPE;
    v_inst  integer;
    v_min   integer;
    v_max   integer;
    v_bar   text;
    v_boot  text;
    v_prev  text;
    v_now   timestamptz;
    v_exp   timestamptz;
    v_gen   bigint;
BEGIN
    -- PostgreSQL treats lock_timeout = 0 as "wait forever", so 0 is rejected
    -- rather than accepted as "do not wait".
    IF p_token IS NULL OR length(p_token) < 32
       OR p_ttl_seconds IS NULL OR NOT (p_ttl_seconds > 0) OR p_ttl_seconds > 86400
       OR p_lock_timeout_ms IS NULL OR p_lock_timeout_ms < 1 OR p_lock_timeout_ms > 600000 THEN
        RAISE EXCEPTION 'lease configuration invalid' USING ERRCODE = '45004';
    END IF;

    v_hash := encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
    v_fp   := left(v_hash, 12);

    -- Bounded wait for every lock below; never an unbounded hang.
    v_prev := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', p_lock_timeout_ms::text, true);

    -- Cross-version guard, taken before the lease row lock and with the same
    -- key for every protocol version (§6.10).
    PERFORM pg_advisory_xact_lock(
        hashtextextended('strikenova:migration_internal:migration_guard', 0));

    SELECT l.* INTO v_row
      FROM migration_internal.migration_lease AS l
     WHERE l.lease_name = 'migration-lease'
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lease row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    -- Restore the caller's lock_timeout so the value does not leak into the
    -- rest of this transaction (for example, Alembic DDL lock waits).
    PERFORM set_config('lock_timeout', v_prev, true);

    -- The authoritative version state is the stable, non-versioned control
    -- row (§6.1, §11.2), read under the lease row lock. The row's own
    -- protocol_version is a mirror and authorizes nothing.
    SELECT pr.installed_version, pr.min_supported_version, pr.max_supported_version,
           pr.barrier_state, pr.bootstrap_state
      INTO v_inst, v_min, v_max, v_bar, v_boot
      FROM migration_internal.lease_protocol AS pr
     WHERE pr.singleton;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'protocol row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    -- A process of any protocol version that passed its preflight and then
    -- resumed after an upgrade fails here, under the guard: installed_version
    -- is no longer its own.
    IF v_inst <> p_protocol OR p_protocol < v_min OR p_protocol > v_max THEN
        RAISE EXCEPTION 'protocol % not admissible: installed %, supported %..%',
            p_protocol, v_inst, v_min, v_max USING ERRCODE = '45006';
    END IF;

    IF v_boot <> 'ready' THEN
        RAISE EXCEPTION 'bootstrap incomplete (state %)', v_boot USING ERRCODE = '45005';
    END IF;

    IF v_row.protocol_version <> v_inst THEN
        RAISE EXCEPTION 'lease row protocol % disagrees with installed %',
            v_row.protocol_version, v_inst USING ERRCODE = '45006';
    END IF;

    IF v_bar = 'closed' THEN
        RAISE EXCEPTION 'deployment barrier closed; only bootstrap may write'
            USING ERRCODE = '45004';
    END IF;

    v_now := clock_timestamp();

    -- An owned row with a NULL expiry is treated as held (fail closed), not
    -- as free: the CHECK constraint prevents it, and this guard is defensive.
    IF v_row.owner_token_hash IS NOT NULL
       AND (v_row.expires_at IS NULL OR v_row.expires_at > v_now) THEN
        RAISE EXCEPTION 'lease held by % until %',
            coalesce(v_row.owner_fingerprint, 'unknown'),
            coalesce(v_row.expires_at::text, 'unknown')
            USING ERRCODE = '45002';
    END IF;

    v_exp := v_now + make_interval(secs => p_ttl_seconds);

    UPDATE migration_internal.migration_lease AS l
       SET protocol_version  = p_protocol,      -- mirror maintenance, never authority
           owner_token_hash  = v_hash,
           owner_fingerprint = v_fp,
           owner_role        = session_user::name,
           acquired_at       = v_now,
           expires_at        = v_exp,
           released_at       = NULL,
           generation        = l.generation + 1,
           updated_at        = v_now
     WHERE l.lease_name = 'migration-lease'
    RETURNING l.generation INTO v_gen;

    RETURN QUERY SELECT v_fp, v_exp, v_gen;
END;
$$;"""

# ADR §6.4 — enter
LEASE_ENTER_V1_SQL = """CREATE FUNCTION migration_internal.lease_enter_v1(
    p_protocol integer,
    p_token text,
    p_ttl_seconds double precision,
    p_lock_timeout_ms integer
) RETURNS TABLE (out_owner_fingerprint text, out_expires_at timestamptz, out_generation bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_hash text;
    v_fp   text;
    v_row  migration_internal.migration_lease%ROWTYPE;
    v_inst integer;
    v_min  integer;
    v_max  integer;
    v_boot text;
    v_prev text;
    v_now  timestamptz;
    v_exp  timestamptz;
BEGIN
    IF p_token IS NULL OR length(p_token) < 32
       OR p_ttl_seconds IS NULL OR NOT (p_ttl_seconds > 0) OR p_ttl_seconds > 86400
       OR p_lock_timeout_ms IS NULL OR p_lock_timeout_ms < 1 OR p_lock_timeout_ms > 600000 THEN
        RAISE EXCEPTION 'lease configuration invalid' USING ERRCODE = '45004';
    END IF;

    v_hash := encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
    v_fp   := left(v_hash, 12);

    v_prev := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', p_lock_timeout_ms::text, true);

    -- First lock of every protected transaction, taken inside the statement
    -- that must remain its first statement (§6.10, §7.1, §7.3).
    PERFORM pg_advisory_xact_lock(
        hashtextextended('strikenova:migration_internal:migration_guard', 0));

    SELECT l.* INTO v_row
      FROM migration_internal.migration_lease AS l
     WHERE l.lease_name = 'migration-lease'
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lease row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    PERFORM set_config('lock_timeout', v_prev, true);

    SELECT pr.installed_version, pr.min_supported_version, pr.max_supported_version,
           pr.bootstrap_state
      INTO v_inst, v_min, v_max, v_boot
      FROM migration_internal.lease_protocol AS pr
     WHERE pr.singleton;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'protocol row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    IF v_inst <> p_protocol OR p_protocol < v_min OR p_protocol > v_max THEN
        RAISE EXCEPTION 'protocol % not admissible: installed %, supported %..%',
            p_protocol, v_inst, v_min, v_max USING ERRCODE = '45006';
    END IF;

    -- Authoritative readiness, read in the same statement path as the version:
    -- a protocol object set that bootstrap has not finished installing must
    -- never accept a protected transaction (§11.2).
    IF v_boot <> 'ready' THEN
        RAISE EXCEPTION 'bootstrap incomplete (state %)', v_boot USING ERRCODE = '45005';
    END IF;

    IF v_row.protocol_version <> v_inst THEN
        RAISE EXCEPTION 'lease row protocol % disagrees with installed %',
            v_row.protocol_version, v_inst USING ERRCODE = '45006';
    END IF;

    IF v_row.owner_token_hash IS NULL THEN
        RAISE EXCEPTION 'lease has no owner' USING ERRCODE = '45002';
    END IF;

    IF v_row.owner_token_hash <> v_hash THEN
        RAISE EXCEPTION 'lease owner mismatch (presented %)', v_fp USING ERRCODE = '45001';
    END IF;

    v_now := clock_timestamp();

    IF v_row.expires_at IS NULL OR v_row.expires_at <= v_now THEN
        RAISE EXCEPTION 'lease owned by % has expired at %', v_fp,
            coalesce(v_row.expires_at::text, 'unknown')
            USING ERRCODE = '45002';
    END IF;

    v_exp := v_now + make_interval(secs => p_ttl_seconds);

    UPDATE migration_internal.migration_lease AS l
       SET expires_at = v_exp,
           updated_at = v_now
     WHERE l.lease_name = 'migration-lease';

    RETURN QUERY SELECT v_fp, v_exp, v_row.generation;
END;
$$;"""

# ADR §6.5 — release
LEASE_RELEASE_V1_SQL = """CREATE FUNCTION migration_internal.lease_release_v1(p_protocol integer, p_token text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_hash text;
    v_row  migration_internal.migration_lease%ROWTYPE;
    v_inst integer;
    v_min  integer;
    v_max  integer;
    v_prev text;
    v_now  timestamptz;
BEGIN
    IF p_token IS NULL OR length(p_token) < 32 THEN
        RAISE EXCEPTION 'lease configuration invalid' USING ERRCODE = '45004';
    END IF;

    v_hash := encode(sha256(convert_to(p_token, 'UTF8')), 'hex');

    v_prev := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', '5000', true);

    -- Same cross-version guard and lock order as acquire and enter (§6.10).
    PERFORM pg_advisory_xact_lock(
        hashtextextended('strikenova:migration_internal:migration_guard', 0));

    SELECT l.* INTO v_row
      FROM migration_internal.migration_lease AS l
     WHERE l.lease_name = 'migration-lease'
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lease row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    PERFORM set_config('lock_timeout', v_prev, true);

    SELECT pr.installed_version, pr.min_supported_version, pr.max_supported_version
      INTO v_inst, v_min, v_max
      FROM migration_internal.lease_protocol AS pr
     WHERE pr.singleton;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'protocol row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    -- Release is deliberately not gated on barrier_state or bootstrap_state:
    -- an owner must always be able to release during a drain window.
    IF v_inst <> p_protocol OR p_protocol < v_min OR p_protocol > v_max THEN
        RAISE EXCEPTION 'protocol % not admissible: installed %, supported %..%',
            p_protocol, v_inst, v_min, v_max USING ERRCODE = '45006';
    END IF;

    IF v_row.protocol_version <> v_inst THEN
        RAISE EXCEPTION 'lease row protocol % disagrees with installed %',
            v_row.protocol_version, v_inst USING ERRCODE = '45006';
    END IF;

    IF v_row.owner_token_hash IS NULL THEN
        RETURN false;                       -- idempotent: already unowned
    END IF;

    IF v_row.owner_token_hash <> v_hash THEN
        RAISE EXCEPTION 'lease owner mismatch (release)' USING ERRCODE = '45001';
    END IF;

    v_now := clock_timestamp();

    UPDATE migration_internal.migration_lease AS l
       SET owner_token_hash  = NULL,
           owner_fingerprint = NULL,
           owner_role        = NULL,
           expires_at        = v_now,
           released_at       = v_now,
           updated_at        = v_now
     WHERE l.lease_name = 'migration-lease';

    RETURN true;
END;
$$;"""

# ADR §6.6 — protocol information
LEASE_PROTOCOL_INFO_V1_SQL = """CREATE FUNCTION migration_internal.lease_protocol_info_v1()
RETURNS TABLE (
    installed_version     integer,
    min_supported_version integer,
    max_supported_version integer,
    barrier_state         text,
    bootstrap_state       text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    RETURN QUERY
        SELECT pr.installed_version,
               pr.min_supported_version,
               pr.max_supported_version,
               pr.barrier_state,
               pr.bootstrap_state
          FROM migration_internal.lease_protocol AS pr
         WHERE pr.singleton;
END;
$$;"""

# ADR §10.2 — revoke set, re-applied after the last CREATE FUNCTION
REVOKE_SET_SQL = """REVOKE ALL ON SCHEMA migration_internal FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA migration_internal FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA migration_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM PUBLIC;

REVOKE ALL ON SCHEMA migration_internal FROM strikenova_production_app;
REVOKE ALL ON ALL TABLES    IN SCHEMA migration_internal FROM strikenova_production_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA migration_internal FROM strikenova_production_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM strikenova_production_app;"""

# Application tables used by the backfill-related scenarios (disposable DBs only)
APPLICATION_TABLES_SQL = """CREATE TABLE public.positions (
    id int PRIMARY KEY,
    user_id int,
    UNIQUE (user_id)
);
CREATE TABLE public.paper_orders (
    id int PRIMARY KEY,
    user_id int,
    strategy_leg_id int
);
CREATE TABLE public.strategy_leg_exposures (
    id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    paper_order_id int,
    exposure double precision
);"""

# ---------------------------------------------------------------------------
# Connection and database helpers
# ---------------------------------------------------------------------------

def _conn_kwargs(dbname, user=None, password=None):
    kwargs = {
        "dbname": dbname,
        "port": DB_PORT,
    }
    user = DB_USER if user is None else user
    password = DB_PASSWORD if password is None else password
    if user:
        kwargs["user"] = user
    if password:
        kwargs["password"] = password
    if DB_HOST:
        kwargs["host"] = DB_HOST
    return kwargs


def require_destructive_optin():
    """Refuse before any connection unless the operator opted in explicitly."""
    problems = []
    if ALLOW_DESTRUCTIVE_TESTS != "1":
        problems.append("ALLOW_DESTRUCTIVE_TESTS must be exactly '1' "
                        f"(got {ALLOW_DESTRUCTIVE_TESTS!r})")
    if HARNESS_CLUSTER_LABEL != REQUIRED_CLUSTER_LABEL:
        problems.append(f"HARNESS_CLUSTER_LABEL must be {REQUIRED_CLUSTER_LABEL!r} "
                        f"(got {HARNESS_CLUSTER_LABEL!r})")
    if problems:
        raise SafetyRefusal("; ".join(problems))


def expected_database_marker():
    """The marker a harness-created database must carry in this cluster."""
    return HARNESS_DB_MARKER_PREFIX + HARNESS_TARGET_SYSTEM_IDENTIFIER


def _scenario_database_state(admin, db_name):
    """Return (exists, catalog comment marker) for one database name."""
    row = admin.execute(
        "SELECT d.datname, shobj_description(d.oid, 'pg_database') "
        "FROM pg_database d WHERE d.datname = %s", (db_name,)).fetchone()
    if row is None:
        return False, None
    return True, row[1]


def verify_cluster_identity(admin):
    """Establish cluster identity from pg_control, never from a static label."""
    if not HARNESS_TARGET_SYSTEM_IDENTIFIER:
        raise SafetyRefusal(
            "HARNESS_TARGET_SYSTEM_IDENTIFIER is not set, so the target cluster's "
            "identity cannot be established independently of HARNESS_CLUSTER_LABEL")
    row = admin.execute(
        "SELECT system_identifier::text FROM pg_control_system()").fetchone()
    actual = row[0] if row else None
    if actual is None:
        raise SafetyRefusal(
            "the cluster did not report pg_control_system().system_identifier")
    if actual != HARNESS_TARGET_SYSTEM_IDENTIFIER:
        raise SafetyRefusal(
            f"cluster system_identifier {actual!r} does not match the independently "
            "confirmed HARNESS_TARGET_SYSTEM_IDENTIFIER")
    return actual


# Exact attributes the harness provisions; an existing role that differs is
# refused rather than altered.
ROLE_LOGIN_EXPECTATIONS = {MIGRATOR_ROLE: True, APP_ROLE: True, OWNER_ROLE: False}
# (granted role, member, INHERIT, SET, ADMIN) — the only membership it creates.
ALLOWED_MEMBERSHIP = (OWNER_ROLE, MIGRATOR_ROLE, False, True, False)


def validate_destructive_target(admin):
    """Refuse a target that is not a proven dedicated disposable cluster.

    Runs after the version probe and before any role or database mutation, so a
    production-like target is never adopted or altered. Every condition fails
    closed: the harness stops rather than repairing or adopting what it finds,
    and every statement executed here is read-only.
    """
    verify_cluster_identity(admin)

    problems = []
    marker = expected_database_marker()
    databases = admin.execute(
        "SELECT d.datname, shobj_description(d.oid, 'pg_database') "
        "FROM pg_database d "
        "WHERE d.datname NOT IN ('postgres', 'template0', 'template1') "
        "ORDER BY d.datname").fetchall()
    unowned = [f"{name} (marker {comment!r})" for name, comment in databases
               if comment != marker]
    if unowned:
        problems.append(
            "cluster holds databases this harness cannot prove it created (no "
            "matching disposable-cluster marker): " + ", ".join(unowned))

    role_rows = admin.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
        "rolcanlogin FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        ([MIGRATOR_ROLE, OWNER_ROLE, APP_ROLE],)).fetchall()
    for (rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,
         rolcanlogin) in role_rows:
        risky = [flag for flag, value in (
            ("SUPERUSER", rolsuper), ("BYPASSRLS", rolbypassrls),
            ("CREATEROLE", rolcreaterole), ("CREATEDB", rolcreatedb)) if value]
        if risky:
            problems.append(f"existing role {rolname} has {', '.join(risky)}; "
                            "refusing to adopt a production-like role set")
        want_login = ROLE_LOGIN_EXPECTATIONS[rolname]
        if bool(rolcanlogin) != want_login:
            found = "LOGIN" if rolcanlogin else "NOLOGIN"
            expected = "LOGIN" if want_login else "NOLOGIN"
            problems.append(
                f"existing role {rolname} is {found}; this harness provisions it as "
                f"{expected} and will not alter an existing role")

    memberships = admin.execute(
        "SELECT r.rolname, m.rolname, am.inherit_option, am.set_option, "
        "am.admin_option FROM pg_auth_members am "
        "JOIN pg_roles r ON r.oid = am.roleid "
        "JOIN pg_roles m ON m.oid = am.member "
        "WHERE r.rolname = ANY(%s) OR m.rolname = ANY(%s) ORDER BY 1, 2",
        ([MIGRATOR_ROLE, OWNER_ROLE, APP_ROLE],
         [MIGRATOR_ROLE, OWNER_ROLE, APP_ROLE])).fetchall()
    for role, member, inherit_option, set_option, admin_option in memberships:
        observed = (role, member, bool(inherit_option), bool(set_option),
                    bool(admin_option))
        if observed != ALLOWED_MEMBERSHIP:
            problems.append(
                f"existing membership {role} -> {member} has options (INHERIT "
                f"{bool(inherit_option)}, SET {bool(set_option)}, ADMIN "
                f"{bool(admin_option)}); the harness requires exactly INHERIT "
                "FALSE, SET TRUE, ADMIN FALSE and will not alter an existing "
                "membership")

    if problems:
        raise SafetyRefusal("; ".join(problems))


def _default_connect(**kwargs):
    return psycopg.connect(**kwargs)


# Indirection so the built-in self-test can inject a fake driver. Production
# code never replaces this.
_connect_impl = _default_connect


def harness_connect(**kwargs):
    """Open a harness connection whose transaction boundaries are explicit.

    autocommit=True is deliberate, not incidental. With psycopg's default
    (autocommit=False) a bare DDL or verification statement silently opens a
    transaction, and every later `with conn.transaction():` becomes a savepoint
    inside it. Statements would then not be committed when the scenario claims
    they are, and "the first statement of the protected transaction" would be
    false. With autocommit=True each standalone statement is its own committed
    transaction and each `with conn.transaction():` block is a real BEGIN/COMMIT.
    """
    conn = _connect_impl(**kwargs)
    conn.autocommit = True
    return conn


def connect_as(role, dbname, password=""):
    """Open a connection to one database as an explicit role."""
    return harness_connect(**_conn_kwargs(dbname, user=role,
                                          password=password or None))


def connect_migrator(dbname):
    """Scenario connections use the migration identity, never the administrator."""
    return connect_as(MIGRATOR_DB_USER, dbname, MIGRATOR_DB_PASSWORD)


def admin_connect(dbname="postgres"):
    """Administrative connection used only for disposable provisioning.

    The default is the cluster's `postgres` database, where roles are
    provisioned and backends are inspected. Objects that live in a scenario
    database must be touched through `scenario_admin_connect()` instead:
    application tables do not exist in `postgres`.
    """
    return harness_connect(**_conn_kwargs(dbname, user=ADMIN_DB_USER,
                                          password=ADMIN_DB_PASSWORD))


def scenario_admin_connect(db_name):
    """Administrator connection to one scenario database, for its own objects."""
    return harness_connect(**_conn_kwargs(db_name, user=ADMIN_DB_USER,
                                          password=ADMIN_DB_PASSWORD))


def provision_cluster_roles(admin):
    """Create the cluster-level test roles before any scenario runs.

    `validate_destructive_target()` has already refused any existing role that
    looks production-like, so this only ever creates a missing role or re-adds
    the one membership the harness owns. Cluster-level side effect: these three
    roles and the owner->migrator membership survive after the run, because
    dropping a database does not drop roles.
    """
    for name, login in ((MIGRATOR_ROLE, True), (APP_ROLE, True), (OWNER_ROLE, False)):
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                               (name,)).fetchone()
        if not exists:
            admin.execute(sql.SQL("CREATE ROLE {} {}").format(
                sql.Identifier(name), sql.SQL("LOGIN" if login else "NOLOGIN")))
    # `validate_destructive_target()` already refused any existing membership
    # with different options; an existing correct membership is left untouched
    # rather than re-granted.
    membership = admin.execute(
        "SELECT 1 FROM pg_auth_members am "
        "JOIN pg_roles r ON r.oid = am.roleid "
        "JOIN pg_roles m ON m.oid = am.member "
        "WHERE r.rolname = %s AND m.rolname = %s",
        (OWNER_ROLE, MIGRATOR_ROLE)).fetchone()
    if not membership:
        admin.execute(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE, ADMIN FALSE").format(
            sql.Identifier(OWNER_ROLE), sql.Identifier(MIGRATOR_ROLE)))


def _terminate_scenario_backends(admin_conn, db_name):
    """Terminate backends connected to one harness-owned scenario database."""
    admin_conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (db_name,),
    )


def prepare_scenario_database(admin_conn, db_name):
    """Create a genuinely empty database and its minimal provisioning grants.

    An existing database with the same name is only touched when its catalog
    comment carries this harness's marker for this cluster identity. A database
    is never terminated or dropped merely because its name looks like a
    scenario name.
    """
    exists, marker = _scenario_database_state(admin_conn, db_name)
    if exists:
        if marker != expected_database_marker():
            raise SafetyRefusal(
                f"database {db_name} already exists and does not carry this "
                "harness's disposable-cluster marker; refusing to terminate or "
                "drop it")
        _terminate_scenario_backends(admin_conn, db_name)
        admin_conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name)))
    admin_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    admin_conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
        sql.Identifier(db_name), sql.Literal(expected_database_marker())))
    admin_conn.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
        sql.Identifier(db_name), sql.Identifier(MIGRATOR_ROLE)))
    admin_conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(db_name), sql.Identifier(APP_ROLE)))
    scoped = scenario_admin_connect(db_name)
    try:
        for role in (MIGRATOR_ROLE, OWNER_ROLE):
            scoped.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                sql.Identifier(role)))
    finally:
        scoped.close()


def drop_scenario_db(admin_conn, db_name):
    """Remove a scenario database only when this harness provably created it."""
    exists, marker = _scenario_database_state(admin_conn, db_name)
    if not exists:
        return
    if marker != expected_database_marker():
        raise SafetyRefusal(
            f"refusing to drop {db_name}: it does not carry this harness's "
            "disposable-cluster marker")
    _terminate_scenario_backends(admin_conn, db_name)
    admin_conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name)))


def run_sql(conn, query, params=None, fetch_mode=None):
    """
    Execute SQL. Never commits or rolls back: the caller owns the transaction.
    fetch_mode: None | "one" | "all".
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        if fetch_mode == "one":
            return cur.fetchone()
        if fetch_mode == "all":
            return cur.fetchall()
    return None


def expect_sqlstate(conn, query, params, expected):
    """
    Run one statement inside a savepoint and return (matched, observed_sqlstate).
    The scenario transaction survives the expected error.
    """
    try:
        with conn.transaction():
            run_sql(conn, query, params)
    except psycopg.Error as exc:
        observed = getattr(exc, "sqlstate", None)
        return observed == expected, observed
    return False, None


def fingerprint(token):
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# ADR §9.2.1 — first installation on a genuinely empty database
# ---------------------------------------------------------------------------

class ProtocolFailure(AssertionError):
    """An executed scenario whose observed behaviour violated the contract."""


class SetupFailure(Exception):
    """A prerequisite could not be established, so the scenario is invalid setup."""


def preflight_first_installation(conn):
    """§9.2.1 steps 1–2: take the bootstrap guard and refuse a partial installation."""
    run_sql(conn, "SELECT pg_advisory_xact_lock("
                  "hashtextextended('strikenova:migration_internal:bootstrap', 0))")
    protocol_present = run_sql(
        conn, "SELECT to_regclass('migration_internal.lease_protocol')",
        fetch_mode="one")[0] is not None
    if protocol_present:
        raise ProtocolFailure(
            "first installation preflight found an existing protocol row; this is "
            "the §9.2.2 verification path, not a first installation")
    schema_present = run_sql(
        conn, "SELECT 1 FROM pg_namespace WHERE nspname = 'migration_internal'",
        fetch_mode="one") is not None
    leftovers = [name for name in ("migration_internal.migration_lease", "alembic_version")
                 if run_sql(conn, "SELECT to_regclass(%s)", (name,), fetch_mode="one")[0]
                 is not None]
    if schema_present or leftovers:
        raise ProtocolFailure(
            "first installation preflight refused a partial or out-of-order install "
            f"(§9.2.1 step 2 -> 45004): schema={schema_present}, leftovers={leftovers}")


BOOTSTRAP_LOCK_TIMEOUT_SQL = "SET lock_timeout = '10s'"

PROTOCOL_INSERT_SQL = (
    "INSERT INTO migration_internal.lease_protocol "
    "(installed_version, min_supported_version, max_supported_version, "
    "barrier_state, bootstrap_state) "
    "VALUES (1, 1, 1, 'closed', 'installing')")

LEASE_ROW_INSERT_SQL = (
    "INSERT INTO migration_internal.migration_lease "
    "(lease_name, protocol_version) "
    "VALUES ('migration-lease', 1)")

# ADR §9.2.1 step 11 — the only statement that moves the row to an admitted state.
PROTOCOL_ACTIVATION_SQL = (
    "UPDATE migration_internal.lease_protocol "
    "SET installed_version = 1, "
    "    min_supported_version = 1, "
    "    max_supported_version = 1, "
    "    barrier_state = 'open', "
    "    bootstrap_state = 'ready', "
    "    updated_at = now(), "
    "    updated_by = session_user "
    "WHERE singleton")


def bootstrap_first_installation(conn):
    """Execute ADR §9.2.1 steps 1–12 as one atomic transaction.

    Order is the ADR's, not a convenient one: the whole structural, ownership,
    constraint, signature, ACL and default-ACL verification (step 10) runs while
    the protocol row still reads `closed` / `installing`, and the row is moved to
    `open` / `ready` (step 11) only after that verification succeeded. A
    verification failure raises ProtocolFailure inside the block, which rolls the
    entire bootstrap back instead of leaving a half-installed schema behind.
    """
    run_sql(conn, BOOTSTRAP_LOCK_TIMEOUT_SQL)
    with conn.transaction():
        # Steps 1–2: bootstrap guard and empty-database preflight.
        preflight_first_installation(conn)
        # Steps 3–8: schema, tables, initial rows, then the four functions.
        run_sql(conn, "CREATE SCHEMA migration_internal")
        run_sql(conn, PROTOCOL_TABLE_SCHEMA)
        run_sql(conn, PROTOCOL_INSERT_SQL)
        run_sql(conn, LEASE_TABLE_SCHEMA)
        run_sql(conn, LEASE_ROW_INSERT_SQL)
        run_sql(conn, LEASE_ACQUIRE_V1_SQL)
        run_sql(conn, LEASE_ENTER_V1_SQL)
        run_sql(conn, LEASE_RELEASE_V1_SQL)
        run_sql(conn, LEASE_PROTOCOL_INFO_V1_SQL)
        # Step 9: revoke set, after the last CREATE FUNCTION.
        run_sql(conn, REVOKE_SET_SQL)
        # Step 10: full verification while the protocol is closed/installing.
        verify_installation(conn, expected_version=1, expect_active=False)
        # Step 11: activation, only after verification passed.
        run_sql(conn, PROTOCOL_ACTIVATION_SQL)
        # Step 12: final protocol-info assertion, before the same COMMIT.
        assert_protocol_info(conn)


EXPECTED_COLUMNS = {
    ("migration_lease", "lease_name"): ("text", "NO"),
    ("migration_lease", "protocol_version"): ("integer", "NO"),
    ("migration_lease", "owner_token_hash"): ("text", "YES"),
    ("migration_lease", "owner_fingerprint"): ("text", "YES"),
    ("migration_lease", "owner_role"): ("name", "YES"),
    ("migration_lease", "acquired_at"): ("timestamp with time zone", "YES"),
    ("migration_lease", "expires_at"): ("timestamp with time zone", "YES"),
    ("migration_lease", "released_at"): ("timestamp with time zone", "YES"),
    ("migration_lease", "generation"): ("bigint", "NO"),
    ("migration_lease", "updated_at"): ("timestamp with time zone", "NO"),
    ("lease_protocol", "singleton"): ("boolean", "NO"),
    ("lease_protocol", "installed_version"): ("integer", "NO"),
    ("lease_protocol", "min_supported_version"): ("integer", "NO"),
    ("lease_protocol", "max_supported_version"): ("integer", "NO"),
    ("lease_protocol", "barrier_state"): ("text", "NO"),
    ("lease_protocol", "bootstrap_state"): ("text", "NO"),
    ("lease_protocol", "updated_at"): ("timestamp with time zone", "NO"),
    ("lease_protocol", "updated_by"): ("name", "NO"),
    ("lease_protocol", "note"): ("text", "YES"),
}

# §9.3 "Check constraints | present with the documented expressions". Each entry
# is the COMPLETE expected `pg_get_constraintdef()` text, not a set of marker
# fragments: an added Boolean branch (`OR true`), a dropped conjunct, an extra
# `IN` alternative or a changed operator produces a different string and fails.
#
# These are the canonical PostgreSQL 16 renderings of the §6.1 DDL. They were NOT
# observed through `pg_get_constraintdef()` in this environment (no PostgreSQL
# 16.16 is installed here), so the first real run must confirm them: a mismatch
# is reported with the observed text and is never silently accepted, which is
# the fail-closed direction.
EXPECTED_CONSTRAINTS = {
    ("migration_lease", "migration_lease_pkey"): (
        "p", "primary key (lease_name)"),
    ("lease_protocol", "lease_protocol_pkey"): (
        "p", "primary key (singleton)"),
    ("migration_lease", "migration_lease_singleton_ck"): (
        "c", "check ((lease_name = 'migration-lease'::text))"),
    ("migration_lease", "migration_lease_generation_ck"): (
        "c", "check ((generation >= 0))"),
    ("migration_lease", "migration_lease_owner_shape_ck"): (
        "c", "check ((((owner_token_hash is null) and (owner_fingerprint is null)) "
             "or ((owner_token_hash is not null) and (owner_fingerprint is not null))))"),
    ("migration_lease", "migration_lease_owned_expiry_ck"): (
        "c", "check (((owner_token_hash is null) or (expires_at is not null)))"),
    ("lease_protocol", "lease_protocol_barrier_state_check"): (
        "c", "check ((barrier_state = any (array['open'::text, 'closed'::text])))"),
    ("lease_protocol", "lease_protocol_bootstrap_state_check"): (
        "c", "check ((bootstrap_state = any (array['ready'::text, 'installing'::text])))"),
    ("lease_protocol", "lease_protocol_singleton_check"): (
        "c", "check (singleton)"),
}

# §9.3 "Function signatures | exactly the documented identity arguments and
# result types", including the five-column protocol-info result.
EXPECTED_FUNCTIONS = {
    "lease_acquire_v1": (
        "integer, text, double precision, integer",
        "table(out_owner_fingerprint text, out_expires_at timestamp with time zone, "
        "out_generation bigint)"),
    "lease_enter_v1": (
        "integer, text, double precision, integer",
        "table(out_owner_fingerprint text, out_expires_at timestamp with time zone, "
        "out_generation bigint)"),
    "lease_release_v1": ("integer, text", "boolean"),
    "lease_protocol_info_v1": (
        "",
        "table(installed_version integer, min_supported_version integer, "
        "max_supported_version integer, barrier_state text, bootstrap_state text)"),
}


def _normalize_sql_text(value):
    """Collapse whitespace and case so catalog rendering differences do not matter."""
    return " ".join((value or "").lower().split())


def assert_constraint_definitions(rows):
    """Compare complete constraint definitions. Pure function: needs no database.

    `rows` are (relation, constraint name, contype, pg_get_constraintdef) tuples.
    Any difference from EXPECTED_CONSTRAINTS raises, including a definition that
    still contains every expected fragment but adds a branch.
    """
    observed = {(rel.split(".")[-1], name): (kind, _normalize_sql_text(definition))
                for rel, name, kind, definition in rows}
    if set(observed) != set(EXPECTED_CONSTRAINTS):
        raise ProtocolFailure(
            "§9.3 verification failed: constraint set differs (missing "
            f"{sorted(set(EXPECTED_CONSTRAINTS) - set(observed))}, extra "
            f"{sorted(set(observed) - set(EXPECTED_CONSTRAINTS))})")
    for key, (kind, definition) in EXPECTED_CONSTRAINTS.items():
        got_kind, got_definition = observed[key]
        if got_kind != kind:
            raise ProtocolFailure(
                f"§9.3 verification failed: {key[1]} contype {got_kind!r} != {kind!r}")
        if got_definition != definition:
            raise ProtocolFailure(
                f"§9.3 verification failed: {key[1]} definition {got_definition!r} "
                f"is not the documented definition {definition!r}")


def verify_installation(conn, expected_version=1, expect_active=True):
    """ADR §9.3 verification, §9.4 default ACLs and §10.3 effective privileges.

    Runs inside the caller's transaction and repairs nothing: every mismatch
    raises ProtocolFailure, which aborts the bootstrap transaction. With
    `expect_active=False` this is the pre-activation pass of §9.2.1 step 10, so
    the protocol row is required to still read `closed` / `installing`; the final
    `open` / `ready` assertion belongs to step 12 (`assert_protocol_info`).
    """
    def fail(message):
        raise ProtocolFailure("§9.3 verification failed: " + message)

    expected_state = ("open", "ready") if expect_active else ("closed", "installing")

    # -- schema -----------------------------------------------------------
    schema_owner = run_sql(
        conn, "SELECT n.nspowner::regrole::text FROM pg_namespace n "
              "WHERE n.nspname = 'migration_internal'", fetch_mode="one")
    if schema_owner is None:
        fail("schema migration_internal is missing")
    if schema_owner[0] != MIGRATOR_ROLE:
        fail(f"schema owner {schema_owner[0]!r} is not {MIGRATOR_ROLE}")

    # -- relations: only the two tables, correct kind, owner and row security --
    relations = run_sql(
        conn, "SELECT c.relname, c.relkind, c.relowner::regrole::text, "
              "c.relrowsecurity "
              "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
              "WHERE n.nspname = 'migration_internal' AND c.relkind <> 'i'",
        fetch_mode="all")
    tables = {name: (kind, owner, rls) for name, kind, owner, rls in relations}
    if set(tables) != {"migration_lease", "lease_protocol"}:
        fail(f"relations in migration_internal are {sorted(tables)}, "
             "expected exactly migration_lease and lease_protocol")
    for name, (kind, owner, rls) in tables.items():
        if kind != "r":
            fail(f"{name} relkind {kind!r} is not an ordinary table")
        if owner != MIGRATOR_ROLE:
            fail(f"{name} owner {owner} is not {MIGRATOR_ROLE}")
        if rls:
            fail(f"{name} has row security enabled")

    # -- columns ----------------------------------------------------------
    columns = run_sql(
        conn, "SELECT table_name, column_name, data_type, is_nullable "
              "FROM information_schema.columns WHERE table_schema = 'migration_internal'",
        fetch_mode="all")
    got_columns = {(t, c): (d, n) for t, c, d, n in columns}
    if got_columns != EXPECTED_COLUMNS:
        missing = sorted(set(EXPECTED_COLUMNS) - set(got_columns))
        extra = sorted(set(got_columns) - set(EXPECTED_COLUMNS))
        wrong = sorted(key for key in set(got_columns) & set(EXPECTED_COLUMNS)
                       if got_columns[key] != EXPECTED_COLUMNS[key])
        fail(f"columns differ from §6.1 (missing {missing}, extra {extra}, "
             f"wrong type/nullability {wrong})")

    # -- constraints: names, types and full definitions --------------------
    constraints = run_sql(
        conn, "SELECT c.conrelid::regclass::text, c.conname, c.contype, "
              "pg_get_constraintdef(c.oid) "
              "FROM pg_constraint c "
              "WHERE c.connamespace = 'migration_internal'::regnamespace",
        fetch_mode="all")
    assert_constraint_definitions(constraints)

    # -- functions: identity args, complete result, owner, security, search_path --
    funcs = run_sql(
        conn, "SELECT p.proname, pg_get_function_identity_arguments(p.oid), "
              "pg_get_function_result(p.oid), p.proowner::regrole::text, "
              "p.prosecdef, p.proconfig "
              "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
              "WHERE n.nspname = 'migration_internal'", fetch_mode="all")
    by_name = {row[0]: row[1:] for row in funcs}
    if set(by_name) != set(EXPECTED_FUNCTIONS):
        fail(f"function set {sorted(by_name)} differs from "
             f"{sorted(EXPECTED_FUNCTIONS)}")
    for name, (args, result) in EXPECTED_FUNCTIONS.items():
        got_args, got_result, owner, secdef, config = by_name[name]
        if _normalize_sql_text(got_args) != _normalize_sql_text(args):
            fail(f"{name} identity arguments {got_args!r} != {args!r}")
        if _normalize_sql_text(got_result) != _normalize_sql_text(result):
            fail(f"{name} result signature {got_result!r} != {result!r}")
        if owner != MIGRATOR_ROLE:
            fail(f"{name} owner {owner} is not {MIGRATOR_ROLE}")
        if not secdef:
            fail(f"{name} is not SECURITY DEFINER")
        if list(config or []) != ["search_path=pg_catalog"]:
            fail(f"{name} proconfig {config!r} is not exactly "
                 "{search_path=pg_catalog}")

    # -- §10.3 item 4: expand ACLs, including NULL ACLs, and look for PUBLIC --
    public_entries = run_sql(
        conn,
        "SELECT 'table:' || c.relname, a.privilege_type "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a "
        "WHERE n.nspname = 'migration_internal' AND c.relkind = 'r' AND a.grantee = 0 "
        "UNION ALL "
        "SELECT 'function:' || p.proname, a.privilege_type "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "WHERE n.nspname = 'migration_internal' AND a.grantee = 0 "
        "UNION ALL "
        "SELECT 'schema:migration_internal', a.privilege_type "
        "FROM pg_namespace n "
        "CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
        "WHERE n.nspname = 'migration_internal' AND a.grantee = 0",
        fetch_mode="all")
    if public_entries:
        fail(f"PUBLIC holds expanded privileges: {public_entries}")

    # -- §10.3 items 1–3: runtime role, when it exists ---------------------
    app_oid_row = run_sql(conn, "SELECT oid FROM pg_roles WHERE rolname = %s",
                          (APP_ROLE,), fetch_mode="one")
    if app_oid_row is None:
        # §9.5: on a clean database before provisioning the runtime role may be
        # absent; bootstrap records that and proceeds. T-15 re-runs these checks
        # after provisioning. Nothing is skipped for PUBLIC above.
        print(f"  note: runtime role {APP_ROLE} is absent; "
              "effective-privilege checks deferred to T-15")
    else:
        app_oid = app_oid_row[0]
        role_entries = run_sql(
            conn,
            "SELECT 'table:' || c.relname, a.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a "
            "WHERE n.nspname = 'migration_internal' AND c.relkind = 'r' "
            "AND a.grantee = %s "
            "UNION ALL "
            "SELECT 'function:' || p.proname, a.privilege_type "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE n.nspname = 'migration_internal' AND a.grantee = %s "
            "UNION ALL "
            "SELECT 'schema:migration_internal', a.privilege_type "
            "FROM pg_namespace n "
            "CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
            "WHERE n.nspname = 'migration_internal' AND a.grantee = %s",
            (app_oid, app_oid, app_oid), fetch_mode="all")
        if role_entries:
            fail(f"{APP_ROLE} holds expanded privileges: {role_entries}")

        if run_sql(conn, "SELECT has_schema_privilege(%s, 'migration_internal', "
                         "'USAGE')", (APP_ROLE,), fetch_mode="one")[0]:
            fail(f"{APP_ROLE} holds effective schema USAGE")

        table_privileges = run_sql(
            conn,
            "SELECT c.relname, p.priv, has_table_privilege(%s, c.oid, p.priv) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) "
            "AS p(priv) "
            "WHERE n.nspname = 'migration_internal' AND c.relkind = 'r'",
            (APP_ROLE,), fetch_mode="all")
        for relname, priv, granted in table_privileges:
            if granted:
                fail(f"{APP_ROLE} holds effective {priv} on migration_internal.{relname}")

        for relname, usage, select in run_sql(
                conn,
                "SELECT c.relname, has_sequence_privilege(%s, c.oid, 'USAGE'), "
                "has_sequence_privilege(%s, c.oid, 'SELECT') "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'migration_internal' AND c.relkind = 'S'",
                (APP_ROLE, APP_ROLE), fetch_mode="all"):
            if usage or select:
                fail(f"{APP_ROLE} holds effective sequence privileges on {relname}")

        for proname, granted in run_sql(
                conn,
                "SELECT p.proname, has_function_privilege(%s, p.oid, 'EXECUTE') "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'migration_internal'",
                (APP_ROLE,), fetch_mode="all"):
            if granted:
                fail(f"{APP_ROLE} holds effective EXECUTE on {proname}")

        reaches = run_sql(
            conn,
            "WITH RECURSIVE reach(roleid, member_id, depth) AS ("
            "  SELECT m.roleid, m.member, 1 FROM pg_auth_members m "
            "   WHERE m.member = %s "
            "  UNION ALL "
            "  SELECT m.roleid, m.member, r.depth + 1 FROM pg_auth_members m "
            "    JOIN reach r ON m.member = r.roleid WHERE r.depth < 10) "
            "SELECT roleid::regrole::text, member_id::regrole::text, depth FROM reach",
            (app_oid,), fetch_mode="all")
        if reaches:
            fail(f"{APP_ROLE} reaches roles through transitive membership: {reaches}")

        attributes = run_sql(
            conn, "SELECT rolname, rolsuper, rolbypassrls, rolinherit "
                  "FROM pg_roles WHERE rolname = %s", (APP_ROLE,), fetch_mode="one")
        if attributes is None:
            fail(f"{APP_ROLE} disappeared during verification")
        _, rolsuper, rolbypassrls, _rolinherit = attributes
        if rolsuper:
            fail(f"{APP_ROLE} is SUPERUSER")
        if rolbypassrls:
            fail(f"{APP_ROLE} is BYPASSRLS")

    # -- §9.4: global and schema-scoped default ACLs -----------------------
    app_oid_for_defaults = app_oid_row[0] if app_oid_row is not None else -1
    default_entries = run_sql(
        conn,
        "SELECT CASE WHEN d.defaclnamespace = 0 THEN '<global>' "
        "            ELSE d.defaclnamespace::regnamespace::text END, "
        "       d.defaclobjtype, a.grantee, a.privilege_type "
        "FROM pg_default_acl d "
        "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
        "WHERE d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = %s) "
        "AND (d.defaclnamespace = 0 "
        "     OR d.defaclnamespace = 'migration_internal'::regnamespace)",
        (MIGRATOR_ROLE,), fetch_mode="all")
    for scope, objtype, grantee, privilege in default_entries:
        if grantee == 0:
            fail(f"default ACL {scope} on {objtype} grants {privilege} to PUBLIC")
        if grantee == app_oid_for_defaults:
            fail(f"default ACL {scope} on {objtype} grants {privilege} to {APP_ROLE}")

    # -- singleton rows ---------------------------------------------------
    protocol = run_sql(
        conn, "SELECT count(*), min(installed_version), min(min_supported_version), "
              "min(max_supported_version), min(barrier_state), min(bootstrap_state) "
              "FROM migration_internal.lease_protocol", fetch_mode="one")
    expected_protocol = (1, expected_version, expected_version, expected_version,
                         expected_state[0], expected_state[1])
    if protocol != expected_protocol:
        fail(f"protocol row state {protocol!r} != {expected_protocol!r}")
    lease = run_sql(
        conn, "SELECT count(*), min(lease_name) FROM migration_internal.migration_lease",
        fetch_mode="one")
    if lease != (1, "migration-lease"):
        fail(f"lease row state {lease!r}")


def assert_protocol_info(conn, expected_version=1):
    """ADR §9.2.1 step 12: exactly one row reading ('open', 'ready')."""
    rows = run_sql(
        conn, "SELECT * FROM migration_internal.lease_protocol_info_v1()",
        fetch_mode="all")
    expected = [(expected_version, expected_version, expected_version, "open", "ready")]
    if rows != expected:
        raise ProtocolFailure(
            f"§9.2.1 step 12 verification failed: protocol info {rows!r} != {expected!r}")


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------

def assert_lease_result(row, expected_generation=None):
    """A successful acquire/enter returns (fingerprint text, timestamptz, bigint)."""
    if row is None or len(row) != 3:
        raise AssertionError(f"expected a 3-column lease result, got {row!r}")
    fp, expires_at, generation = row
    if not isinstance(fp, str) or len(fp) != 12:
        raise AssertionError(f"owner fingerprint is not 12 hex chars: {fp!r}")
    if not isinstance(expires_at, datetime.datetime):
        raise AssertionError(f"expires_at is not a timestamp: {expires_at!r}")
    if not isinstance(generation, int):
        raise AssertionError(f"generation is not an integer: {generation!r}")
    if expected_generation is not None and generation != expected_generation:
        raise AssertionError(f"generation {generation} != expected {expected_generation}")
    return fp, expires_at, generation


def lease_row(conn):
    return run_sql(conn,
                   "SELECT owner_token_hash, owner_fingerprint, expires_at, generation "
                   "FROM migration_internal.migration_lease "
                   "WHERE lease_name = 'migration-lease'",
                   fetch_mode="one")


def scenario_db_name(key):
    return "issue145_" + key.lower().replace("-", "_")


def wait_for_lease_expiry(conn, timeout=30.0):
    """Wait on the database clock until the lease row reports expiry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        expired = run_sql(
            conn, "SELECT expires_at IS NOT NULL AND expires_at <= clock_timestamp() "
                  "FROM migration_internal.migration_lease "
                  "WHERE lease_name = 'migration-lease'", fetch_mode="one")[0]
        if expired:
            return True
        run_sql(conn, "SELECT pg_sleep(0.2)")
    return False


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_T01(conn):
    """T-01: first installation as the migrator from a genuinely empty database."""
    current = run_sql(conn, "SELECT current_user::text", fetch_mode="one")[0]
    if current != MIGRATOR_ROLE:
        raise SetupFailure(
            f"T-01 must execute as {MIGRATOR_ROLE}, connected as {current!r}")

    for obj in ("migration_internal.migration_lease",
                "migration_internal.lease_protocol",
                "public.positions", "public.paper_orders",
                "public.strategy_leg_exposures"):
        if run_sql(conn, "SELECT to_regclass(%s)", (obj,), fetch_mode="one")[0] is not None:
            raise ProtocolFailure(f"T-01 database is not empty: {obj} exists")
    if run_sql(conn, "SELECT to_regclass('alembic_version')", fetch_mode="one")[0] is not None:
        raise ProtocolFailure("T-01 database is not empty: alembic_version exists")

    # §9.2.1 runs as one transaction: preflight, create, revoke set, verify while
    # the protocol is still closed/installing, then activate and commit. Because
    # the connection is autocommit, every read after this point is its own
    # statement — no implicit transaction stays open around acquisition and entry.
    bootstrap_first_installation(conn)
    verify_installation(conn, expected_version=1)

    if run_sql(conn, "SELECT to_regclass('alembic_version')", fetch_mode="one")[0] is not None:
        raise ProtocolFailure("T-01: bootstrap created alembic_version")
    if run_sql(conn, "SELECT to_regclass('public.positions')", fetch_mode="one")[0] is not None:
        raise ProtocolFailure("T-01: bootstrap created an application table")

    with conn.transaction():
        row = run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                      (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one")
        assert_lease_result(row, expected_generation=1)
    with conn.transaction():
        row = run_sql(conn, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                      (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one")
        assert_lease_result(row, expected_generation=1)
    return "PASS"


def scenario_T02(conn):
    """T-02: two concurrent first installations."""
    return ("NOT RUN — requires two independent PostgreSQL backends starting §9.2.1 "
            "simultaneously; the bootstrap guard serialization cannot be observed from one session")


def scenario_T03(conn):
    """T-03: two workers acquire one unowned lease simultaneously."""
    return ("NOT RUN — requires two independent PostgreSQL backends calling acquire "
            "concurrently to observe 45002/55P03 on the loser")


def scenario_T04(conn):
    """T-04: W1 enters twice in one transaction; a second worker's enter is refused 55P03."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"),
            expected_generation=1)

    w2 = connect_migrator(scenario_db_name("T-04"))
    try:
        # A normal context manager: any failed assertion rolls W1 back instead of
        # committing, and the row lock is released on both success and failure.
        with conn.transaction():
            # W1 enters twice in the same transaction; both must succeed.
            assert_lease_result(
                run_sql(conn, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                        (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))
            assert_lease_result(
                run_sql(conn, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                        (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))

            # W2's enter in its own transaction must time out on the row lock.
            with w2.transaction():
                matched, observed = expect_sqlstate(
                    w2,
                    "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, 1000),
                    "55P03")
            if not matched:
                raise ProtocolFailure(
                    f"T-04: expected 55P03 while W1 holds the lock, got {observed}")

        # After W1 commits the row lock is free and W2's enter succeeds.
        with w2.transaction():
            assert_lease_result(
                run_sql(w2, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                        (TOKEN_A, LEASE_TTL_SECONDS, 1000), fetch_mode="one"))
    finally:
        w2.close()
    return "PASS"


def scenario_T05(conn):
    """T-05: GUC forgery cannot substitute for the token comparison."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS))
    fp_before = lease_row(conn)[1]

    with conn.transaction():
        run_sql(conn, "SELECT set_config('app.migration_owner', 'strikenova_prod_migrator', true)")
        run_sql(conn, "SELECT set_config('strikenova.lease_token', %s, true)", (TOKEN_A,))
        run_sql(conn, "SELECT set_config('role', 'strikenova_prod_migrator', true)")
        run_sql(conn, "SET SESSION AUTHORIZATION DEFAULT")
        matched, observed = expect_sqlstate(
            conn,
            "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
            (TOKEN_B, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS),
            "45001")
    if not matched:
        raise AssertionError(f"T-05: expected 45001 from forged-GUC enter, got {observed}")

    if lease_row(conn)[1] != fp_before:
        raise AssertionError("T-05: owner fingerprint changed after forged enter")

    # No lease function may read a GUC that could impersonate the owner.
    sources = run_sql(conn,
                      "SELECT p.proname, p.prosrc FROM pg_proc p "
                      "JOIN pg_namespace n ON n.oid = p.pronamespace "
                      "WHERE n.nspname = 'migration_internal'",
                      fetch_mode="all")
    forbidden = ("app.migration_owner", "strikenova.lease_token",
                 "session_authorization", "current_setting('role')")
    for name, source in sources:
        for marker in forbidden:
            if marker in source:
                raise AssertionError(f"T-05: {name} reads a forgeable setting: {marker}")
    return "PASS"


def scenario_T06(conn):
    """T-06: a valid-format foreign token cannot enter, release, or take over."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        row = run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                      (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one")
    assert_lease_result(row, expected_generation=1)
    fp_before = lease_row(conn)[1]

    for query, expected in (
        ("SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
         "45001"),
        ("SELECT * FROM migration_internal.lease_release_v1(1, %s)", "45001"),
        ("SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)", "45002"),
    ):
        params = (TOKEN_B, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS) if query.count("%s") == 3 \
            else (TOKEN_B,)
        with conn.transaction():
            matched, observed = expect_sqlstate(conn, query, params, expected)
        if not matched:
            raise AssertionError(f"T-06: expected {expected}, got {observed}")

    if lease_row(conn)[1] != fp_before:
        raise AssertionError("T-06: foreign worker modified the lease row")
    return "PASS"


def scenario_T07(conn):
    """T-07: W1 holds the row lock; W2's enter with the correct token must fail 55P03."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"),
            expected_generation=1)

    w2 = connect_migrator(scenario_db_name("T-07"))
    try:
        # W1 opens a transaction and takes the lease row lock without committing.
        with conn.transaction():
            run_sql(conn, "SELECT 1 FROM migration_internal.migration_lease "
                          "WHERE lease_name = 'migration-lease' FOR UPDATE")
            with w2.transaction():
                matched, observed = expect_sqlstate(
                    w2,
                    "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, 1000),
                    "55P03")
            if not matched:
                raise ProtocolFailure(
                    f"T-07: expected 55P03 for the blocked enter, got {observed}")

        # After W1's block ends the lock is free and the same token may enter.
        with w2.transaction():
            assert_lease_result(
                run_sql(w2, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                        (TOKEN_A, LEASE_TTL_SECONDS, 1000), fetch_mode="one"))
    finally:
        w2.close()
    return "PASS"


def scenario_T08(conn):
    """T-08: two workers run an Alembic upgrade head against one lease."""
    return ("NOT RUN — requires the real Alembic chain and two independent backends; "
            "no Alembic binary or migration runner exists in this harness")


def scenario_T09(conn):
    """T-09: concurrent backfill of three users."""
    return ("NOT RUN — requires the real backfill implementation and two independent "
            "backends; no backfill code exists in this harness")


def scenario_T10(conn):
    """T-10: each backfill transaction shows exactly two enter calls."""
    return ("NOT RUN — requires the real backfill implementation; the per-user "
            "transaction pattern cannot be exercised without it")


def scenario_T11(conn):
    """T-11: a real user write rolls back when the pre-commit enter finds the lease expired."""
    bootstrap_first_installation(conn)
    # Standalone DDL on an autocommit connection: committed here, before the
    # lease is acquired, so the protected transaction below contains only the
    # user write it is supposed to roll back.
    run_sql(conn, "CREATE TABLE public.positions (id int PRIMARY KEY, user_id int)")
    short_ttl = 1
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, short_ttl, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"),
            expected_generation=1)
    fp_before = lease_row(conn)[1]

    w2 = connect_migrator(scenario_db_name("T-11"))
    try:
        observed = None
        try:
            with conn.transaction():
                assert_lease_result(
                    run_sql(conn, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                            (TOKEN_A, short_ttl, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))
                run_sql(conn, "INSERT INTO public.positions (id, user_id) VALUES (201, 9)")
                # While W1 holds the row lock, W2 cannot take over.
                with w2.transaction():
                    locked, lock_code = expect_sqlstate(
                        w2,
                        "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                        (TOKEN_B, short_ttl, LEASE_LOCK_TIMEOUT_MS),
                        "55P03")
                if not locked:
                    raise ProtocolFailure(
                        f"T-11: W2 acquired while W1 held the row lock ({lock_code})")
                # Let the database clock pass the expiry; the pre-commit enter must fail.
                run_sql(conn, "SELECT pg_sleep(2)")
                run_sql(conn, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                        (TOKEN_A, short_ttl, LEASE_LOCK_TIMEOUT_MS))
        except psycopg.Error as exc:
            observed = getattr(exc, "sqlstate", None)

        if observed != "45002":
            raise ProtocolFailure(
                f"T-11: expected 45002 from the pre-commit enter, got {observed}")
        if run_sql(conn, "SELECT count(*) FROM public.positions", fetch_mode="one")[0] != 0:
            raise ProtocolFailure("T-11: the protected user write was not rolled back")

        # The failed transaction released the row lock.
        with w2.transaction():
            run_sql(w2, "SELECT 1 FROM migration_internal.migration_lease "
                        "WHERE lease_name = 'migration-lease' FOR UPDATE NOWAIT")

        # After expiry W2 acquires and ownership changes.
        with w2.transaction():
            fp_after, _, generation = assert_lease_result(
                run_sql(w2, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                        (TOKEN_B, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))
        if fp_after != fingerprint(TOKEN_B) or generation != 2:
            raise ProtocolFailure(
                "T-11: takeover did not record the new owner at generation 2")
        if fp_after == fp_before:
            raise ProtocolFailure("T-11: owner state did not change after takeover")
    finally:
        w2.close()
    return "PASS"


def scenario_T12(conn):
    """T-12: kill W1's backend inside a protected transaction and verify recovery."""
    bootstrap_first_installation(conn)
    # Committed before W1's protected transaction starts.
    run_sql(conn, "CREATE TABLE public.positions (id int PRIMARY KEY, user_id int)")
    ttl = 5
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, ttl, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"),
            expected_generation=1)
    fp_before = lease_row(conn)[1]

    admin = admin_connect()
    admin.autocommit = True
    w2 = connect_migrator(scenario_db_name("T-12"))
    try:
        observed = None
        try:
            with conn.transaction():
                # enter is the first statement of the protected transaction.
                enter_row = run_sql(
                    conn, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                    (TOKEN_A, ttl, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one")
                assert_lease_result(enter_row)
                run_sql(conn, "INSERT INTO public.positions (id, user_id) VALUES (101, 7)")
                pid = run_sql(conn, "SELECT pg_backend_pid()", fetch_mode="one")[0]
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
                run_sql(conn, "SELECT 1")
        except psycopg.Error as exc:
            observed = getattr(exc, "sqlstate", None)

        # Backend termination has its own SQLSTATE; a merely closed connection is
        # not evidence and neither is an absent exception.
        if observed not in ("57P01", "08006"):
            raise ProtocolFailure(
                "T-12: expected backend-termination SQLSTATE 57P01 or 08006, "
                f"got {observed!r}")
        if run_sql(w2, "SELECT count(*) FROM public.positions", fetch_mode="one")[0] != 0:
            raise ProtocolFailure(
                "T-12: the killed transaction's application write was not rolled back")

        # The dead backend's transaction released the lease row lock; only the
        # recorded ownership survives until expiry.
        with w2.transaction():
            run_sql(w2, "SELECT 1 FROM migration_internal.migration_lease "
                        "WHERE lease_name = 'migration-lease' FOR UPDATE NOWAIT")

        # Before expiry the recorded owner is still live.
        with w2.transaction():
            matched, code = expect_sqlstate(
                w2, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                (TOKEN_B, ttl, LEASE_LOCK_TIMEOUT_MS), "45002")
        if not matched:
            raise ProtocolFailure(
                f"T-12: W2 acquire before expiry expected 45002, got {code}")
        if lease_row(w2)[1] != fp_before:
            raise ProtocolFailure("T-12: lease ownership changed before expiry")

        # After the database clock passes expiry, W2 takes over cleanly.
        if not wait_for_lease_expiry(w2, timeout=30):
            raise ProtocolFailure("T-12: the lease did not expire within the wait budget")
        with w2.transaction():
            fp_after, _, generation = assert_lease_result(
                run_sql(w2, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                        (TOKEN_B, ttl, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))
        if fp_after != fingerprint(TOKEN_B) or generation != 2:
            raise ProtocolFailure(
                "T-12: takeover did not record the new owner at generation 2")
    finally:
        w2.close()
        admin.close()
    return "PASS"


def scenario_T13(conn):
    """T-13: interrupted backfill and idempotent recovery."""
    return ("NOT RUN — requires the real multi-user backfill implementation, a killed "
            "worker and a recovery run against the same ledger")


def scenario_T14(conn):
    """T-14: installed_version mismatch and a closed barrier are refused."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))
    fp_before = lease_row(conn)[1]

    # installed_version = 2, supported range 1..2, mirror still 1.
    with conn.transaction():
        run_sql(conn, "UPDATE migration_internal.lease_protocol "
                      "SET installed_version = 2, min_supported_version = 1, "
                      "max_supported_version = 2 WHERE singleton")
    for query, expected in (
        ("SELECT * FROM migration_internal.lease_release_v1(1, %s)", "45006"),
        ("SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)", "45006"),
    ):
        params = (TOKEN_A,) if query.count("%s") == 1 \
            else (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS)
        with conn.transaction():
            matched, observed = expect_sqlstate(conn, query, params, expected)
        if not matched:
            raise AssertionError(f"T-14: expected {expected}, got {observed}")

    if lease_row(conn)[1] != fp_before:
        raise AssertionError("T-14: version refusal changed the lease row")

    # Restore version 1 and close the barrier: a normal startup is refused 45004.
    with conn.transaction():
        run_sql(conn, "UPDATE migration_internal.lease_protocol "
                      "SET installed_version = 1, max_supported_version = 1, "
                      "barrier_state = 'closed' WHERE singleton")
    with conn.transaction():
        matched, observed = expect_sqlstate(
            conn,
            "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
            (TOKEN_B, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS),
            "45004")
    if not matched:
        raise AssertionError(f"T-14: expected 45004 with the barrier closed, got {observed}")
    if run_sql(conn, "SELECT barrier_state FROM migration_internal.lease_protocol "
                     "WHERE singleton", fetch_mode="one")[0] != "closed":
        raise AssertionError("T-14: barrier row changed unexpectedly")
    return "PASS"


def _ensure_role(conn, name, login=False):
    exists = run_sql(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s",
                     (name,), fetch_mode="one")
    if exists is None:
        run_sql(conn, sql.SQL("CREATE ROLE {} {}").format(
            sql.Identifier(name), sql.SQL("LOGIN" if login else "NOLOGIN")))


def scenario_T15(conn):
    """T-15: the runtime APP identity has no migration_internal privilege (Topology A)."""
    if run_sql(conn, "SELECT current_user::text", fetch_mode="one")[0] != MIGRATOR_ROLE:
        raise SetupFailure("T-15 must run on a migrator connection")
    bootstrap_first_installation(conn)
    with conn.transaction():
        run_sql(conn, REVOKE_SET_SQL)

    checks = [
        ("SELECT has_schema_privilege(%s, 'migration_internal', 'USAGE')", (APP_ROLE,)),
        ("SELECT has_table_privilege(%s, 'migration_internal.migration_lease', 'SELECT')",
         (APP_ROLE,)),
        ("SELECT has_table_privilege(%s, 'migration_internal.migration_lease', 'UPDATE')",
         (APP_ROLE,)),
        ("SELECT has_table_privilege(%s, 'migration_internal.lease_protocol', 'SELECT')",
         (APP_ROLE,)),
        ("SELECT has_function_privilege(%s, "
         "'migration_internal.lease_acquire_v1(integer,text,float8,integer)', 'EXECUTE')",
         (APP_ROLE,)),
    ]
    for query, params in checks:
        if run_sql(conn, query, params, fetch_mode="one")[0]:
            raise ProtocolFailure(f"T-15: APP holds an effective privilege: {query}")

    for query, params in (
        ("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
         "aclexplode(c.relacl) a WHERE n.nspname = 'migration_internal' AND a.grantee = 0",
         None),
        ("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
         "aclexplode(p.proacl) a WHERE n.nspname = 'migration_internal' AND a.grantee = 0",
         None),
        ("SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) a "
         "WHERE n.nspname = 'migration_internal' AND a.grantee = 0", None),
    ):
        entries = run_sql(conn, query, params, fetch_mode="one")[0]
        if entries != 0:
            raise ProtocolFailure(f"T-15: PUBLIC still holds {entries} ACL entries")

    # A real APP session; direct attempts are denied with 42501.
    app_conn = connect_as(APP_ROLE, scenario_db_name("T-15"))
    try:
        for query, expected in (
            ("SELECT * FROM migration_internal.migration_lease", "42501"),
            ("UPDATE migration_internal.migration_lease SET generation = generation", "42501"),
            ("SELECT 1 FROM migration_internal.migration_lease FOR UPDATE", "42501"),
            ("SELECT * FROM migration_internal.lease_protocol_info_v1()", "42501"),
        ):
            matched, observed = expect_sqlstate(app_conn, query, None, expected)
            if not matched:
                raise ProtocolFailure(
                    f"T-15: expected 42501, got {observed} for {query}")
    finally:
        app_conn.close()
    return "PASS"


def grant_topology_b_application_privileges(scenario_admin, db_name):
    """Apply §8.3's four grants in the database that owns the tables.

    Application tables exist only in the scenario database, so a GRANT issued on
    the cluster's `postgres` database would target objects that do not exist
    there. The connected database is asserted before any grant is issued.
    """
    current = run_sql(scenario_admin, "SELECT current_database()", fetch_mode="one")
    connected = None if current is None else current[0]
    if connected != db_name:
        raise ProtocolFailure(
            "T-16: refusing to grant application privileges outside the scenario "
            f"database {db_name!r}; the connection is to {connected!r}")
    for statement in (
        f"GRANT SELECT ON public.positions TO {MIGRATOR_ROLE}",
        f"GRANT SELECT ON public.paper_orders TO {MIGRATOR_ROLE}",
        f"GRANT SELECT, INSERT ON public.strategy_leg_exposures TO {MIGRATOR_ROLE}",
        f"GRANT USAGE ON SEQUENCE public.strategy_leg_exposures_id_seq TO {MIGRATOR_ROLE}",
    ):
        scenario_admin.execute(statement)


def scenario_T16(conn):
    """T-16: Topology B — the migrator elevates to the owner only inside SET ROLE."""
    if run_sql(conn, "SELECT session_user::text", fetch_mode="one")[0] != MIGRATOR_ROLE:
        raise SetupFailure("T-16 must run on a migrator connection")

    db_name = scenario_db_name("T-16")
    # Administrator connection to the scenario database that owns the tables.
    # T-16 needs no cluster-level operation, so it opens no `postgres` connection.
    scenario_admin = scenario_admin_connect(db_name)
    try:
        # The non-login owner creates the application tables through an explicit
        # elevation window; the migrator never gains owner rights by inheritance.
        with conn.transaction():
            run_sql(conn, "SET ROLE " + OWNER_ROLE)
            if run_sql(conn, "SELECT current_user::text", fetch_mode="one")[0] != OWNER_ROLE:
                raise ProtocolFailure("T-16: SET ROLE owner did not take effect")
            run_sql(conn, APPLICATION_TABLES_SQL)
            run_sql(conn, "RESET ROLE")
        if run_sql(conn, "SELECT current_user::text", fetch_mode="one")[0] != MIGRATOR_ROLE:
            raise ProtocolFailure("T-16: RESET ROLE did not return to the migrator")

        # The migrator owns migration_internal and holds only the four grants.
        bootstrap_first_installation(conn)

        with conn.transaction():
            run_sql(conn, "REVOKE ALL ON SCHEMA migration_internal FROM " + OWNER_ROLE)
            run_sql(conn, "REVOKE ALL ON ALL TABLES IN SCHEMA migration_internal FROM "
                          + OWNER_ROLE)
            run_sql(conn, "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM "
                          + OWNER_ROLE)
            run_sql(conn, "REVOKE ALL ON ALL TABLES IN SCHEMA migration_internal FROM "
                          + APP_ROLE)
            run_sql(conn, "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM "
                          + APP_ROLE)

        # Four minimal grants on the owner's objects, issued in the scenario
        # database that actually contains them — never on `postgres`.
        grant_topology_b_application_privileges(scenario_admin, db_name)

        with conn.transaction():
            expected_matrix = {
                ("positions", "SELECT"): True,
                ("positions", "INSERT"): False,
                ("positions", "UPDATE"): False,
                ("positions", "DELETE"): False,
                ("paper_orders", "SELECT"): True,
                ("paper_orders", "INSERT"): False,
                ("paper_orders", "UPDATE"): False,
                ("paper_orders", "DELETE"): False,
                ("strategy_leg_exposures", "SELECT"): True,
                ("strategy_leg_exposures", "INSERT"): True,
                ("strategy_leg_exposures", "UPDATE"): False,
                ("strategy_leg_exposures", "DELETE"): False,
            }
            for (table, priv), want in expected_matrix.items():
                got = run_sql(conn, "SELECT has_table_privilege(%s, %s, %s)",
                              (MIGRATOR_ROLE, "public." + table, priv), fetch_mode="one")[0]
                if bool(got) != want:
                    raise ProtocolFailure(
                        f"T-16: {table} {priv} = {got}, expected {want}")

            if not run_sql(conn, "SELECT has_sequence_privilege(%s, "
                                 "pg_get_serial_sequence('public.strategy_leg_exposures','id'),"
                                 " 'USAGE')",
                           (MIGRATOR_ROLE,), fetch_mode="one")[0]:
                raise ProtocolFailure("T-16: migrator lacks sequence USAGE")
            if run_sql(conn, "SELECT has_sequence_privilege(%s, "
                             "pg_get_serial_sequence('public.strategy_leg_exposures','id'),"
                             " 'SELECT')",
                       (MIGRATOR_ROLE,), fetch_mode="one")[0]:
                raise ProtocolFailure("T-16: migrator unexpectedly holds sequence SELECT")

            inserted = run_sql(conn, "INSERT INTO public.strategy_leg_exposures "
                                     "(paper_order_id, exposure) VALUES (1, 1.5) RETURNING id",
                               fetch_mode="one")
            if inserted is None:
                raise ProtocolFailure("T-16: INSERT ... RETURNING id failed as the migrator")

            for query in ("UPDATE public.positions SET user_id = user_id",
                          "DELETE FROM public.positions",
                          "UPDATE public.strategy_leg_exposures SET exposure = exposure",
                          "DELETE FROM public.strategy_leg_exposures"):
                matched, observed = expect_sqlstate(conn, query, None, "42501")
                if not matched:
                    raise ProtocolFailure(
                        f"T-16: expected 42501, got {observed} for {query}")

            # The explicit owner window: DDL works, migration_internal does not.
            run_sql(conn, "SET ROLE " + OWNER_ROLE)
            if run_sql(conn, "SELECT current_user::text", fetch_mode="one")[0] != OWNER_ROLE:
                raise ProtocolFailure("T-16: owner elevation window not active")
            run_sql(conn, "ALTER TABLE public.positions ADD COLUMN IF NOT EXISTS note text")
            matched, observed = expect_sqlstate(
                conn, "SELECT * FROM migration_internal.lease_protocol_info_v1()", None, "42501")
            if not matched:
                raise ProtocolFailure(
                    f"T-16: owner unexpectedly called a migration function, got {observed}")
            run_sql(conn, "RESET ROLE")
            if run_sql(conn, "SELECT current_user::text", fetch_mode="one")[0] != MIGRATOR_ROLE:
                raise ProtocolFailure("T-16: RESET ROLE did not restore the migrator")
        return "PASS"
    finally:
        scenario_admin.close()


def scenario_T17(conn):
    """T-17: run `alembic upgrade head` directly against a bootstrapped DB."""
    return ("NOT RUN — no Alembic binary or environment exists in this harness; "
            "the real command cannot be executed")


def scenario_T18(conn):
    """T-18: run `python -m app.migrate upgrade head`."""
    return ("NOT RUN — no application migration runner exists in this harness; "
            "the real command cannot be executed")


def scenario_T19(conn):
    """T-19: injected backfill failure plus a failing cleanup release."""
    return ("NOT RUN — requires one session inside a user transaction plus a blocker "
            "session (lease row lock or mid-run EXECUTE revoke); no blocker exists here")


def scenario_T20(conn):
    """T-20: run the supported command on a database already at head."""
    return ("NOT RUN — the supported application command does not exist in this harness; "
            "lease state alone is not evidence for T-20")


def scenario_T21(conn):
    """T-21: operator close-barrier sequence while an owner holds the row lock."""
    return ("NOT RUN — requires the operator `bootstrap-lease --close-barrier` and "
            "--upgrade-protocol commands, which are not implemented here")


def scenario_T22(conn):
    """T-22: p_lock_timeout_ms = 0 and p_ttl_seconds = 0 are refused immediately."""
    bootstrap_first_installation(conn)
    cases = [
        ("SELECT * FROM migration_internal.lease_acquire_v1(1, %s, 500, 0)", (TOKEN_A,)),
        ("SELECT * FROM migration_internal.lease_acquire_v1(1, %s, 0, 1000)", (TOKEN_A,)),
        ("SELECT * FROM migration_internal.lease_enter_v1(1, %s, 500, 0)", (TOKEN_A,)),
        ("SELECT * FROM migration_internal.lease_enter_v1(1, %s, 0, 1000)", (TOKEN_A,)),
    ]
    fp_before = lease_row(conn)[1]
    for query, params in cases:
        with conn.transaction():
            matched, observed = expect_sqlstate(conn, query, params, "45004")
        if not matched:
            raise AssertionError(f"T-22: expected 45004, got {observed} for {query}")
    if lease_row(conn)[1] != fp_before:
        raise AssertionError("T-22: an invalid configuration call changed the lease row")
    return "PASS"


def scenario_T23(conn):
    """T-23: stale process across an upgrade."""
    return ("NOT RUN — the scenario requires the documented `bootstrap-lease "
            "--upgrade-protocol` flow (barrier close, quiescence check, version and range "
            "check, `migration_lease.protocol_version` mirror transition, reopen). That "
            "operator command is not implemented here, and a direct UPDATE of "
            "installed_version is not an upgrade, so this is not exercised")


def scenario_T24(conn):
    """T-24: cross-version concurrency between two protocol function sets."""
    return ("NOT RUN — requires installed *_v2 function names and two independent "
            "backends contending on the same advisory guard")


def scenario_T25(conn):
    """T-25: plain bootstrap verify refuses a protocol mismatch."""
    return ("NOT RUN — requires the `bootstrap-lease` verify command; it is not "
            "implemented here, and the lease functions alone are not that evidence")


def scenario_T26(conn):
    """T-26: explicit upgrade, refusal and cancellation."""
    return ("NOT RUN — requires the operator `bootstrap-lease --upgrade-protocol` "
            "command and its cancellation path")


def scenario_T27(conn):
    """T-27: enter and acquire refuse an unready protocol without touching the lease row."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        run_sql(conn, "UPDATE migration_internal.lease_protocol "
                      "SET bootstrap_state = 'installing' WHERE singleton")
        run_sql(conn, "CREATE TABLE IF NOT EXISTS public.positions "
                      "(id int PRIMARY KEY, user_id int)")
        run_sql(conn, "INSERT INTO public.positions (id, user_id) VALUES (1, 1) "
                      "ON CONFLICT DO NOTHING")
    before = lease_row(conn)

    for query, expected in (
        ("SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)", "45005"),
        ("SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)", "45005"),
    ):
        with conn.transaction():
            matched, observed = expect_sqlstate(
                conn, query, (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), expected)
        if not matched:
            raise AssertionError(f"T-27: expected {expected}, got {observed}")

    if lease_row(conn) != before:
        raise AssertionError("T-27: the unready protocol refusal wrote the lease row")
    expected_unowned = (None, None, None, 0)
    if before != expected_unowned:
        raise AssertionError(f"T-27: expected an unowned lease row, got {before!r}")
    if run_sql(conn, "SELECT count(*) FROM public.positions", fetch_mode="one")[0] != 1:
        raise AssertionError("T-27: application rows changed")
    return "PASS"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

SCENARIOS = {
    "T-01": scenario_T01,
    "T-02": scenario_T02,
    "T-03": scenario_T03,
    "T-04": scenario_T04,
    "T-05": scenario_T05,
    "T-06": scenario_T06,
    "T-07": scenario_T07,
    "T-08": scenario_T08,
    "T-09": scenario_T09,
    "T-10": scenario_T10,
    "T-11": scenario_T11,
    "T-12": scenario_T12,
    "T-13": scenario_T13,
    "T-14": scenario_T14,
    "T-15": scenario_T15,
    "T-16": scenario_T16,
    "T-17": scenario_T17,
    "T-18": scenario_T18,
    "T-19": scenario_T19,
    "T-20": scenario_T20,
    "T-21": scenario_T21,
    "T-22": scenario_T22,
    "T-23": scenario_T23,
    "T-24": scenario_T24,
    "T-25": scenario_T25,
    "T-26": scenario_T26,
    "T-27": scenario_T27,
}

CATEGORIES = ("PASS", "FAIL", "INVALID_SETUP", "NOT RUN", "INCONCLUSIVE")


def classify(value):
    for category in CATEGORIES:
        if value == category or value.startswith(category + " ") \
                or value.startswith(category + " —") or value.startswith(category + " -"):
            return category
    return None


def server_version(admin):
    """Return (version_text, version_num) from the server itself."""
    version_text = admin.execute("SHOW server_version").fetchone()[0]
    version_num = int(admin.execute("SHOW server_version_num").fetchone()[0])
    return version_text, version_num


def server_version_supported(version_num):
    """Accept exactly PostgreSQL 16, minor version 16 or later.

    A lower bound alone would silently accept PostgreSQL 17 and later, whose
    catalog rendering and behaviour this harness was not written or approved
    against; a different major version needs a separate design decision.
    """
    major, minor = divmod(int(version_num), 10000)
    if major != REQUIRED_SERVER_MAJOR:
        return False, (f"PostgreSQL major version {major} is not supported; this "
                       f"harness requires {REQUIRED_SERVER_MAJOR}."
                       f"{REQUIRED_SERVER_MINOR} or a later "
                       f"{REQUIRED_SERVER_MAJOR}.x minor release")
    if minor < REQUIRED_SERVER_MINOR:
        return False, (f"PostgreSQL {major}.{minor} is older than the required "
                       f"{REQUIRED_SERVER_MAJOR}.{REQUIRED_SERVER_MINOR}")
    return True, None


def not_run_all(reason):
    return {key: f"NOT RUN — {reason}" for key in SCENARIOS}


def run_one_scenario(admin, key, scenario):
    """Run one scenario, classify the outcome, and always tear the database down."""
    db_name = scenario_db_name(key)
    outcome = None
    cleanup_error = None
    try:
        try:
            prepare_scenario_database(admin, db_name)
        except Exception as exc:
            raise SetupFailure(
                f"could not prepare scenario database: {type(exc).__name__}: {exc}")
        try:
            conn = connect_migrator(db_name)
        except Exception as exc:
            raise SetupFailure(
                f"could not connect as {MIGRATOR_DB_USER}: {type(exc).__name__}: {exc}")
        try:
            outcome = scenario(conn)
        finally:
            conn.close()
    except ProtocolFailure as exc:
        outcome = f"FAIL — {exc}"
    except SetupFailure as exc:
        outcome = f"INVALID_SETUP — {exc}"
    except AssertionError as exc:
        outcome = f"FAIL — assertion failed: {exc}"
    except psycopg.Error as exc:
        state = getattr(exc, "sqlstate", None)
        outcome = f"FAIL — unexpected PostgreSQL SQLSTATE {state}: {exc}"
    except Exception as exc:
        outcome = f"FAIL — unexpected {type(exc).__name__}: {exc}"
    finally:
        try:
            drop_scenario_db(admin, db_name)
        except Exception as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
    if outcome is None:
        outcome = "INCONCLUSIVE — scenario produced no outcome"
    return outcome, cleanup_error


def run_harness():
    print("Issue #145 — T-01…T-27 Lease Protocol Harness")
    print("=" * 66)
    print(f"ADR source: {ADR_SOURCE_PATH} @ {ADR_SOURCE_COMMIT}")
    print("No runtime SQL verification is claimed unless PostgreSQL 16.16 is present.")
    print()

    try:
        require_destructive_optin()
    except SafetyRefusal as exc:
        reason = f"destructive safety gate refused: {exc}"
        print(f"Safety gate: REFUSED — {reason}")
        print("No connection was opened and no database or role was touched.")
        results = not_run_all(reason)
        _report(results)
        return results, SAFETY_REFUSAL_EXIT_CODE
    print(f"Safety gate: ACCEPTED (label {HARNESS_CLUSTER_LABEL!r}; cluster identity "
          "is established independently after the version probe)")

    try:
        admin = admin_connect()
    except Exception as exc:
        reason = f"PostgreSQL unreachable ({type(exc).__name__}: {exc})"
        print(f"PostgreSQL availability: UNAVAILABLE — {reason}")
        results = not_run_all(reason)
        _report(results)
        return results, 0

    try:
        try:
            version_text, version_num = server_version(admin)
        except Exception as exc:
            reason = f"server version unknown ({type(exc).__name__}: {exc})"
            print(f"PostgreSQL availability: UNAVAILABLE — {reason}")
            results = not_run_all(reason)
            _report(results)
            return results, 0

        print(f"PostgreSQL server_version: {version_text} "
              f"(server_version_num={version_num})")
        supported, unsupported_reason = server_version_supported(version_num)
        if not supported:
            reason = f"PostgreSQL {version_text} is unsupported: {unsupported_reason}"
            print(f"PostgreSQL availability: UNAVAILABLE — {reason}")
            results = not_run_all(reason)
            _report(results)
            return results, 0

        print(f"PostgreSQL availability: AVAILABLE ({REQUIRED_SERVER_MAJOR}."
              f"{REQUIRED_SERVER_MINOR}+ within major {REQUIRED_SERVER_MAJOR})")
        print()

        # The gate inspects the target before anything is created, dropped or
        # granted. A refusal here leaves the cluster exactly as it was found.
        try:
            validate_destructive_target(admin)
        except SafetyRefusal as exc:
            reason = f"destructive safety gate refused: {exc}"
            print(f"Safety gate: REFUSED — {reason}")
            print("No role or database was created, altered or dropped.")
            results = not_run_all(reason)
            _report(results)
            return results, SAFETY_REFUSAL_EXIT_CODE
        print("Target check: ACCEPTED (verified cluster identity, harness-owned "
              "databases, exact role attributes and membership options)")
        print()

        try:
            provision_cluster_roles(admin)
        except Exception as exc:
            reason = f"cluster role provisioning failed: {type(exc).__name__}: {exc}"
            print(f"Precondition: INVALID_SETUP — {reason}")
            results = {key: f"INVALID_SETUP — {reason}" for key in SCENARIOS}
            _report(results, {})
            return results, 0

        results = {}
        cleanup_failures = {}
        for key, scenario in sorted(SCENARIOS.items()):
            print(f"=== {key} ({scenario_db_name(key)}) ===")
            outcome, cleanup_error = run_one_scenario(admin, key, scenario)
            results[key] = outcome
            if cleanup_error:
                cleanup_failures[key] = cleanup_error
            print(f"  {outcome.splitlines()[0]}")
        _report(results, cleanup_failures)
        return results, 0
    finally:
        admin.close()


def _report(results, cleanup_failures=None):
    cleanup_failures = cleanup_failures or {}
    counts = {category: 0 for category in CATEGORIES}
    print()
    print("=" * 66)
    print("=== Summary ===")
    for key in sorted(results):
        category = classify(results[key])
        if category is None:
            category = "INCONCLUSIVE"
        counts[category] += 1
        print(f"{key}: {results[key]}")
    print("-" * 66)
    print(f"Total: {len(results)}")
    for category in CATEGORIES:
        print(f"{category}: {counts[category]}")
    print("-" * 66)
    if any(classify(value) is None for value in results.values()):
        print("WARNING: at least one scenario has no unambiguous category")
    if cleanup_failures:
        print("=== Cleanup failures (recorded separately) ===")
        for key in sorted(cleanup_failures):
            print(f"{key}: cleanup failed: {cleanup_failures[key]}")


# ---------------------------------------------------------------------------
# Built-in DB-free self-test
# ---------------------------------------------------------------------------
# `python3 run_t01_to_t27.py --self-test` exercises the harness's own logic
# against an injected fake driver. It opens no connection, reads no credential
# and contacts no server. This is the published source of the regression checks
# quoted in the evidence report.

class _FakeDriverError(psycopg.Error):
    def __init__(self, sqlstate):
        super().__init__(f"fake driver error ({sqlstate})")
        self.sqlstate = sqlstate


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeTransaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.transactions += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.rollbacks += 1
        return False


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = _FakeResult([])

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        self._result = self.conn.execute(query, params)

    def fetchone(self):
        return self._result.fetchone()

    def fetchall(self):
        return self._result.fetchall()


class _FakeConnection:
    """Minimal psycopg stand-in: records every statement, answers via handler."""

    def __init__(self, handler=None, dbname="unknown"):
        self.handler = handler or (lambda query, params: [])
        self.dbname = dbname
        self.autocommit = False
        self.closed = False
        self.statements = []
        self.transactions = 0
        self.rollbacks = 0

    def execute(self, query, params=None):
        if self.closed:
            raise _FakeDriverError("08003")
        self.statements.append((self.dbname, str(query), params))
        return _FakeResult(self.handler(str(query), params))

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True

    def transaction(self):
        return _FakeTransaction(self)


def _fake_admin(identity="12345", databases=(), roles=(), memberships=()):
    def handler(query, params):
        if "pg_control_system" in query:
            return [(identity,)]
        if "FROM pg_database" in query:
            return list(databases)
        if "FROM pg_roles" in query:
            return list(roles)
        if "FROM pg_auth_members" in query:
            return list(memberships)
        return []
    return _FakeConnection(handler, dbname="postgres")


def _temporary_globals(**values):
    saved = {name: globals()[name] for name in values}
    globals().update(values)

    def restore():
        globals().update(saved)

    return restore


def _self_test_checks():
    """Return [(name, callable)]; each callable raises AssertionError on failure."""
    checks = []

    def check(name):
        def register(fn):
            checks.append((name, fn))
            return fn
        return register

    @check("every connection opener sets autocommit and the expected database")
    def _autocommit():
        opened = []
        original = _connect_impl

        def fake_connect(**kwargs):
            conn = _FakeConnection(dbname=kwargs.get("dbname", "unknown"))
            conn.connect_kwargs = dict(kwargs)
            opened.append(conn)
            return conn

        globals()["_connect_impl"] = fake_connect
        try:
            cluster = admin_connect()
            scenario = scenario_admin_connect("issue145_t16")
            migrator = connect_migrator("issue145_t01")
            bare = harness_connect(dbname="anything")
        finally:
            globals()["_connect_impl"] = original
        assert len(opened) == 4, f"expected four connections, got {len(opened)}"
        assert all(conn.autocommit is True for conn in opened), \
            "a connection opener left autocommit off"
        assert cluster.dbname == "postgres", f"admin connected to {cluster.dbname!r}"
        assert scenario.dbname == "issue145_t16"
        assert migrator.dbname == "issue145_t01"
        assert bare.dbname == "anything"

    @check("T-16 grants run on the scenario database, never on postgres")
    def _t16_target():
        scenario = _FakeConnection(lambda q, p: [("issue145_t16",)],
                                   dbname="issue145_t16")
        grant_topology_b_application_privileges(scenario, "issue145_t16")
        grants = [q for _, q, _ in scenario.statements
                  if q.upper().startswith("GRANT")]
        assert len(grants) == 4, f"expected four GRANT statements, got {grants}"
        assert all(db == "issue145_t16" for db, _, _ in scenario.statements)

        wrong = _FakeConnection(lambda q, p: [("postgres",)], dbname="postgres")
        try:
            grant_topology_b_application_privileges(wrong, "issue145_t16")
        except ProtocolFailure:
            pass
        else:
            raise AssertionError("a GRANT was accepted while connected to postgres")
        assert not [q for _, q, _ in wrong.statements
                    if q.upper().startswith("GRANT")], \
            "a GRANT reached postgres"

        source = inspect.getsource(scenario_T16)
        assert "grant_topology_b_application_privileges(scenario_admin, db_name)" in source
        assert "GRANT " not in source, "T-16 still issues a GRANT inline"
        assert "scenario_admin_connect(db_name)" in source

    @check("cluster identity comes from pg_control, not from the static label")
    def _identity():
        restore = _temporary_globals(HARNESS_TARGET_SYSTEM_IDENTIFIER="")
        try:
            try:
                verify_cluster_identity(_fake_admin())
            except SafetyRefusal:
                pass
            else:
                raise AssertionError("a missing target identity was accepted")
        finally:
            restore()

        restore = _temporary_globals(HARNESS_TARGET_SYSTEM_IDENTIFIER="999")
        try:
            try:
                verify_cluster_identity(_fake_admin(identity="123"))
            except SafetyRefusal:
                pass
            else:
                raise AssertionError("a mismatched system_identifier was accepted")
            assert verify_cluster_identity(_fake_admin(identity="999")) == "999"
        finally:
            restore()

    @check("an existing scenario database without the marker is refused")
    def _marker_gate():
        restore = _temporary_globals(HARNESS_TARGET_SYSTEM_IDENTIFIER="123")
        try:
            marker = expected_database_marker()
            unmarked = _fake_admin(identity="123",
                                   databases=[("issue145_t01", None)])
            try:
                validate_destructive_target(unmarked)
            except SafetyRefusal:
                pass
            else:
                raise AssertionError("an unmarked issue145_t01 database was adopted")
            mutating = ("DROP", "CREATE", "GRANT", "COMMENT", "ALTER", "REVOKE")
            assert not [q for _, q, _ in unmarked.statements
                        if q.upper().startswith(mutating)], \
                "the gate mutated the cluster before refusing"

            marked = _fake_admin(identity="123",
                                 databases=[("issue145_t01", marker)])
            validate_destructive_target(marked)
        finally:
            restore()

    @check("drop_scenario_db refuses a database it cannot prove it created")
    def _drop_guard():
        restore = _temporary_globals(HARNESS_TARGET_SYSTEM_IDENTIFIER="123")
        try:
            def handler(query, params):
                if "FROM pg_database" in query:
                    return [("issue145_t01", None)]
                return []

            admin = _FakeConnection(handler, dbname="postgres")
            try:
                drop_scenario_db(admin, "issue145_t01")
            except SafetyRefusal:
                pass
            else:
                raise AssertionError("an unmarked database was dropped")
            assert not [q for _, q, _ in admin.statements
                        if "DROP DATABASE" in q.upper()]
            assert not [q for _, q, _ in admin.statements
                        if "pg_terminate_backend" in q]
        finally:
            restore()

    @check("role login attributes and exact membership options are validated")
    def _roles():
        restore = _temporary_globals(HARNESS_TARGET_SYSTEM_IDENTIFIER="123")
        try:
            good_roles = [(MIGRATOR_ROLE, False, False, False, False, True),
                          (OWNER_ROLE, False, False, False, False, False),
                          (APP_ROLE, False, False, False, False, True)]
            good_membership = (OWNER_ROLE, MIGRATOR_ROLE, False, True, False)
            validate_destructive_target(_fake_admin(
                identity="123", roles=good_roles, memberships=[good_membership]))

            for bad_membership in ((OWNER_ROLE, MIGRATOR_ROLE, True, True, False),
                                   (OWNER_ROLE, MIGRATOR_ROLE, False, False, False),
                                   (OWNER_ROLE, MIGRATOR_ROLE, False, True, True)):
                try:
                    validate_destructive_target(_fake_admin(
                        identity="123", roles=good_roles,
                        memberships=[bad_membership]))
                except SafetyRefusal:
                    pass
                else:
                    raise AssertionError(
                        f"membership options {bad_membership[2:]} were accepted")

            nologin_migrator = [(MIGRATOR_ROLE, False, False, False, False, False)]
            try:
                validate_destructive_target(_fake_admin(
                    identity="123", roles=nologin_migrator))
            except SafetyRefusal:
                pass
            else:
                raise AssertionError("a NOLOGIN migrator was adopted")

            superuser_owner = [(OWNER_ROLE, True, False, False, False, False)]
            try:
                validate_destructive_target(_fake_admin(
                    identity="123", roles=superuser_owner))
            except SafetyRefusal:
                pass
            else:
                raise AssertionError("a SUPERUSER owner role was adopted")
        finally:
            restore()

    @check("constraint definitions are compared in full, including weakened ones")
    def _constraints():
        def rows(overrides=None):
            overrides = overrides or {}
            return [("migration_internal." + table, name, kind,
                     overrides.get(name, definition))
                    for (table, name), (kind, definition) in EXPECTED_CONSTRAINTS.items()]

        assert_constraint_definitions(rows())

        weakened_shape = (
            "check ((((owner_token_hash is null) and (owner_fingerprint is null)) "
            "or ((owner_token_hash is not null) and (owner_fingerprint is not null)) "
            "or (true)))")
        try:
            assert_constraint_definitions(rows({
                "migration_lease_owner_shape_ck": weakened_shape}))
        except ProtocolFailure:
            pass
        else:
            raise AssertionError("an added OR true branch was accepted")

        widened_barrier = (
            "check ((barrier_state = any (array['open'::text, 'closed'::text, "
            "'degraded'::text])))")
        try:
            assert_constraint_definitions(rows({
                "lease_protocol_barrier_state_check": widened_barrier}))
        except ProtocolFailure:
            pass
        else:
            raise AssertionError("an extra IN alternative was accepted")

        # OR weakened to AND: every expected fragment is still present.
        changed_operator = (
            "check (((owner_token_hash is null) and (expires_at is not null)))")
        try:
            assert_constraint_definitions(rows({
                "migration_lease_owned_expiry_ck": changed_operator}))
        except ProtocolFailure:
            pass
        else:
            raise AssertionError("a changed expression was accepted")

    @check("only PostgreSQL 16 with minor >= 16 is accepted")
    def _version():
        for number, expected in ((150000, False), (160015, False), (160016, True),
                                 (160020, True), (170000, False), (180000, False)):
            accepted, reason = server_version_supported(number)
            assert accepted is expected, \
                f"server_version_num {number} -> {accepted}, expected {expected}"
            if not accepted:
                assert reason, f"no refusal reason for {number}"

    @check("bootstrap verifies §9.3 before it activates the protocol row")
    def _bootstrap_order():
        source = inspect.getsource(bootstrap_first_installation)
        verify_at = source.index("verify_installation(")
        activate_at = source.index("PROTOCOL_ACTIVATION_SQL")
        assert verify_at < activate_at, "verification does not precede activation"
        assert "expect_active=False" in source[verify_at:activate_at], \
            "the pre-activation pass does not expect the inactive state"
        assert "expect_active=False" in inspect.getsource(verify_installation)

    @check("T-11/T-12 put enter first and assert termination SQLSTATEs")
    def _transactions():
        t11 = inspect.getsource(scenario_T11)
        t12 = inspect.getsource(scenario_T12)
        assert t11.index("lease_enter_v1") < t11.index("INSERT INTO public.positions")
        assert t12.index("lease_enter_v1") < t12.index("INSERT INTO public.positions")
        assert 'observed not in ("57P01", "08006")' in t12
        assert "pg_terminate_backend" in t12
        assert "FOR UPDATE NOWAIT" in t11 and "FOR UPDATE NOWAIT" in t12

    @check("expect_sqlstate uses a real transaction and the driver SQLSTATE")
    def _sqlstate():
        plain = _FakeConnection(lambda q, p: [])
        matched, observed = expect_sqlstate(plain, "SELECT broken", None, "55P03")
        assert not matched and observed is None
        assert plain.transactions == 1 and plain.rollbacks == 0

        def failing(query, params):
            raise _FakeDriverError("55P03")

        erroring = _FakeConnection(failing)
        matched, observed = expect_sqlstate(erroring, "SELECT 1", None, "55P03")
        assert matched is True and observed == "55P03"
        assert erroring.transactions == 1 and erroring.rollbacks == 1

    @check("T-23 stays NOT RUN and never presents a direct UPDATE as an upgrade")
    def _t23():
        outcome = scenario_T23(None)
        assert classify(outcome) == "NOT RUN", f"T-23 classified as {outcome!r}"
        source = inspect.getsource(scenario_T23)
        assert "SET installed_version" not in source
        assert "lease_protocol SET" not in source

    return checks


def run_self_test():
    print("Issue #145 harness — DB-free self-test (no server is contacted)")
    print("=" * 66)
    failures = 0
    checks = _self_test_checks()
    for name, fn in checks:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name} — {exc}")
        except Exception as exc:  # noqa: BLE001 - a broken test is a failure too
            failures += 1
            print(f"FAIL  {name} — unexpected {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")
    print("-" * 66)
    print(f"Total: {len(checks)}, PASS: {len(checks) - failures}, FAIL: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(run_self_test())
    _, exit_code = run_harness()
    sys.exit(exit_code)
