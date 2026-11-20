# StrikeNova — PostgreSQL Migration-Lease and Privilege Design

**Status:** `Proposed — Awaiting Founder Approval — Not Implemented`

**Document type:** Standalone design proposal for founder review. Consolidates the corrected ADR-017 migration-lease protocol and the ADR-018 database-privilege model.

**Target database:** PostgreSQL 16, hosted on Neon. Strategic direction per accepted ADR-019.

**Related:** Issue #145, ADR-017 (migration serialization proposal), ADR-018 (database privilege proposal), ADR-019 (accepted: clean-slate Neon PostgreSQL, CockroachDB retired), PR #146 (open Draft; prior lease implementation attempt).

**Base commit for all repository evidence:** `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b` (current `main`, the merge of PR #148 / ADR-019), 2026-11-20.

**Implementation status:** Nothing described here is implemented. No SQL in this document has been executed against any database. No acceptance test has been run. No Neon production database exists for this design, and no provisioning or cutover is authorized.

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
    v_bar   text;
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

    -- Bounded wait for the row lock; never an unbounded hang.
    v_prev := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', p_lock_timeout_ms::text, true);

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

    IF v_row.protocol_version <> p_protocol THEN
        RAISE EXCEPTION 'lease protocol % does not match installed %',
            p_protocol, v_row.protocol_version USING ERRCODE = '45006';
    END IF;

    SELECT pr.barrier_state INTO v_bar
      FROM migration_internal.lease_protocol AS pr
     WHERE pr.singleton;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'protocol row missing; bootstrap required' USING ERRCODE = '45005';
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
       SET owner_token_hash  = v_hash,
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
- The deployment-barrier check lives **inside** `acquire`, under the lease row lock, so admission and the barrier cannot disagree (see §11.4).
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

    SELECT l.* INTO v_row
      FROM migration_internal.migration_lease AS l
     WHERE l.lease_name = 'migration-lease'
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lease row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    PERFORM set_config('lock_timeout', v_prev, true);

    IF v_row.protocol_version <> p_protocol THEN
        RAISE EXCEPTION 'lease protocol % does not match installed %',
            p_protocol, v_row.protocol_version USING ERRCODE = '45006';
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
    v_prev text;
    v_now  timestamptz;
BEGIN
    IF p_token IS NULL OR length(p_token) < 32 THEN
        RAISE EXCEPTION 'lease configuration invalid' USING ERRCODE = '45004';
    END IF;

    v_hash := encode(sha256(convert_to(p_token, 'UTF8')), 'hex');

    v_prev := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', '5000', true);

    SELECT l.* INTO v_row
      FROM migration_internal.migration_lease AS l
     WHERE l.lease_name = 'migration-lease'
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lease row missing; bootstrap required' USING ERRCODE = '45005';
    END IF;

    PERFORM set_config('lock_timeout', v_prev, true);

    IF v_row.protocol_version <> p_protocol THEN
        RAISE EXCEPTION 'lease protocol % does not match installed %',
            p_protocol, v_row.protocol_version USING ERRCODE = '45006';
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
| `45004` | Configuration error (short token, invalid TTL or `lock_timeout`, misconfigured identity/protocol, **deployment barrier closed**) | Abort; run a bootstrap mode (§11.4.4) |
| `45005` | Required bootstrap object missing | Abort; run the guarded bootstrap |
| `45006` | Unsupported protocol version | Abort; deploy the matching release or run the approved protocol upgrade |
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

### 6.9 PostgreSQL 16 semantic review of the four functions

Reviewed by inspection against PostgreSQL 16 behaviour. **None of it has been executed**; the assertion level is "design-level review", and §14 tests must confirm it.

| Issue | PostgreSQL 16 behaviour | Resolution in this design |
|---|---|---|
| `lock_timeout = 0` | `0` **disables** the timeout (waits indefinitely); it does not mean "do not wait" | `acquire` and `enter` reject `< 1` with `45004`; bounds are 1–600000 ms. A zero never reaches `set_config` |
| `lock_timeout` leakage | `set_config(..., true)` is transaction-local, so it also applies to every later lock wait in the same transaction (including Alembic DDL) | Each function saves `current_setting('lock_timeout')` and restores it immediately after the row lock is taken; the DDL transaction keeps the operator's value |
| `make_interval` units | `make_interval(secs => x)` takes **seconds** as `double precision`; fractional seconds are allowed and there is no implicit millisecond interpretation | TTL is passed in seconds, bounded to `0 < ttl <= 86400`; `not (ttl > 0)` rejects `NULL`, `NaN` and negatives in one expression |
| `%ROWTYPE` vs OUT parameters | `RETURNS TABLE (...)` columns become PL/pgSQL variables, which can collide with table columns in unqualified SQL | OUT parameters are prefixed `out_`; every table reference is aliased (`l.`, `pr.`); `lease_protocol_info_v1` keeps matching names but fully qualifies each column |
| `session_user` type | `session_user` is `name`, and `owner_role` is `name`; relying on an implicit assignment cast is unnecessary | Assignment uses `session_user::name` |
| `has_function_privilege` signature | It resolves a `regprocedure`, i.e. the **identity argument** types only; result/OUT columns and parameter names are irrelevant | §10.3 uses `float8` (the canonical spelling) rather than `double precision`, and no function has default arguments |
| `sha256` | Built-in since PostgreSQL 11 for `bytea`; `pgcrypto` is not needed | `encode(sha256(convert_to(token,'UTF8')),'hex')` |
| Clock source | `now()` is transaction start; `clock_timestamp()` is the actual current time | Expiry and acquisition timestamps use `clock_timestamp()`; defaults on the table use `now()` |
| Row-lock re-entry | A transaction that already holds a row lock can lock it again without blocking | The second `enter` before commit is a refresh, not a deadlock |
| `RETURN QUERY` types | Result columns must match the declared OUT types exactly | `text`, `timestamptz`, `bigint`/`integer` are matched; `generation` is `bigint` in both table and function |
| Owned row with `NULL` expiry | `expires_at > now` is `NULL`, not `true`, so a naive check would silently treat a corrupt owned row as free | `acquire` treats it as held (`45002`); the table has `CHECK (owner_token_hash IS NULL OR expires_at IS NOT NULL)` |


---

## 7. Transaction boundaries

### 7.1 Alembic DDL transaction

| Property | Requirement |
|---|---|
| Connection | The migration engine's dedicated connection, shared by the lease and Alembic |
| First SQL statement | `enter` (which performs `SELECT … FOR UPDATE`) |
| Lock duration | Row lock held from `enter` until `COMMIT` or `ROLLBACK` |
| Pre-commit refresh | `enter` again as the last statement before `COMMIT` |
| Failure | Any exception rolls back all DDL and the `alembic_version` update |
| Bare CLI | `alembic upgrade head` on PostgreSQL fails closed without lease admission (§13) |

```text
BEGIN (implicit, first statement)
  enter(token)                 -- row lock acquired; fails on contention/expiry/mismatch
  alembic upgrade head         -- DDL + alembic_version update
  enter(token)                 -- refresh immediately before COMMIT
COMMIT
```

Holding the row lock for the whole DDL transaction is what keeps a second startup from beginning DDL: its `acquire` blocks on the row lock, or (after commit) sees a live owner and backs off.

### 7.2 User enumeration

- Enumeration is its own short transaction: `SELECT DISTINCT user_id FROM public.positions ORDER BY user_id`.
- It must **finish and commit before the first protected user transaction begins**. A long-lived enumeration transaction would hold a snapshot and (if protected) hold the lease row lock for the whole backfill.
- The result is a plain in-memory list of user ids; no cursor stays open across user transactions.

### 7.3 Per-user backfill transaction

```text
BEGIN
  enter(token)                 -- first SQL statement
  read positions / paper_orders for this user
  insert strategy_leg_exposures rows (idempotent)
  enter(token)                 -- MANDATORY pre-commit refresh
COMMIT
```

Requirements:

- **`enter` is the first SQL statement of every per-user transaction.** No read, no `SET`, no ORM query precedes it.
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
| Allocate ids | sequence returned by `pg_get_serial_sequence('public.strategy_leg_exposures','id')` | `USAGE` (or `SELECT` on the sequence) | `SERIAL` default |
| Read users | `public.users` | **none required today** | the current enumeration reads `positions.user_id`, not `users` |

No `UPDATE` or `DELETE` on application tables is required by the backfill. `exit_exposure_allocations` is written only during live exits, not by this backfill.

If a future change joins `users`, grant column-level `SELECT (id)` rather than table-wide `SELECT`, and extend T-16.

### 8.3 How the migration identity obtains those privileges

Two topologies are possible; the design recommends the first.

**Topology A (recommended): the migration identity owns what Alembic creates.**

On a clean Neon database the Alembic chain runs as `strikenova_prod_migrator`, so the migrator owns `public.positions`, `public.paper_orders`, `public.strategy_leg_exposures`, their indexes, constraints and sequences. Table owners have all table privileges implicitly and need no grants; this is exactly why the design must **verify** ownership instead of assuming it.

Verification (run after the Alembic chain, before declaring the backfill identity ready):

```sql
SELECT c.relname, c.relowner::regrole AS owner
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relname IN ('positions','paper_orders','strategy_leg_exposures');

SELECT pg_get_serial_sequence('public.strategy_leg_exposures','id');

SELECT has_table_privilege('strikenova_prod_migrator','public.positions','SELECT'),
       has_table_privilege('strikenova_prod_migrator','public.paper_orders','SELECT'),
       has_table_privilege('strikenova_prod_migrator','public.strategy_leg_exposures','SELECT,INSERT');
```

If any of these is false, startup fails closed with `45004`; it must not silently fall back to the runtime identity or to a superuser.

**Topology B: application tables are owned by a separate NOLOGIN owner.**

If the founder prefers `strikenova_prod_owner` to own application tables, the migrator needs explicit, minimal grants. These are the only grants this design would add for the backfill:

```sql
GRANT SELECT                 ON public.positions             TO strikenova_prod_migrator;
GRANT SELECT                 ON public.paper_orders          TO strikenova_prod_migrator;
GRANT SELECT, INSERT         ON public.strategy_leg_exposures TO strikenova_prod_migrator;
GRANT USAGE, SELECT ON SEQUENCE public.strategy_leg_exposures_id_seq
                             TO strikenova_prod_migrator;
```

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

### 9.2 One-transaction bootstrap

Bootstrap is one operator command with three modes, all executed by the migration identity as `python -m app.migrate ...`:

| Mode | Purpose | May write `barrier_state` |
|---|---|---|
| `bootstrap-lease` (install/verify) | Create or verify protocol objects; leave the database ready | Yes — sets `open` on success |
| `bootstrap-lease --close-barrier` | Start a maintenance window; block new admissions without touching a live owner | Yes — sets `closed` |
| `bootstrap-lease --upgrade-protocol N` | Install/activate protocol `N` and reopen | Yes — sets `closed` then `open` in one transaction |

Only these modes may write the protocol row. No application code path, migration revision or raw-SQL runbook updates `barrier_state`.

Ordered steps (all inside one transaction):

```text
connect as strikenova_prod_migrator
BEGIN
  1. SELECT pg_advisory_xact_lock(
         hashtextextended('strikenova:migration_internal:bootstrap', 0));
         -- lock_timeout bounded at session level before BEGIN -> 55P03 on contention
  2. read lease_protocol (if it exists)
  3. SELECT ... FROM migration_lease FOR UPDATE       (if it exists) -> 55P03
  4. live owner check                                  -> 45002
  5. UPDATE lease_protocol SET barrier_state = 'closed'   (idempotent)
  6. create/verify schema, tables, functions; verify ownership, shape,
     SECURITY DEFINER, search_path, ACLs, default ACLs   -> 45004 / 45005
  7. version transition checks                          -> 45006
  8. UPDATE lease_protocol SET installed_version, range,
     barrier_state = 'open', bootstrap_state = 'ready'
COMMIT                                                   -- atomic release of both locks
```

- The advisory lock is **transaction-scoped** (`pg_advisory_xact_lock`), so it cannot leak on crash; PostgreSQL releases it at commit, rollback or backend death.
- The fixed key (`hashtextextended(...)`) is stable across protocol versions, so it serializes cold starts even when releases differ.
- `lock_timeout` bounds the wait: `55P03` means another bootstrap is running. The caller retries within the same monotonic budget used by `acquire`, then fails startup.
- Step 4 requires quiescence: an owner whose lease is still live is never hijacked by an upgrade. Bootstrap waits or fails with `45002`; it never terminates a backend itself (§12).
- Step 5 happens **before** installing anything, so no worker can admit against a half-installed protocol. Because the same transaction also sets `open` at step 8, the closed window is invisible to a worker that was blocked on the row lock.
- A failure anywhere rolls the whole bootstrap back. If the operator had pre-closed the barrier, it stays closed (fail closed) and the application stays down until a bootstrap mode succeeds — this is the recovery path in §11.4.4, not a deadlock.
- `bootstrap_state = 'installing'` is written only inside this transaction and is therefore never visible to another session; it is diagnostic, not a lock.

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
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM strikenova_production_app;
```

Ordering is mandatory: PostgreSQL grants `EXECUTE` on a new function to `PUBLIC` **by default**, so a revoke executed before `CREATE FUNCTION` is useless. The revoke set must run after the last object is created, inside the same bootstrap transaction, and the verification phase must confirm the result.

No `GRANT` appears anywhere in the bootstrap. The migration role executes the functions because it owns them.

### 10.3 Verify effective privileges, not just direct grants

Direct ACL inspection is necessary but not sufficient. Verification must cover:

1. **Schema defaults and global defaults** — §9.4.
2. **Role memberships and inheritance** — a runtime role that is a member of the migrator (or of a role the migrator is a member of) inherits access even with an empty ACL:

```sql
WITH RECURSIVE reach(roleid, memberid, depth) AS (
    SELECT m.roleid, m.memberid, 1
      FROM pg_auth_members m
     WHERE m.memberid = 'strikenova_production_app'::regrole
    UNION ALL
    SELECT m.roleid, m.memberid, r.depth + 1
      FROM pg_auth_members m JOIN reach r ON m.memberid = r.roleid
     WHERE r.depth < 10
)
SELECT roleid::regrole AS grants_access_to, memberid::regrole, depth FROM reach;

SELECT rolname, rolinherit, rolsuper, rolbypassrls
  FROM pg_roles WHERE rolname IN ('strikenova_production_app','strikenova_prod_migrator');
```

A runtime role must not reach the migrator/owner role at any depth, and must not be `SUPERUSER` or `BYPASSRLS`.

3. **Effective privilege functions** (these account for ownership, membership and `PUBLIC`):

```sql
SELECT has_schema_privilege('strikenova_production_app','migration_internal','USAGE')          AS schema_usage,
       has_table_privilege('strikenova_production_app','migration_internal.migration_lease','SELECT') AS table_select,
       has_table_privilege('strikenova_production_app','migration_internal.lease_protocol','SELECT')  AS protocol_select,
       has_function_privilege('strikenova_production_app',
           'migration_internal.lease_acquire_v1(integer,text,float8,integer)','EXECUTE')   AS acquire_exec,
       has_function_privilege('strikenova_production_app',
           'migration_internal.lease_enter_v1(integer,text,float8,integer)','EXECUTE')     AS enter_exec,
       has_function_privilege('strikenova_production_app',
           'migration_internal.lease_release_v1(integer,text)','EXECUTE')                           AS release_exec,
       has_function_privilege('strikenova_production_app',
           'migration_internal.lease_protocol_info_v1()','EXECUTE')                                 AS info_exec;
```

Every value must be `false`. `has_function_privilege` takes the **identity argument** types only — `float8` is the canonical spelling of `double precision`, and result columns, OUT names and parameter names play no part in the lookup. No lease function declares a default argument, so these strings match the declarations in §6 exactly.

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
- Function bodies do not read `current_setting(...)` at all, which is what makes GUC forgery irrelevant rather than merely mitigated.

---

## 11. Deployment, protocol versioning and compatibility barrier

### 11.1 Initialization before Alembic

On a clean Neon database the order is:

1. operator provisions the database and the three roles (separate authorized task);
2. operator runs `python -m app.migrate bootstrap-lease` as the migration identity — creates `migration_internal`, the lease row and protocol row, and verifies the catalog;
3. the application starts: `acquire` → Alembic DDL transaction → backfill → `release`.

Alembic never creates `migration_internal`, and bootstrap never creates application tables. If step 2 is skipped, `acquire` fails with `45005` and the application does not start.

### 11.2 Protocol-version detection

- The application pins `EXPECTED_PROTOCOL_VERSION` (1 for this design) and `SUPPORTED_PROTOCOL_MIN/MAX`.
- `lease_protocol.installed_version` is the database's record.
- `installed < expected` → `45006`: the database needs the approved protocol upgrade; the release must not create the new protocol implicitly during a normal startup.
- `installed > expected` → `45006`: this release is older than the database; refuse to run.
- `installed` outside `min_supported_version..max_supported_version` → `45006` for every operation.

### 11.3 Active-worker compatibility

A bootstrap advisory lock protects only bootstrap. It does **not** protect a worker that is already executing `lease_enter_v1` when a new release replaces that function. `CREATE OR REPLACE FUNCTION` would change behaviour under a running transaction without any version check.

The design removes that class of failure:

1. **Version-suffixed objects.** Protocol 1 owns `*_v1` functions and the `migration_lease`/`lease_protocol` tables. Protocol 2 gets its own `*_v2` functions and, if its row semantics change, its own tables. Nothing in place is ever replaced while a release that uses it may still run.
2. **Retirement is an explicit operator step**, performed only after evidence that no release using the old objects is active. It is never part of a normal startup and never part of bootstrap for a new protocol.
3. **A stable cross-version guard.** Every protocol version takes the same transaction-scoped advisory lock key before DDL (bootstrap and migration), and every version validates `installed_version` under the lease row lock. Two incompatible releases therefore cannot both migrate, even if they use different function names.

### 11.4 Deployment barrier

`barrier_state` is a row, not a convention, and it has exactly one writer class.

#### 11.4.1 The rule

- `barrier_state = 'closed'` blocks **every admission path**. A normal startup that passes the version checks fails with `45004` and changes nothing else in the database.
- **Bootstrap/protocol upgrade is the only operation allowed while `barrier_state = 'closed'`**, and the only writer that can set it back to `open` (§9.2). There is no raw-SQL reopen path, no application path, and no migration revision that writes it.
- Closing the barrier does **not** abort an already-admitted owner. `enter` ignores the barrier by design (6.4); quiescence is enforced by bootstrap's live-owner check, not by the barrier itself.
- If an operator closes the barrier and the rollout is abandoned, the database deliberately stays closed. Recovery is a successful bootstrap mode, never a manual `UPDATE` (§11.4.4).

#### 11.4.2 Check order — normal application startup

| Step | Check | Failure |
|---|---|---|
| 1 | Migration identity configured and different from the runtime identity (before any connection) | `45004` |
| 2 | `migration_internal.lease_protocol_info_v1()` exists and returns exactly one row | `42P01`/`42883` mapped to `45005`; missing row `45005` |
| 3 | `bootstrap_state = 'ready'` | `45005` (bootstrap incomplete) |
| 4 | `installed_version` equals this release's expected version and is within its supported range | `45006` |
| 5 | Fail-fast barrier read: `barrier_state = 'open'` | `45004` |
| 6 | `acquire`: row lock → missing lease row → protocol version → **barrier re-check under the row lock** → live owner | `55P03` / `45005` / `45006` / `45004` / `45002` |
| 7 | `enter` in each protected transaction: lock → protocol → owner → expiry | `55P03` / `45005` / `45006` / `45001` / `45002` |
| 8 | `release` after the work: lock → protocol → owner | `55P03` / `45005` / `45006` / `45001` (idempotent `false` if unowned) |

Step 5 fails fast to avoid a pointless retry budget; step 6 is the authoritative check, because it runs inside the same transaction that transfers ownership. A barrier closed between steps 5 and 6 is therefore still caught. Bootstrap writes the protocol row only while holding that same lease row lock (§9.2), so `acquire`'s barrier read cannot race a barrier change.

#### 11.4.3 Check order — bootstrap / protocol upgrade (the exempt path)

| Step | Check | Failure |
|---|---|---|
| 1 | Transaction-scoped advisory lock at the fixed key, bounded by `lock_timeout` | `55P03` |
| 2 | Protocol row read; absence selects the first-install path | none |
| 3 | Lease row `SELECT … FOR UPDATE` (when it exists) | `55P03` |
| 4 | Quiescence: no live owner (`owner_token_hash IS NULL OR expires_at <= clock_timestamp()`) | `45002` |
| 5 | Set `barrier_state = 'closed'` (idempotent) | — |
| 6 | Verify or create objects; verify ownership, shape, `prosecdef`, `proconfig`, ACLs, default ACLs | `45004` / `45005` |
| 7 | Version transition: target must equal this binary's **expected** protocol version, must be `>= installed_version` (no downgrade), and must lie within this binary's supported range — not the range already stored in the row, which step 8 replaces | `45006` |
| 8 | Set `installed_version`, supported range, `barrier_state = 'open'`, `bootstrap_state = 'ready'` | — |
| 9 | `COMMIT` — releases the row lock and the advisory lock atomically | — |

Ordering is the deadlock fix: step 5 executes **before** any install work, step 8 executes **in the same transaction** as step 5, and step 4 guarantees no admitted worker is mid-flight. A closed barrier can therefore always be reopened by this path, and only by this path.

#### 11.4.4 Recovering from a closed barrier

| Situation | Action | Result |
|---|---|---|
| Operator closed the barrier and the rollout was cancelled | Run `bootstrap-lease` (install/verify) at the deployed version | Steps 1–8 run; barrier reopens; app starts |
| Operator closed the barrier and a new protocol was installed but the app was not deployed | Run `bootstrap-lease --upgrade-protocol <installed>`; the target equals `installed_version`, which is permitted | Barrier reopens without a version change |
| A live owner still holds the lease | Bootstrap fails `45002` | Wait for `expires_at` or follow the termination procedure (§12.4); then rerun. Bootstrap never terminates a backend |
| The owner's backend is stuck holding the row lock | Bootstrap fails `55P03` | Diagnose and terminate per §12.4, then rerun |
| Bootstrap itself fails midway | Transaction rolls back; barrier keeps its prior state | Fix the cause and rerun; no half-installed protocol |

`bootstrap-lease --close-barrier` is the only mode that may run while an owner is live, because its purpose is to drain one; it never waits for quiescence.

#### 11.4.5 Required deployment properties

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
| `release` fails after a successful commit | Migration remains committed; cleanup failure logged; startup fails so the operator sees it |

---

## 14. Acceptance-test design (PostgreSQL 16)

**Status of these tests: designed, none executed.** Database-locking and privilege behavior must be exercised against a real PostgreSQL 16 server; SQLite tests cannot substitute. The existing GitHub workflow that provisions a disposable `postgres:16` service is the intended host (PR #146 already contains such a harness for its own scenarios; this matrix supersedes its lease expectations and must be re-authored during implementation).

Conventions below: `W1`, `W2` are separate processes/connections using the migration identity and distinct 256-bit tokens; `APP` is the runtime role; "row state" means `(owner_token_hash IS NULL, expires_at, generation)`. "Regression signal" is what a future failure would look like.

| # | Scenario and setup | Concurrent actors | Expected database state | Expected SQLSTATE / exception | Regression signal |
|---|---|---|---|---|---|
| T-01 | Empty database; run bootstrap as migrator | one | `migration_internal` exists, owned by migrator; 2 tables, 4 functions, 1 protocol row, 1 unowned lease row; `alembic_version` absent | none | objects missing, wrong owner, `PUBLIC` ACL present, or app tables created by bootstrap |
| T-02 | Empty database; run bootstrap twice concurrently | W1, W2 | exactly one schema, one protocol row, one lease row; both calls return success or one gets `55P03` and then succeeds on retry | none (after retry) or `55P03` | duplicate rows, `duplicate_object`, partially installed protocol |
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
| T-16 | Alembic chain applied by migrator | migrator session | `SELECT` on `positions`/`paper_orders`, `SELECT,INSERT` on `strategy_leg_exposures`, sequence `USAGE` all succeed; `UPDATE`/`DELETE` on exposures denied unless deliberately granted | none for required operations | required DML fails with `42501` (means §8.3 verification is incomplete), or the migrator has `DELETE`/broad `ALL` |
| T-17 | Bootstrapped DB; run `alembic upgrade head` directly | one | no DDL; `alembic_version` unchanged; lease row unchanged | non-zero exit; runtime error before `context.configure` | migration applied without lease |
| T-18 | Bootstrapped DB; run `python -m app.migrate upgrade head` | one | lease acquired, entered, DDL applied, pre-commit `enter` executed, committed, released; row unowned with `released_at` set | none | any step skipped, or row left owned |
| T-19 | Force `backfill_exposures` to raise (e.g. injected error) and force `release` to fail (revoke EXECUTE mid-run) | one | DDL (if any) committed/rolled back per its own step; no lease left held after cleanup attempt; no partial user transaction | original exception type propagates; cleanup failure only logged | cleanup exception replaces the original, or the process reports success |
| T-20 | Database already at `head`; run the supported command | one | `alembic_version` unchanged; backfill idempotent; lease still acquired/entered/committed/released; exactly one commit on the DDL connection | none | DDL re-run, version rewritten, or `begin_transaction()` issuing a second `COMMIT` |
| T-21 | Operator runs `bootstrap-lease --close-barrier`; then a normal startup; then `bootstrap-lease --upgrade-protocol <installed>`; then a normal startup again | operator, W1 | after close: barrier `closed`, lease untouched; bootstrap upgrade reopens in one transaction; final state `open`, version unchanged, lease unowned | startup while closed: `45004`; bootstrap with a live owner: `45002`; bootstrap success has no error | startup succeeds while closed, or no bootstrap path can set `open` (deadlock), or bootstrap runs while an owner is live |
| T-22 | `acquire` and `enter` called with `p_lock_timeout_ms = 0` (and separately `p_ttl_seconds = 0`) | W1 | no lease change | `45004` immediately; the call must **not** wait indefinitely | the call blocks (proving `lock_timeout = 0` reached PostgreSQL) |

Additional assertions applied to every test:

- the raw token never appears in captured logs, exception text or the JUnit XML;
- the connection used for `enter` is the same connection used for the writes being protected;
- `owner_fingerprint` in logs matches the lease row while owned;
- no test leaves the lease row owned (except T-11/T-12, which assert the recovery end state).

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
| U11 | The four lease functions were reviewed by inspection, not executed | No PostgreSQL instance was used in this task, so §6.9 records PostgreSQL 16 semantics reasoning, not observed behaviour | T-21/T-22 plus the full §14 matrix on a disposable PostgreSQL 16 during implementation |

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

- This document is a design and acceptance plan only.
- No SQL in it has been executed; §6.9 is an inspection-level PostgreSQL 16 semantic review, not observed behaviour.
- No acceptance test in §14 has been run.
- No database, role, grant, Render service or secret was read or modified while producing it.
- Repository facts in §2 were read from `main` at `eeeebff`; the only file added by this change is this document.
- Approval of D-2 … D-7 is required before any implementation begins.
