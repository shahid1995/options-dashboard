# Issue #145 — T-01…T-27 Harness Evidence for Independent Review

**Branch:** `review/issue145-t01-t27-harness-evidence`
**Base:** `main` at `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`
**ADR source:** `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` at pinned commit `ae2226ef592dc48d5e9619bd2198de33285911df`
**PR #149:** open, unmerged, one file (`docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`) — **untouched**

---

## Verification status: no runtime SQL verification was performed

PostgreSQL 16.16 is **not available** in this cloud environment (`pg_ctl` is not
present and no server listens on the configured port). The harness therefore
**did not execute any scenario against a server** in this environment.

The harness was run twice with no server reachable:

- without the destructive-run opt-in it refused before opening a connection and
  reported **27 NOT RUN** with exit status **2**;
- with the opt-in set it attempted one connection, found nothing listening, and
  reported **27 NOT RUN** with exit status **0**.

Those runs verify the safety gate and the unavailability path, not the lease
protocol. No PASS is claimed for any scenario; every T-01…T-27 outcome in
`results_t01_to_t27_rerun.md` is **NOT RUN**.

---

## Transaction strategy: autocommit connections, explicit blocks

Every harness connection — administrator, migrator and runtime APP — is opened
through `harness_connect()`, which sets `autocommit=True`. This is a correctness
requirement, not a style choice.

With psycopg's default `autocommit=False`, a bare `CREATE TABLE` or verification
`SELECT` silently opens a transaction, and every later `with conn.transaction():`
becomes a *savepoint inside that accidental transaction*. The harness would then
be wrong about the two things T-11 and T-12 exist to test: that the application
table is committed before the lease is acquired, and that `lease_enter_v1` is the
first statement of the protected transaction.

With `autocommit=True`:

- each standalone statement is its own committed transaction — application DDL
  before a protected block is genuinely durable and visible to the second
  connection;
- each `with conn.transaction():` block is a real `BEGIN` / `COMMIT` (or
  `ROLLBACK` on a raised exception), so an assertion failure inside a protected
  block rolls that block back and releases its row lock;
- `run_sql()` still never commits or rolls back: the caller owns the boundary.

Bootstrap follows the same rule: `SET lock_timeout = '10s'` is a standalone
statement before the block, and the whole of §9.2.1 runs inside one
`with conn.transaction():` block.

---

## What the harness does

All SQL is copied verbatim from the pinned ADR — §6.1 tables, §6.3–§6.6
functions, §10.2 revoke set and the §9.2.1 first-installation procedure. A static
check extracts the same fenced blocks from the ADR and asserts the loaded Python
string values equal them exactly.

1. **Real SQL line breaks.** No SQL string contains a literal backslash-n; the
   assertion is made against the *loaded* values.
2. **Caller-owned transactions.** See the section above.
3. **Bootstrap ordering is the ADR's order.** One transaction: preflight, create
   schema/tables/rows, create the four functions, apply the §10.2 revoke set,
   run the full §9.3/§9.4 verification while the protocol row still reads
   `closed` / `installing`, then `UPDATE` it to `open` / `ready`, then assert the
   `lease_protocol_info_v1()` result. A verification failure aborts the whole
   bootstrap; the pre-activation pass uses `expect_active=False` so it cannot
   mistake "not yet activated" for a defect.
4. **Complete §9.3 / §9.4 / §10.3 verifier.** Constraint *definitions* are
   compared through `pg_get_constraintdef()`, not merely names and types;
   function identity arguments and complete result signatures are compared,
   including the five-column `lease_protocol_info_v1()` result; object ACLs are
   expanded with `acldefault(...)` when the catalog ACL is `NULL`; table,
   sequence, function and schema privileges are checked per privilege with
   `has_*_privilege`; transitive membership (`pg_auth_members`, recursive) and
   `rolsuper` / `rolbypassrls` are checked; both global and schema-scoped
   `pg_default_acl` rows are inspected. The verifier repairs nothing: every
   mismatch raises and rolls bootstrap back.
5. **Genuine per-scenario database isolation.** The runner calls
   `prepare_scenario_database()` for every scenario, connects to that unique
   database, runs the scenario, then calls `drop_scenario_db()` in a `finally`
   path. The helper is on the execution path, not merely defined.
6. **Separate identities.** The administrator connection only provisions
   disposable databases and cluster roles. Every scenario connection uses
   `strikenova_prod_migrator`, and the cluster roles are provisioned once before
   T-01. T-15 exercises a real APP session; T-16 opens the owner window with
   `SET ROLE` and returns with `RESET ROLE`.
7. **Unavailability handling.** The connection attempt and the server-version
   probe are both wrapped. If the server is unreachable, or
   `server_version_num` is below `160016`, the runner reports all 27 NOT RUN with
   exit 0. `SELECT 1` alone is never treated as proof of PostgreSQL 16.16.
8. **Result contracts and error handling.** Successful `acquire`/`enter` return
   the documented 3-column row; `release` returns a boolean; `protocol_info`
   returns five columns. SQLSTATE is read from the driver's `sqlstate` attribute,
   never parsed from a message and never modelled as a query row.
9. **Classification.** A failed assertion or an unexpected PostgreSQL SQLSTATE is
   `FAIL`; a failure to create the scenario database or connect as the migrator is
   `INVALID_SETUP`; a scenario whose required prerequisite genuinely does not
   exist returns `NOT RUN`. A teardown error is reported on its own line and never
   replaces the scenario outcome.
10. **No unsupported PASS.** T-13, T-17, T-18, T-19, T-20, T-21, T-23, T-24,
    T-25 and T-26 return NOT RUN with the missing prerequisite named. **T-23 is
    not implemented**: it needs the documented `bootstrap-lease
    --upgrade-protocol` flow, and a direct `UPDATE` of the version columns is not
    an upgrade, so the scenario stays NOT RUN rather than asserting a substitute.

Static source checks cannot establish runtime SQLSTATE values, lock behaviour,
privilege enforcement or recovery. Only execution against PostgreSQL 16.16 can.

---

## Destructive-run safety gate

The harness drops fixed-name databases and mutates cluster-level role
membership, so it must never run against a live environment. It refuses unless
**both** of these are set:

| Variable | Required value | Meaning |
|---|---|---|
| `ALLOW_DESTRUCTIVE_TESTS` | `1` | Explicit, per-run opt-in to destructive behaviour |
| `HARNESS_CLUSTER_LABEL` | `issue145-disposable` | Declares the target a dedicated disposable cluster |

The gate runs in two stages:

1. **Before any connection.** Missing or incorrect opt-in refuses immediately:
   nothing is opened, and the run reports 27 NOT RUN with exit status 2.
2. **After the version probe, before any mutation.** The target is inspected and
   refused if it holds any database other than `postgres`, `template0`,
   `template1` or the harness's own `issue145_*` databases; if a fixed-name role
   already exists with `SUPERUSER`, `BYPASSRLS`, `CREATEROLE` or `CREATEDB`; or if
   any of the three fixed-name roles has a membership the harness did not create.
   A refusal here leaves the cluster exactly as found — the harness does not adopt
   or repair a production-like role set, and does not create, drop or grant
   anything.

### Connection configuration

| Variable | Default | Used for |
|---|---|---|
| `DB_HOST` | empty (Unix socket) | all connections |
| `DB_PORT` | `55432` | all connections |
| `ADMIN_DB_USER` / `ADMIN_DB_PASSWORD` | falls back to `DB_USER` / `DB_PASSWORD` | administrator: create/drop databases, create roles, terminate backends |
| `MIGRATOR_DB_USER` | `strikenova_prod_migrator` | every scenario connection |
| `MIGRATOR_DB_PASSWORD` | empty | migrator login |
| `DB_USER` / `DB_PASSWORD` | empty | administrator fallback, and the password fallback for the APP connection |

The administrator must be allowed to create and drop databases, create roles,
and call `pg_terminate_backend`. The migrator and APP roles must be able to log
in. Use throwaway credentials for this disposable cluster only — no real
StrikeNova credential belongs in this harness or its environment.

### Side effects, by scope

**Database-local (removed with each scenario database):** schema
`migration_internal`, both lease tables, the four functions, the lease and
protocol rows, the `public.positions` table created by T-11/T-12, and all grants
on them.

**Cluster-level (survive the run, because dropping a database does not drop
roles):**

- roles `strikenova_prod_migrator` (LOGIN), `strikenova_production_app` (LOGIN)
  and `strikenova_prod_owner` (NOLOGIN), created only if absent;
- membership `strikenova_prod_owner` → `strikenova_prod_migrator` with
  `INHERIT FALSE, SET TRUE, ADMIN FALSE`;
- per-scenario-database `CONNECT`/`CREATE` grants and `USAGE, CREATE` on
  `public`.

**Per-scenario cleanup:** `drop_scenario_db()` terminates backends connected to
that scenario database (never any other database), then drops it. T-12
additionally terminates one backend **inside its own scenario database**, which
is that scenario's subject under test.

---

### Database-local versus cluster-level objects

A database is dropped and recreated per scenario. Roles are cluster-level: the
harness creates them idempotently inside the disposable cluster and scopes every
object it creates to the scenario database. The lease schema, lease tables,
functions and application tables are all database-local and vanish with the
database.

### SQLSTATE note

`42P07` is `duplicate_table` and `42501` is `insufficient_privilege`; they are
unrelated. The harness never treats `42P07` as a privilege-denial code and never
infers SQLSTATE from an exception message. T-12 requires the
backend-termination SQLSTATE `57P01` or `08006` — a closed connection alone is
not accepted as evidence.

---

## Per-scenario classification (all public outcomes are static; none executed here)

| # | Scenario | Classification | Basis |
|---|---|---|---|
| T-01 | First installation from an empty database | NOT RUN | Implemented; requires PostgreSQL 16.16 |
| T-02 | Concurrent first installation | NOT RUN | Two backends required |
| T-03 | Concurrent acquire on an unowned lease | NOT RUN | Two backends required |
| T-04 | W1 enters twice; second worker refused | NOT RUN | Implemented (two connections); not executed |
| T-05 | GUC forgery cannot substitute for the token | NOT RUN | Implemented; not executed |
| T-06 | Foreign valid-format token is refused | NOT RUN | Implemented; not executed |
| T-07 | Row-lock contention with the correct token | NOT RUN | Implemented (two connections); not executed |
| T-08 | Two workers run `alembic upgrade head` | NOT RUN | Requires the real Alembic chain |
| T-09 | Concurrent backfill of three users | NOT RUN | Requires the real backfill |
| T-10 | Two `enter` calls per backfill transaction | NOT RUN | Requires the real backfill |
| T-11 | TTL genuinely exceeded by the database clock | NOT RUN | Implemented (committed table, acquisition, `enter`-first protected write, rollback, lock release, takeover); not executed |
| T-12 | Terminate W1's backend mid-transaction | NOT RUN | Implemented (`enter` first, protected write, backend kill, 57P01/08006, rollback, lock release, 45002 before expiry, DB-clock expiry, takeover); not executed |
| T-13 | Interrupted backfill and recovery | NOT RUN | Requires the real backfill |
| T-14 | Version mismatch and closed barrier | NOT RUN | Implemented; not executed |
| T-15 | Topology A — runtime APP has no lease privilege | NOT RUN | Implemented; not executed |
| T-16 | Topology B — non-inherited membership and grants | NOT RUN | Implemented (migrator session, `SET ROLE` owner window, owner denied functions, return to migrator matrix); not executed |
| T-17 | `alembic upgrade head` directly | NOT RUN | No Alembic binary |
| T-18 | `python -m app.migrate upgrade head` | NOT RUN | No application runner |
| T-19 | Injected backfill and release failures | NOT RUN | Requires a blocker session |
| T-20 | Supported command on a database already at head | NOT RUN | No application command |
| T-21 | Operator close-barrier sequence | NOT RUN | Requires operator commands |
| T-22 | `p_lock_timeout_ms = 0` / `p_ttl_seconds = 0` | NOT RUN | Implemented; not executed |
| T-23 | Stale protocol-1 worker after upgrade to 2 | NOT RUN | Not implemented: requires the `bootstrap-lease --upgrade-protocol` flow; a direct version `UPDATE` is not an upgrade |
| T-24 | Cross-version concurrency | NOT RUN | Requires `*_v2` function set and concurrency |
| T-25 | Plain bootstrap verify refuses a mismatch | NOT RUN | Requires the `bootstrap-lease` command |
| T-26 | Explicit upgrade, refusal and cancellation | NOT RUN | Requires operator commands |
| T-27 | `enter`/`acquire` refuse an unready protocol | NOT RUN | Implemented; not executed |

Totals: **27 NOT RUN, 0 PASS, 0 FAIL, 0 INVALID_SETUP, 0 INCONCLUSIVE.**

A NOT RUN outcome is never also counted as FAIL.

---

## How to run it (only on a dedicated disposable PostgreSQL 16.16 cluster)

```bash
pip install 'psycopg[binary]>=3.1'

export ALLOW_DESTRUCTIVE_TESTS=1        # explicit destructive opt-in
export HARNESS_CLUSTER_LABEL=issue145-disposable
export DB_HOST=""                       # Unix socket
export DB_PORT=55432                    # disposable PostgreSQL 16.16
export DB_USER=""                       # administrator, CREATEROLE/CREATEDB
export DB_PASSWORD=""
export MIGRATOR_DB_USER=strikenova_prod_migrator
export MIGRATOR_DB_PASSWORD=""
export LEASE_TTL_SECONDS=500
export LEASE_LOCK_TIMEOUT_MS=1000

python3 docs/reviews/issue145-t01-t27-harness/run_t01_to_t27.py
```

Exit status is `0` when the run completed (including all-NOT-RUN and
INVALID_SETUP outcomes) and `2` when the safety gate refused the target.

---

## Historical, superseded evidence

The original publication on this branch reported 12 PASS / 13 FAIL /
2 INVALID_SETUP from an earlier harness. That harness did not implement this
protocol faithfully (literal-backslash SQL, caller-committing helper, no real
isolation, SQLSTATE inferred from text, and simulated results modelled as rows).
Those numbers are preserved in Git history as **historical and superseded**;
they are not current results and are not reused anywhere in this document.

---

## Static checks performed for this revision

- `python3 -m py_compile` — passed.
- Loaded SQL constants compared exactly with the pinned ADR blocks for §6.1
  (both tables), §6.3, §6.4, §6.5, §6.6 and §10.2 — all match; no literal
  backslash-n.
- DB-free suite with an injected fake driver: every connection opener sets
  `autocommit=True`; the gate refuses before connecting; the accepted-but-
  unreachable path reports 27 NOT RUN with exit 0; target validation refuses a
  foreign database, a `SUPERUSER`/`BYPASSRLS` role and an unexpected membership;
  `expect_sqlstate` reads the driver SQLSTATE inside a real transaction.
- Structural assertions: bootstrap order is preflight → create → revoke →
  verify → activate → protocol-info, verification is pre-activation, T-11/T-12
  commit the application table before acquisition and place `enter` before the
  protected write, T-12 asserts `57P01`/`08006`, the verifier contains every
  required §9.3/§9.4/§10.3 check and no GRANT/REVOKE, and T-23 remains NOT RUN.
- No protocol behaviour was exercised against a database in this environment.
