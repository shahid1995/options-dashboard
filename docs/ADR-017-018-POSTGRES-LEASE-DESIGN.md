# StrikeNova — PostgreSQL Migration-Lease and Privilege Design

**Status:** `Proposed — Awaiting Founder Approval — Not Implemented`

**Document type:** Standalone design proposal for founder review. Consolidates the corrected ADR-017 migration-lease protocol and the ADR-018 database-privilege model.

**Target database:** PostgreSQL 16, hosted on Neon. Strategic direction per accepted ADR-019.

**Related:** Issue #145, ADR-017 (migration serialization proposal), ADR-018 (database privilege proposal), ADR-019 (accepted: clean-slate Neon PostgreSQL, CockroachDB retired), PR #146 (open Draft; prior lease implementation attempt).

**Base commit for all repository evidence:** `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b` (current `main`, the merge of PR #148 / ADR-019), 2026-11-20.

**Implementation status:** Nothing described here is implemented. A limited subset of this document's SQL — the four lease functions, the bootstrap orders, the advisory-lock order, the privilege queries and one failed-release scenario — was executed against a **disposable local PostgreSQL 16.16 cluster**; §14.1 reports 83 checks as passing and lists them individually. The T-01…T-24 acceptance matrix below **was not run as written**. No Neon database, no Alembic application path, no backfill, no Render deployment and no staging or production system has been touched. No Neon production database exists for this design, and no provisioning or cutover is authorized.

---

## 1. Purpose and scope

This document specifies a corrected design for serializing StrikeNova's startup schema migration and its per-user exposure backfill on PostgreSQL, together with the privilege boundary that keeps migration infrastructure unreachable from the runtime application identity.

It exists because the previous Issue #145 proposal was not safe to implement:

- it derived ownership from a GUC / session variable that any caller could set;
- it removed the lease renewer but left the backfill running without any lease protection;
- it did not refresh the lease immediately before each per-user backfill commit;
- it assumed the migration identity could read and write application tables without checking grants;
- it did not define bootstrap object verification, default-ACL inspection, or protocol-version compatibility;
- it retained CockroachDB assumptions that ADR-019 has retired.

This document replaces those assumptions with a single ownership primitive (the PostgreSQL row lock), explicit transaction boundaries, catalog-verified bootstrap, and a PostgreSQL 16 acceptance matrix.

**In scope:** design, invariants, SQL object shapes, privilege model, bootstrap and verification procedure, deployment barrier, liveness guidance, Alembic integration arrangement, acceptance-test design, prior-finding disposition, founder decisions.

**Out of scope:** implementation, code changes, migration changes, database provisioning, role creation, grants on any live database, Render changes, credential rotation, cutover, and any claim of production readiness.

---

## 2. Evidence base and repository state

All statements below were verified by reading the repository at `eeeebff`. Nothing in this section is inferred from conversation memory.

| # | Verified fact | Evidence |
|---|---|---|
| E1 | `main` is `eeeebff`; the only ADR file on `main` is `docs/ADR-019-NEON-CLEAN-SLATE.md`. | `git ls-tree -r origin/main`; `docs/` listing |
| E2 | `DECISIONS.md` contains no ADR-017 or ADR-018 entry. ADR-019 refers to ADR-017/018 as *separate proposals*. | `grep -n "ADR-01[6-9]" DECISIONS.md` returned only generic text |
| E3 | `requirements.txt` pins `alembic==1.15.2`. | `options-dashboard-project/backend/requirements.txt:7` |
| E4 | `alembic/env.py` (143 lines) shares an **Engine** through `config.attributes["connectable"]`, then does `with connectable.connect() as connection: context.configure(...); with context.begin_transaction(): context.run_migrations()`. It has no lease integration and no PostgreSQL refusal path. | `alembic/env.py` |
| E5 | There is **no** `app/migrate.py` module, so the operator command `python -m app.migrate upgrade head` does not exist yet. | `find . -name migrate.py` returned nothing |
| E6 | `app/db.py::init_db()` runs `_run_alembic_migrations()` → `backfill_all_exposures(session)` → SQLite-only indexes. No lease, no guard, no shared transaction. | `app/db.py` |
| E7 | `app/db.py` uses a single `DATABASE_URL` identity. No `STRIKENOVA_MIGRATION_DATABASE_URL`, no separate migration engine. | `app/db.py`, `app/config.py` |
| E8 | `backfill_all_exposures(db)` enumerates `SELECT DISTINCT positions.user_id`, then calls `backfill_exposures(db, user_id)` per user, which ends with `db.commit()`. One implicit commit per user, with no lease check. | `app/services/leg_exposure.py` |
| E9 | The backfill touches `positions`, `paper_orders`, `strategy_leg_exposures`. Primary keys are `Integer`, i.e. PostgreSQL `SERIAL` sequences. `users` (`app/identity.py`) is **not** read by the current backfill. | `app/models.py`, `app/identity.py`, `leg_exposure.py` |
| E10 | None of the 7 Alembic revision files use `autocommit_block` or `CREATE INDEX CONCURRENTLY`. | `grep -rn "autocommit_block\|CONCURRENTLY" alembic/versions` returned nothing |
| E11 | The role names `strikenova_prod_migrator`, `strikenova_production_app` and `strikenova_prod_admin` do **not** appear anywhere in `main`'s tracked tree. They occur only in **untracked** review-workspace artifacts (`NEON_STAGING_EXECUTION_PLAN.md`, `AUDIT_REPORT*.md`, `FINAL_REPORT.md`, `NEON_STAGING_PRIVILEGE_PROPOSAL.md`), none of which is committed at `eeeebff`. `DATA.md` §5 describes the paper-trading data domain, not database roles. **No database-role contract exists in the tracked repository (`DECISIONS.md`, `INVARIANTS.md`, `DATA.md`, `SECURITY.md`).** | `git ls-tree -r eeeebff` (no match); grep of the untracked artifacts; `DATA.md` §5 |
| E12 | Those uncommitted audit artifacts record that the three role names were **not created** in Neon staging, and that provider role `strikenova_staging_owner` was the only application role, with `replication` and `bypass_rls`. Uncommitted staging evidence only; not production evidence and not an accepted contract. | untracked `NEON_STAGING_PRIVILEGE_PROPOSAL.md` §1 |
| E13 | PR #146 is **OPEN**, **Draft**, unmerged, head `7134a72d60f72c9dd121513d7fb11515db576a06`; its base is `feat/strikenova-day35-portfolio-intelligence`, not `main`; its files contain no lease module. | `gh pr view 146` |
| E14 | PR #148 is **MERGED** at `eeeebff`. | `gh pr view 148` |

**Consequences of E2 and E11:** ADR-017 and ADR-018 are proposals referenced by ADR-019, not accepted registry entries. The role names are unresolved and appear nowhere in the tracked repository, so this document cannot cite an accepted privilege contract. This design therefore treats both as proposals requiring approval (D-2 … D-7) and marks privilege assumptions as unverified against any live database.

---

## 3. Authoritative architectural direction

1. **D-1 is resolved.** Neon PostgreSQL is the accepted clean-slate production database target (ADR-019).
2. CockroachDB is retired as a strategic production target.
3. No CockroachDB data recovery, export, or historical-data migration is required.
4. Neon staging is separate from future Neon production.
5. Production Neon provisioning and cutover are **not** authorized.
6. The lease targets PostgreSQL/Neon only.
7. CockroachDB compatibility tests are **not** required, and none are specified here.
8. This document must not be read as evidence that a Neon production database exists, that roles exist, or that cutover is approved.

D-1 is not reopened by this document.

---

## 4. Failure model

| Failure | Naive outcome | Required outcome |
|---|---|---|
| Two instances start together | Both run DDL and backfill; unique violation or partial schema | Exactly one holds the lease; the other waits within budget or fails startup |
| Holder crashes mid-DDL | — | Transaction rolls back; lease eligible after expiry; next admission takes over; no partial DDL |
| Holder crashes between per-user transactions | Stale lease blocks startups until expiry | Bounded by TTL; takeover safe and idempotent |
| Holder stalls with an open transaction holding the row lock | Nobody can take over | Operator identifies and terminates the abandoned backend; expiry alone is insufficient |
| Holder's lease expires while it still runs | Continues writing; corruption | Every protected transaction re-verifies ownership and fails closed |
| Runtime role or PUBLIC calls lease functions | Privilege boundary broken | Zero effective access, verified from the catalog |
| Bootstrap runs twice concurrently | Duplicate/partial objects | Serialized by a transaction-scoped advisory lock; idempotent install; verified shape |
| New release deployed while old release runs | Old worker calls a replaced function or fights a new protocol | Protocol-versioned objects plus an explicit deployment barrier |
| Cleanup raises while handling a migration error | Original error masked | Original exception always propagates; cleanup failure logged |

---

## 5. Actors, identities and design overview

### 5.1 Identities

| Identity | Purpose | Required properties |
|---|---|---|
| `strikenova_prod_migrator` | Owns `migration_internal`, runs Alembic DDL and the startup backfill | LOGIN; `CREATE` on `public`; owns or has DDL+DML on application tables; never used by request handling |
| `strikenova_production_app` | Runtime application traffic | LOGIN; DML only on application tables; **zero** access to `migration_internal`; no membership in migrator/owner roles |
| `strikenova_prod_owner` *(optional)* | NOLOGIN owner of application tables if ownership is separated | NOLOGIN; migrator is a member with `INHERIT` if this topology is chosen |

Names are proposals (E11). The properties matter; identifiers may change.

### 5.2 Ownership proof

> **The PostgreSQL lease-row lock is the only proof of transaction ownership.**

1. `migration_lease_acquire` is the only operation that transfers ownership. It takes `SELECT … FOR UPDATE` on the single lease row and transfers ownership only when the row has no owner or its expiry has passed.
2. `migration_lease_enter` executes inside a protected transaction and takes `SELECT … FOR UPDATE` on the same row. Holding that lock while the recorded owner fingerprint matches is the proof.
3. `enter` verifies the recorded owner and refreshes the advisory expiry.
4. **No GUC, session variable, process flag, timestamp, or fencing epoch is ownership evidence.** `generation` exists only for diagnostics.
5. There is no handoff mode, no renewer thread, no renewal contract, and no GUC-based ownership anywhere in this design.

A row lock lasts only for its transaction, so ownership *between* transactions is the recorded (owner-token hash, expiry) pair; the lock is what makes verification atomic. A worker that cannot take the row lock cannot claim or verify ownership.

### 5.3 Lifecycle

```
 connect (migration identity)
   SET lock_timeout (session level, outside any transaction)
 bootstrap / verify migration_internal          (one transaction, advisory xact lock)
 acquire(protocol, token, ttl)                  (one transaction, row lock, commit)
   ┌───────────────────────────────────────────────────────────────┐
   │ DDL transaction:  enter → alembic upgrade head → enter → COMMIT│
   ├───────────────────────────────────────────────────────────────┤
   │ enumerate users (short read transaction, then COMMIT)          │
   ├───────────────────────────────────────────────────────────────┤
   │ for each user: enter → backfill → enter → COMMIT   (one tx)    │
   └───────────────────────────────────────────────────────────────┘
 release(token)                                 (separate short transaction)
```

---

## 6. Lease protocol

### 6.1 Private schema and objects

Schema `migration_internal`, owned by the bootstrap executor (`strikenova_prod_migrator`). Protocol-version objects are name-suffixed so a future protocol can be installed alongside without replacing anything an active worker is executing.

| Object | Kind | Purpose |
|---|---|---|
| `migration_internal.migration_lease` | table | The single lease row |
| `migration_internal.lease_protocol` | table | Installed version, supported range, barrier and bootstrap state |
| `migration_internal.lease_acquire_v1(integer, text, double precision, integer)` | `SECURITY DEFINER` function | The only ownership transfer |
| `migration_internal.lease_enter_v1(integer, text, double precision, integer)` | `SECURITY DEFINER` function | Ownership verification + expiry refresh inside a protected transaction |
| `migration_internal.lease_release_v1(integer, text)` | `SECURITY DEFINER` function | Ownership-checked release |
| `migration_internal.lease_protocol_info_v1()` | `SECURITY DEFINER` function | Read installed/supported/barrier state |

`migration_internal.lease_protocol` is the **stable, non-versioned control object**. It is created once by the first installation, is never dropped, recreated or replaced by a protocol upgrade, and every protocol version — the one installed today and any later one — reads the same row. It is the single authoritative record of `installed_version`, the supported range and the barrier.

Version suffixes apply only to executable code and, if a future protocol changes the row semantics, to that protocol's own lease-row table. A version-suffixed object never carries the authoritative version state:

| State | Authoritative home | Mirrors and diagnostics (never proof, never authorization) |
|---|---|---|
| Installed protocol version | `lease_protocol.installed_version` | `migration_lease.protocol_version` (written by `acquire` and by the upgrade, compared only as a corruption guard), `lease_protocol_info_v1()` output |
| Supported range | `lease_protocol.min_supported_version` / `max_supported_version` | the release's own pinned constants |
| Barrier | `lease_protocol.barrier_state` | — |
| Bootstrap completion | `lease_protocol.bootstrap_state` | — |

`migration_lease.protocol_version` therefore never authorizes anything. A `*_v2` function set installed alongside `*_v1` reads the same `lease_protocol` row through the same guard, which is what makes a stale process detectable (§11.2, §11.3).

```sql
CREATE TABLE migration_internal.migration_lease (
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
);

CREATE TABLE migration_internal.lease_protocol (
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
```

The lease row is created once by bootstrap and is never deleted. Deleting it is not a supported operation.

### 6.2 Owner credentials

- The raw owner token is **256 bits** from a cryptographically secure source (`secrets.token_hex(32)` or equivalent), generated by the acquiring process and held only in memory for that startup.
- The database stores only `sha256(token)` as lowercase hex (`encode(sha256(convert_to(p_token, 'UTF8')), 'hex')`). PostgreSQL's built-in `sha256()` avoids a `pgcrypto` dependency.
- `owner_fingerprint` is `left(sha256_hex, 12)` and is the only owner-related value permitted in logs, exceptions, metrics or operator output.
- The raw token must never be logged, written to a file, placed in a GUC, included in an exception message, or included in a statement whose parameters are logged.
- Exceptions name the lease, the protocol version and the fingerprint — never the token or its full hash.

### 6.3 `acquire`

```sql
CREATE FUNCTION migration_internal.lease_acquire_v1(
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
$$;
```

- The caller commits after `acquire` returns; the lock proves the transfer was atomic.
- A concurrent `acquire` blocks on the row lock while another lease transaction is open. If the owner is working *between* transactions the row is unlocked, so the second `acquire` reads a live unexpired owner and raises `45002`. That is the normal serialization path.
- Expiry uses `clock_timestamp()` — the database clock — never client time.
- The authoritative version, supported-range and `bootstrap_state` checks, and the barrier check, all live **inside** `acquire`, under the lease row lock and the advisory guard, so admission and the protocol state cannot disagree (§11.2, §11.4).
- `acquire` takes the advisory guard `M` first and the lease row lock second (§6.10). A second worker therefore waits at most its `lock_timeout` for either lock and then raises `55P03`.
- The client treats `45002` as "another worker is initializing", retries within its monotonic budget (6.7), then fails startup.

### 6.4 `enter`

```sql
CREATE FUNCTION migration_internal.lease_enter_v1(
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
```

Properties enforced:

- `enter` can succeed only inside a transaction holding the row lock.
- A forged GUC, session variable, process state or client timestamp cannot substitute for the token comparison.
- Re-entry in the same transaction is allowed and merely refreshes expiry; that is why the pre-commit refresh is cheap.
- A different transaction presenting the same token blocks until the lock is free, then succeeds — one writer at a time, by construction.
- `enter` **never** retries internally; `55P03` propagates and the startup fails.
- `enter` deliberately does **not** consult `barrier_state`: closing the barrier must stop new admissions without aborting an already-admitted transaction mid-flight (§11.4).
- `generation` increments on acquisition only and is reported for correlation. It is never proof of takeover.
- `enter` re-reads `installed_version` from the stable control row under the advisory guard and the lease row lock, so a process of any protocol version that resumed after an upgrade fails `45006` before it can write anything (§11.2).

### 6.5 `release`

```sql
CREATE FUNCTION migration_internal.lease_release_v1(p_protocol integer, p_token text)
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
$$;
```

Release runs in its **own short transaction, after** the DDL transaction and the final backfill transaction have ended. It must never run inside a protected transaction: that would clear ownership while the transaction is still writing. Its fixed 5 s `lock_timeout` is bounded and saved/restored like the other functions, so a release blocked by an abandoned transaction fails instead of hanging.

### 6.6 Protocol information

```sql
CREATE FUNCTION migration_internal.lease_protocol_info_v1()
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
```

It is read-only, takes no row lock, and is the only lease function whose output is not security-sensitive. This is the one function whose OUT parameter names intentionally match column names; every reference in its body is qualified with the `pr` alias, so PL/pgSQL cannot resolve one as the other.

### 6.7 Error model and retry policy

| SQLSTATE | Meaning | Caller behavior |
|---|---|---|
| `45001` | Ownership mismatch (presented token is not the recorded owner) | Abort; never retry with the same token |
| `45002` | Invalid lease state (held by another, no owner, expired own lease, bootstrap quiescence refused) | `acquire`: retry within budget; `enter`: abort |
| `45004` | Configuration error (short token, invalid TTL or `lock_timeout`, misconfigured identity/protocol, **deployment barrier closed**) | Abort; run a bootstrap mode (§11.4.5) |
| `45005` | Required bootstrap object missing | Abort; run the guarded bootstrap |
| `45006` | Unsupported protocol version — `installed_version` is not the caller's protocol, or the caller's protocol is outside `min_supported_version..max_supported_version`, or the lease-row mirror disagrees with the control row | Abort; deploy the matching release or run the approved protocol upgrade |
| `55P03` | Lock not available (`lock_timeout`) | `acquire`: retry within budget; `enter`: abort |
| `40001` / `40P01` | Serialization failure / deadlock | `acquire`: retry within budget; `enter`: abort |

- Only `acquire` retries, and only for `45002`, `55P03`, `40001`, `40P01`.
- Every attempt uses a **new transaction**, a fresh `lock_timeout`, and a rollback of the failed attempt.
- The budget is **monotonic elapsed time** (default 180 s), not an attempt count alone. Backoff starts near 200 ms with jitter and a 2 s cap.
- Exhausting the budget fails startup with a non-zero exit; the platform restart policy provides the next attempt.
- `enter` never retries: contention there means another transaction is writing the lease row, which is never normal for a legitimate owner.

### 6.8 Cleanup must not mask the original failure

```python
primary = None
try:
    ...  # acquire, DDL, backfill, release
except BaseException as exc:
    primary = exc
    raise
finally:
    if needs_cleanup:
        try:
            guard.release()
        except Exception:
            logger.exception("lease cleanup failed (fingerprint=%s)", fp)
            if primary is None:
                raise
```

- On an aborted transaction, `ROLLBACK` first, then open a new short transaction for `release` (a function call inside an aborted transaction fails with `25P02`).
- Cleanup failure is logged with the fingerprint and never replaces, wraps or chains over the original migration/backfill exception.
- If cleanup is the only failure, it is surfaced and startup fails.
- **A failed `release` does not clear the lease owner.** `lease_release_v1` clears `owner_token_hash`/`owner_fingerprint` only in its own `UPDATE`, in the transaction that holds the row lock; anything that raises before it — `55P03`, `45001`, `45006` — rolls the whole call back, so the recorded owner and `expires_at` are exactly as they were. The rollback releases the row lock, not the ownership.
- A new worker therefore cannot acquire a lease whose recorded owner is still live: `acquire` raises `45002` until `expires_at` passes or an operator recovers the lease under §12.4. That is the intended fail-closed outcome — failed cleanup must not hand the lease to a second worker while the first may still be running.

### 6.9 PostgreSQL 16 semantic review of the four functions

Reviewed by inspection against PostgreSQL 16 behaviour, then executed on a disposable PostgreSQL 16.16 instance for the four functions and the barrier/privilege checks (§14.1). The table records the semantics; §14.1 records what was observed, including a statement in this review that was wrong (the `NaN` row). The rest of the §14 matrix is still unexecuted.

| Issue | PostgreSQL 16 behaviour | Resolution in this design |
|---|---|---|
| `lock_timeout = 0` | `0` **disables** the timeout (waits indefinitely); it does not mean "do not wait" | `acquire` and `enter` reject `< 1` with `45004`; bounds are 1–600000 ms. A zero never reaches `set_config` |
| `lock_timeout` leakage | `set_config(..., true)` is transaction-local, so it also applies to every later lock wait in the same transaction (including Alembic DDL) | Each function saves `current_setting('lock_timeout')` and restores it immediately after the row lock is taken; the DDL transaction keeps the operator's value |
| `make_interval` units | `make_interval(secs => x)` takes **seconds** as `double precision`; fractional seconds are allowed and there is no implicit millisecond interpretation | TTL is passed in seconds, bounded to `0 < ttl <= 86400`; `not (ttl > 0)` rejects `NULL` and negatives, and the upper bound rejects `NaN` and `Infinity`, because PostgreSQL orders `NaN` above every finite number (executed: §14.1 V-5) |
| `%ROWTYPE` vs OUT parameters | `RETURNS TABLE (...)` columns become PL/pgSQL variables, which can collide with table columns in unqualified SQL | OUT parameters are prefixed `out_`; every table reference is aliased (`l.`, `pr.`); `lease_protocol_info_v1` keeps matching names but fully qualifies each column |
| `session_user` type | `session_user` is `name`, and `owner_role` is `name`; relying on an implicit assignment cast is unnecessary | Assignment uses `session_user::name` |
| `has_function_privilege` signature | It resolves a `regprocedure`, i.e. the **identity argument** types only; result/OUT columns and parameter names are irrelevant | §10.3 uses `float8` (the canonical spelling) rather than `double precision`, and no function has default arguments |
| `sha256` | Built-in since PostgreSQL 11 for `bytea`; `pgcrypto` is not needed | `encode(sha256(convert_to(token,'UTF8')),'hex')` |
| Clock source | `now()` is transaction start; `clock_timestamp()` is the actual current time | Expiry and acquisition timestamps use `clock_timestamp()`; defaults on the table use `now()` |
| Row-lock re-entry | A transaction that already holds a row lock can lock it again without blocking | The second `enter` before commit is a refresh, not a deadlock |
| `RETURN QUERY` types | Result columns must match the declared OUT types exactly | `text`, `timestamptz`, `bigint`/`integer` are matched; `generation` is `bigint` in both table and function |
| Advisory lock and `lock_timeout` | `pg_advisory_xact_lock` honours `lock_timeout` like any other lock wait, and is released by the server at commit, rollback or backend death | The cross-version guard is taken inside `enter`/`acquire`/`release` with the caller's bounded timeout, so contention is `55P03` and a crashed backend cannot leak it (executed: §14.1 V-18) |
| Owned row with `NULL` expiry | `expires_at > now` is `NULL`, not `true`, so a naive check would silently treat a corrupt owned row as free | `acquire` treats it as held (`45002`); the table has `CHECK (owner_token_hash IS NULL OR expires_at IS NOT NULL)` |


### 6.10 Advisory locks and global lock order

Two transaction-scoped advisory locks exist, with fixed, non-overlapping roles. Both are taken with `pg_advisory_xact_lock`, so PostgreSQL releases them at `COMMIT`, `ROLLBACK` or backend death; neither can leak.

| Guard | Key expression | Taken by | Mode | Held until |
|---|---|---|---|---|
| Migration guard (`M`) | `hashtextextended('strikenova:migration_internal:migration_guard', 0)` | `lease_acquire_v1`, `lease_enter_v1`, `lease_release_v1` — inside the function, before the lease row lock | exclusive, transaction-scoped | the protected transaction ends |
| Bootstrap guard (`B`) | `hashtextextended('strikenova:migration_internal:bootstrap', 0)` | `bootstrap-lease` install/verify, `--upgrade-protocol`, `--close-barrier` | exclusive, transaction-scoped | the bootstrap transaction ends |

**Global lock order.** Every path takes its locks in this order and no other:

```text
B (bootstrap modes only)  ->  M (worker functions only)  ->  lease row FOR UPDATE  ->  protocol row FOR UPDATE
```

No path takes a later lock before an earlier one, and no path ever takes both `B` and `M` in the opposite order, because no path takes both. `--close-barrier`'s lease-row probe is `NOWAIT` (§11.4.4), so it never waits on the lease row at all and cannot form a cycle.

**`M` is the cross-version guard, and it lives in the application path.** It is acquired inside `lease_enter_v1` — as the statement that must remain the first statement of every protected transaction — before that transaction touches the lease row. Consequently:

- every DDL transaction (§7.1) and every per-user backfill transaction (§7.3) holds `M` for its whole duration;
- `lease_acquire_v1` and `lease_release_v1` take the same key, so admission and release are ordered against protected transactions and against each other;
- a future `*_v2` function set installed alongside `*_v1` takes the **same** key, so two protocol versions — even ones whose lease-row tables differ — cannot run their DDL or their backfills concurrently.

**`M` is not ownership evidence.** Ownership remains the token-hash comparison under the lease row lock (§5.2). `M` provides cross-version mutual exclusion only.

**Bootstrap modes deliberately do not take `M`.** An admitted owner holds `M` for the whole DDL transaction; if `--close-barrier` took `M`, it would return `55P03` for exactly the window it exists to open — the defect recorded as F15 and executed as §14.1 V-11/V-12. Bootstrap's exclusion from running workers comes from the lease row lock, which every protected transaction holds, and from the quiescence check (§9.2.2), not from `M`.

Contention on either guard is bounded by the caller's `lock_timeout`, exactly like the lease row lock: `55P03`, no internal retry, and the same retry budget as §6.7.

---

## 7. Transaction boundaries

### 7.1 Alembic DDL transaction

| Property | Requirement |
|---|---|
| Connection | The migration engine's dedicated connection, shared by the lease and Alembic |
| First SQL statement | `enter` (which takes the advisory guard `M`, then `SELECT … FOR UPDATE` on the lease row) |
| Lock duration | Row lock held from `enter` until `COMMIT` or `ROLLBACK` |
| Pre-commit refresh | `enter` again as the last statement before `COMMIT` |
| Failure | Any exception rolls back all DDL and the `alembic_version` update |
| Bare CLI | `alembic upgrade head` on PostgreSQL fails closed without lease admission (§13) |

```text
BEGIN (implicit, first statement)
  enter(token)                 -- takes advisory guard M, then the lease row lock;
                               -- fails on contention/expiry/mismatch/version
  alembic upgrade head         -- DDL + alembic_version update
  enter(token)                 -- refresh immediately before COMMIT
COMMIT
```

`enter` remains the first statement of the transaction. The cross-version advisory guard `M` is acquired *by* that statement, before the lease row lock (§6.10), so the transaction flow above is unchanged while a second protocol version is excluded for the whole DDL duration.

Holding the row lock for the whole DDL transaction is what keeps a second startup from beginning DDL: its `acquire` blocks on the row lock, or (after commit) sees a live owner and backs off.

### 7.2 User enumeration

- Enumeration is its own short transaction: `SELECT DISTINCT user_id FROM public.positions ORDER BY user_id`.
- It must **finish and commit before the first protected user transaction begins**. A long-lived enumeration transaction would hold a snapshot and (if protected) hold the lease row lock for the whole backfill.
- The result is a plain in-memory list of user ids; no cursor stays open across user transactions.

### 7.3 Per-user backfill transaction

```text
BEGIN
  enter(token)                 -- first SQL statement; takes guard M, then the row lock
  read positions / paper_orders for this user
  insert strategy_leg_exposures rows (idempotent)
  enter(token)                 -- MANDATORY pre-commit refresh
COMMIT
```

Requirements:

- **`enter` is the first SQL statement of every per-user transaction.** No read, no `SET`, no ORM query precedes it. The advisory guard `M` is acquired inside that statement (§6.10), so every per-user transaction also holds the cross-version guard for its whole duration.
- **Every per-user transaction includes the pre-commit `enter`.** This was missing from the previous proposal and is a blocking correction: without it, a worker whose lease expired mid-backfill could commit writes after another worker had taken over.
- One user per transaction. No cross-user transaction and no batching of users.
- The existing `db.commit()` inside `backfill_exposures()` must be removed or relocated in the implementation; the boundary belongs to the caller, not the per-user helper.
- `enter` failure rolls back that user's transaction and aborts the whole startup. The implementation must not skip the user and continue, because the remaining users would run under a lease the worker no longer owns.
- The backfill stays idempotent (`uq_exposure_source_order`), so a restart after a partial run creates no duplicates.

Intended shape (design intent only):

```python
user_ids = enumerate_users(conn)               # own transaction, committed
for user_id in user_ids:
    with migration_engine.begin() as conn:     # one transaction per user
        guard.enter(conn)                      # first statement
        backfill_user(conn, user_id)           # no internal commit
        guard.enter(conn)                      # pre-commit refresh
```

### 7.4 Expected behavior

| Situation | Behavior |
|---|---|
| Crash during DDL | Transaction rolls back; server releases the row lock; the lease row stays owned until `expires_at`; the next `acquire` after expiry takes over and re-runs DDL from the last committed revision |
| Crash between user transactions | Committed users stay committed (idempotent); lease expires; the next `acquire` takes over and processes the full user list again |
| Stall with an open transaction | Row lock is held as long as the backend lives; **expiry alone cannot enable takeover** (§12); the pre-commit `enter` fails once expiry passes, so the stalled transaction rolls back instead of committing |
| Expired, and another worker admitted | The superseded worker's next `enter` raises `45001` (different token) or `45002` (its own lease expired); its transaction rolls back; it must not retry |
| Superseded while idle (no open transaction) | Its next `enter` raises `45001`; startup aborts |
| Released normally | Row is unowned; the next worker admits immediately |

### 7.5 Availability trade-off (explicit)

Per-user transactions mean the lease can expire while the backfill is still running. Another worker may then take over and redo the remaining users. That is **safe** (each transaction is verified at entry and before commit, and writes are idempotent) but not free: a restart can duplicate work, and a user transaction longer than the TTL self-aborts at the pre-commit `enter`.

Sizing rule: `ttl > p99(single_user_backfill) + margin`, and `statement_timeout` for a user transaction must be below the TTL so a runaway query fails with `57014` before ownership expires. Both must come from measured data, not guesses. This is founder decision D-7.

---

## 8. Backfill execution identity and privileges

### 8.1 Identity

The startup backfill runs under the **migration identity**, so the runtime application role needs no lease or migration privileges at all.

At `eeeebff` this is not possible without change: `app/db.py` builds one engine from `DATABASE_URL` and `init_db()` uses that same engine for Alembic and for `backfill_all_exposures()` (E6, E7). The implementation must:

1. add a migration engine from `STRIKENOVA_MIGRATION_DATABASE_URL` (or an explicit migration configuration) that is **required** when `DATABASE_URL` is PostgreSQL;
2. refuse to run when the migration identity equals the runtime identity on PostgreSQL (`45004`), because that would defeat ADR-018's boundary;
3. run Alembic and the backfill on the migration engine only.

The backfill does not become "the app role with extra grants"; it becomes a migration-time operation, which is why D-3 requires founder approval.

### 8.2 Minimum privileges, derived from the code

The following is derived by reading the current backfill path (E8, E9). It is **not** verified against any database, because no authorized database exists for this design. Acceptance test T-16 exists to verify it.

| Operation | Object | Privilege | Why the code needs it |
|---|---|---|---|
| Enumerate users | `public.positions` | `SELECT` | `SELECT DISTINCT user_id FROM positions` |
| Read positions per user | `public.positions` | `SELECT` | `select(Position).where(Position.user_id == …)` reads all columns |
| Read source orders | `public.paper_orders` | `SELECT` | entry/exit order queries |
| Idempotency check | `public.strategy_leg_exposures` | `SELECT` | `select(StrategyLegExposure).where(user_id, order_id)` |
| Insert attribution | `public.strategy_leg_exposures` | `INSERT` | `create_exposures_for_orders()` |
| Allocate ids | sequence returned by `pg_get_serial_sequence('public.strategy_leg_exposures','id')` | `USAGE` only | `SERIAL` default calls `nextval` |
| Return the generated id | `public.strategy_leg_exposures.id` | `SELECT` on the column, already covered by the table `SELECT` above | SQLAlchemy's PostgreSQL insert uses `INSERT … RETURNING id` |
| Read users | `public.users` | **none required today** | the current enumeration reads `positions.user_id`, not `users` |

Sequence privilege, precisely: `nextval` requires `USAGE` on the sequence; `currval` is also authorized by `USAGE`. Sequence `SELECT` authorizes only `currval`, which `USAGE` already covers, so it is **not** granted and `SELECT` alone would not authorize `nextval` (executed: §14.1 V-22). `INSERT … RETURNING id` additionally requires `SELECT` on the returned column, which is why the table-wide `SELECT` on `strategy_leg_exposures` is required as well as `INSERT` (executed: §14.1 V-23).

No `UPDATE` or `DELETE` on application tables is required by the backfill. `exit_exposure_allocations` is written only during live exits, not by this backfill.

If a future change joins `users`, grant column-level `SELECT (id)` rather than table-wide `SELECT`, and extend T-16.

### 8.3 How the migration identity obtains those privileges

Two topologies are possible; the design recommends the first.

**Topology A (recommended): the migration identity owns what Alembic creates.**

On a clean Neon database the Alembic chain runs as `strikenova_prod_migrator`, so the migrator owns `public.positions`, `public.paper_orders`, `public.strategy_leg_exposures`, their indexes, constraints and sequences. Table owners have all table privileges implicitly and need no grants; this is exactly why the design must **verify** ownership instead of assuming it.

Verification (run after the Alembic chain, before declaring the backfill identity ready):

Ownership also means the owner can write anything it owns, including `DELETE` on audit-bearing data the backfill never removes. Topology A therefore needs the ordinary privilege revokes below, applied by the operator (or by provisioning) after the Alembic chain; PostgreSQL applies them to the owner's ACL and they are effective — an `INSERT` after the revoke fails `42501` even for the owner (executed: §14.1 V-20). They bound accident, not intent: an owner can always grant a privilege back to itself, which is why §8.4 and D-2 still govern who owns the tables.

```sql
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.positions              FROM strikenova_prod_migrator;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.paper_orders           FROM strikenova_prod_migrator;
REVOKE UPDATE, DELETE, TRUNCATE          ON public.strategy_leg_exposures FROM strikenova_prod_migrator;
REVOKE ALL                               ON SEQUENCE public.strategy_leg_exposures_id_seq
                                                                        FROM strikenova_prod_migrator;
GRANT  USAGE                             ON SEQUENCE public.strategy_leg_exposures_id_seq
                                                                        TO strikenova_prod_migrator;
```

Verification (run after the revokes, before declaring the backfill identity ready). Each privilege is checked separately, because `has_table_privilege('…','SELECT,INSERT')` answers a different question — "any of these" — than the one T-16 asserts:

```sql
SELECT c.relname, p.priv,
       has_table_privilege('strikenova_prod_migrator', c.oid, p.priv) AS effective
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) AS p(priv)
 WHERE n.nspname = 'public'
   AND c.relname IN ('positions', 'paper_orders', 'strategy_leg_exposures')
 ORDER BY c.relname, p.priv;

SELECT c.relowner::regrole AS owner, pg_get_serial_sequence('public.strategy_leg_exposures','id')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relname = 'strategy_leg_exposures';

SELECT has_sequence_privilege('strikenova_prod_migrator',
           pg_get_serial_sequence('public.strategy_leg_exposures','id'), 'USAGE')  AS seq_usage,
       has_sequence_privilege('strikenova_prod_migrator',
           pg_get_serial_sequence('public.strategy_leg_exposures','id'), 'SELECT') AS seq_select;
```

Expected effective matrix for Topology A:

| Table | `SELECT` | `INSERT` | `UPDATE` | `DELETE` |
|---|---|---|---|---|
| `public.positions` | true | false | false | false |
| `public.paper_orders` | true | false | false | false |
| `public.strategy_leg_exposures` | true | true | false | false |

and `seq_usage = true`, `seq_select = false`.

If any value differs, startup fails closed with `45004`; it must not silently fall back to the runtime identity or to a superuser.

**Topology B: application tables are owned by a separate NOLOGIN owner.**

If the founder prefers `strikenova_prod_owner` to own application tables, the migrator needs explicit, minimal grants. These are the only grants this design would add for the backfill:

```sql
GRANT SELECT         ON public.positions              TO strikenova_prod_migrator;
GRANT SELECT         ON public.paper_orders           TO strikenova_prod_migrator;
GRANT SELECT, INSERT ON public.strategy_leg_exposures TO strikenova_prod_migrator;
GRANT USAGE          ON SEQUENCE public.strategy_leg_exposures_id_seq
                                                      TO strikenova_prod_migrator;
```

`USAGE` alone is the correct sequence grant: it authorizes `nextval` and `currval`. Sequence `SELECT` is deliberately absent — it authorizes only `currval`, which `USAGE` already covers, and it does **not** authorize `nextval` (executed: §14.1 V-22). Under Topology B no revokes on application tables are needed, because the migrator holds nothing beyond the four grants above; the same verification queries as Topology A must confirm the matrix, with `SELECT`/`INSERT` present exactly as granted.

Alternatives and why they are rejected:

- `GRANT ALL ON ALL TABLES IN SCHEMA public` — unjustified breadth, including `DELETE` on audit-bearing data.
- Making the migrator a superuser — destroys the ADR-018 boundary.
- Using the runtime role for the backfill — the runtime role would then need lease access and migration-time DML, which is exactly what D-2/D-3 exist to prevent.

### 8.4 What the runtime role must not have

- No membership in `strikenova_prod_migrator` or `strikenova_prod_owner`, direct or transitive.
- No `CREATE` on `public`.
- No `SELECT/INSERT/UPDATE/DELETE` on `migration_internal` objects.
- No `EXECUTE` on any `migration_internal` function.
- No ownership of any `migration_internal` object.

---

## 9. Private schema, bootstrap and verification

### 9.1 Bootstrap is the one pre-Alembic schema operation

Alembic remains the sole authority for application schema. The migration-lease schema is the narrowly scoped exception: it must exist **before** Alembic runs, because Alembic's DDL transaction needs `enter`. Founder decision D-6 covers this exception.

Bootstrap is an operator/startup command (`python -m app.migrate bootstrap-lease` in the intended CLI) run as the migration identity. It is **not** a `SECURITY DEFINER` function: creating the definer role's own objects through a definer function adds recursion and privilege ambiguity for no benefit.

### 9.2 Bootstrap: first installation and existing-installation verification

Bootstrap is one operator command with three modes, all executed by the migration identity as `python -m app.migrate ...`:

| Mode | Purpose | May write `barrier_state` |
|---|---|---|
| `bootstrap-lease` (install/verify) | Create or verify protocol objects; leave the database ready | Yes — sets `open` on success |
| `bootstrap-lease --close-barrier` | Start a maintenance window; block new admissions without waiting for, locking or aborting a live owner | Yes — sets `closed`; never sets `open` |
| `bootstrap-lease --upgrade-protocol N` | Install/activate protocol `N` and reopen | Yes — sets `closed` then `open` in one transaction |

Only these modes may write the protocol row. No application code path, migration revision or raw-SQL runbook updates `barrier_state`.

Two procedures use that write path and they are specified separately, because one ordered list cannot be correct for both. On an empty database there is no `lease_protocol` row to `UPDATE` and no `migration_lease` table to `SELECT ... FOR UPDATE`; on an existing database, creating anything would silently paper over a broken installation. The command selects the procedure by reading for the protocol row: absent → §9.2.1, present → §9.2.2.

#### 9.2.1 First installation on an empty database

Preconditions, checked before anything is written: the database contains none of this design's objects (no `migration_internal` schema, no `migration_lease`, no `lease_protocol`, no `lease_*` function) and no `alembic_version` table. The migration identity exists and can connect. `public` is left exactly as provisioning made it; bootstrap never creates, alters or drops an application object (D-6).

Every step runs in one transaction:

```text
connect as strikenova_prod_migrator
SET lock_timeout = '10s'                    -- session level, before BEGIN
BEGIN
  1. SELECT pg_advisory_xact_lock(B)        -- B = bootstrap guard (§6.10) -> 55P03
  2. Read for migration_internal.lease_protocol.
       present      -> not a first installation: continue with §9.2.2 step 2
                       in this same transaction (never a second CREATE)
       absent, but migration_internal or migration_lease or alembic_version
       exists       -> partial or out-of-order install, refuse      -> 45004
       absent, tree empty -> continue below
  3. CREATE SCHEMA migration_internal
  4. CREATE TABLE migration_internal.lease_protocol                 (§6.1)
  5. INSERT INTO migration_internal.lease_protocol
       (installed_version, min_supported_version, max_supported_version,
        barrier_state, bootstrap_state)
       VALUES (1, 1, 1, 'closed', 'installing');
  6. CREATE TABLE migration_internal.migration_lease                (§6.1)
  7. INSERT INTO migration_internal.migration_lease
       (lease_name, protocol_version)
       VALUES ('migration-lease', 1);    -- owner_token_hash NULL: unowned
  8. CREATE FUNCTION lease_acquire_v1, lease_enter_v1,
       lease_release_v1, lease_protocol_info_v1                     (§6.3–§6.6)
  9. Revoke set (§10.2) — after the last CREATE FUNCTION
 10. Verification phase (§9.3): schema/table/function owner, shape, constraints,
     relrowsecurity, prosecdef, proconfig, signatures, ACLs, default ACLs,
     exactly one protocol row and exactly one lease row         -> 45004 / 45005
 11. UPDATE migration_internal.lease_protocol
       SET installed_version = 1,
           min_supported_version = 1,
           max_supported_version = 1,
           barrier_state = 'open',
           bootstrap_state = 'ready',
           updated_at = now(),
           updated_by = session_user
     WHERE singleton;
 12. Re-verify with lease_protocol_info_v1(): one row, ('open','ready')  -> 45004
COMMIT
```

Steps 5, 7 and 11 are the only statements in this procedure that write rows, and each is shown above in the exact form it is executed; every other statement is the §6.1, §6.3–§6.6 or §10.2 text verbatim.

Why this ordering is the only correct one:

- **Nothing is updated or locked before it exists.** Steps 3–8 create every object the later steps touch; steps 5 and 7 are `INSERT`s into rows created by steps 4 and 6 in the same transaction. No `UPDATE` and no `SELECT ... FOR UPDATE` in the first-install path targets an object or a row that does not yet exist.
- **No lease row lock is taken.** The lease row is created by this transaction, so no other session can see it and there is nothing to serialize against. Quiescence (§9.2.2 step 4) is meaningless on an empty database and is not attempted here.
- **The first visible state is the final one.** Step 5 writes `barrier_state = 'closed', bootstrap_state = 'installing'`, so the row is never committed in an admitted state; steps 11–12 move it to `open`/`ready` before the same `COMMIT`. A crash before that commit leaves no schema at all rather than a half-installed one.
- **Creation, seeding, function installation, revokes, verification and the transition to `open`/`ready` all occur in this one transaction**, which holds the bootstrap guard `B` throughout. A concurrent first installation blocks on `B`; when it proceeds, step 2 finds the protocol row and continues as §9.2.2 verification instead of creating anything twice.
- **Bootstrap creates no application table and no `alembic_version` row.** The first Alembic run happens later, under the lease, inside the DDL transaction (§7.1). T-01 asserts both facts, from a genuinely empty database.

#### 9.2.2 Verification or upgrade of an existing installation

Ordered steps (all inside one transaction):

```text
connect as strikenova_prod_migrator
BEGIN
  1. SELECT pg_advisory_xact_lock(
         hashtextextended('strikenova:migration_internal:bootstrap', 0));
         -- lock_timeout bounded at session level before BEGIN -> 55P03 on contention
  2. read lease_protocol: absence means "never installed" -> 45005 (§9.2.1)
  3. SELECT ... FROM migration_lease FOR UPDATE                 -> 55P03
  4. live owner check                                           -> 45002
  5. UPDATE lease_protocol SET barrier_state = 'closed'         (idempotent)
  6. VERIFY schema, tables, functions: ownership, shape,
     SECURITY DEFINER, search_path, ACLs, default ACLs,
     singleton rows. This path creates, repairs and drops nothing
                                                    -> 45004 / 45005
  7. version transition checks                          -> 45006
  8. UPDATE lease_protocol SET installed_version, range,
     barrier_state = 'open', bootstrap_state = 'ready';
     UPDATE migration_lease SET protocol_version = <target>
       -- mirror maintenance: the row is quiescent (step 3) and the mirror must
       -- never disagree with the control row after a succeeded upgrade, or the
       -- next acquire would fail its own corruption guard
COMMIT                                                   -- atomic release of both locks
```

- The advisory lock is **transaction-scoped** (`pg_advisory_xact_lock`), so it cannot leak on crash; PostgreSQL releases it at commit, rollback or backend death.
- The fixed key (`hashtextextended(...)`) is stable across protocol versions, so it serializes cold starts even when releases differ.
- `lock_timeout` bounds the wait: `55P03` means another bootstrap is running. The caller retries within the same monotonic budget used by `acquire`, then fails startup.
- Step 4 requires quiescence: an owner whose lease is still live is never hijacked by an upgrade. Bootstrap waits or fails with `45002`; it never terminates a backend itself (§12).
- This path is verification only: every required object must already exist and match §9.3, and a missing object fails `45005` instead of being created. Repairing a broken installation is a deliberate operator action, not something bootstrap does silently.
- Step 5 happens **before** any version transition, so no worker can admit against a half-installed protocol. Because the same transaction also sets `open` at step 8, the closed window is invisible to a worker that was blocked on the row lock.
- A failure anywhere rolls the whole bootstrap back. If the operator had pre-closed the barrier, it stays closed (fail closed) and the application stays down until a bootstrap mode succeeds — this is the recovery path in §11.4.5, not a deadlock.
- `bootstrap_state` is written only inside these transactions and is therefore never visible to another session; it is diagnostic, not a lock
- **`--close-barrier` is exempt from steps 3, 4 and 8.** It has its own check order in §11.4.4 precisely because a live owner holds the lease row lock for the whole DDL transaction: taking that lock here would fail with `55P03` exactly when the operator needs the drain window (executed: §14.1 V-11, V-12). It writes `closed` and commits; only a later install/verify or `--upgrade-protocol` reopens.

### 9.3 Idempotent installation is verified, not assumed

`CREATE SCHEMA IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` is **not** sufficient: it silently accepts an object of the wrong shape, owner or ACL. After the create phase, bootstrap runs a verification phase and fails closed on any mismatch.

Expected state:

| Check | Expected |
|---|---|
| Schema owner | `strikenova_prod_migrator` |
| Table owner | `strikenova_prod_migrator` |
| Table `relkind` | `r` |
| Columns | exactly the documented names, types, nullability |
| Primary key | `migration_lease(lease_name)`, `lease_protocol(singleton)` |
| Check constraints | present with the documented expressions |
| Row security | disabled (`relrowsecurity = false`) |
| Function owner | `strikenova_prod_migrator` |
| `prosecdef` | `true` for all four functions |
| `proconfig` | exactly `{"search_path=pg_catalog"}` for all four |
| Function signatures | exactly the documented identity arguments and result types |
| ACLs | no `PUBLIC` entry; no runtime-role entry (see §10) |
| Protocol row | exactly one row; `installed_version` = expected; `bootstrap_state = 'ready'` |
| Lease row | exactly one row with `lease_name = 'migration-lease'` |

Verification queries:

```sql
-- Schema / table ownership and shape
SELECT n.nspname, n.nspowner::regrole
  FROM pg_namespace n WHERE n.nspname = 'migration_internal';

SELECT c.relname, c.relkind, c.relowner::regrole, c.relrowsecurity, c.relacl
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'migration_internal' AND c.relkind = 'r';

SELECT table_name, column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_schema = 'migration_internal'
 ORDER BY table_name, ordinal_position;

SELECT conrelid::regclass, conname, contype, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE connamespace = 'migration_internal'::regnamespace;

-- Functions
SELECT p.proname,
       pg_get_function_identity_arguments(p.oid) AS args,
       pg_get_function_result(p.oid)             AS result,
       p.proowner::regrole,
       p.prosecdef,
       p.proconfig,
       p.proacl
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'migration_internal'
 ORDER BY p.proname;
```

Any discrepancy fails bootstrap with `45004` (configuration error) or `45005` (missing object), and the transaction rolls back. Bootstrap never repairs silently.

### 9.4 Default ACL inspection

Object ACLs are not the whole story. PostgreSQL applies defaults at creation time:

- `pg_default_acl` with `defaclnamespace = 0` holds **global** defaults for the creating role;
- rows with a specific namespace apply only to objects created in that schema;
- global defaults can grant `EXECUTE ON FUNCTIONS` to the runtime role for *every* future function created by the migrator, including functions in `migration_internal`.

Bootstrap inspects both scopes:

```sql
SELECT d.defaclrole::regrole AS creator,
       CASE WHEN d.defaclnamespace = 0 THEN '<global>'
            ELSE d.defaclnamespace::regnamespace::text END AS scope,
       d.defaclobjtype,
       d.defaclacl
  FROM pg_default_acl d
 WHERE d.defaclrole = 'strikenova_prod_migrator'::regrole
   AND (d.defaclnamespace = 0
        OR d.defaclnamespace = 'migration_internal'::regnamespace);
```

Fail-closed rule: if any default ACL entry would grant any privilege on functions, tables or sequences to `PUBLIC` or to the runtime role, bootstrap **fails** with `45004` and reports the entry. It does not silently `ALTER DEFAULT PRIVILEGES`: default ACLs are shared configuration whose modification is an explicit operator/founder decision, and bootstrap is forbidden from issuing grants.

This is the specific risk that "moving objects out of `public`" does not address. Creating a new schema removes PUBLIC's schema `USAGE` (there is no default grant on a new schema), but it does **not** remove the default `EXECUTE` grant to `PUBLIC` on newly created functions, and it does not remove global default ACLs.

### 9.5 Fail-closed conditions

Bootstrap fails (transaction rolls back, no partial state) when:

- the schema/table/function owner is not the expected migration role;
- an object exists with unexpected columns, types, constraints, signature, `prosecdef` or `proconfig`;
- `PUBLIC` or the runtime role holds any privilege on `migration_internal` objects, directly or through a default ACL;
- `installed_version` is neither this release's expected version nor an upgrade source explicitly permitted by `--upgrade-protocol` (a normal startup never creates a new protocol implicitly, §11.2);
- more than one lease row or protocol row exists;
- the advisory lock cannot be acquired within the bounded timeout;
- the runtime role exists and has effective access (schema `USAGE`, table privileges, function `EXECUTE`) or transitive membership in the migrator/owner role.

If the runtime role does not exist yet (clean database before provisioning), bootstrap records that fact and proceeds; the post-provisioning verification task must re-run the effective-privilege checks (T-15).

---

## 10. Privilege boundary — ADR-018

### 10.1 Requirement

The runtime application identity has **zero effective access** to `migration_internal`:

- no schema `USAGE`;
- no table privileges;
- no function `EXECUTE`;
- no ownership, and no direct or inherited membership that provides access.

`PUBLIC` has no access either.

### 10.2 Revoke set (reapplied after every create)

```sql
REVOKE ALL ON SCHEMA migration_internal FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA migration_internal FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA migration_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM PUBLIC;

REVOKE ALL ON SCHEMA migration_internal FROM strikenova_production_app;
REVOKE ALL ON ALL TABLES    IN SCHEMA migration_internal FROM strikenova_production_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA migration_internal FROM strikenova_production_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM strikenova_production_app;
```

Ordering is mandatory: PostgreSQL grants `EXECUTE` on a new function to `PUBLIC` **by default**, so a revoke executed before `CREATE FUNCTION` is useless. The revoke set must run after the last object is created, inside the same bootstrap transaction, and the verification phase must confirm the result.

No `GRANT` appears anywhere in the bootstrap. The migration role executes the functions because it owns them.

### 10.3 Verify effective privileges, not just direct grants

Direct ACL inspection is necessary but not sufficient. Verification must cover:

1. **Schema defaults and global defaults** — §9.4.
2. **Role memberships and inheritance** — a runtime role that is a member of the migrator (or of a role the migrator is a member of) inherits access even with an empty ACL:

```sql
WITH RECURSIVE reach(roleid, member_id, depth) AS (
    SELECT m.roleid, m.member, 1
      FROM pg_auth_members m
     WHERE m.member = 'strikenova_production_app'::regrole
    UNION ALL
    SELECT m.roleid, m.member, r.depth + 1
      FROM pg_auth_members m JOIN reach r ON m.member = r.roleid
     WHERE r.depth < 10
)
SELECT roleid::regrole AS grants_access_to, member_id::regrole, depth FROM reach;

SELECT rolname, rolinherit, rolsuper, rolbypassrls
  FROM pg_roles WHERE rolname IN ('strikenova_production_app','strikenova_prod_migrator');
```

A runtime role must not reach the migrator/owner role at any depth, and must not be `SUPERUSER` or `BYPASSRLS`. `pg_auth_members.member` is the PostgreSQL 16 column name; the pre-16 spelling `memberid` fails on 16 with `42703` (executed: §14.1 V-17).

3. **Effective privilege functions** (these account for ownership, membership and `PUBLIC`):

```sql
SELECT has_schema_privilege('strikenova_production_app','migration_internal','USAGE') AS schema_usage;

SELECT c.relname, p.priv,
       has_table_privilege('strikenova_production_app', c.oid, p.priv) AS effective
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) AS p(priv)
 WHERE n.nspname = 'migration_internal' AND c.relkind = 'r'
 ORDER BY c.relname, p.priv;

SELECT c.relname,
       has_sequence_privilege('strikenova_production_app', c.oid, 'USAGE')  AS seq_usage,
       has_sequence_privilege('strikenova_production_app', c.oid, 'SELECT') AS seq_select
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'migration_internal' AND c.relkind = 'S';

SELECT p.proname,
       has_function_privilege('strikenova_production_app', p.oid, 'EXECUTE') AS effective
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'migration_internal'
 ORDER BY p.proname;
```

Table privileges are checked **per privilege**, not as one `'SELECT,INSERT'` string: `has_table_privilege` accepts a comma-separated list and answers "true if the role has any of these", which would report a single leaked privilege as `true` and is exactly the wrong question for a zero-access requirement. Every table row (both tables × four privileges), every sequence row and every function row must come back `false`, and so must `schema_usage`.

`has_function_privilege` takes the **identity argument** types only — `float8` is the canonical spelling of `double precision`, and result columns, OUT names and parameter names play no part in the lookup. The query above passes `p.oid`, which is exact by construction and immune to spelling; the literal form `'migration_internal.lease_acquire_v1(integer,text,float8,integer)'` resolves to the same function (executed: §14.1 V-15). No lease function declares a default argument, so both forms match the declarations in §6 exactly.

4. **`PUBLIC` access**, which `has_*_privilege` cannot express directly, is checked by expanding the effective ACL:

```sql
SELECT c.relname,
       a.grantee, a.privilege_type, a.is_grantable
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
 WHERE n.nspname = 'migration_internal' AND a.grantee = 0;   -- 0 = PUBLIC

SELECT p.proname,
       a.grantee, a.privilege_type, a.is_grantable
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
 WHERE n.nspname = 'migration_internal' AND a.grantee = 0;
```

Both must return zero rows. `acldefault(...)` matters: a `NULL` ACL means "the default", not "no privileges", and expanding it is what exposes the implicit `PUBLIC` `EXECUTE` grant on functions.

5. **Direct attempts** as the runtime role are still exercised in T-15 (`SELECT`, `UPDATE`, `SELECT … FOR UPDATE`, function calls) and must each fail with `42501`.

### 10.4 Safe routine configuration

- `SECURITY DEFINER` on all four functions, owned by the migration role.
- `SET search_path = pg_catalog` on each function, with every object reference schema-qualified. `pg_temp` is deliberately excluded so a temp-schema object cannot shadow a referenced name.
- No dynamic SQL in the functions; no `EXECUTE format(...)`.
- Function bodies read exactly one GUC, the caller's `lock_timeout`, saved and restored around the row lock (§6.9); they never derive identity, ownership or the barrier from a GUC, which is what makes GUC forgery irrelevant rather than merely mitigated.

---

## 11. Deployment, protocol versioning and compatibility barrier

### 11.1 Initialization before Alembic

On a clean Neon database the order is:

1. operator provisions the database and the three roles (separate authorized task);
2. operator runs `python -m app.migrate bootstrap-lease` as the migration identity — creates `migration_internal`, the lease row and protocol row, and verifies the catalog;
3. the application starts: `acquire` → Alembic DDL transaction → backfill → `release`.

Alembic never creates `migration_internal`, and bootstrap never creates application tables. If step 2 is skipped, `acquire` fails with `45005` and the application does not start.

### 11.2 One authoritative protocol-version state

There is exactly one authoritative record and one mechanism that enforces it.

| Value | Role |
|---|---|
| `lease_protocol.installed_version` | **Authoritative.** Read by every function of every protocol version, under the advisory guard `M` and the lease row lock. |
| `lease_protocol.min_supported_version` / `max_supported_version` | **Authoritative range.** A caller's protocol must lie inside it even when it equals `installed_version`. |
| `lease_protocol.bootstrap_state` | **Authoritative readiness.** Anything other than `ready` refuses admission (`45005`). |
| `migration_lease.protocol_version` | **Mirror only.** Written by the acquiring release for diagnosis; a disagreement with `installed_version` is itself a `45006`. It never authorizes admission, entry or release. |
| `lease_protocol_info_v1()` | **Preflight convenience only.** It takes no lock and returns a value that may be stale by the next statement. |

- The application pins `EXPECTED_PROTOCOL_VERSION` (1 for this design) and `SUPPORTED_PROTOCOL_MIN/MAX`.
- The preflight read of `lease_protocol_info_v1()` (§11.4.2 step 4) exists only to fail fast with a clear message. **It is not sufficient and is not relied on.** Every admission and entry path re-reads `installed_version`, the supported range and `bootstrap_state` from the control row while holding `M` and the lease row lock (§6.3, §6.4, §6.5), so a process whose preflight succeeded can still be refused later by SQL that demonstrably checks the authoritative value.
- `installed < expected` → `45006`: the database needs the approved protocol upgrade; the release must not create the new protocol implicitly during a normal startup.
- `installed > expected` → `45006`: this release is older than the database; refuse to run.
- `installed` outside `min_supported_version..max_supported_version` → `45006` for every operation.
- A protocol upgrade writes `installed_version` and the range **while holding the lease row lock** (§9.2.2 step 8), and every other reader of that row is under the same lock, so no session can read a half-updated version record.

**Stale-process regression.** A release of protocol 1 passes its preflight, pauses (GC, container freeze, operator intervention), the database is upgraded to protocol 2, and the release resumes. Because `lease_acquire_v1` checks `installed_version = 1` under the guard, it fails `45006` and writes nothing; because `lease_enter_v1` performs the same check, a process that somehow acquired before the upgrade cannot enter afterwards either. No protocol-1 lease and no protocol-1 application write can occur after the upgrade commits. This is T-23.

### 11.3 Active-worker compatibility

A bootstrap advisory lock protects only bootstrap. It does **not** protect a worker that is already executing `lease_enter_v1` when a new release replaces that function. `CREATE OR REPLACE FUNCTION` would change behaviour under a running transaction without any version check.

The design removes that class of failure:

1. **Version-suffixed code.** Protocol 1 owns the `*_v1` functions and the `migration_lease` table. A protocol 2 may install its own `*_v2` functions and, if its row semantics change, its own lease-row table. Nothing in place is ever replaced while a release that uses it may still run. **`migration_internal.lease_protocol` is not version-suffixed** and is never replaced: it is the stable control object every version reads (§6.1).
2. **Retirement is an explicit operator step**, performed only after evidence that no release using the old objects is active. It is never part of a normal startup and never part of bootstrap for a new protocol.
3. **A stable cross-version guard in the actual migration path.** The advisory guard `M` (§6.10) is acquired inside `lease_acquire_v1`, `lease_enter_v1` and `lease_release_v1` — so every admission, every DDL transaction and every per-user backfill transaction of **every** protocol version holds the same key — and it is taken before the lease row lock. Every version then reads `installed_version`, the supported range and `bootstrap_state` from the non-versioned control row under that guard and the lease row lock, and fails `45006` when the value is not its own. Two incompatible releases therefore cannot both migrate, even when their function names, or their lease-row tables, differ.

### 11.4 Deployment barrier

`barrier_state` is a row, not a convention, and it has exactly one writer class.

#### 11.4.1 The rule

- `barrier_state = 'closed'` blocks **every admission path**. A normal startup that passes the version checks fails with `45004` and changes nothing else in the database.
- **Bootstrap/protocol upgrade is the only operation allowed while `barrier_state = 'closed'`**, and the only writer that can set it back to `open` (§9.2). There is no raw-SQL reopen path, no application path, and no migration revision that writes it.
- Closing the barrier does **not** abort an already-admitted owner. `enter` ignores the barrier by design (6.4); quiescence is enforced by bootstrap's live-owner check, not by the barrier itself.
- If an operator closes the barrier and the rollout is abandoned, the database deliberately stays closed. Recovery is a successful bootstrap mode, never a manual `UPDATE` (§11.4.5).

#### 11.4.2 Check order — normal application startup

| Step | Check | Failure |
|---|---|---|
| 1 | Migration identity configured and different from the runtime identity (before any connection) | `45004` |
| 2 | `migration_internal.lease_protocol_info_v1()` exists and returns exactly one row | `42P01`/`42883` mapped to `45005`; missing row `45005` |
| 3 | `bootstrap_state = 'ready'` | `45005` (bootstrap incomplete) |
| 4 | Preflight read of `lease_protocol_info_v1()`: `bootstrap_state = 'ready'`, `installed_version` equals this release's expected version and is within its supported range. Advisory only — step 6 re-checks the authoritative row | `45006` / `45005` |
| 5 | Fail-fast barrier read: `barrier_state = 'open'` (advisory only — step 6 re-checks) | `45004` |
| 6 | `acquire`: advisory guard → row lock → missing lease row → **authoritative version/range/readiness from `lease_protocol`** → mirror check → **barrier re-check under the row lock** → live owner | `55P03` / `45005` / `45006` / `45004` / `45002` |
| 7 | `enter` in each protected transaction: lock → protocol → owner → expiry | `55P03` / `45005` / `45006` / `45001` / `45002` |
| 8 | `release` after the work: lock → protocol → owner | `55P03` / `45005` / `45006` / `45001` (idempotent `false` if unowned) |

Steps 4 and 5 fail fast to avoid a pointless retry budget, but neither authorizes anything: both are reads that take no lock and may be stale one statement later. Step 6 is the authoritative check, because it runs inside the same transaction that transfers ownership, under the advisory guard and the lease row lock. A version change or a barrier close that happens between step 5 and step 6 is therefore still caught. Bootstrap writes the protocol row only while holding that same lease row lock (§9.2.2), so `acquire`'s reads cannot observe a half-written state.

#### 11.4.3 Check order — bootstrap / protocol upgrade (the exempt path)

| Step | Check | Failure |
|---|---|---|
| 1 | Transaction-scoped advisory lock at the fixed key, bounded by `lock_timeout` | `55P03` |
| 2 | Protocol row read; absence means "never installed" and is not this path's business | `45005` (§9.2.1) |
| 3 | Lease row `SELECT … FOR UPDATE` | `55P03` |
| 4 | Quiescence: no live owner (`owner_token_hash IS NULL OR expires_at <= clock_timestamp()`) | `45002` |
| 5 | Set `barrier_state = 'closed'` (idempotent) | — |
| 6 | Verify objects — this path creates nothing; objects are created only by the first-installation procedure (§9.2.1). Verify ownership, shape, `prosecdef`, `proconfig`, ACLs, default ACLs | `45004` / `45005` |
| 7 | Version transition: target must equal this binary's **expected** protocol version, must be `>= installed_version` (no downgrade), and must lie within this binary's supported range — not the range already stored in the row, which step 8 replaces | `45006` |
| 8 | Set `installed_version`, supported range, `barrier_state = 'open'`, `bootstrap_state = 'ready'`, and update the `migration_lease.protocol_version` mirror | — |
| 9 | `COMMIT` — releases the row lock and the advisory lock atomically | — |

Ordering is the deadlock fix: step 5 executes **before** any version transition, step 8 executes **in the same transaction** as step 5, and step 4 guarantees no admitted worker is mid-flight. A closed barrier can therefore always be reopened by this path, and only by this path.

#### 11.4.4 Check order — `bootstrap-lease --close-barrier`

`--close-barrier` is the one mode that must succeed **while an admitted owner is live**, so it must not take the lease row lock. An owner holds that lock for the whole DDL transaction and for every per-user transaction (§7.1, §7.3); a `SELECT … FOR UPDATE` here would therefore return `55P03` (or hang forever with `lock_timeout = 0`) at exactly the moment the drain window is requested. Executed evidence: §14.1 V-11 (documented order fails `55P03`) and V-12 (this order completes in 0.00 s).

| Step | Check | Failure |
|---|---|---|
| 1 | Effective identity is the migration identity that owns `migration_internal`, checked before any write | `42501` from PostgreSQL; `45004` if the metadata is readable but the identity is not the expected one |
| 2 | Session `lock_timeout` is bounded and non-zero (`> 0`); `0` means "wait forever" and is refused before anything else | `45004` |
| 3 | `pg_advisory_xact_lock(hashtextextended('strikenova:migration_internal:bootstrap', 0))`, bounded by that `lock_timeout` — serializes with every other bootstrap mode | `55P03` |
| 4 | `lease_protocol` contains exactly one row. A missing row means "never bootstrapped", not "close it" | `45005`; two or more rows `45004` |
| 5 | `SELECT … FROM migration_internal.lease_protocol WHERE singleton FOR UPDATE` — the protocol row, **not** the lease row. Every documented writer of this row also holds the step-3 advisory lock, so this is the explicit backstop against a future or manual writer; bounded | `55P03` |
| 6 | Probe the lease row without waiting: `SELECT … FOR UPDATE NOWAIT` inside a `SAVEPOINT`, immediately followed by `ROLLBACK TO SAVEPOINT`, so the probe is an observation and never retains a lock. Success proves no protected transaction holds the row lock at that instant; `55P03` proves one does, and is caught and reported rather than raised. A live owner is reported and **not** refused | none — `55P03` at this step is caught; `45002` is never raised by this mode |
| 7 | `UPDATE migration_internal.lease_protocol SET barrier_state = 'closed', updated_at = now()`, idempotent when already closed | none |
| 8 | `COMMIT` — releases the protocol row lock and the advisory lock together | none |

Executed form of steps 2–8 (step 1 is the operator's identity check before connecting):

```sql
SET lock_timeout = '10s';
BEGIN;
SELECT pg_advisory_xact_lock(
    hashtextextended('strikenova:migration_internal:bootstrap', 0));            -- step 3
SELECT 1 FROM migration_internal.lease_protocol WHERE singleton FOR UPDATE;     -- step 5
SAVEPOINT close_probe;
SELECT owner_fingerprint, expires_at                                            -- step 6
  FROM migration_internal.migration_lease
 WHERE lease_name = 'migration-lease'
   FOR UPDATE NOWAIT;                          -- 55P03 here => 'held', reported
ROLLBACK TO SAVEPOINT close_probe;                                             -- stay open
UPDATE migration_internal.lease_protocol                                        -- step 7
   SET barrier_state = 'closed', updated_at = now()
 WHERE singleton;
COMMIT;                                                                         -- step 8
```

`ROLLBACK TO SAVEPOINT close_probe` runs unconditionally after the probe: it keeps the outcome identical whether the probe succeeded (releasing the lease row lock it took) or failed (recovering from `55P03` without aborting the transaction). The barrier write is therefore never performed inside a failed subtransaction, and a probe result can never abort the close.

What each live-owner situation does:

| Situation | Steps 5–8 | Result |
|---|---|---|
| No owner, barrier `open` | 5 → 8 | Barrier `closed`; exit 0 |
| Barrier already `closed` | 7 rewrites the same value | Exit 0, "already closed"; no error and no state change |
| **Live owner holds the lease row lock (mid-DDL or mid-backfill)** | 5 locks only the protocol row; 6 reports `lease row lock: held` with the committed fingerprint/`expires_at` and continues | Barrier `closed` in one short transaction; the owner continues via `enter`, which ignores the barrier (§6.4) |
| **Admission in flight — an `acquire` holds the lease row lock and has not yet committed the ownership update** | 6 reports `lease row lock: held` and notes that the holder may be an admission; it cannot tell the two apart from the lock alone, and does not try | Barrier `closed`; exit 0. The in-flight admission linearizes either before the close commit (admitted, becomes a live owner the upgrade path will refuse, §11.4.5) or after it (reads `closed`, fails `45004`) |
| Live owner between transactions (row unlocked) | 6's probe succeeds and is rolled back to the savepoint; reports `free` plus the committed fingerprint and `expires_at` | Barrier `closed`; the owner's next `enter` still succeeds because the token matches |
| Another bootstrap or `--upgrade-protocol` is running (holds the step-3 advisory lock) | 3 waits until `lock_timeout` | `55P03`; retry once that bootstrap commits, or follow §12.4 if it was abandoned |
| A backend left `idle in transaction` holding the protocol row lock (for example an abandoned manual session) | 3 succeeds, 5 waits until `lock_timeout` | `55P03`; diagnose per §12.3, terminate per §12.4 |
| No protocol row (never bootstrapped) | 4 | `45005`; run `bootstrap-lease` install instead |

Properties this order guarantees:

- `--close-barrier` never sets `open` and never creates or repairs objects. A failed or abandoned close leaves the metadata as it was and cannot half-reopen anything.
- It imposes no quiescence. `45002` is not a possible outcome; that check belongs to install/verify and `--upgrade-protocol` (§11.4.3 step 4), which is what stops an upgrade from hijacking a live owner.
- **Linearization point for admission.** `lease_acquire_v1` linearizes at the barrier read it performs while holding the lease row lock (§6.3). That one statement decides the outcome: `open` admits, `closed` fails `45004`. Everything earlier — the preflight read, waiting for `M`, waiting for the row lock — is provisional. Therefore an `acquire` that is still waiting for the row lock when the close commits has not linearized and will read `closed`; an `acquire` that already read `open` has linearized and is admitted, and the close never retroactively aborts an admitted owner (§11.4.1). The close itself linearizes at its own `COMMIT`: before it admissions may still succeed, after it none can.
- **What the close reports when an admission is in flight.** Step 6 distinguishes only "lease row lock free" (`free`, no protected transaction in flight) from "held" (`held`, which may be a live owner transaction **or** an in-flight admission — the lock does not say which). Exit status is 0 in both cases and the barrier closes in both cases. When the report is `held`, the operator does not guess: after the drain window, rerun `--close-barrier`; a run that reports `free` proves that no admission is in flight at that moment. Any admission that committed in between is now a live owner, and the next step refuses it explicitly rather than racing it.
- **The upgrade path never overtakes an admission.** `--upgrade-protocol` takes the lease row `FOR UPDATE` (§11.4.3 step 3), which cannot be granted while an in-flight admission or any owner transaction holds it, and then applies the quiescence check (§11.4.3 step 4). An admission that linearized before the close commit is invisible to the close but visible to the upgrade: it appears as a live owner and the upgrade fails `45002` until that owner releases, expires or is recovered under §12. Protocol replacement therefore cannot proceed until every admitted owner has completed or has been recovered according to the documented procedure.
- Exit status is 0 for a successful close, including the already-closed case. A live owner is logged with fingerprint and `expires_at`, never with the token.

#### 11.4.5 Recovering from a closed barrier

| Situation | Action | Result |
|---|---|---|
| Operator closed the barrier and the rollout was cancelled | Run `bootstrap-lease` (install/verify) at the deployed version | Steps 1–8 run; barrier reopens; app starts |
| Operator closed the barrier and a new protocol was installed but the app was not deployed | Run `bootstrap-lease --upgrade-protocol <installed>`; the target equals `installed_version`, which is permitted | Barrier reopens without a version change |
| An admission committed between `--close-barrier` and the upgrade | Nothing to do before trying the upgrade | The upgrade fails `45002` naming the live owner's fingerprint and `expires_at`; wait for expiry, or recover per §12.4, then rerun. The upgrade cannot proceed past an admitted owner |
| A live owner must be drained before maintenance | Run `bootstrap-lease --close-barrier` (§11.4.4) | Barrier closes immediately even though the owner holds the lease row lock; the owner may finish |
| A live owner still holds the lease when reopening (install/verify or `--upgrade-protocol`) | Quiescence check fails `45002` | Wait for `expires_at` or follow the termination procedure (§12.4); then rerun. Bootstrap never terminates a backend |
| The owner's backend is stuck holding the row lock | Bootstrap fails `55P03` | Diagnose and terminate per §12.4, then rerun |
| Bootstrap itself fails midway | Transaction rolls back; barrier keeps its prior state | Fix the cause and rerun; no half-installed protocol |

`bootstrap-lease --close-barrier` is the only mode that may run while an owner is live, because its purpose is to drain one. It neither waits for quiescence nor takes the lease row lock, so a live owner can never block it (§11.4.4).

#### 11.4.6 Required deployment properties

To be confirmed, not assumed (§17):

- the Render service runs **one** instance during migration windows;
- deploys use stop-then-start ordering, not overlapping old/new instances;
- the pre-deploy/startup command runs bootstrap **before** the web process starts.

If the platform cannot guarantee non-overlap, the barrier plus version checks still prevent concurrent DDL, but old and new releases can alternate, so the barrier must stay `closed` until the rollout is complete and then be reopened by a bootstrap mode.


---

## 12. Liveness and operational recovery

Safety and liveness are different problems:

- **Safety** is guaranteed by ownership verification: a worker that no longer owns the lease cannot commit (the pre-commit `enter` fails).
- **Liveness** is not guaranteed by expiry. A backend that stays alive while holding the row lock blocks every other `acquire`/`enter` on that row until it ends.

### 12.1 What `idle_in_transaction_session_timeout` does and does not do

- It terminates a backend that is **idle inside an open transaction**.
- It does **not** terminate a backend that is actively executing a query, however long that query runs.
- It does not cover a backend blocked on a lock while in `active` state.

Therefore this design does **not** claim that `idle_in_transaction_session_timeout` bounds every stalled transaction. It bounds one specific failure mode (client stalled between statements).

Complementary bounds, each with a different scope:

| Setting | Scope | Failure it bounds |
|---|---|---|
| `lock_timeout` | per lock wait | indefinite waiting for the lease row lock |
| `statement_timeout` | per statement | a single runaway query |
| `idle_in_transaction_session_timeout` | per idle-in-transaction period | a client that stops sending statements |
| lease `expires_at` | ownership eligibility | how soon a *new* worker may take over **after** the lock is free |

### 12.2 Crash vs. live-but-stuck

| Case | Database behavior | Recovery |
|---|---|---|
| Process killed (`SIGKILL`), host lost, TCP connection dropped | PostgreSQL detects the dead client, terminates the backend, rolls back, releases the row lock | After `expires_at`, `acquire` takes over automatically; no operator action beyond the platform restart |
| Connection alive, backend idle in transaction | Row lock held; `idle_in_transaction_session_timeout` eventually terminates it (if configured) | Automatic after termination; alert on the timeout event |
| Connection alive, long-running query | Row lock held; no timeout necessarily applies | Operator decision (12.4) |
| Backend blocked on a lock | Row lock held by its own transaction; not progressing | Diagnose with `pg_blocking_pids()`; terminate the true blocker if abandoned |

### 12.3 Identifying a stuck backend

```sql
SELECT a.pid, a.usename, a.state, a.wait_event_type, a.wait_event,
       a.xact_start, a.query_start, now() - a.xact_start AS xact_age,
       a.backend_xid, a.backend_xmin,
       pg_blocking_pids(a.pid) AS blocked_by,
       left(a.query, 200) AS query_head
  FROM pg_stat_activity a
 WHERE a.datname = current_database()
   AND (a.usename = 'strikenova_prod_migrator' OR a.xact_start IS NOT NULL)
 ORDER BY a.xact_start NULLS LAST;

SELECT l.pid, l.locktype, l.mode, l.granted, l.relation::regclass
  FROM pg_locks l
 WHERE l.relation = 'migration_internal.migration_lease'::regclass;
```

Alerting inputs (no token material):

- lease `expires_at` passed while a backend still holds the row lock;
- `pg_stat_activity` shows a migration-identity transaction older than the TTL;
- repeated `45002` from startup (possible stuck owner);
- any `45001` (a worker lost ownership — should be rare and is always worth investigating);
- bootstrap `55P03` repeated beyond the budget.

### 12.4 When terminating a backend is justified

Terminate (`pg_terminate_backend(pid)`) only when all of the following hold:

1. the backend is the recorded lease owner (or its blocker), identified by pid and `xact_start`;
2. its transaction has exceeded the TTL plus a documented grace period;
3. the owning process is known to be gone or hung (platform shows no live instance, or the process is wedged);
4. the operator has recorded the pid, the fingerprint and the timestamps.

Terminating the wrong backend can interrupt an unrelated transaction. The row lock is released by the server on termination, after which the rollback makes the lease eligible for takeover once `expires_at` has passed.

### 12.5 Separation of responsibilities

- **Application:** fails closed, never retries `enter`, never attempts takeover, never logs tokens.
- **Platform:** restarts failed instances; does not "fix" the database.
- **Operator:** runs bootstrap/upgrade, inspects `pg_stat_activity`, decides on termination, owns the barrier.
- **Founder:** approves the TTL, the barrier procedure and any residual availability risk (D-7).

---

## 13. Alembic integration

### 13.1 Transaction arrangement

The lease row lock and the Alembic DDL must use the **same connection and the same transaction**. The arrangement:

```python
conn = migration_engine.connect()
try:
    conn.exec_driver_sql("SET lock_timeout = '10s'")   # session level, before the transaction
    conn.commit()                                       # end SQLAlchemy's implicit transaction
    tx = conn.begin()                                   # the protected transaction starts here
    guard.enter(conn)                                   # FIRST statement in the transaction
    cfg = Config("alembic.ini")
    cfg.attributes["connection"] = conn                  # Connection, not Engine
    cfg.attributes["lease_guard"] = guard
    command.upgrade(cfg, "head")
    guard.enter(conn)                                   # refresh immediately before commit
    tx.commit()
except BaseException:
    tx.rollback()
    raise
```

`env.py` changes required by the implementation:

- accept `config.attributes["connection"]` (a `Connection`) and use it directly; when only `config.attributes["connectable"]` (an `Engine`) is present, keep today's `connect()` behaviour;
- do **not** call `connection.connect()` on a `Connection`;
- when the dialect is PostgreSQL and `config.attributes["lease_guard"]` is absent, raise before configuring the context.

### 13.2 External transaction and `context.begin_transaction()`

Alembic 1.15.2's `MigrationContext.configure()` records whether the supplied connection is already in a transaction (`_in_external_transaction`), and `context.begin_transaction()` then yields a no-op context instead of issuing `BEGIN`/`COMMIT`. That is what keeps `run_migrations()` inside the caller's transaction.

This behaviour is **not assumed silently**: acceptance tests T-18/T-20 assert it against the pinned version. If it does not hold for 1.15.2, `env.py` must skip `begin_transaction()` when the connection is already in a transaction. Either way, exactly one `COMMIT` may exist on that connection, and it is the caller's.

### 13.3 Supported and prohibited operator paths

- **Supported:** `python -m app.migrate upgrade head` — acquires the lease, enters, runs Alembic, refreshes, commits, releases.
- **Prohibited:** bare `alembic upgrade head` against PostgreSQL. `env.py` raises (exit non-zero) before any DDL, because there is no lease admission and no way to prove ownership. This must include `alembic -x ...` and `command.upgrade()` called without a guard.
- **Prohibited without a new design:** `op.get_context().autocommit_block()`, `CREATE INDEX CONCURRENTLY`, `ALTER TYPE ... ADD VALUE` outside a transaction, and any other operation that cannot run inside the DDL transaction. The current chain contains none (E10); a future migration that needs one requires a separate locking design, because it would commit outside the lease-held transaction.

### 13.4 Failure behavior

| Failure point | Result |
|---|---|
| `acquire` exhausted or denied | No connection state changed beyond the rolled-back attempts; startup exits non-zero |
| `enter` before DDL fails | Transaction rolls back; no DDL; startup exits non-zero |
| A revision raises | Whole DDL transaction rolls back, including `alembic_version`; release still attempted as cleanup |
| `enter` before commit fails | Whole DDL transaction rolls back; startup exits non-zero; schema remains at the previous revision |
| `release` fails after a successful commit | Migration remains committed; cleanup failure logged; startup fails so the operator sees it. The lease stays recorded as owned — the failed call rolls back without clearing the owner — so the next worker is refused `45002` until expiry or recovery (§6.8, T-19) |

---

## 14. Acceptance-test design (PostgreSQL 16)

**Status: designed and partially exercised. §14.1 records a limited, adapted execution of part of this document's SQL against a disposable PostgreSQL 16.16 instance — 83 checks including the first-install, stale-version, cross-version, close-barrier-race, privilege, sequence and failed-release checks — but the T-01…T-24 matrix below has not been run as written.** Database-locking and privilege behavior must be exercised against a real PostgreSQL 16 server; SQLite tests cannot substitute. The existing GitHub workflow that provisions a disposable `postgres:16` service is the intended host (PR #146 already contains such a harness for its own scenarios; this matrix supersedes its lease expectations and must be re-authored during implementation).

Conventions below: `W1`, `W2` are separate processes/connections using the migration identity and distinct 256-bit tokens; `APP` is the runtime role; "row state" means `(owner_token_hash IS NULL, expires_at, generation)`. "Regression signal" is what a future failure would look like.

| # | Scenario and setup | Concurrent actors | Expected database state | Expected SQLSTATE / exception | Regression signal |
|---|---|---|---|---|---|
| T-01 | **First installation.** Genuinely empty database (no `migration_internal`, no `alembic_version`, fresh `public`); execute the §9.2.1 procedure statement by statement, capturing the ordered statement log | one | schema `migration_internal` exists, owned by migrator; 2 tables, 4 functions, 1 protocol row `('open','ready',installed=1,range=1..1)`, 1 unowned lease row; `alembic_version` absent; **the statement log contains no `UPDATE` and no `SELECT … FOR UPDATE` on `lease_protocol`/`migration_lease` before their `CREATE`** | none | any statement reading or updating an object before it is created (`42P01`/`42703` in the log), objects missing, wrong owner, `PUBLIC` ACL present, app tables or `alembic_version` created by bootstrap, or a committed intermediate state (`installing`/`closed`) |
| T-02 | **First installation, twice concurrently.** Two empty-database runs of §9.2.1 start at the same moment | W1, W2 | exactly one schema, one protocol row, one lease row; the loser blocks on the bootstrap guard `B` and then continues as §9.2.2 verification — no second `CREATE`, no `duplicate_object`; both exit 0, or one exits `55P03` and succeeds on retry | none (after retry) or `55P03` | duplicate rows, `duplicate_object`, partially installed protocol, or a second run that re-creates an existing object |
| T-03 | Bootstrapped DB, unowned lease | W1, W2 simultaneously call `acquire` | one owner; `generation` = 1; `owner_fingerprint` = winner's fingerprint | loser: `45002` (and `55P03` if it lost the lock race) | both succeed, `generation` = 2, two different fingerprints in logs |
| T-04 | W1 owns; open one transaction | W1 calls `enter` twice in that tx; W2 calls `enter` in its own tx | expiry refreshed twice; row lock held by W1 until commit; after commit W2's `enter` succeeds | W2 while W1 open: `55P03` (with short `lock_timeout`) | W2 blocks indefinitely, or both hold "ownership" |
| T-05 | W1 owns; GUC forgery | W2 sets `app.migration_owner`, `strikenova.lease_token`, `role`, `session_authorization` to W1-like values, then calls `enter` with its own token | row owner unchanged; W1 still works | W2: `45001` | W2 succeeds, or any function reads `current_setting` |
| T-06 | W1 owns; W2 has its own valid-format token | W1, W2 | row owner unchanged; no write by W2 | W2 `enter`: `45001`; W2 `release`: `45001`; W2 `acquire` (unexpired): `45002` | W2 modifies the row |
| T-07 | W1 holds an open transaction holding the row lock | W2 calls `enter` with `p_lock_timeout_ms = 1000` | unchanged; W1's tx still open | W2: `55P03` within ~1 s; no internal retry | W2 waits beyond timeout or succeeds |
| T-08 | Bootstrapped DB; a slow test revision (e.g. `SELECT pg_sleep(3)`) | W1 and W2 start `upgrade head` | one DDL transaction commits; `alembic_version` has exactly one row and one head; the slow revision applied once | W2: `45002`/`55P03` then success after W1 commits; no second application | DDL applied twice, two heads, or `alembic_version` rewrite |
| T-09 | 3 users with legacy positions | W1 and W2 start backfill | no duplicate `strategy_leg_exposures`; each user processed inside a transaction that held the lease row lock; per-user commits visible | none for the winner; loser `45001`/`45002` after takeover | rows for the same user written concurrently by two transactions; duplicate `(user_id, order_id)` |
| T-10 | 3 users; instrument `enter` calls per transaction | W1 | each user transaction shows exactly 2 `enter` calls: first statement and last statement before commit | none | any user transaction with 1 or 0 `enter` calls |
| T-11 | W1 opens a user transaction and sleeps past TTL | W1, W2 | W1's pre-commit `enter` fails → that user's writes roll back; row lock released; after expiry W2 takes over | W1: `45002`; W2 `acquire`: `45002` until expiry, then success | W1 commits after expiry, or W2 takes over while the lock is held |
| T-12 | W1 owns and is inside a user transaction | kill W1's backend (`pg_terminate_backend` or process `SIGKILL`) | transaction rolled back; row lock released; lease row still owned until expiry; after expiry W2 acquires and completes idempotently | W1 connection: `57P01`/`08006`; W2 before expiry `45002` | lease stuck forever, partial writes, duplicate rows after takeover |
| T-13 | W1 commits user 1, then is killed before user 2 | W1, W2 | user 1 rows present once; after expiry W2 reprocesses users idempotently; final ledger exact | W2 before expiry `45002`, after expiry success | duplicate rows or missing user 2 |
| T-14 | `installed_version = 2`; release expects 1; separately `barrier_state='closed'` with a normal startup | W1 | no DDL, no lease change, no backfill; barrier row still `closed` | `45006` for version; `45004` for closed barrier (from step 5 and again from `acquire`) | any write before the version/barrier check, or `acquire` ignoring the closed barrier |
| T-15 | Roles provisioned; `APP` exists | APP session | `has_schema_privilege`/`has_table_privilege`/`has_function_privilege` all false; `aclexplode(... grantee = 0)` empty; direct `SELECT`/`UPDATE`/`SELECT … FOR UPDATE`/function call all denied | `42501` on each direct attempt | any effective privilege, or `PUBLIC` entry |
| T-16 | Alembic chain applied by migrator; §8.3 revokes (Topology A) or grants (Topology B) applied. Run the §8.3 verification queries and then perform the actual backfill DML | migrator session | Effective matrix matches the topology: Topology A — `SELECT` true on all three tables, `INSERT` true only on `strategy_leg_exposures`, `UPDATE`/`DELETE` false on all three, `seq_usage` true, `seq_select` false. Topology B — the same matrix, produced by grants alone, with every unlisted privilege false. `INSERT … RETURNING id` succeeds; `UPDATE`/`DELETE` fail `42501` | none for required operations; `42501` for `UPDATE`/`DELETE` | required DML fails `42501` (incomplete §8.3 verification), `UPDATE`/`DELETE` succeed, the migrator holds broad `ALL`, or the sequence has `SELECT` it does not need |
| T-17 | Bootstrapped DB; run `alembic upgrade head` directly | one | no DDL; `alembic_version` unchanged; lease row unchanged | non-zero exit; runtime error before `context.configure` | migration applied without lease |
| T-18 | Bootstrapped DB; run `python -m app.migrate upgrade head` | one | lease acquired, entered, DDL applied, pre-commit `enter` executed, committed, released; row unowned with `released_at` set | none | any step skipped, or row left owned |
| T-19 | W1 owns the lease and is inside a user transaction. Force `backfill_exposures` to raise (injected error) **and** force the cleanup `release` to fail (revoke `EXECUTE` on `lease_release_v1` mid-run, or hold the lease row lock from a second session with `release`'s 5 s timeout) | one plus a blocker | the user transaction rolls back; the row lock is released by that rollback; **`owner_token_hash`/`owner_fingerprint` are unchanged and `expires_at` is still in the future**; a new worker's `acquire` raises `45002` while that lease is live and succeeds only after expiry | original backfill exception propagates unchanged; `release` failure is logged with fingerprint and SQLSTATE (`42501`/`55P03`/`45001`); no `45002`-clearing side effect | the cleanup exception replaces the original, the process reports success, the failed `release` clears or shortens ownership, or a new worker acquires while the recorded lease is still live |
| T-20 | Database already at `head`; run the supported command | one | `alembic_version` unchanged; backfill idempotent; lease still acquired/entered/committed/released; exactly one commit on the DDL connection | none | DDL re-run, version rewritten, or `begin_transaction()` issuing a second `COMMIT` |
| T-21 | Operator runs `bootstrap-lease --close-barrier` **while W1 is inside its DDL transaction holding the lease row lock**; then a normal startup; then `bootstrap-lease --upgrade-protocol <installed>`; then a normal startup again | operator, W1 | close completes, takes no lease row wait and leaves the lease row untouched (its step 6 probe reports `held`); W1's next `enter` still succeeds; bootstrap upgrade reopens in one transaction; final state `open`, version unchanged, lease unowned | startup while closed: `45004`; close-barrier with a live owner: exit 0, never `45002`/`55P03`; upgrade with a live owner: `45002`; bootstrap success has no error | startup succeeds while closed, or no bootstrap path can set `open`, or close-barrier blocks or returns `55P03` while an owner holds the row lock, or the close reports `free` while a transaction holds the row |
| T-22 | `acquire` and `enter` called with `p_lock_timeout_ms = 0` (and separately `p_ttl_seconds = 0`) | W1 | no lease change | `45004` immediately; the call must **not** wait indefinitely | the call blocks (proving `lock_timeout = 0` reached PostgreSQL) |
| T-23 | **Stale process across an upgrade.** W1 (protocol 1) completes its preflight read of `lease_protocol_info_v1()` and pauses before `acquire`; operator activates protocol 2 (`installed_version = 2`, range `1..2`, barrier reopened) in its own connection; W1 resumes and calls `lease_acquire_v1(1, …)`, then (separately) `lease_enter_v1(1, …)` with a token acquired before the upgrade | W1, operator | lease row exactly as the upgrade left it; `owner_token_hash` not written by W1; no application-table row written by W1 | `acquire`: `45006`; `enter`: `45006` | W1 acquires or enters with protocol 1 after the upgrade, or writes any application row |
| T-24 | **Cross-version concurrency.** Protocol 2 function set installed alongside protocol 1 (separate `*_v2` names, same advisory guard key and same control row). W1 (v1) holds an open protected transaction; W2 (v2) calls its `enter` on a second connection, and the reverse order is repeated | W1, W2 | only one protected transaction runs at a time; the loser waits on the advisory guard and fails `55P03` at its `lock_timeout`; no deadlock (`40P01`) is reported by either session | loser: `55P03`; after the barrier/version change: `45006` | both transactions run concurrently, either session reports `40P01`, or the guard is not held by one of the two versions |

Additional assertions applied to every test:

- the raw token never appears in captured logs, exception text or the JUnit XML;
- the connection used for `enter` is the same connection used for the writes being protected;
- `owner_fingerprint` in logs matches the lease row while owned;
- no test leaves the lease row owned (except T-11/T-12, which assert the recovery end state).

### 14.1 Executed evidence — disposable PostgreSQL 16.16 (2026-11-20)

**Scope.** A local, disposable PostgreSQL 16.16 cluster (`server_version_num = 160016`) on a Unix socket: `strikenova_prod_migrator` (owner), `strikenova_production_app` (runtime) and two throwaway sequence-test roles. No network, no Neon, no application. Every statement executed was **extracted from this document's fenced blocks** (§6.1, §6.3–§6.6, §8.3, §10.2, §10.3, §11.4.4) or, for §9.2.1, from the literal `INSERT`/`UPDATE` statements printed in its step list; nothing was retyped except the two procedures' scaffolding and the simulated `*_v2` names (see Limitations). Two databases were used, each created empty: `freshinstall` (never touched before step 1 of §9.2.1) and `leasetest`.

**83 checks, 83 passed, exit status 0.**

This is not Neon, not a provisioned role set, not an application run. U3, U4, U5 and U7 remain open.

| # | Check | Observed |
|---|---|---|
| V-1 | The four `CREATE FUNCTION` statements compile as written (`%ROWTYPE`, `out_` OUT parameters, `RETURNS TABLE`) | compiled; `lease_protocol_info_v1()` returned `(1,1,1,'open','ready')` |
| V-2 | `acquire` happy path | fingerprint `sha256(token)[:12]`, `generation = 1` |
| V-3 | `enter` twice in one transaction | refresh, no deadlock, `generation` unchanged |
| V-4 | Live lease / wrong token on `enter` / `enter` unowned / wrong token on `release` / `release` twice | `45002` / `45001` / `45002` / `45001` / `false` |
| V-5 | TTL `0`, negative, `NaN`, `Infinity`, `86400.5`; then `86400` | `45004` five times; `86400` accepted |
| V-6 | `p_lock_timeout_ms` `0`, `-1`, `600001` | `45004` three times |
| V-7 | Contended `acquire` with `p_lock_timeout_ms = 250` | `55P03` after 0.25 s, no internal retry |
| V-8 | Caller `SET LOCAL lock_timeout = '1234ms'` then `acquire` | `1234ms` still set afterwards — no leak into later DDL waits |
| V-9 | Session `lock_timeout = 0` with a contended `acquire` | still waiting after 2 s, completed when the lock was released |
| V-10 | Barrier `closed`: new `acquire`; admitted owner `enter` | `45004`; `enter` still succeeded |
| V-11 | §9.2.2 step 3 (`lease … FOR UPDATE`) against a live owner holding the row lock | `55P03` — the shared order cannot start a drain window |
| V-12 | §11.4.4 order against the same live owner | close completed in 0.00 s; the owner was untouched |
| V-13 | Default `PUBLIC` `EXECUTE` before the revoke step | `has_function_privilege(runtime, …, 'EXECUTE') = true` |
| V-14 | §10.2 revokes, then the three §10.3 queries | schema `USAGE` false; 2 tables × 4 privileges all false; no sequences in the schema; 4 functions all false; runtime call and direct table `SELECT` both `42501` |
| V-15 | Identity-argument spelling | `float8` and `double precision` resolve to the same `regprocedure`; `lease_release_v1(integer,integer)` → `42883` |
| V-16 | `pg_temp.sha256(bytea)` shadow created in the caller's session | not used; the fingerprint still matched the real SHA-256 |
| V-17 | `pg_auth_members.memberid` on PostgreSQL 16 | `42703`: the column is `member` (corrected in §10.3) |
| V-18 | Advisory guard `M` contention with `p_lock_timeout_ms = 300` | `55P03` after 0.30 s; released with the transaction, no leak |
| V-19 | **Stale protocol-1 process after protocol 2 activation** (T-23): preflight read, upgrade, then resume | `acquire` `45006`, `enter` `45006`; lease row still `(NULL, NULL, generation 0)` although the preflight had returned `(1,1,1,'open','ready')` |
| V-20 | **Topology A revokes** applied to the owning migrator | matrix exactly as documented in §8.3; `UPDATE public.positions` and `DELETE FROM public.strategy_leg_exposures` both `42501` for the owner |
| V-21 | **`--close-barrier` racing an `acquire` that holds the row lock but has not committed** | probe `55P03` reported as `held`; close committed in 0.00 s; the admission then committed as a live owner; a new `acquire` got `45004`; the upgrade path's row lock and quiescence check saw the live owner |
| V-22 | **Sequence minimum privilege** | `USAGE` alone → `nextval` and `currval` succeed with sequence `SELECT` false; sequence `SELECT` alone → `nextval` `42501` |
| V-23 | **`INSERT … RETURNING id`** with table `SELECT` revoked, then granted | `42501` without `SELECT`; returns an id with it |
| V-24 | **First installation from a genuinely empty database** (T-01) | 2 tables, 4 functions, protocol row `(1,1,1,'open','ready')`, one unowned lease row, no `alembic_version`. Statement log: advisory guard → `CREATE SCHEMA` → `CREATE TABLE` (step 2) → seed `INSERT` (step 3) → four functions → revokes → verification → `UPDATE` (step 11). **No `SELECT … FOR UPDATE` anywhere in the path**, and no write before the object it targets exists |
| V-25 | Partial tree (schema + lease table, no protocol table) | detected as a refused first install (`45004` precondition) |
| V-26 | **Existing-installation verification** (T-02 second half) | completed, left `(1,'open','ready')`, created and dropped no object (4 relations before and after) |
| V-27 | **Cross-version concurrency** (T-24) | protocol-2 admission succeeded on the shared control row; a protocol-1 `enter` while a protocol-2 transaction held `M` → `55P03` at 0.30 s; after that transaction committed, protocol 1 got `45006` from both `acquire` and `enter`; two protocol-2 sessions serialized with `55P03`; rolling the control row back to 1 refused protocol 2 with `45006`. No `40P01` in any of them |
| V-28 | **Lock order, both directions** | bootstrap order behind a live `enter` → `55P03`; `enter` behind a bootstrap holding `B` and the row lock → `55P03`; no deadlock either way |
| V-29 | **T-19 failed release** | primary `ValueError` preserved while cleanup raised `45001`; ownership hash, fingerprint, expiry and generation all unchanged; the rolled-back user transaction left no row; a new worker got `45002`; a release blocked by the row lock failed `55P03` after its 5 s bound and again left ownership unchanged; a later unblocked release returned `true` |

**Gaps this run found in the document, and their corrections**

1. **`--close-barrier` could not start a drain window.** The shared bootstrap order locked the lease row and required quiescence, so with a live owner the close returned `55P03` (V-11). It now has its own order (§11.4.4) that takes only the protocol row lock, probes the lease row `NOWAIT` and never requires quiescence (V-12).
2. **First installation and verification shared one order.** On an empty database that order `UPDATE`s and locks objects that do not exist yet. Split into §9.2.1 (create, seed, install, revoke, verify, then update) and §9.2.2 (verify only), executed as V-24/V-25/V-26.
3. **The installed version was recorded twice and only the mirror was checked.** A stale process could admit after an upgrade, and §11.3 claimed an advisory lock that no function acquired. `lease_protocol` is now the single authoritative object, read inside the functions under the guard `M`, which `enter`/`acquire`/`release` actually take (V-18, V-19, V-27, V-28).
4. **The close-barrier race with an in-flight admission was undefined.** Now linearized explicitly, probed `NOWAIT` and reported (V-21).
5. **Privilege verification collapsed four table privileges into one string, and sequence `SELECT` was claimed for `nextval`.** Corrected, with Topology A's revokes documented and executed (V-14, V-20, V-22, V-23).
6. **The version mirror and the upgrade disagreed.** Found while running V-27: `acquire` did not write the mirror and the upgrade did not maintain it, so the first admission after an upgrade failed its own corruption guard. `acquire` now writes `protocol_version` and §9.2.2 step 8 updates the mirror in the same transaction as the control row.
7. **§10.3 used a pre-16 catalog column; §10.4 overstated the GUC property; §6.9 misattributed the `NaN` rejection.** Corrected (V-5, V-17).

**Limitations of this evidence**

- Disposable local cluster, not Neon: no provisioning, no default-ACL state, no real role attributes, no TLS or pooled connection.
- Alembic, the application, the ORM and the per-user backfill were **not** run. The DDL and per-user transaction shapes were exercised only as bare `BEGIN … enter … COMMIT` blocks.
- **Protocol 2 was simulated**: the `*_v2` functions were produced by renaming the documented `*_v1` bodies, and activation was an `UPDATE` of the control row, not an installed second function set. T-23's "the process pauses" was simulated by retaining the preflight value in the test.
- No `40P01` was observed, but only two concurrent sessions plus one blocker were used; there is no stress beyond that, and no attempt to provoke a deadlock deliberately.
- The application-table matrix used synthetic tables shaped like the real ones, not the Alembic-created schema or its constraints.
- Render non-overlap (§11.4.6) and every operational procedure in §12 are unexecuted.

---

## 15. Disposition of previous review findings

Nothing is silently dropped. "Resolved by design" means the design removes the failure mode and a test exists to prove it; "open" means design alone cannot close it; "implementation test" means the design is only credible once code exists.

| # | Finding | Disposition | Where | Proof required |
|---|---|---|---|---|
| F1 | GUC-forgery bypass: ownership derived from a settable GUC/session variable | **Resolved by design.** Ownership is token-hash + row lock; no function reads any GUC; re-entry is lock-based | §5.2, §6.2, §6.4, §10.4 | T-05 |
| F2 | Backfill left unprotected after the renewer was removed | **Resolved by design.** Every DDL and per-user transaction enters first and refreshes before commit; ownership loss aborts startup | §7.1, §7.3, §7.4 | T-09, T-10 |
| F3 | Missing per-user pre-commit refresh (blocking correction) | **Resolved by design.** Mandatory second `enter` immediately before every per-user commit; absence is a test failure | §7.3 | T-10 |
| F4 | Migration identity's access to application tables unknown | **Resolved by design, verification open.** §8.2 derives the exact minimum; §8.3 defines ownership-based acquisition plus verified checks; the actual target database has not been inspected | §8, §17 | T-16 |
| F5 | Bootstrap catalog races | **Resolved by design.** One transaction + transaction-scoped advisory lock + bounded `lock_timeout` + idempotent install + verification | §9.2 | T-01, T-02 |
| F6 | Incomplete ownership verification (`IF NOT EXISTS` only) | **Resolved by design.** Catalog verification of schema/table/function owner, shape, constraints, `prosecdef`, `proconfig`, signatures | §9.3 | T-01 (negative cases) |
| F7 | `PUBLIC` / default `EXECUTE` exposure | **Resolved by design.** Revokes after object creation; `acldefault`/`aclexplode` verification | §10.2, §10.3 | T-15 |
| F8 | Global default ACL risk (`defaclnamespace = 0`) | **Resolved by design; environmental verification open.** Bootstrap inspects global and schema-scoped defaults and fails closed; actual Neon defaults not inspected | §9.4, §17 | T-15 plus operator pre-check |
| F9 | Protocol-version upgrade and rolling-deployment risk | **Partly resolved.** Version-suffixed objects + stable advisory guard + barrier handle concurrent releases; the platform's non-overlap guarantee is unverified | §11 | T-14, deployment evidence |
| F10 | Weak owner-token entropy and raw-token disclosure | **Resolved by design.** 256-bit token, only SHA-256 stored, 12-hex fingerprint, no token in exceptions | §6.2 | T-19 log assertions |
| F11 | Stalled active transaction liveness | **Open by nature, documented.** Expiry does not free a held lock; operator termination procedure and alerting defined; residual risk is a founder decision | §12 | Operational drill (not a unit test) |
| F12 | Cleanup error masking | **Resolved by design.** Original exception preserved; cleanup logged; aborted-transaction rollback before release | §6.8 | T-19 |
| F13 | Alembic transaction integration | **Resolved by design.** Connection sharing, external transaction, one commit, fail-closed bare CLI, prohibited non-transactional ops | §13 | T-17, T-18, T-20 |
| F14 | Incorrect CockroachDB compatibility assumptions | **Resolved.** PostgreSQL-only protocol; no CockroachDB tests; D-1 not reopened | §3 | Review |
| F15 | `--close-barrier` shared the lease-row `FOR UPDATE` and quiescence steps, so a live owner made the close fail `55P03` and no drain window could start | **Resolved by design.** Mode-specific check order without the lease row lock; executed V-11/V-12 | §9.2, §11.4.4 | T-21 |
| F16 | §10.3 used `pg_auth_members.memberid` (pre-16) and §6.9/§10.4 overstated two facts | **Resolved.** Corrected after execution on 16.16 | §6.9, §10.3, §10.4 | V-5, V-17 |
| F17 | §9.2 gave first installation and existing-installation verification one order, which cannot be correct for both: an empty database has no protocol row to `UPDATE` and no lease table to lock | **Resolved by design.** Two procedures with their own orders; the first installation creates, seeds, installs, revokes, verifies and only then updates (§9.2.1) | §9.2.1, §9.2.2 | T-01, T-02 |
| F18 | The installed version was recorded in two places and only the lease-row mirror was checked by the functions, so a stale process could admit after an upgrade; §11.3 claimed a cross-version advisory lock that no function acquired | **Resolved by design and executed.** `lease_protocol` is the single authoritative control object, read under the guard by `acquire`, `enter` and `release`; the advisory guard `M` is acquired inside those functions, so the application path really holds it | §6.1, §6.3–§6.5, §6.10, §11.2, §11.3 | T-23, T-24, V-18, V-19 |
| F19 | The close-barrier race with an acquisition that holds the lease row lock but has not committed was undefined | **Resolved by design.** Explicit admission linearization point; `--close-barrier` probes the row `NOWAIT` and reports `free`/`held`; the upgrade path serializes behind every in-flight admission and refuses a live owner with `45002` | §11.4.4, §11.4.5 | T-24, V-21 |
| F20 | Privilege verification collapsed four table privileges into one `'SELECT,INSERT'` string, and sequence `SELECT` was granted/claimed for `nextval` | **Resolved.** Table privileges checked one by one; sequence `USAGE` only; Topology A documented with effective revokes and a per-topology expected matrix; T-16 asserts that matrix | §8.2, §8.3, §10.3, T-16 | V-20, V-22, V-23 |

---

## 16. Required founder decision checklist

### Already resolved

**D-1 — PostgreSQL/Neon-only strategic database target.**
Accepted via ADR-019 (merged in PR #148). CockroachDB is retired; no historical data migration. This document does not reopen it.

### Still requiring founder approval

None of D-2 … D-7 is approved. Each states the recommendation and the consequence of refusal.

| Decision | Recommended choice | Consequence of refusal |
|---|---|---|
| **D-2 — Zero runtime privilege on migration infrastructure** | Approve: `strikenova_production_app` has no schema `USAGE`, table privilege, function `EXECUTE`, ownership or inherited membership on `migration_internal`; `PUBLIC` likewise | Without it, the lease can be manipulated by the runtime credential. The alternative is to keep lease access in the runtime role, which returns to the previous design's exposure and makes the privilege boundary advisory rather than enforced |
| **D-3 — Run startup backfill under the migration identity** | Approve: add a required migration identity/URL; backfill runs there; refuse to start if migration and runtime identities are equal on PostgreSQL | Refusing means the runtime role must hold `SELECT`/`INSERT` on application tables *and* lease access, or the backfill must be dropped from startup. Dropping it leaves legacy exposure rows unattributed |
| **D-4 — Retire the renewal-thread contract** | Approve: no background renewer, no handoff mode, no lease renewal API; long transactions use the pre-commit refresh and a TTL sized above them | Refusing keeps a second ownership mechanism, which is exactly what produced the original lease-loss defect; it would also require its own failure analysis and tests |
| **D-5 — Require the guarded operator CLI** | Approve: `python -m app.migrate upgrade head` is the only supported path; bare `alembic upgrade head` fails closed on PostgreSQL | Refusing permits unleased DDL. The design would then need a different proof of serialization for manual migrations, which does not exist |
| **D-6 — Authorize pre-Alembic bootstrap as a narrow exception to Alembic-only schema authority** | Approve: bootstrap may create only `migration_internal` (lease table, protocol table, four functions) before Alembic, once, under the advisory lock, verified and revocable | Refusing leaves a circular dependency: Alembic's protected transaction needs `enter`, and `enter` needs an object Alembic is not allowed to create. The alternative is no lease at all |
| **D-7 — Accept per-user backfill transactions and the availability trade-off** | Approve: one transaction per user, each with entry and pre-commit refresh; TTL sized from measured per-user duration; a restart may repeat work but cannot duplicate rows | Refusing implies either one long transaction for all users (a single lease hold for the whole backfill, higher blast radius on failure) or skipping the per-user refresh (unsafe). A long global transaction also makes liveness worse |

---

## 17. Open technical uncertainties and required evidence

These are recorded rather than resolved. None may be presented as settled.

| # | Uncertainty | Why it is open | How to close it |
|---|---|---|---|
| U1 | The migration identity does not exist in code | `eeeebff` has a single `DATABASE_URL` (E7); no migration URL, no separate engine | Implementation of §8.1 plus T-16 on a disposable PostgreSQL 16 |
| U2 | No accepted database-role contract | The three role names appear only in uncommitted audit artifacts (E11) and nowhere in the tracked tree; `DATA.md` §5 is not a role contract | Founder approves names/attributes as part of D-2/D-3, or a new ADR records them |
| U3 | Neon default ACL state unknown | No authorized access to any Neon database in this task; the staging audit did not capture `pg_default_acl` | Run §9.4 against the target database during provisioning; fail closed on any runtime/PUBLIC entry |
| U4 | Table/sequence ownership on the target database unknown | Depends on which role runs the Alembic chain and whether provisioning pre-creates objects | Run §8.3 verification; it fails closed |
| U5 | Alembic 1.15.2 external-transaction behavior | Documented upstream, but not exercised at the pinned version in this repository | T-18/T-20 must assert exactly one commit; fall back to skipping `begin_transaction()` |
| U6 | `python -m app.migrate` does not exist | E5 | Implement the CLI as part of the migration work; until then only the design exists |
| U7 | Render deploy non-overlap and startup ordering unverified | No deployment inspection or change authorized here | Confirm service instance count and deploy strategy before relying on the barrier |
| U8 | TTL and timeout values are defaults, not measurements | No backfill timing evidence for the target dataset | Measure per-user backfill duration; set TTL/`statement_timeout` accordingly (D-7) |
| U9 | Whether the existing PR #146 harness scenarios encode the old protocol | E13: PR #146 is based on a feature branch and its tests target a lease that does not exist in `main` | Re-author the harness from §14 before implementation; do not merge PR #146 as-is |
| U10 | Production provisioning and cutover | Not authorized (ADR-019, §3) | Separate authorized work |
| U11 | **Partly closed.** The four lease functions, both bootstrap orders, the advisory-lock order, the privilege queries, the failed-release path and a simulated protocol upgrade have executed evidence on a disposable PostgreSQL 16.16 instance (§14.1). Nothing has run on Neon, and no application, Alembic or backfill path has been executed | A bare two-role cluster is not the target environment: default ACLs, real table ownership, Alembic's external transaction, connection topology and platform overlap stay untested (U3, U4, U5, U7). The protocol-2 side of T-23/T-24 was simulated by changing the control row, not by installing a second version-suffixed function set | Treat §14.1 as the design baseline, run the full T-01…T-24 matrix on a disposable PostgreSQL 16 during implementation, then repeat on staging |

---

## 18. Out of scope

- Any implementation, code change, Alembic revision or test addition.
- Creating or altering roles, grants, schemas or objects on any database.
- Neon provisioning, staging changes, production cutover.
- Render service, environment variable, secret or deployment changes.
- Credential generation, rotation or storage.
- CockroachDB access, recovery or compatibility work.
- Merging, closing or changing the state of PR #146 or PR #148.
- Any claim that a test has passed, that Neon production exists, or that the system is production-ready.

---

## 19. Verification statement for this document

- This document is a design and acceptance plan only. Nothing in it is implemented.
- The SQL in §6.1, §6.3–§6.6, §8.3, §10.2, §10.3 and the §11.4.4 close-barrier order, together with the literal seed and transition statements printed in §9.2.1, was executed on a **disposable local PostgreSQL 16.16 cluster**; §14.1 records **83 checks, 83 passing**, and states what each one covered.
- That harness is **not** the acceptance matrix. No test in §14 was run as written. Nothing was executed on Neon, through Alembic, through the application, through the backfill, on Render, or in staging or production. No `pg_default_acl` inspection, no provisioning and no cutover occurred.
- Every other SQL statement in this document is unexecuted, and the §6.9 rows that are not cross-referenced to §14.1 are inspection-level only.
- No database, role, grant, Render service or secret outside the disposable local cluster was read or modified.
- Repository facts in §2 were read from `main` at `eeeebff`; the only file added by this change is this document.
- Approval of D-2 … D-7 is required before any implementation begins.
