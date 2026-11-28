"""ADR-017 PostgreSQL migration lease (transactional ownership).

This module replaces the retired renewal-thread lease (``_migration_lock``).
Ownership is proven by the PostgreSQL row lock on
``migration_internal.migration_lease`` plus a 256-bit owner token whose
SHA-256 hash is the only owner credential stored in the database.

Design contract (``docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md``, commit
``ae2226e``):

* ``migration_internal.lease_protocol`` is the single authoritative control
  row (installed version, supported range, barrier, bootstrap state).
* ``migration_internal.migration_lease`` is the single shared lease row.
* ``lease_acquire_v1`` / ``lease_enter_v1`` / ``lease_release_v1`` are
  ``SECURITY DEFINER`` functions owned by the migration identity, with
  ``search_path = pg_catalog`` and strict ACLs.
* Every protected transaction starts with ``enter`` (which takes the
  cross-version advisory guard ``M`` and then the lease row lock) and ends
  with a second ``enter`` immediately before ``COMMIT``.
* Expiry uses the database clock. There is no renewal thread, no handoff
  mode and no GUC/session-variable ownership.

Nothing in this module ever logs, formats or transmits the raw owner token;
only the 12-hex ``owner_fingerprint`` may appear in logs and exceptions.
"""

from __future__ import annotations

import logging
import math
import random
import secrets
import time
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)

SCHEMA = "migration_internal"
LEASE_TABLE = "migration_internal.migration_lease"
PROTOCOL_TABLE = "migration_internal.lease_protocol"
LEASE_NAME = "migration-lease"

#: Cross-version guard (ADR §6.10). Fixed by the protocol: every version of
#: every lease function takes this exact key.
GUARD_SQL = "hashtextextended('strikenova:migration_internal:migration_guard', 0)"
#: Bootstrap guard (ADR §6.10). Bootstrap modes only.
BOOTSTRAP_GUARD_SQL = "hashtextextended('strikenova:migration_internal:bootstrap', 0)"

#: The protocol version this binary implements (ADR §11.2).
EXPECTED_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_MIN = 1
SUPPORTED_PROTOCOL_MAX = 1
#: Versions from which this binary may perform ``--upgrade-protocol``.
AUTHORIZED_UPGRADE_SOURCES: tuple[int, ...] = ()

#: SQLSTATEs (ADR §6.7).
SQLSTATE_OWNER_MISMATCH = "45001"
SQLSTATE_INVALID_STATE = "45002"
SQLSTATE_CONFIG = "45004"
SQLSTATE_MISSING = "45005"
SQLSTATE_PROTOCOL = "45006"
SQLSTATE_LOCK_NOT_AVAILABLE = "55P03"
SQLSTATE_SERIALIZATION = "40001"
SQLSTATE_DEADLOCK = "40P01"

RETRYABLE_SQLSTATES = frozenset(
    {SQLSTATE_INVALID_STATE, SQLSTATE_LOCK_NOT_AVAILABLE, SQLSTATE_SERIALIZATION, SQLSTATE_DEADLOCK}
)

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_LOCK_TIMEOUT_MS = 10_000
DEFAULT_WAIT_SECONDS = 180.0


class LeaseError(RuntimeError):
    """A lease protocol failure carrying the PostgreSQL SQLSTATE.

    Messages never contain the raw owner token or its full hash.
    """

    def __init__(self, sqlstate: str | None, message: str, *, fingerprint: str | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.fingerprint = fingerprint


def sqlstate_of(exc: BaseException) -> str | None:
    """Return the PostgreSQL SQLSTATE for a SQLAlchemy/psycopg exception."""
    orig = getattr(exc, "orig", None)
    for candidate in (getattr(orig, "sqlstate", None), getattr(getattr(orig, "diag", None), "sqlstate", None)):
        if candidate:
            return str(candidate)
    if isinstance(orig, BaseException) and orig.args:
        # psycopg2 exposes ``pgcode``.
        code = getattr(orig, "pgcode", None)
        if code:
            return str(code)
    return None


def token_hash(token: str) -> str:
    """Lowercase hex SHA-256 of the raw owner token (ADR §6.2)."""
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fingerprint(token: str) -> str:
    """First 12 hex characters of the token hash — the only loggable value."""
    return token_hash(token)[:12]


def generate_token(nonce_prefix: str = "") -> str:
    """Return a 256-bit owner token (``secrets.token_hex(32)``).

    An optional process nonce prefix is accepted so a rehearsal run can
    correlate a single run's tokens without ever publishing them; the token
    still carries at least 128 bits of fresh entropy beyond the prefix.
    """
    return f"{nonce_prefix}{secrets.token_hex(32)}"


def _retry_delay(attempt: int) -> float:
    """Backoff starting near 200 ms, capped at 2 s, with jitter (ADR §6.7).

    The exponent is capped before exponentiation: ``2 ** (attempt - 1)`` is
    unbounded and a long contention run (thousands of attempts) would spend
    time and memory computing a gigantic integer only to discard it. Capping
    at ``2 ** 4`` keeps the 0.2 s initial delay and the 2 s ceiling exactly.
    """
    exponent = min(max(0, attempt - 1), 4)
    base = min(2.0, 0.2 * (2**exponent))
    return min(2.0, base * (0.5 + random.random()))


# ---------------------------------------------------------------------------
# Protocol objects (ADR §6.1, §6.3–§6.6, §10.2)
# ---------------------------------------------------------------------------

CREATE_SCHEMA_SQL = f"CREATE SCHEMA {SCHEMA}"

CREATE_TABLES_SQL = (
    f"""
CREATE TABLE {LEASE_TABLE} (
    lease_name        text        PRIMARY KEY,
    protocol_version  integer     NOT NULL,
    owner_token_hash  text,
    owner_fingerprint text,
    owner_role        name,
    acquired_at       timestamptz,
    expires_at        timestamptz,
    released_at       timestamptz,
    generation        bigint      NOT NULL DEFAULT 0,
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
);
""",
    f"""
CREATE TABLE {PROTOCOL_TABLE} (
    singleton             boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    installed_version     integer NOT NULL,
    min_supported_version integer NOT NULL,
    max_supported_version integer NOT NULL,
    barrier_state         text NOT NULL CHECK (barrier_state IN ('open', 'closed')),
    bootstrap_state       text NOT NULL CHECK (bootstrap_state IN ('ready', 'installing')),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            name NOT NULL DEFAULT session_user,
    note                  text
);
""",
)

ACQUIRE_SQL = f"""
CREATE FUNCTION {SCHEMA}.lease_acquire_v1(
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
    IF p_token IS NULL OR length(p_token) < 32
       OR p_ttl_seconds IS NULL OR NOT (p_ttl_seconds > 0) OR p_ttl_seconds > 86400
       OR p_lock_timeout_ms IS NULL OR p_lock_timeout_ms < 1 OR p_lock_timeout_ms > 600000 THEN
        RAISE EXCEPTION 'lease configuration invalid' USING ERRCODE = '45004';
    END IF;

    v_hash := encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
    v_fp   := left(v_hash, 12);

    v_prev := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', p_lock_timeout_ms::text, true);

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
           pr.barrier_state, pr.bootstrap_state
      INTO v_inst, v_min, v_max, v_bar, v_boot
      FROM migration_internal.lease_protocol AS pr
     WHERE pr.singleton;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'protocol row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

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

    IF v_row.owner_token_hash IS NOT NULL
       AND (v_row.expires_at IS NULL OR v_row.expires_at > v_now) THEN
        RAISE EXCEPTION 'lease held by % until %',
            coalesce(v_row.owner_fingerprint, 'unknown'),
            coalesce(v_row.expires_at::text, 'unknown')
            USING ERRCODE = '45002';
    END IF;

    v_exp := v_now + make_interval(secs => p_ttl_seconds);

    UPDATE migration_internal.migration_lease AS l
       SET protocol_version  = p_protocol,
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
$$;
"""

ENTER_SQL = f"""
CREATE FUNCTION {SCHEMA}.lease_enter_v1(
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
$$;
"""

RELEASE_SQL = f"""
CREATE FUNCTION {SCHEMA}.lease_release_v1(p_protocol integer, p_token text)
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

    IF v_inst <> p_protocol OR p_protocol < v_min OR p_protocol > v_max THEN
        RAISE EXCEPTION 'protocol % not admissible: installed %, supported %..%',
            p_protocol, v_inst, v_min, v_max USING ERRCODE = '45006';
    END IF;

    IF v_row.protocol_version <> v_inst THEN
        RAISE EXCEPTION 'lease row protocol % disagrees with installed %',
            v_row.protocol_version, v_inst USING ERRCODE = '45006';
    END IF;

    IF v_row.owner_token_hash IS NULL THEN
        RETURN false;
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
$$;
"""

INFO_SQL = f"""
CREATE FUNCTION {SCHEMA}.lease_protocol_info_v1()
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
$$;
"""

FUNCTION_SQL: tuple[str, ...] = (ACQUIRE_SQL, ENTER_SQL, RELEASE_SQL, INFO_SQL)

REVOKE_SQL: tuple[str, ...] = (
    f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC",
    f"REVOKE ALL ON ALL TABLES    IN SCHEMA {SCHEMA} FROM PUBLIC",
    f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {SCHEMA} FROM PUBLIC",
    f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {SCHEMA} FROM PUBLIC",
    f"REVOKE ALL ON SCHEMA {SCHEMA} FROM {{runtime_role}}",
    f"REVOKE ALL ON ALL TABLES    IN SCHEMA {SCHEMA} FROM {{runtime_role}}",
    f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {SCHEMA} FROM {{runtime_role}}",
    f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {SCHEMA} FROM {{runtime_role}}",
)


# ---------------------------------------------------------------------------
# Catalog verification (ADR §9.3, §9.4, §10.3)
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "migration_lease": {
        "lease_name": ("text", "NO"),
        "protocol_version": ("integer", "NO"),
        "owner_token_hash": ("text", "YES"),
        "owner_fingerprint": ("text", "YES"),
        "owner_role": ("name", "YES"),
        "acquired_at": ("timestamp with time zone", "YES"),
        "expires_at": ("timestamp with time zone", "YES"),
        "released_at": ("timestamp with time zone", "YES"),
        "generation": ("bigint", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
    },
    "lease_protocol": {
        "singleton": ("boolean", "NO"),
        "installed_version": ("integer", "NO"),
        "min_supported_version": ("integer", "NO"),
        "max_supported_version": ("integer", "NO"),
        "barrier_state": ("text", "NO"),
        "bootstrap_state": ("text", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
        "updated_by": ("name", "NO"),
        "note": ("text", "YES"),
    },
}

EXPECTED_CONSTRAINTS: dict[str, set[str]] = {
    "migration_lease": {
        "migration_lease_pkey",
        "migration_lease_singleton_ck",
        "migration_lease_generation_ck",
        "migration_lease_owner_shape_ck",
        "migration_lease_owned_expiry_ck",
    },
    "lease_protocol": {
        "lease_protocol_pkey",
        "lease_protocol_singleton_check",
        "lease_protocol_barrier_state_check",
        "lease_protocol_bootstrap_state_check",
    },
}

EXPECTED_FUNCTIONS: dict[str, tuple[tuple[str, ...], str]] = {
    # identity-argument types (format_type) -> result type
    "lease_acquire_v1": (("integer", "text", "double precision", "integer"), "record"),
    "lease_enter_v1": (("integer", "text", "double precision", "integer"), "record"),
    "lease_release_v1": (("integer", "text"), "boolean"),
    "lease_protocol_info_v1": ((), "record"),
}


def _problems_raise(problems: Iterable[str], context: str) -> None:
    items = list(problems)
    if items:
        raise LeaseError(
            SQLSTATE_CONFIG,
            f"{context}: " + "; ".join(items),
        )


def _exec_sql(conn: Connection, sql: str):
    """Execute literal SQL without DBAPI placeholder processing.

    psycopg's pyformat paramstyle treats ``%`` as a placeholder introducer, and
    the lease functions contain PL/pgSQL ``RAISE ... %`` placeholders and
    ``%ROWTYPE``. Running the statement on the driver cursor with no parameters
    keeps the SQL byte-exact, which is what the ADR specifies.
    """
    cur = conn.connection.cursor()
    try:
        cur.execute(sql)
    finally:
        cur.close()


def _scalar(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def _role_oid(conn: Connection, role: str) -> int | None:
    return conn.execute(
        text("SELECT oid FROM pg_roles WHERE rolname = :role"), {"role": role}
    ).scalar()


def verify_installation(
    conn: Connection,
    *,
    migrator_role: str,
    runtime_role: str | None,
    expected_version: int,
) -> None:
    """Verify the installed protocol objects fail-closed (ADR §9.3–§9.5).

    This function creates, repairs and drops nothing. Any mismatch raises
    ``LeaseError`` with SQLSTATE 45004/45005 and the caller's transaction
    rolls back.
    """
    problems: list[str] = []

    migrator_oid = _role_oid(conn, migrator_role)
    if migrator_oid is None:
        raise LeaseError(
            SQLSTATE_CONFIG, f"migration identity {migrator_role!r} does not exist"
        )

    schema = conn.execute(
        text(
            "SELECT n.nspowner::regrole::text AS owner, "
            "(SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid "
            "  AND c.relkind = 'r') AS tables "
            "FROM pg_namespace n WHERE n.nspname = :schema"
        ),
        {"schema": SCHEMA},
    ).first()
    if schema is None:
        raise LeaseError(SQLSTATE_MISSING, f"schema {SCHEMA} is missing; bootstrap required")
    if schema.owner != migrator_role:
        problems.append(f"schema owner is {schema.owner}, expected {migrator_role}")
    if int(schema.tables) != 2:
        problems.append(f"schema has {schema.tables} tables, expected 2")

    for table in EXPECTED_COLUMNS:
        row = conn.execute(
            text(
                "SELECT c.relkind::text AS relkind, c.relowner::regrole::text AS owner, "
                "c.relrowsecurity AS rls, c.relacl::text AS acl "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table"
            ),
            {"schema": SCHEMA, "table": table},
        ).first()
        if row is None:
            problems.append(f"{SCHEMA}.{table} is missing")
            continue
        if row.relkind != "r":
            problems.append(f"{SCHEMA}.{table} relkind is {row.relkind}, expected r")
        if row.owner != migrator_role:
            problems.append(f"{SCHEMA}.{table} owner is {row.owner}, expected {migrator_role}")
        if row.rls:
            problems.append(f"{SCHEMA}.{table} has row security enabled")

        columns = {
            r.column_name: (r.data_type, r.is_nullable)
            for r in conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table"
                ),
                {"schema": SCHEMA, "table": table},
            )
        }
        expected_columns = EXPECTED_COLUMNS[table]
        if set(columns) != set(expected_columns):
            problems.append(
                f"{table} columns {sorted(columns)} != {sorted(expected_columns)}"
            )
        else:
            for name, signature in expected_columns.items():
                if columns[name] != signature:
                    problems.append(
                        f"{table}.{name} is {columns[name]}, expected {signature}"
                    )

        constraints = {
            r.conname
            for r in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE connamespace = cast(:schema AS regnamespace)"
                ),
                {"schema": SCHEMA},
            )
            if r.conname in EXPECTED_CONSTRAINTS[table]
        }
        missing = EXPECTED_CONSTRAINTS[table] - constraints
        if missing:
            problems.append(f"{table} missing constraints {sorted(missing)}")

    # Functions: identity arguments from the catalog types, never text.
    functions = {
        r.proname: r
        for r in conn.execute(
            text(
                "SELECT p.proname, "
                " (SELECT array_agg(format_type(t, NULL)) FROM "
                "    unnest(p.proargtypes::oid[]) WITH ORDINALITY AS u(t, ord)) AS argtypes, "
                " pg_get_function_result(p.oid) AS result, "
                " p.proowner::regrole::text AS owner, p.prosecdef, p.proconfig, "
                " p.proacl::text AS acl "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema"
            ),
            {"schema": SCHEMA},
        )
    }
    for name, (expected_args, expected_result) in EXPECTED_FUNCTIONS.items():
        row = functions.get(name)
        if row is None:
            problems.append(f"function {name} is missing")
            continue
        actual_args = tuple(row.argtypes or ())
        if actual_args != expected_args:
            problems.append(f"{name} args {actual_args} != {expected_args}")
        if row.owner != migrator_role:
            problems.append(f"{name} owner is {row.owner}, expected {migrator_role}")
        if not row.prosecdef:
            problems.append(f"{name} is not SECURITY DEFINER")
        if list(row.proconfig or []) != ["search_path=pg_catalog"]:
            problems.append(f"{name} proconfig is {row.proconfig}, expected search_path=pg_catalog")
        if expected_result == "boolean" and row.result != "boolean":
            problems.append(f"{name} result is {row.result}, expected boolean")
        if expected_result == "record" and "TABLE" not in (row.result or ""):
            problems.append(f"{name} result is {row.result}, expected TABLE")

    unknown = set(functions) - set(EXPECTED_FUNCTIONS)
    if unknown:
        problems.append(f"unexpected functions in {SCHEMA}: {sorted(unknown)}")

    problems.extend(_acl_problems(conn, runtime_role))
    problems.extend(_schema_acl_problems(conn, runtime_role))
    problems.extend(_default_acl_problems(conn, migrator_role, runtime_role))
    problems.extend(_row_problems(conn, expected_version))

    _problems_raise(problems, "migration lease installation verification failed")


def _acl_problems(conn: Connection, runtime_role: str | None) -> list[str]:
    """No PUBLIC (grantee 0) and no runtime-role grants on lease objects."""
    problems: list[str] = []
    runtime_oid = _role_oid(conn, runtime_role) if runtime_role else None

    for row in conn.execute(
        text(
            "SELECT c.relname, a.grantee, a.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a "
            "WHERE n.nspname = :schema AND c.relkind IN ('r', 'S') "
            "  AND (a.grantee = 0 OR a.grantee = :runtime_oid)"
        ),
        {"schema": SCHEMA, "runtime_oid": runtime_oid or -1},
    ):
        problems.append(
            f"{SCHEMA}.{row.relname} grants {row.privilege_type} to grantee {row.grantee}"
        )

    for row in conn.execute(
        text(
            "SELECT p.proname, a.grantee, a.privilege_type "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE n.nspname = :schema AND (a.grantee = 0 OR a.grantee = :runtime_oid)"
        ),
        {"schema": SCHEMA, "runtime_oid": runtime_oid or -1},
    ):
        problems.append(
            f"{SCHEMA}.{row.proname} grants {row.privilege_type} to grantee {row.grantee}"
        )

    if runtime_oid is not None:
        for row in conn.execute(
            text(
                "SELECT c.relname, p.priv, "
                "has_table_privilege(:runtime, c.oid, p.priv) AS effective "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) AS p(priv) "
                "WHERE n.nspname = :schema AND c.relkind = 'r'"
            ),
            {"runtime": runtime_role, "schema": SCHEMA},
        ):
            if row.effective:
                problems.append(
                    f"runtime role has {row.priv} on {SCHEMA}.{row.relname}"
                )
        for row in conn.execute(
            text(
                "SELECT p.proname, has_function_privilege(:runtime, p.oid, 'EXECUTE') AS effective "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema"
            ),
            {"runtime": runtime_role, "schema": SCHEMA},
        ):
            if row.effective:
                problems.append(f"runtime role has EXECUTE on {row.proname}")
    return problems


def _schema_acl_problems(conn: Connection, runtime_role: str | None) -> list[str]:
    """Verify ``pg_namespace.nspacl`` on the migration schema (ADR §9.3).

    The schema is the outermost boundary: a PUBLIC ``USAGE`` grant would make
    the lease objects reachable, and a ``CREATE`` grant would let an
    unprivileged identity plant objects inside it. Both the stored ACL (with
    ``acldefault('n', nspowner)`` for an untouched schema) and the effective
    privilege path — inheritance, membership and superuser status included —
    are checked, so a grant to a role the runtime identity inherits through is
    caught as well.
    """
    problems: list[str] = []
    schema = conn.execute(
        text(
            "SELECT n.nspowner::regrole::text AS owner, n.nspacl IS NULL AS implicit_acl "
            "FROM pg_namespace n WHERE n.nspname = :schema"
        ),
        {"schema": SCHEMA},
    ).first()
    if schema is None:
        return [f"schema {SCHEMA} is missing"]

    runtime_oid = _role_oid(conn, runtime_role) if runtime_role else None
    for row in conn.execute(
        text(
            "SELECT a.grantee, a.privilege_type "
            "FROM pg_namespace n "
            "CROSS JOIN LATERAL aclexplode("
            "  coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
            "WHERE n.nspname = :schema AND (a.grantee = 0 OR a.grantee = :runtime_oid)"
        ),
        {"schema": SCHEMA, "runtime_oid": runtime_oid or -1},
    ):
        grantee = "PUBLIC" if int(row.grantee) == 0 else str(runtime_role)
        problems.append(
            f"schema {SCHEMA} grants {row.privilege_type} to {grantee}"
        )

    if runtime_role:
        for privilege in ("USAGE", "CREATE"):
            effective = conn.execute(
                text("SELECT has_schema_privilege(:runtime, :schema, :priv)"),
                {"runtime": runtime_role, "schema": SCHEMA, "priv": privilege},
            ).scalar()
            if effective:
                problems.append(
                    f"runtime role {runtime_role} effectively has {privilege} on {SCHEMA}"
                )

        for row in conn.execute(
            text(
                "SELECT rolname, rolsuper FROM pg_roles "
                "WHERE rolname = :runtime  OR oid = "
                "  (SELECT nspowner FROM pg_namespace WHERE nspname = :schema)"
            ),
            {"runtime": runtime_role, "schema": SCHEMA},
        ):
            if row.rolname == runtime_role and row.rolsuper:
                problems.append(f"runtime role {runtime_role} is a superuser")

        owner_reach = conn.execute(
            text(
                "SELECT n.nspowner::regrole::text FROM pg_namespace n "
                "WHERE n.nspname = :schema "
                "  AND pg_has_role(:runtime, n.nspowner, 'MEMBER')"
            ),
            {"runtime": runtime_role, "schema": SCHEMA},
        ).first()
        if owner_reach is not None:
            problems.append(
                f"runtime role {runtime_role} is a member of {SCHEMA} owner "
                f"{owner_reach[0]}"
            )
    return problems


def _default_acl_problems(
    conn: Connection, migrator_role: str, runtime_role: str | None
) -> list[str]:
    """Inspect global AND schema-scoped default ACLs (ADR §9.4)."""
    problems: list[str] = []
    runtime_oid = _role_oid(conn, runtime_role) if runtime_role else None
    rows = conn.execute(
        text(
            "SELECT CASE WHEN d.defaclnamespace = 0 THEN '<global>' "
            "            ELSE d.defaclnamespace::regnamespace::text END AS scope, "
            "       d.defaclobjtype, a.grantee, a.privilege_type "
            "FROM pg_default_acl d "
            "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole = cast(:migrator AS regrole) "
            "  AND (d.defaclnamespace = 0 "
            "       OR d.defaclnamespace = cast(:schema AS regnamespace)) "
            "  AND (a.grantee = 0 OR a.grantee = :runtime_oid)"
        ),
        {"migrator": migrator_role, "schema": SCHEMA, "runtime_oid": runtime_oid or -1},
    )
    for row in rows:
        problems.append(
            f"default ACL ({row.scope}, {row.defaclobjtype}) grants "
            f"{row.privilege_type} to grantee {row.grantee}"
        )
    return problems


#: ADR §8.3 application objects and the ordinary privileges the *migration*
#: identity may hold on them. Everything else in ``_APP_TABLE_PRIVILEGES`` is a
#: violation; ownership (Topology A) or the owner role (Topology B) is checked
#: separately.
_APP_TABLES: tuple[str, ...] = (
    "positions",
    "paper_orders",
    "strategy_leg_exposures",
)
_APP_TABLE_PRIVILEGES: tuple[str, ...] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    # Not part of the §8.3 matrix, but an examined privilege nonetheless:
    # REFERENCES allows a foreign key onto the table and TRIGGER allows a
    # trigger to be attached, both of which are unintended write paths.
    "REFERENCES",
    "TRIGGER",
)
_APP_MIGRATOR_MATRIX: dict[str, frozenset[str]] = {
    "positions": frozenset({"SELECT"}),
    "paper_orders": frozenset({"SELECT"}),
    "strategy_leg_exposures": frozenset({"SELECT", "INSERT"}),
}
_APP_RUNTIME_MATRIX: frozenset[str] = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE"}
)
#: ADR §8.3 gives the runtime identity ``USAGE`` on sequences and nothing
#: more: ``USAGE`` authorises ``nextval``/``currval``, ``SELECT`` would expose
#: ``last_value`` and ``UPDATE`` would authorise ``setval``.
_APP_RUNTIME_SEQUENCE_MATRIX: frozenset[str] = frozenset({"USAGE"})
_APP_SEQUENCE_PRIVILEGES: tuple[str, ...] = ("USAGE", "SELECT", "UPDATE")
_APP_SEQUENCE_TABLE = "strategy_leg_exposures"
_APP_SCHEMA = "public"


def _application_ownership(
    conn: Connection, expected_owner: str
) -> tuple[dict[str, str], list[str]]:
    owners: dict[str, str] = {}
    problems: list[str] = []
    rows = conn.execute(
        text(
            "SELECT c.relname, c.relowner::regrole::text AS owner "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relkind = 'r' "
            "  AND c.relname = ANY(:tables)"
        ),
        {"schema": _APP_SCHEMA, "tables": list(_APP_TABLES)},
    )
    for row in rows:
        owners[row.relname] = row.owner
    for table in _APP_TABLES:
        owner = owners.get(table)
        if owner is None:
            problems.append(f"{_APP_SCHEMA}.{table} is missing")
        elif owner != expected_owner:
            problems.append(
                f"{_APP_SCHEMA}.{table} is owned by {owner}, expected {expected_owner}"
            )
    return owners, problems


def _privilege_matrix(
    conn: Connection, role: str, table: str
) -> dict[str, bool]:
    """Effective table privileges of ``role``, one privilege at a time.

    ``has_table_privilege(role, oid, 'SELECT,INSERT')`` answers "any of", which
    is not the question the ADR §8.3 matrix asks, so each privilege is read
    separately.
    """
    relation = conn.execute(
        text(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table"
        ),
        {"schema": _APP_SCHEMA, "table": table},
    ).scalar()
    if relation is None:
        return {}
    return {
        privilege: bool(
            conn.execute(
                text("SELECT has_table_privilege(:role, :oid, :priv)"),
                {"role": role, "oid": relation, "priv": privilege},
            ).scalar()
        )
        for privilege in _APP_TABLE_PRIVILEGES
    }


def _sequence_problems(conn: Connection, role: str, *, label: str) -> list[str]:
    """Enforce the ADR §8.3 sequence matrix: ``USAGE`` and nothing else.

    Both identities are held to the same matrix. ``USAGE`` is what an
    ``INSERT ... RETURNING id`` needs; ``SELECT`` would additionally expose
    ``last_value`` and ``UPDATE`` would authorise ``setval``, so either one is
    sequence control that neither identity is granted.
    """
    problems: list[str] = []
    sequence = conn.execute(
        text("SELECT pg_get_serial_sequence(:qualified, 'id')"),
        {"qualified": f"{_APP_SCHEMA}.{_APP_SEQUENCE_TABLE}"},
    ).scalar()
    if not sequence:
        return [f"{_APP_SCHEMA}.{_APP_SEQUENCE_TABLE} has no id sequence"]
    effective = {
        privilege: bool(
            conn.execute(
                text("SELECT has_sequence_privilege(:role, :seq, :priv)"),
                {"role": role, "seq": sequence, "priv": privilege},
            ).scalar()
        )
        for privilege in _APP_SEQUENCE_PRIVILEGES
    }
    if not effective["USAGE"]:
        problems.append(
            f"{label} role {role} has no USAGE on the exposure id sequence"
        )
    for forbidden in sorted(
        set(_APP_SEQUENCE_PRIVILEGES) - _APP_RUNTIME_SEQUENCE_MATRIX
    ):
        if effective[forbidden]:
            problems.append(
                f"{label} role {role} has {forbidden} on the exposure id "
                "sequence; ADR §8.3 allows USAGE only"
            )
    return problems


#: Privileges PostgreSQL can grant at column granularity. TRUNCATE and TRIGGER
#: are relation-level only, so they are excluded here and checked by
#: ``_privilege_matrix`` instead.
_COLUMN_GRANTABLE_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "REFERENCES"})


def _column_privilege_problems(
    conn: Connection,
    role: str,
    table: str,
    *,
    forbidden: frozenset[str],
    label: str,
) -> list[str]:
    """Detect column-level grants that bypass a table-level restriction.

    ``has_table_privilege`` answers false when only ``GRANT UPDATE (col)`` was
    issued, so a column-level grant is invisible to the table matrix while it
    still authorises the operation the ADR §8.3 matrix forbids. Each forbidden
    column-grantable privilege is read with ``has_column_privilege``; it is
    reported only when the table-level privilege is absent, which is precisely
    the column-level bypass rather than a duplicate of an existing failure.
    """
    wanted = sorted(set(forbidden) & _COLUMN_GRANTABLE_PRIVILEGES)
    if not wanted:
        return []
    relation = conn.execute(
        text(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table"
        ),
        {"schema": _APP_SCHEMA, "table": table},
    ).scalar()
    if relation is None:
        return []
    problems: list[str] = []
    rows = conn.execute(
        text(
            "SELECT a.attname, p.priv "
            "FROM pg_attribute a "
            "CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) AS p(priv) "
            "WHERE a.attrelid = :oid AND a.attnum > 0 AND NOT a.attisdropped "
            "  AND has_column_privilege(:role, a.attrelid, a.attnum, p.priv) "
            "  AND NOT has_table_privilege(:role, a.attrelid, p.priv) "
            "ORDER BY a.attnum, p.priv"
        ),
        {"oid": relation, "role": role},
    )
    for row in rows:
        if row.priv not in wanted:
            continue
        problems.append(
            f"{label} role {role} has column-level {row.priv} on "
            f"{_APP_SCHEMA}.{table}.{row.attname}; ADR §8.3 forbids it"
        )
    return problems


def _runtime_migration_internal_problems(
    conn: Connection, runtime_role: str
) -> list[str]:
    problems: list[str] = []
    for privilege in ("USAGE", "CREATE"):
        effective = conn.execute(
            text("SELECT has_schema_privilege(:role, :schema, :priv)"),
            {"role": runtime_role, "schema": SCHEMA, "priv": privilege},
        ).scalar()
        if effective:
            problems.append(
                f"runtime role {runtime_role} effectively has {privilege} on {SCHEMA}"
            )
    for row in conn.execute(
        text(
            "SELECT c.relname, p.priv "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE')) AS p(priv) "
            "WHERE n.nspname = :schema AND c.relkind = 'r' "
            "  AND has_table_privilege(:role, c.oid, p.priv)"
        ),
        {"schema": SCHEMA, "role": runtime_role},
    ):
        problems.append(
            f"runtime role {runtime_role} has {row.priv} on {SCHEMA}.{row.relname}"
        )
    for row in conn.execute(
        text(
            "SELECT p.proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = :schema AND has_function_privilege(:role, p.oid, 'EXECUTE')"
        ),
        {"schema": SCHEMA, "role": runtime_role},
    ):
        problems.append(
            f"runtime role {runtime_role} has EXECUTE on {SCHEMA}.{row.proname}"
        )
    return problems


def _application_default_acl_problems(
    conn: Connection, creator: str, runtime_role: str | None
) -> list[str]:
    """Require schema-scoped defaults on the actual creator for the runtime role."""
    problems: list[str] = []
    runtime_oid = _role_oid(conn, runtime_role) if runtime_role else None
    table_privileges: set[str] = set()
    sequence_privileges: set[str] = set()
    rows = conn.execute(
        text(
            "SELECT CASE WHEN d.defaclnamespace = 0 THEN '<global>' "
            "            ELSE d.defaclnamespace::regnamespace::text END AS scope, "
            "       d.defaclobjtype, a.grantee, a.privilege_type "
            "FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole = cast(:creator AS regrole)"
        ),
        {"creator": creator},
    )
    for row in rows:
        scope = row.scope
        if scope == "<global>":
            problems.append(
                f"default ACL for {creator} is global (`<global>`, {row.defaclobjtype}); "
                "ADR §9.4 requires IN SCHEMA public so migration_internal never inherits it"
            )
            continue
        if int(row.grantee) == 0:
            problems.append(
                f"default ACL for {creator} in {scope} grants {row.privilege_type} to PUBLIC"
            )
            continue
        if runtime_oid is None or int(row.grantee) != runtime_oid:
            continue
        if scope != _APP_SCHEMA:
            problems.append(
                f"default ACL for {creator} in {scope} grants runtime "
                f"{row.privilege_type}; only {_APP_SCHEMA} may carry runtime defaults"
            )
            continue
        if row.defaclobjtype == "r":
            table_privileges.add(row.privilege_type)
        elif row.defaclobjtype == "S":
            sequence_privileges.add(row.privilege_type)
        else:
            problems.append(
                f"default ACL for {creator} in {scope} grants runtime "
                f"{row.privilege_type} on object type {row.defaclobjtype!r}; "
                "ADR §8.3 defines default privileges for tables and sequences only"
            )
    # Both directions fail closed: a missing default leaves the runtime unable
    # to use the next application table, and an excess default (TRUNCATE,
    # REFERENCES or TRIGGER on tables; SELECT or UPDATE on sequences) grants a
    # future object a privilege the accepted matrix never approved.
    missing_tables = _APP_RUNTIME_MATRIX - table_privileges
    if missing_tables:
        problems.append(
            f"default ACL for {creator} in {_APP_SCHEMA} is missing runtime "
            f"table privileges {sorted(missing_tables)}"
        )
    excess_tables = table_privileges - _APP_RUNTIME_MATRIX
    if excess_tables:
        problems.append(
            f"default ACL for {creator} in {_APP_SCHEMA} grants the runtime "
            f"excess table privileges {sorted(excess_tables)}; ADR §8.3 allows "
            "SELECT, INSERT, UPDATE, DELETE only"
        )
    missing_sequences = _APP_RUNTIME_SEQUENCE_MATRIX - sequence_privileges
    if missing_sequences:
        problems.append(
            f"default ACL for {creator} in {_APP_SCHEMA} is missing runtime "
            f"sequence privileges {sorted(missing_sequences)}"
        )
    excess_sequences = sequence_privileges - _APP_RUNTIME_SEQUENCE_MATRIX
    if excess_sequences:
        problems.append(
            f"default ACL for {creator} in {_APP_SCHEMA} grants the runtime "
            f"excess sequence privileges {sorted(excess_sequences)}; ADR §8.3 "
            "allows sequence USAGE only"
        )
    return problems


def _role_attribute_problems(row, label: str) -> list[str]:
    """Role attributes no identity in the §8.3 topology may carry.

    CREATEROLE/CREATEDB/BYPASSRLS/REPLICATION are read here because each of
    them is an escape from the matrix the grants express: CREATEROLE can grant
    itself the migrator, CREATEDB can host a second schema, BYPASSRLS defeats
    row security and REPLICATION can read the WAL.
    """
    problems: list[str] = []
    if row.rolsuper:
        problems.append(f"{label} is a superuser")
    for attribute, name in (
        ("rolcreaterole", "CREATEROLE"),
        ("rolcreatedb", "CREATEDB"),
        ("rolbypassrls", "BYPASSRLS"),
        ("rolreplication", "REPLICATION"),
    ):
        if getattr(row, attribute):
            problems.append(f"{label} has {name}")
    return problems


def _membership_grants(conn: Connection, role: str) -> list[str]:
    """Roles ``role`` is a member of, directly (transitivity included by PG)."""
    return [
        row.rolname
        for row in conn.execute(
            text(
                "SELECT r.rolname FROM pg_auth_members m "
                "JOIN pg_roles r ON r.oid = m.roleid "
                "WHERE m.member = cast(:role AS regrole) ORDER BY r.rolname"
            ),
            {"role": role},
        )
    ]


def _incoming_members(conn: Connection, role: str) -> list[str]:
    """Roles allowed to inherit from or ``SET ROLE`` to ``role``.

    The outgoing matrix alone is not the whole membership model: another login
    granted membership in the migration identity (Topology A) or in the owner
    role (Topology B) can assume those privileges without any change to the
    migrator, the runtime role or the grants this verifier inspects.
    """
    return [
        row.rolname
        for row in conn.execute(
            text(
                "SELECT r.rolname FROM pg_auth_members m "
                "JOIN pg_roles r ON r.oid = m.member "
                "WHERE m.roleid = cast(:role AS regrole) ORDER BY r.rolname"
            ),
            {"role": role},
        )
    ]


def verify_application_privileges(
    conn: Connection,
    *,
    migrator_role: str,
    runtime_role: str | None,
    owner_role: str | None = None,
) -> None:
    """Enforce the ADR §8.3 application privilege topology (fail closed).

    Read-only: this function never grants, revokes, repairs or creates. It runs
    inside the protected DDL transaction after the topology step and before
    the pre-commit ``enter``, and again before the first startup backfill
    write, so a wrong matrix can never reach the backfill.

    Checks, in both topologies: role attributes (no SUPERUSER, CREATEROLE,
    CREATEDB, BYPASSRLS or REPLICATION on any identity; the migrator and the
    runtime are LOGIN roles and the owner is NOLOGIN), the incoming membership
    model (no other role may inherit or assume the migrator; in Topology B the
    migrator is the owner's only approved member), object ownership, the full
    effective migrator and runtime table matrix (including REFERENCES and
    TRIGGER) and the sequence matrix, Topology B's single non-inherited
    set-only membership (or Topology A's absence of any membership), zero
    membership paths from the runtime or the owner to any other role, effective
    privileges on ``migration_internal`` for both the runtime and the owner, the
    runtime identity's inability to create objects in ``public``, and
    schema-scoped default privileges bound to the actual object creator.
    """
    problems: list[str] = []
    owner_role = (owner_role or "").strip() or None

    roles = {
        row.rolname: row
        for row in conn.execute(
            text(
                "SELECT rolname, rolsuper, rolcanlogin, rolcreaterole, rolcreatedb, "
                "       rolbypassrls, rolreplication "
                "FROM pg_roles WHERE rolname = ANY(:roles)"
            ),
            {"roles": [migrator_role] + ([runtime_role] if runtime_role else []) + ([owner_role] if owner_role else [])},
        )
    }
    migrator = roles.get(migrator_role)
    if migrator is None:
        problems.append(f"migration identity {migrator_role!r} does not exist")
    else:
        problems.extend(
            _role_attribute_problems(migrator, f"migration identity {migrator_role}")
        )
        if not migrator.rolcanlogin:
            problems.append(
                f"migration identity {migrator_role} is not a LOGIN role; "
                "ADR §8.1 requires a dedicated LOGIN migration identity"
            )
    runtime = roles.get(runtime_role) if runtime_role else None
    if runtime_role and runtime is None:
        problems.append(f"runtime identity {runtime_role!r} does not exist")
    elif runtime is not None:
        problems.extend(
            _role_attribute_problems(runtime, f"runtime identity {runtime_role}")
        )
        if not runtime.rolcanlogin:
            problems.append(
                f"runtime identity {runtime_role} is not a LOGIN role; it could "
                "not serve application traffic"
            )

    expected_owner = owner_role or migrator_role
    if owner_role:
        owner = roles.get(owner_role)
        if owner is None:
            problems.append(f"owner role {owner_role!r} does not exist")
        else:
            problems.extend(
                _role_attribute_problems(owner, f"owner role {owner_role}")
            )
            if owner.rolcanlogin:
                problems.append(
                    f"owner role {owner_role} is a LOGIN role; Topology B requires NOLOGIN"
                )

    # Membership matrix: Topology B exactly one non-inherited, set-only row;
    # Topology A none at all. Any other membership is an unintended elevation
    # path and fails closed.
    memberships = conn.execute(
        text(
            "SELECT r.rolname, m.inherit_option, m.set_option, m.admin_option "
            "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
            "WHERE m.member = cast(:migrator AS regrole)"
        ),
        {"migrator": migrator_role},
    ).fetchall()
    if owner_role:
        if len(memberships) != 1 or memberships[0].rolname != owner_role:
            problems.append(
                f"Topology B requires exactly one membership of {owner_role} for "
                f"{migrator_role}; found {[(m.rolname) for m in memberships]}"
            )
        else:
            row = memberships[0]
            if row.inherit_option:
                problems.append(
                    f"membership {owner_role} -> {migrator_role} is INHERIT TRUE; "
                    "it would grant owner privileges to the backfill identity"
                )
            if not row.set_option:
                problems.append(
                    f"membership {owner_role} -> {migrator_role} is SET FALSE; "
                    "the verified SET ROLE DDL window could not work"
                )
            if row.admin_option:
                problems.append(
                    f"membership {owner_role} -> {migrator_role} is ADMIN TRUE; "
                    "ADR §8.3 requires ADMIN FALSE"
                )
    elif memberships:
        problems.append(
            "Topology A requires no role membership for the migration identity; "
            f"found {[m.rolname for m in memberships]}"
        )

    # Incoming membership closes the other direction: a login granted the
    # migrator (any topology) or the owner (Topology B) can inherit or assume
    # those identities without altering a single grant above.
    incoming_migrator = _incoming_members(conn, migrator_role)
    if incoming_migrator:
        problems.append(
            f"roles {incoming_migrator} are members of the migration identity "
            f"{migrator_role}; ADR §8.3 allows no other role to inherit or "
            "assume it"
        )
    if owner_role:
        unexpected_owner_members = [
            member for member in _incoming_members(conn, owner_role) if member != migrator_role
        ]
        if unexpected_owner_members:
            problems.append(
                f"roles {unexpected_owner_members} are members of the owner role "
                f"{owner_role}; Topology B allows exactly one member, the "
                f"migration identity {migrator_role}"
            )

    if runtime_role:
        for elevated in [migrator_role] + ([owner_role] if owner_role else []):
            if conn.execute(
                text("SELECT pg_has_role(:runtime, :elevated, 'MEMBER')"),
                {"runtime": runtime_role, "elevated": elevated},
            ).scalar():
                problems.append(
                    f"runtime role {runtime_role} is a member of {elevated}"
                )
        # No membership at all, not only none in migrator/owner: any other
        # membership (pg_read_all_data, a team role, a second owner) is an
        # unreviewed privilege path, direct or transitive.
        for granted in _membership_grants(conn, runtime_role):
            problems.append(
                f"runtime role {runtime_role} is a member of {granted}"
            )
        if conn.execute(
            text("SELECT has_schema_privilege(:role, 'public', 'CREATE')"),
            {"role": runtime_role},
        ).scalar():
            problems.append(
                f"runtime role {runtime_role} effectively has CREATE on schema public"
            )

    if owner_role:
        for granted in _membership_grants(conn, owner_role):
            problems.append(
                f"owner role {owner_role} is a member of {granted}; Topology B "
                "allows no membership path from the owner"
            )
        if conn.execute(
            text("SELECT pg_has_role(:owner, :migrator, 'MEMBER')"),
            {"owner": owner_role, "migrator": migrator_role},
        ).scalar():
            problems.append(
                f"owner role {owner_role} can reach the migrator role "
                f"{migrator_role}"
            )
        for privilege in ("USAGE", "CREATE"):
            if conn.execute(
                text("SELECT has_schema_privilege(:owner, :schema, :priv)"),
                {"owner": owner_role, "schema": SCHEMA, "priv": privilege},
            ).scalar():
                problems.append(
                    f"owner role {owner_role} effectively has {privilege} on {SCHEMA}"
                )
        for row in conn.execute(
            text(
                "SELECT p.proname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema "
                "  AND has_function_privilege(:owner, p.oid, 'EXECUTE')"
            ),
            {"owner": owner_role, "schema": SCHEMA},
        ):
            problems.append(
                f"owner role {owner_role} has EXECUTE on {SCHEMA}.{row.proname}"
            )

    _, ownership_problems = _application_ownership(conn, expected_owner)
    problems.extend(ownership_problems)

    for table in _APP_TABLES:
        matrix = _privilege_matrix(conn, migrator_role, table)
        if not matrix:
            continue
        expected = _APP_MIGRATOR_MATRIX[table]
        for privilege, effective in matrix.items():
            wanted = privilege in expected
            if effective is not wanted:
                problems.append(
                    f"migrator {privilege} on {_APP_SCHEMA}.{table} is {effective}, "
                    f"expected {wanted} (ADR §8.3)"
                )
        if runtime_role:
            runtime_matrix = _privilege_matrix(conn, runtime_role, table)
            for privilege in _APP_RUNTIME_MATRIX:
                if not runtime_matrix.get(privilege):
                    problems.append(
                        f"runtime role has no {privilege} on {_APP_SCHEMA}.{table}"
                    )
            for forbidden in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                if runtime_matrix.get(forbidden):
                    problems.append(
                        f"runtime role has {forbidden} on {_APP_SCHEMA}.{table}"
                    )
        # Column-level grants are a separate ACL from the table ACL: a
        # ``GRANT UPDATE (col)`` leaves ``has_table_privilege`` false while it
        # still authorises the write. Both ownership topologies are held to
        # the same per-table forbidden set.
        problems.extend(
            _column_privilege_problems(
                conn,
                migrator_role,
                table,
                forbidden=frozenset(_APP_TABLE_PRIVILEGES)
                - _APP_MIGRATOR_MATRIX[table],
                label="migrator",
            )
        )
        if runtime_role:
            problems.extend(
                _column_privilege_problems(
                    conn,
                    runtime_role,
                    table,
                    forbidden=frozenset({"REFERENCES", "TRIGGER"}),
                    label="runtime",
                )
            )

    problems.extend(_sequence_problems(conn, migrator_role, label="migrator"))
    if runtime_role:
        problems.extend(_sequence_problems(conn, runtime_role, label="runtime"))
        problems.extend(_runtime_migration_internal_problems(conn, runtime_role))

    problems.extend(
        _application_default_acl_problems(conn, expected_owner, runtime_role)
    )

    _problems_raise(problems, "ADR §8.3 application privilege verification failed")


def _row_problems(conn: Connection, expected_version: int) -> list[str]:
    problems: list[str] = []
    protocol_rows = conn.execute(
        text(f"SELECT count(*) AS n FROM {PROTOCOL_TABLE}")
    ).scalar()
    if int(protocol_rows) != 1:
        problems.append(f"{PROTOCOL_TABLE} has {protocol_rows} rows, expected 1")
    lease_rows = conn.execute(
        text(f"SELECT count(*) AS n FROM {LEASE_TABLE} WHERE lease_name = :name"),
        {"name": LEASE_NAME},
    ).scalar()
    if int(lease_rows) != 1:
        problems.append(f"{LEASE_TABLE} has {lease_rows} '{LEASE_NAME}' rows, expected 1")
    if int(protocol_rows) == 1:
        row = conn.execute(
            text(f"SELECT installed_version, min_supported_version, max_supported_version, "
                 f"barrier_state, bootstrap_state FROM {PROTOCOL_TABLE} WHERE singleton")
        ).first()
        if row is not None:
            if not (row.min_supported_version <= int(row.installed_version) <= row.max_supported_version):
                problems.append(
                    f"installed version {row.installed_version} is outside the stored range "
                    f"{row.min_supported_version}..{row.max_supported_version}"
                )
            if row.barrier_state not in ("open", "closed"):
                problems.append(f"barrier_state {row.barrier_state!r} is invalid")
            if row.bootstrap_state not in ("ready", "installing"):
                problems.append(f"bootstrap_state {row.bootstrap_state!r} is invalid")
            # The lease row and the control row carry the protocol version
            # independently. An idle lease row at the wrong version passes
            # every individual check yet makes the next ``lease_acquire_v1``
            # fail with 45006, so the pair must agree before this installation
            # is reported valid. Nothing here repairs the mismatch.
            if int(lease_rows) == 1:
                lease_protocol = conn.execute(
                    text(
                        f"SELECT protocol_version FROM {LEASE_TABLE} "
                        "WHERE lease_name = :name"
                    ),
                    {"name": LEASE_NAME},
                ).scalar()
                if lease_protocol is None or int(lease_protocol) != int(
                    row.installed_version
                ):
                    problems.append(
                        f"lease row protocol_version {lease_protocol!r} does not "
                        f"match the installed protocol {row.installed_version}; "
                        "the mirror pair is inconsistent"
                    )
    return problems


# ---------------------------------------------------------------------------
# Protocol state reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtocolInfo:
    installed_version: int
    min_supported_version: int
    max_supported_version: int
    barrier_state: str
    bootstrap_state: str


def read_protocol_info(engine: Engine) -> ProtocolInfo:
    """Read the control row through ``lease_protocol_info_v1`` (no lock)."""
    with engine.connect() as conn:
        try:
            row = conn.execute(
                text(f"SELECT * FROM {SCHEMA}.lease_protocol_info_v1()")
            ).first()
        except DBAPIError as exc:
            state = sqlstate_of(exc)
            if state in ("42P01", "42883"):
                raise LeaseError(
                    SQLSTATE_MISSING,
                    "migration lease is not installed; run 'python -m app.migrate bootstrap-lease'",
                ) from None
            raise LeaseError(state, "lease protocol state is unavailable") from None
    if row is None:
        raise LeaseError(SQLSTATE_MISSING, "lease protocol row is missing; bootstrap required")
    return ProtocolInfo(
        installed_version=int(row.installed_version),
        min_supported_version=int(row.min_supported_version),
        max_supported_version=int(row.max_supported_version),
        barrier_state=str(row.barrier_state),
        bootstrap_state=str(row.bootstrap_state),
    )


def preflight(
    engine: Engine,
    *,
    expected_version: int = EXPECTED_PROTOCOL_VERSION,
    supported_min: int = SUPPORTED_PROTOCOL_MIN,
    supported_max: int = SUPPORTED_PROTOCOL_MAX,
) -> ProtocolInfo:
    """Fail-fast preflight (ADR §11.4.2 steps 2–5).

    Advisory only: every admission re-checks the authoritative row inside
    ``lease_acquire_v1`` under the guard and the lease row lock.
    """
    info = read_protocol_info(engine)
    if info.bootstrap_state != "ready":
        raise LeaseError(
            SQLSTATE_MISSING,
            f"bootstrap incomplete (state {info.bootstrap_state}); run bootstrap-lease",
        )
    if not (supported_min <= info.installed_version <= supported_max):
        raise LeaseError(
            SQLSTATE_PROTOCOL,
            f"installed protocol {info.installed_version} is outside this release's "
            f"supported range {supported_min}..{supported_max}",
        )
    if info.installed_version != expected_version:
        raise LeaseError(
            SQLSTATE_PROTOCOL,
            f"database protocol {info.installed_version} != release protocol {expected_version}; "
            "run the approved protocol upgrade",
        )
    if info.barrier_state != "open":
        raise LeaseError(
            SQLSTATE_CONFIG, "deployment barrier closed; only bootstrap may write"
        )
    return info


# ---------------------------------------------------------------------------
# Lease client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseState:
    fingerprint: str
    expires_at: object
    generation: int


class MigrationLease:
    """Owner-side client for the ADR-017 lease protocol.

    ``acquire`` retries within a monotonic budget; ``enter`` never retries.
    ``release`` runs in its own short transaction and never masks the
    original failure (ADR §6.8).
    """

    def __init__(
        self,
        engine: Engine,
        *,
        ttl_seconds: float | None = None,
        lock_timeout_ms: int | None = None,
        wait_seconds: float | None = None,
        protocol: int = EXPECTED_PROTOCOL_VERSION,
        token: str | None = None,
    ):
        from app.config import settings

        self.engine = engine
        self.ttl_seconds = float(
            ttl_seconds if ttl_seconds is not None else settings.MIGRATION_LEASE_TTL_SECONDS
        )
        self.lock_timeout_ms = int(
            lock_timeout_ms
            if lock_timeout_ms is not None
            else settings.MIGRATION_LEASE_LOCK_TIMEOUT_MS
        )
        self.wait_seconds = float(
            wait_seconds if wait_seconds is not None else settings.MIGRATION_LEASE_WAIT_SECONDS
        )
        self.protocol = protocol
        self._token = token or generate_token()
        self.state: LeaseState | None = None
        # Validate even when the values were passed explicitly: a NaN or
        # infinite wait would make the monotonic deadline meaningless, and an
        # out-of-range TTL/lock timeout is rejected by the SQL functions with
        # 45004 anyway. Failing here keeps the client consistent with the
        # server contract.
        for name, value in (
            ("ttl_seconds", self.ttl_seconds),
            ("wait_seconds", self.wait_seconds),
        ):
            if not math.isfinite(value) or not (0 < value <= 86400):
                raise LeaseError(
                    SQLSTATE_CONFIG,
                    f"lease {name} must be finite, > 0 and <= 86400",
                )
        if not (1 <= self.lock_timeout_ms <= 600000):
            raise LeaseError(
                SQLSTATE_CONFIG,
                "lease lock_timeout_ms must be between 1 and 600000",
            )

    # -- credentials -----------------------------------------------------
    @property
    def fingerprint(self) -> str:
        return fingerprint(self._token)

    def _token_for_sql(self) -> str:  # pragma: no cover - trivial indirection
        return self._token

    # -- admission -------------------------------------------------------
    def acquire(self) -> LeaseState:
        """Acquire ownership, retrying within the monotonic budget.

        The budget is checked *before* every attempt and again after the
        connection is checked out; if it expired during checkout, the attempt
        is abandoned without executing the lease SQL. Each attempt's
        ``lock_timeout`` is the smaller of the configured value and the
        remaining budget. Connection checkout and establishment are bounded
        separately by the engine's ``pool_timeout`` and ``connect_timeout``;
        an in-flight checkout can therefore consume the remainder and delay
        the failure message, so this is not a strict wall-clock deadline.
        Jitter is calculated once per attempt and reused for both the
        deadline test and the sleep.
        """
        deadline = time.monotonic() + self.wait_seconds
        attempt = 0
        last: LeaseError | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last is None:
                    raise LeaseError(
                        SQLSTATE_INVALID_STATE,
                        f"migration lease not acquired: the "
                        f"{self.wait_seconds:.0f}s wait budget was already exhausted",
                        fingerprint=self.fingerprint,
                    )
                raise LeaseError(
                    last.sqlstate,
                    f"migration lease not acquired within {self.wait_seconds:.0f}s "
                    f"(last: {last})",
                    fingerprint=self.fingerprint,
                )

            attempt += 1
            try:
                with self.engine.connect() as conn:
                    # Checkout may have blocked for most of the budget; never
                    # run the acquisition SQL after the deadline has passed.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LeaseError(
                            SQLSTATE_INVALID_STATE,
                            f"migration lease not acquired: the "
                            f"{self.wait_seconds:.0f}s wait budget expired "
                            "during connection checkout",
                            fingerprint=self.fingerprint,
                        )
                    attempt_lock_timeout = max(
                        1, min(self.lock_timeout_ms, int(remaining * 1000))
                    )
                    with conn.begin():
                        row = conn.execute(
                            text(
                                f"SELECT * FROM {SCHEMA}.lease_acquire_v1("
                                ":protocol, :token, :ttl, :lock_timeout)"
                            ),
                            {
                                "protocol": self.protocol,
                                "token": self._token,
                                "ttl": self.ttl_seconds,
                                "lock_timeout": attempt_lock_timeout,
                            },
                        ).one()
                self.state = LeaseState(
                    fingerprint=row.out_owner_fingerprint,
                    expires_at=row.out_expires_at,
                    generation=int(row.out_generation),
                )
                logger.info(
                    "migration lease acquired (fingerprint=%s generation=%s)",
                    self.state.fingerprint,
                    self.state.generation,
                )
                return self.state
            except DBAPIError as exc:
                last = LeaseError(
                    sqlstate_of(exc), _sanitize(exc), fingerprint=self.fingerprint
                )
            except LeaseError as exc:  # pragma: no cover - defensive
                last = exc

            if last.sqlstate not in RETRYABLE_SQLSTATES:
                raise last
            delay = _retry_delay(attempt)
            if time.monotonic() + delay >= deadline:
                raise LeaseError(
                    last.sqlstate,
                    f"migration lease not acquired within {self.wait_seconds:.0f}s "
                    f"(last: {last})",
                    fingerprint=self.fingerprint,
                )
            time.sleep(delay)

    # -- protected transaction entry ------------------------------------
    def enter(self, conn: Connection) -> LeaseState:
        """Take the guard and the lease row lock, verify ownership, refresh expiry.

        Must be the first SQL statement of every protected transaction.
        """
        try:
            row = conn.execute(
                text(
                    f"SELECT * FROM {SCHEMA}.lease_enter_v1("
                    ":protocol, :token, :ttl, :lock_timeout)"
                ),
                {
                    "protocol": self.protocol,
                    "token": self._token,
                    "ttl": self.ttl_seconds,
                    "lock_timeout": self.lock_timeout_ms,
                },
            ).one()
        except DBAPIError as exc:
            raise LeaseError(
                sqlstate_of(exc), _sanitize(exc), fingerprint=self.fingerprint
            ) from None
        state = LeaseState(
            fingerprint=row.out_owner_fingerprint,
            expires_at=row.out_expires_at,
            generation=int(row.out_generation),
        )
        self.state = state
        return state

    # -- release ---------------------------------------------------------
    def release(self) -> bool:
        """Release ownership in its own short transaction (never inside work)."""
        try:
            with self.engine.connect() as conn:
                with conn.begin():
                    released = conn.execute(
                        text(
                            f"SELECT {SCHEMA}.lease_release_v1(:protocol, :token)"
                        ),
                        {"protocol": self.protocol, "token": self._token},
                    ).scalar()
            logger.info(
                "migration lease released (fingerprint=%s released=%s)",
                self.fingerprint,
                released,
            )
            return bool(released)
        except DBAPIError as exc:
            raise LeaseError(
                sqlstate_of(exc), _sanitize(exc), fingerprint=self.fingerprint
            ) from None


def _sanitize(exc: BaseException) -> str:
    """Best-effort single-line message that carries no connection material."""
    message = str(getattr(exc, "orig", exc)).strip().splitlines()
    return message[0] if message else type(exc).__name__


# ---------------------------------------------------------------------------
# Bootstrap and operator modes (ADR §9.2, §11.4.4)
# ---------------------------------------------------------------------------


def bootstrap_lease(
    engine: Engine,
    *,
    mode: str = "verify",
    upgrade_to: int | None = None,
    expected_version: int | None = None,
    supported_min: int | None = None,
    supported_max: int | None = None,
    authorized_sources: tuple[int, ...] = AUTHORIZED_UPGRADE_SOURCES,
    migrator_role: str | None = None,
    runtime_role: str | None = None,
    lock_timeout: str = "10s",
) -> dict:
    """Run one bootstrap mode: ``verify`` (install/verify), ``close_barrier``, ``upgrade``.

    Returns a small report dict for the CLI. Raises :class:`LeaseError` on
    any refusal; the transaction rolls back and nothing changes.
    """
    from app.config import settings

    migrator_role = migrator_role or settings.STRIKENOVA_MIGRATOR_ROLE
    runtime_role = runtime_role if runtime_role is not None else settings.STRIKENOVA_RUNTIME_ROLE
    expected_version = (
        expected_version if expected_version is not None else EXPECTED_PROTOCOL_VERSION
    )
    supported_min = supported_min if supported_min is not None else SUPPORTED_PROTOCOL_MIN
    supported_max = supported_max if supported_max is not None else SUPPORTED_PROTOCOL_MAX

    if mode not in ("verify", "close_barrier", "upgrade"):
        raise LeaseError(SQLSTATE_CONFIG, f"unknown bootstrap mode {mode!r}")
    if mode == "close_barrier":
        return _close_barrier(engine, migrator_role=migrator_role, lock_timeout=lock_timeout)
    if mode == "upgrade":
        if upgrade_to is None:
            raise LeaseError(SQLSTATE_CONFIG, "--upgrade-protocol requires a target version")

    with engine.connect() as conn:
        try:
            _exec_sql(conn, f"SET lock_timeout = '{lock_timeout}'")
            conn.commit()
        except DBAPIError as exc:
            raise LeaseError(sqlstate_of(exc), _sanitize(exc)) from None

        with conn.begin():
            _assert_migrator_identity(conn, migrator_role)
            _exec_sql(conn, f"SELECT pg_advisory_xact_lock({BOOTSTRAP_GUARD_SQL})")

            installed = conn.execute(
                text(f"SELECT to_regclass('{PROTOCOL_TABLE}') IS NOT NULL AS present")
            ).scalar()
            report: dict = {"mode": mode}

            if not installed:
                if mode == "upgrade":
                    raise LeaseError(
                        SQLSTATE_MISSING,
                        "migration lease is not installed; run bootstrap-lease first",
                    )
                report.update(
                    _first_install(
                        conn,
                        expected_version=expected_version,
                        supported_min=supported_min,
                        supported_max=supported_max,
                        migrator_role=migrator_role,
                        runtime_role=runtime_role,
                    )
                )
            else:
                report.update(
                    _verify_or_upgrade(
                        conn,
                        mode=mode,
                        upgrade_to=upgrade_to,
                        expected_version=expected_version,
                        supported_min=supported_min,
                        supported_max=supported_max,
                        authorized_sources=authorized_sources,
                        migrator_role=migrator_role,
                        runtime_role=runtime_role,
                    )
                )

            info = conn.execute(
                text(f"SELECT * FROM {SCHEMA}.lease_protocol_info_v1()")
            ).one()
            if info.installed_version != (upgrade_to if mode == "upgrade" else expected_version):
                raise LeaseError(
                    SQLSTATE_CONFIG,
                    "post-bootstrap protocol state does not match the requested version",
                )
            if info.barrier_state != "open" or info.bootstrap_state != "ready":
                raise LeaseError(
                    SQLSTATE_CONFIG,
                    f"post-bootstrap state is {info.barrier_state}/{info.bootstrap_state}, "
                    "expected open/ready",
                )
            report.update(
                installed_version=int(info.installed_version),
                barrier_state=info.barrier_state,
                bootstrap_state=info.bootstrap_state,
            )
            return report


def _assert_migrator_identity(conn: Connection, migrator_role: str) -> None:
    """Prove the bootstrap connection is the configured migration identity.

    ``session_user`` is what the server authenticated — it cannot be changed
    by ``SET ROLE`` or an inherited membership — so it, not only
    ``current_user``, must be the migrator. Otherwise a login that can assume
    the migrator role could bootstrap as it.
    """
    session_user, current = conn.exec_driver_sql(
        "SELECT session_user, current_user"
    ).one()
    if session_user != migrator_role:
        raise LeaseError(
            SQLSTATE_CONFIG,
            f"bootstrap must run as {migrator_role!r}; the connection's "
            f"session_user is {session_user!r}",
        )
    if current != migrator_role:
        raise LeaseError(
            SQLSTATE_CONFIG,
            f"bootstrap must run as {migrator_role!r}; current_user is {current!r}",
        )


def _first_install(
    conn: Connection,
    *,
    expected_version: int,
    supported_min: int,
    supported_max: int,
    migrator_role: str,
    runtime_role: str | None,
) -> dict:
    """ADR §9.2.1 — create, seed, install, revoke, verify, then open/ready."""
    schema_present = conn.execute(
        text("SELECT to_regnamespace(:schema) IS NOT NULL"), {"schema": SCHEMA}
    ).scalar()
    lease_present = conn.execute(
        text(f"SELECT to_regclass('{LEASE_TABLE}') IS NOT NULL")
    ).scalar()
    alembic_present = conn.execute(
        text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    ).scalar()
    if schema_present or lease_present or alembic_present:
        raise LeaseError(
            SQLSTATE_CONFIG,
            "partial or out-of-order installation detected (migration_internal, "
            "migration_lease or alembic_version already exists without a protocol row); "
            "refusing to create",
        )

    _exec_sql(conn, CREATE_SCHEMA_SQL)
    for statement in CREATE_TABLES_SQL:
        _exec_sql(conn, statement)
    conn.execute(
        text(
            f"INSERT INTO {PROTOCOL_TABLE} (installed_version, min_supported_version, "
            "max_supported_version, barrier_state, bootstrap_state) "
            "VALUES (:installed, :min, :max, 'closed', 'installing')"
        ),
        {"installed": expected_version, "min": supported_min, "max": supported_max},
    )
    conn.execute(
        text(
            f"INSERT INTO {LEASE_TABLE} (lease_name, protocol_version) "
            "VALUES (:name, :protocol)"
        ),
        {"name": LEASE_NAME, "protocol": expected_version},
    )
    for statement in FUNCTION_SQL:
        _exec_sql(conn, statement)
    for statement in REVOKE_SQL:
        _exec_sql(conn, statement.format(runtime_role=_quote_ident(runtime_role)))

    verify_installation(
        conn,
        migrator_role=migrator_role,
        runtime_role=runtime_role,
        expected_version=expected_version,
    )

    conn.execute(
        text(
            f"UPDATE {PROTOCOL_TABLE} SET installed_version = :installed, "
            "min_supported_version = :min, max_supported_version = :max, "
            "barrier_state = 'open', bootstrap_state = 'ready', "
            "updated_at = now(), updated_by = session_user WHERE singleton"
        ),
        {"installed": expected_version, "min": supported_min, "max": supported_max},
    )
    return {"installed": "created"}


def _verify_or_upgrade(
    conn: Connection,
    *,
    mode: str,
    upgrade_to: int | None,
    expected_version: int,
    supported_min: int,
    supported_max: int,
    authorized_sources: tuple[int, ...],
    migrator_role: str,
    runtime_role: str | None,
) -> dict:
    """ADR §9.2.2 / §11.4.3 steps 3–8 for an existing installation."""
    row = conn.execute(
        text(
            f"SELECT owner_token_hash, owner_fingerprint, expires_at, "
            f"clock_timestamp() AS now FROM {LEASE_TABLE} "
            "WHERE lease_name = :name FOR UPDATE"
        ),
        {"name": LEASE_NAME},
    ).first()
    if row is None:
        raise LeaseError(SQLSTATE_MISSING, f"{LEASE_TABLE} row is missing")

    if row.owner_token_hash is not None and (
        row.expires_at is None or row.expires_at > row.now
    ):
        raise LeaseError(
            SQLSTATE_INVALID_STATE,
            f"lease is held by {row.owner_fingerprint or 'unknown'} until {row.expires_at}; "
            "bootstrap requires quiescence",
            fingerprint=row.owner_fingerprint,
        )

    conn.execute(
        text(
            f"UPDATE {PROTOCOL_TABLE} SET barrier_state = 'closed', "
            "updated_at = now(), updated_by = session_user WHERE singleton"
        )
    )

    verify_installation(
        conn,
        migrator_role=migrator_role,
        runtime_role=runtime_role,
        expected_version=expected_version,
    )

    control = conn.execute(
        text(
            f"SELECT installed_version, min_supported_version, max_supported_version "
            f"FROM {PROTOCOL_TABLE} WHERE singleton"
        )
    ).one()

    if mode == "verify":
        if int(control.installed_version) != expected_version:
            raise LeaseError(
                SQLSTATE_PROTOCOL,
                f"installed protocol {control.installed_version} != this release's "
                f"expected protocol {expected_version}; use --upgrade-protocol",
            )
        if not (
            control.min_supported_version
            <= expected_version
            <= control.max_supported_version
        ):
            raise LeaseError(
                SQLSTATE_PROTOCOL,
                f"installed protocol {expected_version} is outside the stored range "
                f"{control.min_supported_version}..{control.max_supported_version}",
            )
        conn.execute(
            text(
                f"UPDATE {PROTOCOL_TABLE} SET barrier_state = 'open', "
                "bootstrap_state = 'ready', updated_at = now(), "
                "updated_by = session_user WHERE singleton"
            )
        )
        return {"installed": "verified"}

    # upgrade mode
    target = int(upgrade_to)  # type: ignore[arg-type]
    if target != expected_version:
        raise LeaseError(
            SQLSTATE_PROTOCOL,
            f"--upgrade-protocol {target} is not this release's expected protocol "
            f"{expected_version}",
        )
    if target < int(control.installed_version):
        raise LeaseError(
            SQLSTATE_PROTOCOL,
            f"refusing downgrade from {control.installed_version} to {target}",
        )
    if int(control.installed_version) not in authorized_sources:
        raise LeaseError(
            SQLSTATE_PROTOCOL,
            f"installed protocol {control.installed_version} is not an authorized "
            f"upgrade source for {target}",
        )

    conn.execute(
        text(
            f"UPDATE {PROTOCOL_TABLE} SET installed_version = :target, "
            "min_supported_version = :min, max_supported_version = :max, "
            "barrier_state = 'open', bootstrap_state = 'ready', "
            "updated_at = now(), updated_by = session_user WHERE singleton"
        ),
        {"target": target, "min": supported_min, "max": supported_max},
    )
    conn.execute(
        text(
            f"UPDATE {LEASE_TABLE} SET protocol_version = :target, updated_at = now() "
            "WHERE lease_name = :name"
        ),
        {"target": target, "name": LEASE_NAME},
    )
    return {"installed": f"upgraded {control.installed_version}->{target}"}


def _close_barrier(engine: Engine, *, migrator_role: str, lock_timeout: str) -> dict:
    """ADR §11.4.4 — drain window without waiting on the lease row lock."""
    with engine.connect() as conn:
        try:
            _exec_sql(conn, f"SET lock_timeout = '{lock_timeout}'")
            conn.commit()
        except DBAPIError as exc:
            raise LeaseError(sqlstate_of(exc), _sanitize(exc)) from None

        with conn.begin():
            _assert_migrator_identity(conn, migrator_role)
            _exec_sql(conn, f"SELECT pg_advisory_xact_lock({BOOTSTRAP_GUARD_SQL})")

            rows = conn.execute(text(f"SELECT count(*) FROM {PROTOCOL_TABLE}")).scalar()
            if int(rows) == 0:
                raise LeaseError(
                    SQLSTATE_MISSING,
                    "migration lease is not installed; run bootstrap-lease",
                )
            if int(rows) != 1:
                raise LeaseError(SQLSTATE_CONFIG, f"{PROTOCOL_TABLE} has {rows} rows")

            conn.execute(
                text(f"SELECT 1 FROM {PROTOCOL_TABLE} WHERE singleton FOR UPDATE")
            )

            report: dict = {"mode": "close_barrier"}
            _exec_sql(conn, "SAVEPOINT close_probe")
            try:
                probe = conn.execute(
                    text(
                        f"SELECT owner_fingerprint, expires_at FROM {LEASE_TABLE} "
                        "WHERE lease_name = :name FOR UPDATE NOWAIT"
                    ),
                    {"name": LEASE_NAME},
                ).first()
                report["lease_row_lock"] = "free"
                report["owner_fingerprint"] = (
                    probe.owner_fingerprint if probe is not None else None
                )
                report["expires_at"] = str(probe.expires_at) if probe is not None else None
            except DBAPIError as exc:
                if sqlstate_of(exc) != SQLSTATE_LOCK_NOT_AVAILABLE:
                    raise LeaseError(sqlstate_of(exc), _sanitize(exc)) from None
                report["lease_row_lock"] = "held"
                report["owner_fingerprint"] = None
                report["expires_at"] = None
            finally:
                _exec_sql(conn, "ROLLBACK TO SAVEPOINT close_probe")

            conn.execute(
                text(
                    f"UPDATE {PROTOCOL_TABLE} SET barrier_state = 'closed', "
                    "updated_at = now(), updated_by = session_user WHERE singleton"
                )
            )
            return report


def _quote_ident(identifier: str | None) -> str:
    """Quote a role name for the bootstrap REVOKE set."""
    if not identifier:
        return "PUBLIC"
    return '"' + identifier.replace('"', '""') + '"'
