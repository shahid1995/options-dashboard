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
`test_issue145_startup_concurrency.py`, `test_issue145_harness_safety.py`) and
the old behavioural workflow are removed; the new evidence is listed in §9.

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
verification failure (`45004`). Because an object's owner inherently holds every
privilege on it, the §8.3 revoke set is only meaningful for the migrator under
Topology B: under Topology A the migrator owns the tables and can therefore
always alter or drop them. Neither topology grants the runtime role anything
beyond DML.

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

```
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
   ACLs and default ACLs, then sets `bootstrap_state='ready'`.
5. `python -m app.migrate upgrade head` — lease + full Alembic chain in one
   transaction; in Topology B the DDL runs inside a verified
   `SET ROLE strikenova_prod_owner` / `RESET ROLE` window, in Topology A there
   is no role window at all.
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
   `state after` are printed.
4. Reopen the barrier with `--verify` and resume admission.
5. Cancellation: nothing is committed until the protocol row transaction
   commits, so an interrupted upgrade leaves the previous version installed;
   re-verify and retry.

### 5.4 Deployment ordering (availability)

1. Bootstrap (once), then `upgrade head`, **then** ADR-018 grants for the
   runtime role, **then** start/roll instances.
2. Rolling deploys: existing instances keep serving; new instances wait on the
   lease (bounded by `MIGRATION_LEASE_WAIT_SECONDS`) or fail closed with
   `45002`/`45004` and are restarted by the platform.
3. The startup backfill of a fresh instance runs under the migrator identity
   after `RESET ROLE`; the runtime role never performs it.

### 5.5 Recovery

| Situation | Behavior |
| --- | --- |
| Process dies holding the lease | PostgreSQL aborts its transaction; the next admission takes the row after `expires_at`, no manual cleanup |
| Process dies mid-backfill | Its user transaction rolls back; that user is retried on the next startup; already committed users are not duplicated |
| Admission times out | `LeaseError` with the last SQLSTATE (`55P03` under contention); platform restart policy applies |
| Barrier left closed | `--verify` reports `barrier_state=closed`; reopen only after quiescence |
| Runtime role tries migration DDL/functions | `42501` (no `USAGE` on `migration_internal`, no EXECUTE) |
| Bootstrap half-finished | `bootstrap_state <> 'ready'` → `45005`; re-run bootstrap in a known-empty state |

## 6. SQLSTATE contract (as implemented)

| SQLSTATE | Meaning | Retryable by the lease client |
| --- | --- | --- |
| `45001` | Owner mismatch (token/fingerprint) | no |
| `45002` | Lease held by a live owner / no owner to enter | yes (until the wait budget) |
| `45004` | Configuration refused: ids equal/missing, barrier closed, unguarded path | no (barrier: fail fast) |
| `45005` | Lease/protocol row missing, bootstrap not ready | no |
| `45006` | Protocol version unsupported by this build | no |
| `55P03` | `lock_timeout` on the row lock | yes |
| `40001` / `40P01` | Serialization failure / deadlock | yes |

## 7. Measured backfill timing and derived values

Measured on a disposable PostgreSQL 16.16 instance
(`test_measure_per_user_backfill_timing`, 50 synthetic users × 2 positions,
idempotent upserts inside a per-user transaction with `enter` and a pre-commit
refresh). Two runs are on record and both are **synthetic/single-host**; neither
is a Neon or production performance result:

| Measurement | p50 | p99 | max | Source |
| --- | --- | --- | --- | --- |
| Historical (earlier revision) | 4.9 ms | 35.3 ms | 35.3 ms | earlier local run, recorded in PR #146 |
| Current implementation | 6.0 ms | 18.2 ms | 18.2 ms | local disposable 16.16 run of this revision |

```
per-user backfill over 50 users: p50=6.0ms p99=18.2ms max=18.2ms
ttl=300s statement_timeout=150000ms
```

The derivation below uses the **slower** of the two (35.3 ms), so the defaults
stay conservative regardless of which measurement is cited.

Derivation:

* **TTL 300 s**: ≈ 8 500 × the slower measured p99 (35.3 ms). It only bounds how long a *new*
  process waits before taking over an abandoned row, so a wide margin costs
  nothing at normal scale while keeping a wedged deploy bounded to minutes.
* **Statement timeout 150 s**: ≈ 4 250 × the slower measured p99 for one user.
  The configuration validator enforces it **strictly below** the TTL
  (`MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS < MIGRATION_LEASE_TTL_SECONDS ×
  1000`): equality and larger values are refused at the settings boundary, so
  a single user can never hold the lease past its own expiry. It is large
  enough for a slower production host or a user with many positions.
* **Wait 180 s / lock timeout 10 s**: the wait budget must exceed a normal
  full-chain migration on this chain (~20 s observed end to end on the test
  instance, including backfill) with headroom for platform cold starts; the
  lock timeout keeps each attempt cheap so the retry loop stays responsive.

These are *measured defaults for discussion*, not a production sizing claim:
the measurement is synthetic and single-host. Re-measure on the target
instance class before enabling a real cutover.

## 8. Privilege boundary

* `migration_internal` schema ACL: PUBLIC has neither `USAGE` nor `CREATE`, and
  the runtime role has neither effectively (`has_schema_privilege`), checked
  through `pg_namespace.nspacl` with `aclexplode(...)` and
  `acldefault('n', nspowner)` so an untouched schema is judged on its implicit
  ACL as well. The runtime role is also required to be neither a superuser nor
  a member of the schema owner, so an inherited path cannot grant access.
* `migration_lease` / `lease_protocol` table privileges: owner only.
* Functions: EXECUTE to the migrator only; no PUBLIC; `SECURITY DEFINER` with
  `SET search_path = pg_catalog`.
* Runtime role: no direct or transitive membership in migrator/owner; no
  ownership of migration objects; no `CREATE` on schema `public`.
* Migrator: in Topology B `SET`-only membership in the owner role, no
  `INHERIT`, no superuser, and no default privileges of its own; in Topology A
  it owns the application objects it creates.
* Default ACLs are inspected globally (`pg_default_acl`) and for
  `migration_internal`; a global default that would grant PUBLIC or the runtime
  role access to the schema is a verification failure.

Prohibited operations were proven to raise `42501`: direct DML on the lease
  tables, `SELECT` from the schema, `EXECUTE` of the lease functions,
  `SET ROLE` into the owner from the runtime identity, `ALTER`/`DROP`/`TRUNCATE`
  of application tables, and `CREATE TABLE` in schema `public`.

Negative controls (`test_schema_acl_verification_positive_and_negative_controls`)
prove the verifier fails closed: a valid installation passes, granting PUBLIC
`USAGE` or `CREATE` on `migration_internal` makes `bootstrap-lease --verify` fail
with `45004`, granting the runtime role `USAGE` fails the same way, and after
revoking each grant the installation verifies again with an unchanged protocol
version and no residual PUBLIC entries.

## 9. Evidence

| Suite | Scope | Result |
| --- | --- | --- |
| `tests/test_issue145_application_integration.py` | real PG 16.16: concurrent `init_db()`, single connection, killed worker, injected failure, CLI, barrier, protocol, expiry, privileges, both ownership topologies, schema-ACL positive/negative controls, timing | 22 passed |
| `tests/test_migration_serialization.py` | DB-free protocol/config/retirement contract, including the timeout-below-TTL rule and its boundaries | 46 passed |
| `tests/test_db_migration.py`, `test_alembic_*.py`, `test_day5_alembic_authority.py`, `test_migration_identity_separation.py`, `test_leg_exposure.py` | SQLite chain, FK behavior, identity separation, exposure idempotency | 135 passed |
| `PostgreSQL compatibility` suite (9 modules: compatibility, importer, failure injection, large rehearsal, hardening, Day 5/6) | disposable PG 16.16 schemas built only through `python -m app.migrate upgrade head` | 122 passed |
| `.github/workflows/issue145-application-pg16.yml` | the two suites above on a pinned `postgres@sha256:3950ad…` 16.16 service, exact `server_version_num = 160016` gate, evidence upload, cleanup | see PR #146 checks |

Local verification used PGDG 16.16 server binaries extracted into the workspace
(the container socket was unavailable in that environment); CI uses the pinned
digest. Both report `server_version_num = 160016`.

## 10. Known limitations

* Concurrent first installation on a genuinely empty database is exercised with
  sequential cold starts plus a dedicated bootstrap-contention test; a
  simultaneous two-process first-ever bootstrap is not claimed as proven.
* The compatibility and importer rehearsals build their schema as Topology A
  and perform the data import as the migration identity; they do not re-prove
  the runtime-identity boundary (that lives in the integration suite).
* Topology A cannot revoke table privileges from its own creator: an object's
  owner always holds them. §8.3 revokes are therefore a Topology B control.
* The TTL/timing measurement is synthetic and single-host (§7).
* No production/Neon/Render change is part of this work: no live roles, grants,
  environment variables or cutover were touched.
* The unrelated Pylint workflow still fails on its own `actions/setup-python@v3`
  setup on Ubuntu 24.04; it is not repaired here.
