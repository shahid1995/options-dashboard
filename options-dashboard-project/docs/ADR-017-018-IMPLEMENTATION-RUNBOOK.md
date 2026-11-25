# ADR-017/018 implementation runbook — migration lease, identities and operator CLI

**Status:** implemented on `issue-145-fix` (PR #146, Draft), verified against a
disposable **PostgreSQL 16.16** instance and the SQLite suite.
**Design of record:** `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` (PR #149,
commit `ae2226e`). This runbook describes the code as implemented, not the
retired renewal-thread design.

## 1. What changed

| Area | Before | Now |
| --- | --- | --- |
| Serialization | `app/_migration_lock.py`: expiring lease + background renewal thread, polled TTL | `app/_migration_lease.py`: PostgreSQL row-lock lease with a 256-bit owner token (only its SHA-256 stored), versioned `SECURITY DEFINER` functions, deployment barrier |
| Migration entry point | `alembic upgrade head` (guarded only by convention) | `python -m app.migrate upgrade head`; bare Alembic on PostgreSQL fails closed in `alembic/env.py` |
| Connection | Alembic opened its own connection | The lease and the Alembic DDL share **one physical connection and one transaction** |
| Identity | one URL, implicitly the runtime role | `STRIKENOVA_MIGRATION_DATABASE_URL` required on PostgreSQL and must differ from `DATABASE_URL` |
| Startup backfill | per-user commits on the runtime engine | one transaction per user on the migration identity, `enter` first, mandatory refresh before commit |
| Recovery | renewal thread, takeover flags | server-side transaction abort: a dead owner's row lock is released by PostgreSQL |

The renewal thread, `try_acquire`, `wait_for_release`, `_execute_serialized`
and `MIGRATION_LOCK_*` settings are deleted. Old-protocol tests
(`test_migration_rehearsal_postgres.py`, `test_migration_rehearsal_cockroachdb.py`,
`test_issue145_startup_concurrency.py`, `test_issue145_harness_safety.py`)
and the old behavioural workflow are removed; the new evidence is listed in §9.

## 2. Configuration contract

| Setting | Required | Default | Meaning |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | — | Serving identity (`strikenova_production_app`) |
| `STRIKENOVA_MIGRATION_DATABASE_URL` | **yes on PostgreSQL** | — | Migration identity (`strikenova_prod_migrator`). Startup and every CLI command fail closed without it |
| `STRIKENOVA_OWNER_ROLE` | no | **empty** (Topology A) | Topology B only: `NOLOGIN` object owner; the migrator then holds non-inherited `SET`-only membership. Set it to e.g. `strikenova_prod_owner` to activate Topology B |
| `STRIKENOVA_MIGRATOR_ROLE` | no | `strikenova_prod_migrator` | Migration/backfill login |
| `STRIKENOVA_RUNTIME_ROLE` | no | `strikenova_production_app` | Serving login; must have no migration access |
| `MIGRATION_LEASE_TTL_SECONDS` | no | `300` | Bound on recovery of an abandoned lease row (measured, §7) |
| `MIGRATION_LEASE_WAIT_SECONDS` | no | `180` | Monotonic admission budget across retryable SQLSTATEs |
| `MIGRATION_LEASE_LOCK_TIMEOUT_MS` | no | `10000` | Server-side `lock_timeout` per attempt |
| `MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS` | no | `150000` | Per-user backfill statement timeout |
| `STRIKENOVA_LEASE_NONCE` | no | random per process | Extra entropy mixed into the owner token; never logged |

Validation is enforced at the settings boundary (positive, bounded integers;
statement timeout strictly below the TTL). Identities are compared after URL
normalization, so `postgres://`, `postgresql://` and `+psycopg` spellings of the
same endpoint are detected as equal and refused (`45004`).

### 2.1 Which topology is active

There is exactly one switch, and the default is Topology A:

| `STRIKENOVA_OWNER_ROLE` | Active topology | Who creates/owns application objects | Default privileges bound to |
| --- | --- | --- | --- |
| empty (default) | **Topology A** | the migration identity | `FOR ROLE <migrator> IN SCHEMA public` |
| set to a role name | **Topology B** | that `NOLOGIN` owner role, reached only through the `SET ROLE` window | `FOR ROLE <owner> IN SCHEMA public` |

In both topologies the runtime role receives DML and sequence `USAGE` on new
application objects through **schema-scoped** defaults (`IN SCHEMA public`). A
*global* default privilege would also cover `migration_internal` and is a
verification failure (`45004`). Neither topology grants the runtime role
anything beyond DML.

### 2.2 The two privilege models in detail

**Topology A (default) — the migrator owns, and its ordinary privileges are
revoked.** The migrator creates and owns `positions`, `paper_orders`,
`strategy_leg_exposures` and their sequences. Ownership alone would let the
migration identity write and delete anything it owns, so the guarded migration
applies the ADR §8.3 restrictions **inside the same protected DDL transaction**,
after Alembic and `RESET ROLE`, and before the pre-commit `enter`:

```sql
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.positions              FROM <migrator>;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.paper_orders           FROM <migrator>;
REVOKE UPDATE, DELETE, TRUNCATE          ON public.strategy_leg_exposures FROM <migrator>;
REVOKE ALL                               ON SEQUENCE public.strategy_leg_exposures_id_seq FROM <migrator>;
GRANT  USAGE                             ON SEQUENCE public.strategy_leg_exposures_id_seq TO   <migrator>;
```

PostgreSQL 16 **does** apply these revokes to the object owner's own ACL: after
the chain, `UPDATE public.positions` and `DELETE FROM public.strategy_leg_exposures`
fail `42501` **for the owning migrator**, while `SELECT` everywhere,
`INSERT` on `strategy_leg_exposures`, and sequence `USAGE` remain. The revokes
are re-applied on **every** later `upgrade head` (the code runs them inside the
same transaction as the DDL), so a subsequent migration cannot silently restore
the privileges.

What the revokes cannot remove is ownership itself. The owner always keeps
inherent object-definition powers — it can `ALTER` or `DROP` its own tables and
grant an ordinary privilege back to itself. Those are not ordinary DML
privileges and are deliberately **not** represented as such: the §8.3 revokes
bound accident, while ADR §8.4 and D-2 govern who owns the tables.

**Topology B — ownership is separated and reached only through `SET ROLE`.**
The `NOLOGIN` owner creates the objects inside the explicit window; the migrator
holds no inherited rights at all. Its privileges come from four explicit
grants and the effective matrix is identical to Topology A:

```sql
GRANT SELECT         ON public.positions              TO <migrator>;
GRANT SELECT         ON public.paper_orders           TO <migrator>;
GRANT SELECT, INSERT ON public.strategy_leg_exposures TO <migrator>;
GRANT USAGE          ON SEQUENCE public.strategy_leg_exposures_id_seq TO <migrator>;
```

The backfill never elevates: `RESET ROLE` happens before the post-migration
callback. The elevation exists only so Alembic's DDL can `ALTER` owner-owned
objects, and it is verified (`current_user`) at both ends.

## 3. Transaction and connection boundaries

* **Lease + DDL:** one connection, one transaction. `acquire` → `enter` on the
  same `Connection`, then Alembic runs with `attributes["connection"]` and
  `transaction_per_migration=False`; `env.py` refuses any PostgreSQL
  connection that did not arrive through the lease.
* **Ownership:** the lease row lock itself. A live owner cannot be stolen from
  while its transaction is open; a crashed owner is aborted by PostgreSQL.
  The `expires_at` column only tells a *new* process whether it may take over
  an abandoned row.
* **Backfill:** user enumeration is committed first; each user then gets its
  own transaction whose first statement is `enter_lease`, followed by the
  idempotent exposure writes and a mandatory ownership refresh immediately
  before `COMMIT`. Any ownership error rolls back that user only. Backfill
  always runs as the migration identity: in Topology B the `RESET ROLE`
  happens before the post-migration callback, so the backfill is never
  elevated.
* **Barrier:** `close_barrier` takes the protocol row lock, records the owner
  fingerprint it observed, sets `barrier_state='closed'`, and refuses new
  admission with `45004`. An already-admitted owner keeps working.

## 4. Operator CLI

```bash
python -m app.migrate upgrade head                  # only supported migration path
python -m app.migrate bootstrap-lease               # verify (default; never writes a version)
python -m app.migrate bootstrap-lease --verify      # explicit verification
python -m app.migrate bootstrap-lease --close-barrier
python -m app.migrate bootstrap-lease --upgrade-protocol 2
python -m app.migrate status                        # read-only snapshot
```

* `--verify` prints `state before` / report / `state after`. On a valid install
  all three show the same `installed_version`; a mismatch is reported and
  refused, never repaired.
* `--upgrade-protocol N` is the only path that may change `installed_version`,
  and only for the declared authorized transition. A direct
  `UPDATE migration_internal.lease_protocol SET installed_version = …` is not a
  supported upgrade.
* `--close-barrier` records the live owner and blocks admission; reopening is
  a verified bootstrap (`--verify`) once the owner has quiesced.
* Interrupting any command rolls the open transaction back; `status` and
  `--verify` hold the lease row lock briefly and release it on commit.

CLI and startup use the **migration** identity, never the runtime identity.

## 5. Runbooks

### 5.1 Empty database (first bootstrap)

1. Provision roles (out of band, an admin identity that is never used by the
   application):
   * **Topology B** — create `strikenova_prod_owner` (`NOLOGIN`),
     `strikenova_prod_migrator` (`LOGIN`) and `strikenova_production_app`
     (`LOGIN`); grant `strikenova_prod_owner` to the migrator **with
     `INHERIT FALSE, SET TRUE`**; set `STRIKENOVA_OWNER_ROLE=strikenova_prod_owner`.
   * **Topology A** (default) — create only `strikenova_prod_migrator`
     (`LOGIN`) and `strikenova_production_app` (`LOGIN`); leave
     `STRIKENOVA_OWNER_ROLE` **empty**. The migrator owns the database and
     every object it creates.
2. Set `DATABASE_URL` (runtime) and `STRIKENOVA_MIGRATION_DATABASE_URL`
   (migrator) to distinct identities.
3. Create the schema-scoped default privileges for the creator role
   (`STRIKENOVA_OWNER_ROLE` if set, else the migrator) in schema `public`:
   `SELECT/INSERT/UPDATE/DELETE ON TABLES` and `USAGE/SELECT ON SEQUENCES` to
   the runtime role. Do **not** create a global (`ALTER DEFAULT PRIVILEGES`
   without `IN SCHEMA`) entry.
4. `python -m app.migrate bootstrap-lease` — creates the schema, tables,
   functions and protocol row, verifies catalog shape, schema/table/function
   ACL and default ACLs, then sets `bootstrap_state='ready'`.
5. `python -m app.migrate upgrade head` — lease + full Alembic chain in one
   transaction; in Topology B the DDL runs inside a verified
   `SET ROLE strikenova_prod_owner` / `RESET ROLE` window, in Topology A there
   is no role window and the §8.3 restrictions (§2.2) are applied before the
   transaction commits. No manual revoke step is needed, and re-running the
   command re-applies them.
6. Apply the ADR-018 §8.3 grants to the runtime role and start the app.

On a genuinely empty database the migrator must not assume the runtime grants
exist yet; the startup backfill only becomes possible after step 6.

### 5.2 Existing installation

`--verify` must pass before and after any change. If verification reports a
catalog or ACL problem, stop: no automatic repair is performed by design.
Re-run `bootstrap-lease` only when the installation is genuinely absent.

### 5.3 Protocol upgrade and barrier

1. `bootstrap-lease --close-barrier`; confirm the reported
   `lease_row_lock`/owner fingerprint.
2. Wait for the live owner to finish (quiescence), then `--verify`.
3. `bootstrap-lease --upgrade-protocol N`; both `state before` and
   `state after` must show the same `installed_version`; a mismatch is
   reported and refused, never repaired.

## 6. CI / test evidence workflows

The CI suite is divided into four independent gated workflows. Each runs in
its own PostgreSQL context and produces authoritative JUnit XML evidence.

### 6.1 Application integration / regression suite

* **Uses a PostgreSQL service container** pinned to `postgres@sha256:3950ad7c3148ca2175ef1b6c36c9efbf78957ae8fbe8b3a5d7c35af50500382f` (PostgreSQL 16.16).
* Runs `tests/test_issue145_application_integration.py` and
  `tests/test_issue145_pg_restart.py` against the service container.
* 83 passed (all four suites combined; integration + regression share the service container).

### 6.2 PostgreSQL compatibility suite

* **Uses a PostgreSQL service container** (same pinned image).
* Runs the compatibility modules: compatibility, importer, failure injection,
  large rehearsal, hardening, Day 5/6.
* 122 passed.

### 6.3 Restart / reconnection isolated CI job

* **Has its own isolated PostgreSQL 16.16 installation and its own data directory.**
  It deliberately has **no shared PostgreSQL service container**.
* Fetches the SHA-256-pinned 16.16 PGDG build for Ubuntu 24.04 from
  `ISSUE145_PG_BINDIR`, asserts `postgres (PostgreSQL) 16.16` exactly,
  fails if `ISSUE145_PG_BINDIR` is unset.
* Runs `tests/test_issue145_pg_restart.py` as its own job with a minimum of 5 passes.
* The job creates its own `initdb` cluster in a temporary directory and
  restarts only that server; `ISSUE145_PG_ADMIN_URL` is not used.
* 5 passed (local 16.16).

### 6.4 StrikeNova Status Gate

* **A separate status-tracker/master-plan validation workflow.**
  It does **not** run a PostgreSQL database suite.
* Validates application-level status and master-plan state, independent of
  database gates.
* Does not classify as a service-container database gate.

### 6.5 Three evidence classes kept distinct

1. **Local execution.** The three service-container gates and the restart
   module were also run locally against PGDG 16.16 server binaries extracted
   into the workspace (the container socket was unavailable in that
   environment). The gated suites are run exactly as the workflows run them,
   with `--junitxml` plus the recorded pytest exit status passed to
   `scripts/pytest_gate.py`.

2. **Isolated restart CI job** (`restart-runtime-reconnect`). It has no
   `services:` block and owns the binaries and data directory it restarts:

   ```bash
   ISSUE145_PG_BINDIR=<16.16 bin> python -m pytest -q tests/test_issue145_pg_restart.py
   ```

   The job fetches the exact SHA-256-pinned 16.16 build, asserts
   `postgres (PostgreSQL) 16.16`, fails if `ISSUE145_PG_BINDIR` is unset,
   and gates the JUnit report (`restart|5|…`), so a skipped or silently
   uncollected test fails the job. The module creates its own `initdb` cluster
   in a temporary directory and restarts only that server;
   `ISSUE145_PG_ADMIN_URL` is not used by it.

3. **The three service-container database gates**
   (`application-integration`, `PostgreSQL compatibility`).
   Their PostgreSQL is a service container they cannot restart; they never
   exercise the restart path.

4. **StrikeNova Status Gate** — A separate status-tracker/master-plan validation
   workflow that does not run a PostgreSQL database suite. It is distinct from
   all three database evidence classes above.

## 7. PostgreSQL version check

The restart/reconnection workflow validates PostgreSQL 16.16 by checking the
package version output. The check accepts `postgres (PostgreSQL) 16.16` with an
optional distribution suffix (e.g. `16.16-1.pgdg24.04+1`). It does **not**
require exact equality against a suffix-free output string — the version
validation tolerates the distribution suffix that package managers append.

The workflow asserts `postgres (PostgreSQL) 16.16([[:space:]]|$)` via a grep
pattern, so both `postgres (PostgreSQL) 16.16` and
`postgres (PostgreSQL) 16.16 (Ubuntu 16.16-1.pgdg24.04+1)` are accepted,
while `16.15`, `16.17`, and `16.160` are correctly rejected.

## 8. Known limitations

* Concurrent first installation on a genuinely empty database is exercised with
  sequential cold starts plus a dedicated bootstrap-contention test; a
  simultaneous two-process first-ever bootstrap is not claimed as proven.
* The two-process startup proof (§9) is deterministic for *contention*: the
  test pauses the first worker **inside its first per-user backfill
  transaction**, after `enter` and after it has attempted real writes, and
  observes the second worker waiting for admission; releasing the pause lets
  both finish. It is not a stress test and does not claim to measure
  throughput or a real deployment's timing. It asserts the advisory guard
  lock specifically — `lease_enter_v1` takes `pg_advisory_xact_lock` before
  the lease row `FOR UPDATE`, so a queued second worker never reaches the row
  lock. The test deliberately does not claim row-lock contention.
* The endpoint-equivalence proof covers same-host URLs and explicitly
  declared aliases only, and comma-separated migration host lists are refused
  rather than proven host by host. After preflight, every new migration
  connection re-checks the proven identity, so a reconnect cannot silently land
  on an unproven cluster. The identity is proven by the cluster's
  `system_identifier` plus the database name and `pg_database` OID, all of
  which survive a restart; the postmaster start time is diagnostic and is
  never compared as identity. A revoked or absent `pg_control_system()` fails
  closed with the one grant that restores it — there is no weaker fallback to
  accidentally accept. `system_identifier` does not distinguish Neon branches
  (a branch is a copy-on-write fork of the same cluster, so it shares the
  identifier and database OIDs); branch and endpoint separation is enforced by
  the static host/alias check instead, and no provider API is queried, so
  nothing here proves that a pooled route terminates at the same *project*
  rather than a same-named cluster.
* The real-restart proof (`tests/test_issue145_pg_restart.py`) runs in its own
  isolated CI job (`restart-runtime-reconnect`) with its own 16.16 binaries and
  data directory, because the PostgreSQL the three other suites use is a
  service container they cannot restart and must not disturb. The proof is a
  real server process restart on an unchanged data directory — the same
  identity-invalidation event a Neon compute suspend/wake or provider failover
  produces — but **no live Neon suspend/wake, branch fork, TLS or failover was
  tested**. `system_identifier` does not distinguish Neon branches, so branch
  separation rests on the static host/alias check, not on this proof. The
  reconnection proof is also single-node and local to the job: it shows that
  a pooled connection survives a restart via `pool_pre_ping`, but it does not
  exercise multi-node or production topologies.

## 10. CI run details (from PR #151)

The following CI jobs are defined in `.github/workflows/issue145-application-pg16.yml`:

| Job name | Runs on | Description | Test counts |
| --- | --- | --- | --- |
| `application-integration` | ubuntu-latest | Integration and regression suites against a pinned PostgreSQL 16.16 service container | See PR #151 checks |
| `restart-runtime-reconnect` | ubuntu-latest | Isolated restart job with its own 16.16 PGDG build, no service container | See PR #151 checks |
| `postgres-compatibility` | ubuntu-latest | PostgreSQL compatibility modules against a service container | 122 passed |
| `Status Gate` | ubuntu-latest | Status-tracker/master-plan validation (no PostgreSQL database suite) | N/A |

## 11. Evidence table summary

| Suite | Tests | Passed | Notes |
| --- | --- | --- | --- |
| Integration & regression | 181 | 83+ (shared service container) | Includes application-integration and regression |
| PostgreSQL compatibility | 122 | 122 | Service-container database gates |
| Restart / reconnection | 5 | 5 | Isolated CI job, own PG 16.16 binaries |
| Status Gate | N/A | N/A | Non-database validation workflow |

Three evidence classes are kept strictly distinct:
- Service-container database gates (integration, regression, compatibility).
- Isolated restart CI job (real server restart, own binaries).
- Status Gate (status-tracker validation, no database suite).