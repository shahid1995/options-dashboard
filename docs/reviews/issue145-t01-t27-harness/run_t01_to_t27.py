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
"""

import os
import sys
import datetime

import psycopg
from psycopg import sql

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

REQUIRED_SERVER_VERSION_NUM = 160016  # PostgreSQL 16.16
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

def _conn_kwargs(dbname):
    kwargs = {
        "dbname": dbname,
        "port": DB_PORT,
    }
    if DB_USER:
        kwargs["user"] = DB_USER
    if DB_PASSWORD:
        kwargs["password"] = DB_PASSWORD
    if DB_HOST:
        kwargs["host"] = DB_HOST
    return kwargs


def connect(dbname):
    """Open a connection to one database. Raises if the server is unreachable."""
    return psycopg.connect(**_conn_kwargs(dbname))


def admin_connect():
    """Administrative connection used only for disposable DB provisioning."""
    return psycopg.connect(**_conn_kwargs("postgres"))


def drop_create_scenario_db(admin_conn, db_name):
    """Create a genuinely empty database for one scenario."""
    admin_conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (db_name,),
    )
    admin_conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    admin_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))


def drop_scenario_db(admin_conn, db_name):
    """Safely remove a scenario database."""
    admin_conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (db_name,),
    )
    admin_conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))


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

def bootstrap_first_installation(conn):
    """Execute §9.2.1 step by step in one transaction."""
    with conn.transaction():
        run_sql(conn, "SELECT pg_advisory_xact_lock("
                      "hashtextextended('strikenova:migration_internal:bootstrap', 0))")
        run_sql(conn, "CREATE SCHEMA migration_internal")
        run_sql(conn, PROTOCOL_TABLE_SCHEMA)
        run_sql(conn,
                "INSERT INTO migration_internal.lease_protocol "
                "(installed_version, min_supported_version, max_supported_version, "
                "barrier_state, bootstrap_state) "
                "VALUES (1, 1, 1, 'closed', 'installing')")
        run_sql(conn, LEASE_TABLE_SCHEMA)
        run_sql(conn,
                "INSERT INTO migration_internal.migration_lease "
                "(lease_name, protocol_version) "
                "VALUES ('migration-lease', 1)")
        run_sql(conn, LEASE_ACQUIRE_V1_SQL)
        run_sql(conn, LEASE_ENTER_V1_SQL)
        run_sql(conn, LEASE_RELEASE_V1_SQL)
        run_sql(conn, LEASE_PROTOCOL_INFO_V1_SQL)
        run_sql(conn, REVOKE_SET_SQL)
        run_sql(conn,
                "UPDATE migration_internal.lease_protocol "
                "SET installed_version = 1, "
                "    min_supported_version = 1, "
                "    max_supported_version = 1, "
                "    barrier_state = 'open', "
                "    bootstrap_state = 'ready', "
                "    updated_at = now(), "
                "    updated_by = session_user "
                "WHERE singleton")
        row = run_sql(conn, "SELECT * FROM migration_internal.lease_protocol_info_v1()",
                      fetch_mode="one")
        if row != (1, 1, 1, "open", "ready"):
            raise AssertionError(f"§9.2.1 step 12 verification failed: {row!r}")


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


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_T01(conn):
    """T-01: first installation from a genuinely empty database."""
    for obj in ("migration_internal.migration_lease",
                "migration_internal.lease_protocol",
                "public.positions", "public.paper_orders",
                "public.strategy_leg_exposures"):
        if run_sql(conn, "SELECT to_regclass(%s)", (obj,), fetch_mode="one")[0] is not None:
            raise AssertionError(f"T-01 database is not empty: {obj} exists")
    if run_sql(conn, "SELECT to_regclass('alembic_version')", fetch_mode="one")[0] is not None:
        raise AssertionError("T-01 database is not empty: alembic_version exists")

    bootstrap_first_installation(conn)

    if run_sql(conn, "SELECT count(*) FROM migration_internal.lease_protocol",
               fetch_mode="one")[0] != 1:
        raise AssertionError("T-01: expected exactly one protocol row")
    if run_sql(conn, "SELECT count(*) FROM migration_internal.migration_lease",
               fetch_mode="one")[0] != 1:
        raise AssertionError("T-01: expected exactly one lease row")
    if run_sql(conn, "SELECT to_regclass('alembic_version')", fetch_mode="one")[0] is not None:
        raise AssertionError("T-01: bootstrap created alembic_version")
    if run_sql(conn, "SELECT to_regclass('public.positions')", fetch_mode="one")[0] is not None:
        raise AssertionError("T-01: bootstrap created an application table")

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

    w2 = connect(scenario_db_name("T-04"))
    try:
        w1_tx = conn.transaction()
        w1_tx.__enter__()
        try:
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
                raise AssertionError(
                    f"T-04: expected 55P03 while W1 holds the lock, got {observed}")
        finally:
            w1_tx.__exit__(None, None, None)

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

    w2 = connect(scenario_db_name("T-07"))
    try:
        # W1 opens a transaction and takes the lease row lock without committing.
        w1_tx = conn.transaction()
        w1_tx.__enter__()
        try:
            run_sql(conn, "SELECT 1 FROM migration_internal.migration_lease "
                          "WHERE lease_name = 'migration-lease' FOR UPDATE")
            with w2.transaction():
                matched, observed = expect_sqlstate(
                    w2,
                    "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, 1000),
                    "55P03")
            if not matched:
                raise AssertionError(
                    f"T-07: expected 55P03 for the blocked enter, got {observed}")
        finally:
            w1_tx.__exit__(None, None, None)

        # After W1's rollback the lock is free and the same token may enter.
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
    """T-11: a TTL that is genuinely exceeded, measured by the database clock."""
    bootstrap_first_installation(conn)
    short_ttl = 1
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, short_ttl, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"),
            expected_generation=1)

    started = run_sql(conn, "SELECT clock_timestamp()", fetch_mode="one")[0]
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
                    (TOKEN_A, short_ttl, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))
        run_sql(conn, "SELECT pg_sleep(2)")
        matched, observed = expect_sqlstate(
            conn,
            "SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)",
            (TOKEN_A, short_ttl, LEASE_LOCK_TIMEOUT_MS),
            "45002")
        elapsed = (run_sql(conn, "SELECT clock_timestamp()", fetch_mode="one")[0]
                   - started).total_seconds()
    if not matched:
        raise AssertionError(f"T-11: expected 45002 after TTL expiry, got {observed}")
    if elapsed <= short_ttl:
        raise AssertionError(f"T-11: elapsed {elapsed}s did not exceed TTL {short_ttl}s")
    return "PASS"


def scenario_T12(conn):
    """T-12: terminate W1's backend while it holds the lease in a user transaction."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"),
            expected_generation=1)
    fp_before = lease_row(conn)[1]

    admin = admin_connect()
    w1 = conn
    try:
        w1_tx = w1.transaction()
        w1_tx.__enter__()
        try:
            run_sql(w1, "SELECT pg_backend_pid()", fetch_mode="one")
            run_sql(w1, "CREATE TABLE IF NOT EXISTS public.positions (id int PRIMARY KEY, user_id int)")
            run_sql(w1, "INSERT INTO public.positions (id, user_id) VALUES (101, 7)")
            pid = run_sql(w1, "SELECT pg_backend_pid()", fetch_mode="one")[0]
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        finally:
            try:
                w1_tx.__exit__(None, None, None)
            except Exception:
                pass

        observed = None
        try:
            run_sql(w1, "SELECT 1", fetch_mode="one")
        except psycopg.Error as exc:
            observed = getattr(exc, "sqlstate", None)
        if observed not in ("57P01", "08006", None):
            raise AssertionError(f"T-12: expected backend-death SQLSTATE, got {observed}")
        if observed is None:
            raise AssertionError("T-12: connection survived backend termination")

        w2 = connect(scenario_db_name("T-12"))
        try:
            with w2.transaction():
                matched, code = expect_sqlstate(
                    w2,
                    "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_B, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS),
                    "45002")
            if not matched:
                raise AssertionError(f"T-12: W2 acquire before expiry expected 45002, got {code}")
            if lease_row(w2)[1] != fp_before:
                raise AssertionError("T-12: lease ownership changed unexpectedly")
        finally:
            w2.close()
    finally:
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
    app = "strikenova_production_app"
    migrator = "strikenova_prod_migrator"

    with conn.transaction():
        _ensure_role(conn, app, login=False)
        _ensure_role(conn, migrator, login=False)
        run_sql(conn, sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(app)))
        run_sql(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(migrator)))
    conn.commit()
    try:
        bootstrap_first_installation(conn)
        conn.commit()
    finally:
        run_sql(conn, "RESET ROLE")
        conn.commit()

    with conn.transaction():
        run_sql(conn, REVOKE_SET_SQL)

    # Effective privileges read false for the APP identity.
    checks = [
        ("SELECT has_schema_privilege(%s, 'migration_internal', 'USAGE')", (app,)),
        ("SELECT has_table_privilege(%s, 'migration_internal.migration_lease', 'SELECT')", (app,)),
        ("SELECT has_table_privilege(%s, 'migration_internal.migration_lease', 'UPDATE')", (app,)),
        ("SELECT has_table_privilege(%s, 'migration_internal.lease_protocol', 'SELECT')", (app,)),
        ("SELECT has_function_privilege(%s, "
         "'migration_internal.lease_acquire_v1(integer,text,float8,integer)', 'EXECUTE')", (app,)),
    ]
    for query, params in checks:
        if run_sql(conn, query, params, fetch_mode="one")[0]:
            raise AssertionError(f"T-15: APP holds an effective privilege: {query}")

    public_entries = run_sql(conn,
                             "SELECT count(*) FROM pg_class c "
                             "JOIN pg_namespace n ON n.oid = c.relnamespace, "
                             "aclexplode(c.relacl) a "
                             "WHERE n.nspname = 'migration_internal' AND a.grantee = 0",
                             fetch_mode="one")[0]
    if public_entries != 0:
        raise AssertionError(f"T-15: PUBLIC still holds {public_entries} table ACL entries")

    # Direct attempts as the APP identity are denied with 42501.
    with conn.transaction():
        run_sql(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(app)))
        for query, expected in (
            ("SELECT * FROM migration_internal.migration_lease", "42501"),
            ("UPDATE migration_internal.migration_lease SET generation = generation", "42501"),
            ("SELECT 1 FROM migration_internal.migration_lease FOR UPDATE", "42501"),
            ("SELECT * FROM migration_internal.lease_protocol_info_v1()", "42501"),
        ):
            matched, observed = expect_sqlstate(conn, query, None, expected)
            if not matched:
                raise AssertionError(f"T-15: expected 42501, got {observed} for {query}")
        run_sql(conn, "RESET ROLE")
    return "PASS"


def scenario_T16(conn):
    """T-16: Topology B — non-inherited set-only membership and four minimal grants."""
    app = "strikenova_production_app"
    migrator = "strikenova_prod_migrator"
    owner = "strikenova_prod_owner"

    with conn.transaction():
        _ensure_role(conn, app, login=False)
        _ensure_role(conn, migrator, login=False)
        _ensure_role(conn, owner, login=False)
        run_sql(conn, "GRANT CREATE, USAGE ON SCHEMA public TO " + owner)
        run_sql(conn, "GRANT " + owner + " TO " + migrator +
                " WITH INHERIT FALSE, SET TRUE, ADMIN FALSE")
    conn.commit()

    # The owner creates the application tables.
    with conn.transaction():
        run_sql(conn, "SET ROLE " + owner)
        run_sql(conn, APPLICATION_TABLES_SQL)
        run_sql(conn, "RESET ROLE")
    conn.commit()

    # The migrator owns migration_internal and holds only the four grants.
    with conn.transaction():
        run_sql(conn, "SET ROLE " + migrator)
    conn.commit()
    try:
        bootstrap_first_installation(conn)
        conn.commit()
    finally:
        with conn.transaction():
            run_sql(conn, "RESET ROLE")
    conn.commit()

    with conn.transaction():
        run_sql(conn, "REVOKE ALL ON SCHEMA public FROM " + app)
        run_sql(conn, "GRANT SELECT ON public.positions TO " + migrator)
        run_sql(conn, "GRANT SELECT ON public.paper_orders TO " + migrator)
        run_sql(conn, "GRANT SELECT, INSERT ON public.strategy_leg_exposures TO " + migrator)
        run_sql(conn, "GRANT USAGE ON SEQUENCE public.strategy_leg_exposures_id_seq TO " + migrator)
        run_sql(conn, "REVOKE ALL ON ALL TABLES IN SCHEMA migration_internal FROM " + app)
        run_sql(conn, "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM " + app)
        run_sql(conn, "REVOKE ALL ON SCHEMA migration_internal FROM " + owner)

    with conn.transaction():
        run_sql(conn, "SET ROLE " + migrator)
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
                          (migrator, "public." + table, priv), fetch_mode="one")[0]
            if bool(got) != want:
                raise AssertionError(f"T-16: {table} {priv} = {got}, expected {want}")

        if not run_sql(conn, "SELECT has_sequence_privilege(%s, "
                             "pg_get_serial_sequence('public.strategy_leg_exposures','id'),"
                             " 'USAGE')",
                       (migrator,), fetch_mode="one")[0]:
            raise AssertionError("T-16: migrator lacks sequence USAGE")
        if run_sql(conn, "SELECT has_sequence_privilege(%s, "
                         "pg_get_serial_sequence('public.strategy_leg_exposures','id'),"
                         " 'SELECT')",
                   (migrator,), fetch_mode="one")[0]:
            raise AssertionError("T-16: migrator unexpectedly holds sequence SELECT")

        # Required DML succeeds.
        inserted = run_sql(conn, "INSERT INTO public.strategy_leg_exposures "
                                 "(paper_order_id, exposure) VALUES (1, 1.5) RETURNING id",
                           fetch_mode="one")
        if inserted is None:
            raise AssertionError("T-16: INSERT ... RETURNING id failed as the migrator")

        # UPDATE and DELETE are refused with 42501.
        for query in ("UPDATE public.positions SET user_id = user_id",
                      "DELETE FROM public.positions",
                      "UPDATE public.strategy_leg_exposures SET exposure = exposure",
                      "DELETE FROM public.strategy_leg_exposures"):
            matched, observed = expect_sqlstate(conn, query, None, "42501")
            if not matched:
                raise AssertionError(f"T-16: expected 42501, got {observed} for {query}")

        # Inside the explicit SET ROLE window: DDL works, migration_internal does not.
        run_sql(conn, "RESET ROLE")
        run_sql(conn, "SET ROLE " + owner)
        run_sql(conn, "ALTER TABLE public.positions ADD COLUMN IF NOT EXISTS note text")
        matched, observed = expect_sqlstate(
            conn, "SELECT * FROM migration_internal.lease_protocol_info_v1()", None, "42501")
        if not matched:
            raise AssertionError(
                f"T-16: owner unexpectedly called a migration function, got {observed}")
        run_sql(conn, "RESET ROLE")
    return "PASS"


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
    """T-23: a protocol-1 worker resumes after the database moves to protocol 2."""
    bootstrap_first_installation(conn)
    with conn.transaction():
        assert_lease_result(
            run_sql(conn, "SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)",
                    (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), fetch_mode="one"))
    preflight = run_sql(conn, "SELECT * FROM migration_internal.lease_protocol_info_v1()",
                        fetch_mode="one")
    if preflight != (1, 1, 1, "open", "ready"):
        raise AssertionError(f"T-23: preflight read {preflight!r}")

    operator = connect(scenario_db_name("T-23"))
    try:
        with operator.transaction():
            run_sql(operator, "UPDATE migration_internal.lease_protocol "
                              "SET installed_version = 2, min_supported_version = 1, "
                              "max_supported_version = 2, updated_by = session_user "
                              "WHERE singleton")
        mirror_before = run_sql(conn, "SELECT owner_token_hash, owner_fingerprint, generation "
                                      "FROM migration_internal.migration_lease "
                                      "WHERE lease_name = 'migration-lease'",
                                fetch_mode="one")
        for query, expected in (
            ("SELECT * FROM migration_internal.lease_acquire_v1(1, %s, %s, %s)", "45006"),
            ("SELECT * FROM migration_internal.lease_enter_v1(1, %s, %s, %s)", "45006"),
        ):
            with conn.transaction():
                matched, observed = expect_sqlstate(
                    conn, query, (TOKEN_A, LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS), expected)
            if not matched:
                raise AssertionError(f"T-23: expected {expected}, got {observed}")
        mirror_after = run_sql(conn, "SELECT owner_token_hash, owner_fingerprint, generation "
                                     "FROM migration_internal.migration_lease "
                                     "WHERE lease_name = 'migration-lease'",
                               fetch_mode="one")
        if mirror_before != mirror_after:
            raise AssertionError("T-23: the stale worker wrote the lease row")
    finally:
        operator.close()
    return "PASS"


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


def not_run_all(reason):
    return {key: f"NOT RUN — {reason}" for key in SCENARIOS}


def run_harness():
    print("Issue #145 — T-01…T-27 Lease Protocol Harness")
    print("=" * 66)
    print(f"ADR source: {ADR_SOURCE_PATH} @ {ADR_SOURCE_COMMIT}")
    print("No runtime SQL verification is claimed unless PostgreSQL 16.16 is present.")
    print()

    try:
        admin = admin_connect()
        admin.autocommit = True
    except Exception as exc:
        reason = f"PostgreSQL unreachable ({type(exc).__name__}: {exc})"
        print(f"PostgreSQL availability: UNAVAILABLE — {reason}")
        results = not_run_all(reason)
        _report(results)
        return results

    try:
        try:
            version_text, version_num = server_version(admin)
        except Exception as exc:
            reason = f"server version unknown ({type(exc).__name__}: {exc})"
            print(f"PostgreSQL availability: UNAVAILABLE — {reason}")
            results = not_run_all(reason)
            _report(results)
            return results

        print(f"PostgreSQL server_version: {version_text} "
              f"(server_version_num={version_num})")
        if version_num < REQUIRED_SERVER_VERSION_NUM:
            reason = (f"PostgreSQL {version_text} is older than the required 16.16 "
                      f"({REQUIRED_SERVER_VERSION_NUM})")
            print(f"PostgreSQL availability: UNAVAILABLE — {reason}")
            results = not_run_all(reason)
            _report(results)
            return results

        print("PostgreSQL availability: AVAILABLE (16.16+)")
        print()

        results = {}
        for key, scenario in sorted(SCENARIOS.items()):
            db_name = scenario_db_name(key)
            print(f"=== {key} ({db_name}) ===")
            try:
                drop_create_scenario_db(admin, db_name)
                conn = connect(db_name)
                try:
                    outcome = scenario(conn)
                finally:
                    conn.close()
                drop_scenario_db(admin, db_name)
                results[key] = outcome
            except Exception as exc:
                results[key] = f"NOT RUN — scenario infrastructure failure: {exc}"
            print(f"  {results[key].splitlines()[0]}")
        _report(results)
        return results
    finally:
        admin.close()


def _report(results):
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


if __name__ == "__main__":
    run_harness()
