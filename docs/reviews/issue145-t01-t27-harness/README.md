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
   compared in full through `pg_get_constraintdef()` — the whole normalized
   expression, not marker fragments — so an added `OR true` branch, a dropped
   conjunct, a changed operator or an extra `IN` alternative fails; the expected
   strings are PostgreSQL 16 renderings of the §6.1 DDL that have not been
   observed in this environment, and a mismatch reports the observed text instead
   of accepting it;
   function identity arguments and complete result signatures are compared,
   including the five-column `lease_protocol_info_v1()` result; object ACLs are
   expanded with `acldefault(...)` when the catalog ACL is `NULL`; table,
   sequence, function and schema privileges are checked per privilege with
   `has_*_privilege`; the complete transitive membership graph (`pg_auth_members`,
   traversed without a depth limit and cycle-safe) and `rolsuper` /
   `rolbypassrls` are checked; both global and schema-scoped
   `pg_default_acl` rows are inspected. The verifier repairs nothing: every
   mismatch raises and rolls bootstrap back.
5. **Genuine per-scenario database isolation.** The runner calls
   `prepare_scenario_database()` for every scenario, connects to that unique
   database, runs the scenario, then calls `drop_scenario_db()` in a `finally`
   path. The helper is on the execution path, not merely defined.
6. **Separate identities.** The administrator connection only provisions
   disposable databases and cluster roles; it never grants on application
   tables, and nothing here assumes the administrator is a superuser, the table
   owner, a member of the owner role or a grant-option holder. T-16 issues its
   four grants on a separate `strikenova_prod_migrator` connection to
   `issue145_t16`, after asserting `current_database()`, inside an explicit
   `SET ROLE strikenova_prod_owner` window: the owner context is asserted before
   the grants, `RESET ROLE` is asserted back to the migrator after them, and the
   connection is closed in `finally`. This uses the designed `SET TRUE`
   membership rather than giving the cluster administrator object-grant
   authority. Every scenario connection uses the migrator, and the cluster roles
   are provisioned once before T-01. T-15 exercises a real APP session; T-16
   keeps its own DDL owner window and returns with `RESET ROLE`.
7. **Unavailability handling.** The connection attempt and the server-version
   probe are both wrapped. If the server is unreachable, or it is not PostgreSQL
   major 16 with minor 16 or later, the runner reports all 27 NOT RUN with exit 0.
   A lower bound alone is not used: PostgreSQL 17 and later are refused because
   this harness was not written or approved against another major version, and a
   different major needs a separate design decision. `SELECT 1` alone is never
   treated as proof of version.
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
**all four** of these are set:

| Variable | Required value | Meaning |
|---|---|---|
| `ALLOW_DESTRUCTIVE_TESTS` | `1` | Explicit, per-run opt-in to destructive behaviour |
| `HARNESS_CLUSTER_LABEL` | `issue145-disposable` | Declares the target a dedicated disposable cluster |
| `HARNESS_TARGET_SYSTEM_IDENTIFIER` | the cluster's `system_identifier` | Independently observed cluster identity |
| `HARNESS_CLUSTER_GUARD` | the random nonce written into the guard database at provisioning | Pre-provisioned proof that this cluster was set up for the harness |

`HARNESS_CLUSTER_LABEL` is a declaration, not proof, and neither is an
`issue145_*` database name. Identity is established from
`pg_control_system().system_identifier`, and ownership of an existing database
only from a guard-bound catalog marker. A matching label plus system identifier
without the pre-provisioned guard is refused.

The gate runs in two stages:

1. **Before any connection.** Missing or incorrect opt-in refuses immediately:
   nothing is opened, and the run reports 27 NOT RUN with exit status 2.
2. **After the version probe, before any mutation.** Every check fails closed and
   every statement in this stage is read-only:
   - the cluster's `system_identifier` must equal
     `HARNESS_TARGET_SYSTEM_IDENTIFIER`;
   - the guard database name may not be a PostgreSQL system database
     (`postgres`, `template0`, `template1`) or any scenario database
     (`issue145_t01`…`issue145_t27`); the guard must be a separate database, and
     this is checked before anything is provisioned;
   - the guard database `HARNESS_GUARD_DATABASE` must already exist and its
     comment must read `issue145-harness-guard:<system_identifier>:<nonce>` with
     the verified identifier and `HARNESS_CLUSTER_GUARD` as the nonce. The
     harness never creates this database, never writes the comment and never
     generates the nonce;
   - every database other than `postgres`, `template0`, `template1` and the
     guard database must be named exactly `issue145_t01` … `issue145_t27` and
     carry the marker `strikenova-issue145-harness-disposable:<identifier>:<nonce>`
     in its catalog comment. Any other name — including another `issue145_*`
     name — is refused, and nothing is terminated or dropped at this stage;
   - an existing fixed-name role may not have `SUPERUSER`, `BYPASSRLS`,
     `CREATEROLE` or `CREATEDB`, and its `LOGIN`/`NOLOGIN` attribute must be the
     one the harness would provision (`migrator` and `app` are LOGIN, `owner` is
     NOLOGIN). A mismatch is refused, never repaired with `ALTER ROLE`;
   - every membership involving those roles must be exactly
     `strikenova_prod_owner -> strikenova_prod_migrator` with `INHERIT FALSE,
     SET TRUE, ADMIN FALSE`. Any other membership, or any other option set, is
     refused rather than altered.

   A refusal here leaves the cluster exactly as found.

### Provisioning the guard (once, by the cluster owner, before any run)

The guard is the only part of the safety model the harness cannot create for
itself. Run this on the disposable cluster, as a role allowed to create a
database and execute `pg_control_system()`:

1. Generate a random nonce outside the harness and keep it out of Git:

   ```bash
   python3 -c 'import secrets; print(secrets.token_hex(32))'
   ```

2. Build the comment value from the cluster's own identity:

   ```bash
   psql -At -c "SELECT 'issue145-harness-guard:' || system_identifier::text || ':<nonce>' FROM pg_control_system()"
   ```

3. Create the guard database and paste that value into the comment:

   ```sql
   CREATE DATABASE issue145_guard;
   COMMENT ON DATABASE issue145_guard IS 'issue145-harness-guard:<system_identifier>:<nonce>';
   ```

   The guard must be its own database. It may never be `postgres`, `template0`,
   `template1` or one of `issue145_t01`…`issue145_t27`; the gate refuses any of
   those names before it provisions or drops anything.

4. Export the same nonce as `HARNESS_CLUSTER_GUARD` for every run, alongside
   `HARNESS_TARGET_SYSTEM_IDENTIFIER` from step 2.

`COMMENT ON DATABASE` accepts only a literal, so the value must be pasted in
verbatim. The nonce is not a database credential, but it is the evidence that
this cluster was provisioned for the harness: treat it as sensitive and never
commit it. A guard copied from another cluster keeps that cluster's identifier
and is refused here.

### Connection configuration

| Variable | Default | Used for |
|---|---|---|
| `DB_HOST` | empty (Unix socket) | all connections |
| `DB_PORT` | `55432` | all connections |
| `HARNESS_TARGET_SYSTEM_IDENTIFIER` | empty (run is refused) | cluster identity check, database marker |
| `HARNESS_CLUSTER_GUARD` | empty (run is refused) | guard nonce check, database marker |
| `HARNESS_GUARD_DATABASE` | `issue145_guard` | name of the pre-provisioned guard database; system and scenario database names are refused |
| `ADMIN_DB_USER` / `ADMIN_DB_PASSWORD` | falls back to `DB_USER` / `DB_PASSWORD` | administrator: create/drop databases and roles, grant on a scenario database, terminate backends |
| `MIGRATOR_DB_USER` | `strikenova_prod_migrator` | every scenario connection |
| `MIGRATOR_DB_PASSWORD` | empty | migrator login |
| `DB_USER` / `DB_PASSWORD` | empty | administrator fallback, and the password fallback for the APP connection |

The administrator must be allowed to:

- create and drop databases (to provision and tear down scenario databases);
- create roles (to provision the three fixed names);
- call `pg_terminate_backend` on harness-owned backends (T-12 and teardown);
- execute `pg_control_system()` (the cluster identity check). PostgreSQL 16
  restricts this function to superusers and members of `pg_monitor` unless
  `EXECUTE` is granted separately, so the administrator needs one of those. The
  harness **never** grants `pg_monitor`, superuser or function `EXECUTE`; if the
  check raises `42501` the run stops before any mutation and reports the
  privilege as a refused target (exit status 2, all 27 NOT RUN).

The migrator and APP roles must be able to log in. Use throwaway credentials for
this disposable cluster only — no real StrikeNova credential belongs in this
harness or its environment.

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

**Per-scenario cleanup:** `prepare_scenario_database()` writes the marker comment
on each database it creates, and `drop_scenario_db()` terminates backends
connected to that scenario database (never any other database) and drops it only
when the marker matches. A pre-existing database without the marker is refused,
not adopted. T-12 additionally terminates one backend **inside its own scenario
database**, which is that scenario's subject under test.

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
| T-16 | Topology B — non-inherited membership and grants | NOT RUN | Implemented (migrator session, `SET ROLE` owner window, the four grants issued on a separate migrator connection to `issue145_t16` after asserting `current_database()` and the owner context and `RESET ROLE` back, owner denied functions, return to migrator matrix); not executed |
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
export HARNESS_TARGET_SYSTEM_IDENTIFIER=<system_identifier of that cluster>
export HARNESS_CLUSTER_GUARD=<nonce written into the guard database>
export HARNESS_GUARD_DATABASE=issue145_guard   # default
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
  backslash-n; the SQL constants are unchanged from the previous revision.
- Built-in DB-free self-test, reproducible and published in the harness itself:

  ```bash
  python3 docs/reviews/issue145-t01-t27-harness/run_t01_to_t27.py --self-test
  # Total: 18, PASS: 18, FAIL: 0   (exit 0)
  ```

  It injects a fake driver and contacts no server. Its checks cover: autocommit
  on every connection opener and the expected database per opener; T-16 issuing
  its four grants on the `issue145_t16` connection and refusing them on
  `postgres` (with a source assertion that T-16 contains no inline `GRANT`);
  cluster identity from `pg_control_system()` rejecting a missing or mismatched
  identifier; refusal of an unmarked pre-existing scenario database with no
  mutation; `drop_scenario_db()` refusing an unmarked database; exact role
  `LOGIN` and membership-option validation (`INHERIT TRUE`, `SET FALSE`,
  `ADMIN TRUE` and a NOLOGIN migrator are all refused); complete constraint
  comparison rejecting `OR true`, an extra `IN` alternative and a changed
  operator; the version matrix (15.0, 16.15, 16.16, 16.20, 17.0, 18.0); bootstrap
  verifying before activation;   T-11/T-12 ordering, `FOR UPDATE NOWAIT` lock
  probes and the `57P01`/`08006` assertion; `expect_sqlstate` reading the driver
  SQLSTATE inside a real transaction; and T-23 remaining NOT RUN. This revision
  adds: the pre-provisioned guard being required, prefix-checked,
  identity-bound and nonce-checked (including the copied-cluster case); the
  `issue145_t01`…`issue145_t27` name allow-list rejecting `issue145_t28`,
  `issue145_t00`, `issue145_disposable`, `issue145_t01_backup` and `app_db`; a
  `42501` on `pg_control_system()` being reported as a refusal with no mutation;
  an unbounded 14-role membership chain reaching the migrator, a direct
  membership and a membership cycle all being rejected; the guard name being
  refused for `postgres`, `template0`, `template1` and scenario names; the four
  T-16 grants executing only inside the asserted owner window on the
  `issue145_t16` connection, with no GRANT on a wrong database, without the
  owner context, or after a failed `RESET ROLE`; and a counter proving no real
  driver connection is attempted during the self-test.
- Two no-server runs still produce 27 NOT RUN: exit 2 without the opt-in (no
  connection opened) and exit 0 with it (no server reachable; here `psycopg` is
  also absent, so the module's stub driver reports the unreachable path).
- No protocol behaviour was exercised against a database in this environment.
- **Unverified here:** PostgreSQL 16.16 `pg_get_constraintdef()` rendering of
  the nine expected definitions, `pg_control_system()` output shape, and every
  runtime SQLSTATE, lock and privilege observation. Static comparison cannot
  establish live catalog output.
