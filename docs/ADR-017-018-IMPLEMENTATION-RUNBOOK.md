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
| `STRIKENOVA_MIGRATION_TARGET_ALIASES` | no | empty | Explicit `hostA=hostB` route equivalence for one logical database (e.g. a Neon pooled route and its direct route). Empty means different hosts are different databases |

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

### 2.3 How the two URLs are proven to target one logical database

The validator no longer stops at "the identities are different". ADR-018 §8.1
requires two *distinct, named* logins for *one* logical database, so
`assert_guarded_migration_configuration` checks five independent properties of
the parsed URLs and refuses (`45004`) anything it cannot prove:

1. **Dialect** — both `DATABASE_URL` and `STRIKENOVA_MIGRATION_DATABASE_URL`
   must be PostgreSQL. SQLite is not an alternative migration target, and
   CockroachDB is retired; either is refused rather than silently migrated.
   A PostgreSQL migration URL with a non-PostgreSQL runtime URL is refused too
   (it would have no proven target).
2. **Distinct login** — the usernames must differ. The same login with a
   different password is *not* identity separation and is refused.
3. **Same database name** — a different `database` component is refused.
4. **Same endpoint** — `host:port` must be identical after normalization
   (lower-cased host, trailing dot stripped, default port 5432). A different
   host is refused unless it is *declared* equivalent in
   `STRIKENOVA_MIGRATION_TARGET_ALIASES`.
5. **The configured identities** — the migration URL login must be exactly
   `STRIKENOVA_MIGRATOR_ROLE` (a blank role name is refused), and the runtime
   URL login must be exactly `STRIKENOVA_RUNTIME_ROLE` when that is configured.
   "Different from the runtime login" is a weaker contract: an unrelated login,
   a shared admin login or a superuser that merely differs from the runtime
   role would satisfy separation while executing migrations as the wrong
   principal.

The parser also refuses two connection forms that would make a URL's text
diverge from the target it really reaches:

* any query parameter libpq merges into the connect arguments after the
  authority/path — `host`, `hostaddr`, `port`, `dbname`, `database`, `user`,
  `password`, `service`, `servicefile`, `passfile` — because `?user=` or
  `?dbname=` can authenticate as, or connect to, something other than the URL
  appears to name. Unrelated options (`sslmode`, `application_name`, …) are
  preserved;
* a comma-separated host list, because it is a libpq failover list and a later
  host may serve a cluster this check never proved.

Every URL that is blank, malformed, partial (no host, no database, no login)
or unparseable fails closed here, before an engine, a connection, a lease or
any DDL exists. Error text names only the setting and the failing attribute
(role, database name, ports); the URL and its password are never embedded.

On SQLite, a separate `STRIKENOVA_MIGRATION_DATABASE_URL` naming a *different*
file is refused (`45004`). The SQLite path hands Alembic the runtime engine, so
the only supported local arrangement is one database (single identity): the
configured migration target must resolve to the runtime SQLite file. A
migration must never silently apply to one file while the configured target is
another.

### 2.4 The live connection, not only the URL, must be the migrator

The URL contract proves what the configuration *asks for*. Three further
checks prove what the server *actually authenticated*:

* `_install_migration_identity_guard` registers a SQLAlchemy `connect`
  listener on the migration engine before its first connection. Every DBAPI
  connection therefore reads `session_user`, `current_user` and the session
  login's role attributes, and refuses (`45004`) while the connection is still
  being created when the login is not the configured migrator, when
  `current_user` differs, when the role is not a `LOGIN`, or when it carries
  `SUPERUSER`, `CREATEROLE`, `CREATEDB`, `BYPASSRLS` or `REPLICATION`.
  `lease.acquire()`, the DDL transaction, every backfill transaction and every
  pooled reconnect are covered, not one sampled connection.
* `_assert_migration_session_identity` runs before the §8.3 verification in
the DDL transaction (i.e. after `RESET ROLE`) and again before the startup
  backfill's first write. `session_user` cannot be changed by `SET ROLE` or an
  inherited membership, so a login that can *assume* the migrator is still
  refused.
* `bootstrap-lease` and `--close-barrier` extend `_assert_migrator_identity` to
  require both `session_user` and `current_user` to be the migrator.

Topology B's approved exception is preserved: inside the DDL window
`current_user` is the NOLOGIN owner, but `session_user` remains the migrator and
`_reset_owner_role` proves the window closed before verification and before the
backfill.

The runtime side is verified the same way. The runtime engine registers a
`connect` listener that reads `session_user`/`current_user` on **every** new
DBAPI connection, requires both to be `STRIKENOVA_RUNTIME_ROLE`, requires that
role to differ from the migrator, and refuses elevated attributes. The §8.3
runtime checks are then evaluated against that verified `session_user`, never
against the username declared in `DATABASE_URL`. This is what catches a URL
whose `?user=` parameter authenticates as the migrator while its authority
username still looks like the application.

The target proof is no longer one-time. After the first fully proven migration
connection, `_install_migration_identity_guard` records the restart-stable
cluster/database identity and every later migration connection re-reads and
must match it before it may execute a statement. A reconnect or failover to
another cluster is refused (`45004`) before the lease, DDL or backfill can use
it, while a reconnection after a restart of the *same* server is accepted.

**Supported endpoint topology.** One PostgreSQL cluster, one database, two
logins. A second route to that same endpoint (the Neon pooled host
`ep-x-pooler.<region>.aws.neon.tech` versus the direct host
`ep-x.<region>.aws.neon.tech`) is accepted only when the pair is declared
explicitly, e.g.

```
STRIKENOVA_MIGRATION_TARGET_ALIASES=ep-x-pooler.eu.aws.neon.tech=ep-x.eu.aws.neon.tech
```

Declarations are validated, not trusted: both names must be literal hosts,
distinct, and the ports must match. A declaration alone is not proof, so after
the static check `_verify_logical_target` opens both URLs and compares what the
server reports, and refuses (`45004`) when they differ.

**The identity is restart-stable.** It is the tuple

```
(current_database(), pg_database.oid, pg_control_system().system_identifier)
```

All three values live in the data directory rather than in the server process,
so all three survive a PostgreSQL restart, crash recovery and a Neon compute
suspend/wake. `system_identifier` is the 64-bit value `initdb` generates: it is
identical for every connection to one cluster (primaries and replicas) and
different for any other cluster. It is readable without superuser on stock
PostgreSQL 16 because the function is created with the default function ACL,
which grants `EXECUTE` to `PUBLIC` (`=X/<owner>`), so both the runtime and the
migration LOGIN read it directly.

`pg_postmaster_start_time()` is deliberately **not** part of the identity. It
changes on every restart without changing the target, so using it as identity
can only reject a legitimate reconnection after a restart or a compute
suspend/wake. It is still read (canonical epoch microseconds, timezone-free)
as a diagnostic: a restart observed between the two preflight connections is
logged and does not change the verdict. `tests/test_issue145_pg_restart.py`
proves this against a real server process restart on an unchanged data
directory.

**What the server-side proof does and does not establish.** It proves that two
connections (and every later reconnection) reach one PostgreSQL cluster and one
database. It does **not** identify a Neon branch, project or compute endpoint:
Neon branches are copy-on-write forks, so a branch shares its parent's
`system_identifier` and database OIDs. Branch/endpoint separation therefore
rests on the *static* check above — an undeclared different host is refused
before any connection is opened, and a declared alias is the operator's
explicit statement that both routes are the same endpoint. There is no implicit
`-pooler` suffix heuristic and no wildcard.

**When the identifier is unreadable, the check fails closed.** PostgreSQL
documents that a hardened installation may revoke `EXECUTE` on
`pg_control_system()`. Without it the only remaining evidence is restart-unsafe,
so the connection is refused (`45004`) with the exact remedy rather than
accepted on a weaker comparison: grant `EXECUTE` on `pg_control_system()` to
the runtime and migration logins. That is one function privilege which stock
PostgreSQL 16 already grants to `PUBLIC`; no administrative or superuser
privilege is required or introduced. Unexpected database errors (anything other
than `42501`/`42883`) also fail closed.

This connection-time proof runs in `_migration_engine()`, so every caller
(`app.migrate` commands, startup migration, tests) fails before
`lease.acquire()` if the target cannot be established.

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
* **Timeouts:** `lock_timeout` bounds each lock acquisition. `statement_timeout`
  bounds each *statement*, **not** the whole multi-statement transaction; a
  per-user transaction may contain several statements that each fit the
  timeout. The TTL does **not** bound the duration of an active transaction: a
  running user transaction keeps the lease row lock and its own locks past
  `expires_at` while a statement is still executing, or blocked on a lock.
  The mandatory pre-commit `enter` refresh fails once ownership has expired,
  so the transaction cannot *commit* as owner after expiry; PostgreSQL rolls
  it back. Until that transaction actually ends, a new process's
  `lease_acquire` waits for the row lock, so TTL expiry alone does not admit
  it. When a timeout fires, PostgreSQL rolls that user's transaction back, the
  remaining users are not attempted, and the lease is released; users already
  committed stay committed. Supported recovery for a wedged or long-running
  statement is to let it finish, cancel the backend (`pg_cancel_backend`) when
  that is safe, or terminate it (`pg_terminate_backend`) as an operator
  action; the lease then becomes acquirable. There is no background renewal
  thread.
* **Cleanup:** a failure before acquisition (preflight or `acquire`) attempts
  no release because ownership was never taken, but still disposes the
  migration engine. After acquisition, release runs in its own short
  transaction exactly once; a cleanup failure is logged and never hides the
  original exception, and engine disposal still runs in the outer `finally`
  even when `release` itself raises.

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

1. Provision roles and their prerequisites (out of band, an admin identity
   that is never used by the application):
   * **Topology B** — create `strikenova_prod_owner` (`NOLOGIN`),
     `strikenova_prod_migrator` (`LOGIN`) and `strikenova_production_app`
     (`LOGIN`).
     * The migrator needs `CREATE` on the database (or database ownership) so
       `bootstrap-lease` can create the `migration_internal` schema. On a
       database owned by an administrator:
       `GRANT CREATE ON DATABASE <db> TO strikenova_prod_migrator`.
     * The owner needs `CREATE` and `USAGE` on schema `public` so the guarded
       DDL can create application objects there:
       `GRANT CREATE, USAGE ON SCHEMA public TO strikenova_prod_owner`
       (executed by the schema owner or a superuser; PostgreSQL refuses it
       otherwise).
     * Grant the owner to the migrator with all three options the verifier
       asserts:
       `GRANT strikenova_prod_owner TO strikenova_prod_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE`.
       `INHERIT TRUE` would hand the owner's privileges to the backfill
       identity and is refused.
     * Set `STRIKENOVA_OWNER_ROLE=strikenova_prod_owner`.
   * **Topology A** (default) — create only `strikenova_prod_migrator`
     (`LOGIN`) and `strikenova_production_app` (`LOGIN`); leave
     `STRIKENOVA_OWNER_ROLE` **empty**. The migrator owns the database and
     every object it creates.
2. Set `DATABASE_URL` (runtime) and `STRIKENOVA_MIGRATION_DATABASE_URL`
   (migrator) to the same host/port/database with distinct logins (§2.3). If
   the provider exposes a second route to the same endpoint, declare it in
   `STRIKENOVA_MIGRATION_TARGET_ALIASES`; otherwise the second URL is refused.
3. Grant the runtime role `USAGE` on schema `public` and make sure it has no
   `CREATE` there: `GRANT USAGE ON SCHEMA public TO
   strikenova_production_app; REVOKE CREATE ON SCHEMA public FROM
   strikenova_production_app;`. Its table and sequence privileges come only
   from the defaults in the next sentence.

   PostgreSQL 15+ no longer grants `CREATE` on `public` to `PUBLIC`, but a
   database upgraded from an earlier major version can still carry that
   inherited grant. Inspect and remove it when present (as the schema owner):
   `REVOKE CREATE ON SCHEMA public FROM PUBLIC;`. Verify the runtime role's
   **effective** privilege, which includes anything inherited through
   `PUBLIC`: `SELECT has_schema_privilege('strikenova_production_app',
   'public', 'CREATE');` must return `false`. The guarded upgrade enforces the
   same effective check and refuses to migrate otherwise.

   Create the schema-scoped default privileges for the creator role
   (`STRIKENOVA_OWNER_ROLE` if set, else the migrator) in schema `public`:
   `SELECT/INSERT/UPDATE/DELETE ON TABLES` and `USAGE` **only** on sequences to
   the runtime role. Do **not** create a global (`ALTER DEFAULT PRIVILEGES`
   without `IN SCHEMA`) entry.
4. `python -m app.migrate bootstrap-lease` — creates the schema, tables,
   functions and protocol row, verifies catalog shape, schema/table/function
   ACLs and default ACLs, then sets `bootstrap_state='ready'`.
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

* **TTL 300 s**: ≈ 8 500 × the slower measured p99 (35.3 ms). It only decides
  when a *new* process may take over a row left by a crashed or vanished owner;
  the server must still release that owner's row lock, and an active transaction
  can hold locks past the TTL (§3). A wide margin costs nothing at normal scale
  while keeping takeover after a crash bounded to minutes.
* **Statement timeout 150 s**: ≈ 4 250 × the slower measured p99 for one user.
  The configuration validator enforces it **strictly below** the TTL
  (`MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS < MIGRATION_LEASE_TTL_SECONDS ×
  1000`): equality and larger values are refused at the settings boundary. The
  timeout applies to each statement, not to the whole transaction; the
  pre-commit `enter` refresh guarantees a user cannot **commit** after expiry.
  It does not cap how long an active transaction may keep its locks, so a
  wedged statement is recovered by cancel/terminate rather than by waiting for
  the TTL (§3). It is large enough for a slower production host or a user with
  many positions.
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
  it owns the application objects it creates **and** holds only the §8.3
  restricted ordinary privileges on them (`SELECT` on `positions` and
  `paper_orders`, `SELECT`+`INSERT` on `strategy_leg_exposures`, sequence
  `USAGE` only).
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

### 8.1 Application privilege matrix is enforced, not just tested

`verify_application_privileges` (in `app/_migration_lease.py`) is the
read-only, fail-closed verifier for the application objects. It never grants,
revokes or repairs. It runs in two places:

* inside the protected DDL transaction, **after** the Alembic chain and the
topology step (`apply_migrator_privilege_restrictions` in Topology A,
`apply_migrator_backfill_grants` in Topology B) and **before** the pre-commit
`enter` — so a wrong matrix aborts the whole migration and nothing commits;
* again at the start of the startup backfill, before the first write, so the
backfill can never begin on an installation whose matrix is wrong.

It reads the catalogs and the effective-privilege functions (never the
statements the code issued) and refuses with `45004` when any of these fail:

| Check | Topology A | Topology B |
| --- | --- | --- |
| Ownership of `positions`, `paper_orders`, `strategy_leg_exposures` | the migrator | the NOLOGIN owner |
| Role attributes | no SUPERUSER, CREATEROLE, CREATEDB, BYPASSRLS or REPLICATION on any identity; the migrator and the runtime are LOGIN roles | same, plus the owner is NOLOGIN |
| Owner role | absent (a membership row is a failure) | exists, NOLOGIN, no elevated attributes |
| Migrator membership | none at all | exactly one row to the owner: `INHERIT FALSE, SET TRUE, ADMIN FALSE` |
| Incoming membership (who may *become* an identity) | no role is a member of the migrator | same, and the migrator is the owner role's only member; any other member of either role is a failure |
| Migrator table matrix (`has_table_privilege`, one privilege at a time) | `SELECT` on `positions`/`paper_orders`; `SELECT, INSERT` on `strategy_leg_exposures`; no `UPDATE`/`DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER` anywhere | identical |
| Migrator sequence | `USAGE` true, `SELECT` false, `UPDATE` false | identical |
| Column-level privileges (`has_column_privilege`) | every forbidden column-grantable privilege (`SELECT`, `INSERT`, `UPDATE`, `REFERENCES`) is refused when it exists only at column level; a `GRANT UPDATE (col)` or `GRANT REFERENCES (col)` is a bypass of the table matrix | identical |
| Runtime matrix | `SELECT/INSERT/UPDATE/DELETE` on all three; no `TRUNCATE`, `REFERENCES` or `TRIGGER` | identical |
| Runtime sequence / schema | sequence `USAGE` true, `SELECT` false, `UPDATE` false; no `USAGE`/`CREATE` on `migration_internal`, no table or function privilege there; no effective `CREATE` on schema `public` | identical |
| Runtime membership | no membership at all (not only not migrator/owner): any granted role is an unreviewed privilege path | identical |
| Owner membership | — | none at all, and `pg_has_role(owner, migrator, 'MEMBER')` false; no effective `USAGE`/`CREATE`/`EXECUTE` on `migration_internal` |
| Default ACLs | bound to the actual creator (`migrator` in A, `owner` in B), `IN SCHEMA public`, table `SELECT/INSERT/UPDATE/DELETE` + sequence `USAGE` for the runtime role; no `<global>` entry and nothing to PUBLIC | identical |

The two enforcement points are the reason the aliasing/`SET ROLE` ordering is
documented in §2.2: Topology B's four grants are installed inside the verified
owner window before `RESET ROLE`, so the backfill's effective identity is the
restricted migrator by construction, not by provisioning luck.

Topology A's restriction step revokes `REFERENCES` and `TRIGGER` as well as
the ordinary `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE` set, so the verifier's
stricter matrix and what the migration actually installs converge instead of
diverging.

Negative controls in the integration suite deliberately install each invalid
state (superuser migrator, excessive `UPDATE`/`DELETE`, sequence `SELECT`,
sequence `UPDATE` for the runtime, `INHERIT TRUE` membership, `LOGIN` owner,
runtime `CREATE` on `public`, runtime `CREATEDB`/`CREATEROLE`/`BYPASSRLS`,
owner `CREATEROLE`/`CREATEDB`, owner or runtime membership of an intermediate
role, a transitive path into `migration_internal`, a revoked runtime default
privilege, an excess runtime default (`TRUNCATE`, `REFERENCES`/`TRIGGER` on
future tables; `SELECT`/`UPDATE` on future sequences), excess privileges on
the existing exposure sequence, an unexpected incoming member of the migrator,
an unexpected incoming member of the owner, a column-level `UPDATE` or
`REFERENCES` grant to the migrator, and a NOLOGIN migration identity) and
assert `45004` with the specific message; the valid Topology A
and Topology B installations pass. `test_bad_privilege_matrix_aborts_the_guarded_upgrade`
and `test_guarded_upgrade_refuses_runtime_public_create` prove the enforcement
path itself refuses an installation the DDL cannot repair (the superuser case
is now refused one step earlier, by the connection-identity guard, and the test
asserts that earlier refusal). PostgreSQL refuses
the direct `GRANT <migrator> TO <owner>` cycle (the migrator is already a member
of the owner), which is why the owner's `pg_has_role(..., 'MEMBER')` check is
defence-in-depth and the constructible case is tested through an intermediate
role.

The connection-identity controls run the real `python -m app.migrate upgrade
head` with a wrong migration URL — the admin/superuser login, an unrelated
login, and a login granted membership in the migrator — and assert `45004`
**plus** that the lease generation/owner, `alembic_version` and the application
data are byte-for-byte unchanged, so the refusal is proven to happen before
lease acquisition, DDL or backfill rather than merely somewhere in the run. A
URL naming the migrator while that role is `SUPERUSER` is refused by the
connection guard; a login holding `SET TRUE` membership is refused even after
it issues `SET ROLE` to the migrator, because `session_user` remains unapproved.

The privilege model itself remains bounded: the verifier reads what
PostgreSQL reports *now*, so a role that could grant itself privileges later
(superuser, CREATEROLE, or Topology A ownership of the objects) is refused
rather than reasoned about.

## 9. Evidence

| Suite | Scope | Result |
| --- | --- | --- |
| `tests/test_issue145_application_integration.py` | real PG 16.16: concurrent `init_db()`, single connection, killed worker, injected failure, CLI, barrier, protocol, expiry, privileges, both ownership topologies, the Topology A §8.3 matrix and its restricted backfill, schema-ACL positive/negative controls, timing, **the §8.1 application privilege verifier (both topologies plus its negative/positive controls, including every default-privilege overgrant direction and excess privileges on the existing exposure sequence)**, **incoming membership and NOLOGIN controls**, **the actual-connection identity controls (superuser admin login, unrelated login, migrator-member login, superuser migrator role) with unchanged lease/version/data**, **deterministic, restart-stable `system_identifier` target identity, with an unreadable identifier refused and named with its exact grant (unexpected errors fail closed too)**, **a deterministic two-process contention proof that pauses the first worker inside its first protected per-user backfill transaction (database trigger on an advisory lock) and observes the second worker queued on the admission guard with no lease row lock**, **the runtime principal read from the live connection and a `?user=` URL that authenticates as the migrator refused**, **column-level `UPDATE`/`REFERENCES` bypasses refused in both topologies and aborting the guarded upgrade**, **the lease/control protocol mirror mismatch refused without repair**, **a URL-encoded `%40` password accepted on the guarded path**, **a new migration connection pointed at another database refused before it can run a statement**, **every new runtime connection compared with the startup-proven target identity and refused when it differs**, **a timezone-independent diagnostic postmaster epoch (UTC and Asia/Kolkata agree) that is proven absent from the target identity**, **the actual lease-row lock proven with `SELECT … FOR UPDATE NOWAIT` (SQLSTATE 55P03) while worker A is paused, with a negative control after release**, **an inherited `PUBLIC` CREATE on `public` refused and then accepted once revoked** | 83 passed |
| `tests/test_issue145_pg_restart.py` | **real restart of a self-provisioned PostgreSQL 16.16 cluster** (not a simulated signature change). (1) *Existing process, pre-restart proof:* startup preflight publishes `_RUNTIME_TARGET_SIGNATURE` once; a runtime engine with the application's listener and pool options (`pool_size=5`, `max_overflow=10`, `pool_timeout=30`, `pool_recycle=1800`, `pool_pre_ping=True`) is created and holds one pooled connection; the server is restarted; the **same engine object** is checked out again with no dispose, no `_migration_engine()`/`_verify_logical_target()` and no restart of application startup (both are replaced by fail-fast stubs). `pool_pre_ping` must replace the terminated pooled DBAPI connection (backend PID changes, a new `connect` event fires), the runtime listener must accept it, `SELECT 1` must work, `_RUNTIME_TARGET_SIGNATURE` must stay the *same object and value*, the identity must be unchanged and the postmaster start time must have changed. The test also records the identity the guard itself computes on the reconnection, so the comparison input is observed rather than assumed. (2) *New connection, re-proven target:* a new preflight and a new runtime connection are accepted after the restart, and the start time is asserted absent from the identity. (3) *Negative controls:* a different database in the same cluster and a second cluster with the same database name are still refused, and a revoked `pg_control_system()` after a restart fails closed with the grant and publishes nothing. Requires `ISSUE145_PG_BINDIR`; creates and owns its own scratch cluster and never restarts the CI/admin cluster | 5 passed (local 16.16) |
| `tests/test_migration_serialization.py` | DB-free protocol/config/retirement contract, the timeout-below-TTL rule and its boundaries, finite/positive wait validation, no attempt past the admission deadline, no SQL after a checkout consumes the budget, bounded backoff for very high attempt counts, unchanged fast 55P03 retry, pre-acquisition cleanup, unconditional engine disposal when `release` raises, SQLite URI identity (`mode=memory`, `mode=rwc`, `uri=true`, unsupported modes), connection query/host-list override refusal, disposable-harness endpoint preservation (authority and query-form host/port) and service refusal, and the **JUnit-XML CI gate** (authoritative counts, fabricated-log controls, malformed/inconsistent reports, failures/errors/skips, rc and minimum) | 93 passed (file) |
| `tests/test_db_migration.py`, `test_alembic_*.py`, `test_day5_alembic_authority.py`, `test_migration_identity_separation.py`, `test_leg_exposure.py` | SQLite chain, FK behavior, identity separation (**including the 16-target §8.1 URL contract: accepted pair, same-login, different database, different endpoint, declared alias, alias port mismatch, Neon pooled route, SQLite and CockroachDB migration targets, blank/malformed URLs, malformed alias configuration, no-password-in-errors, and the configured-identity binding — migration login must be `STRIKENOVA_MIGRATOR_ROLE`, runtime login must be `STRIKENOVA_RUNTIME_ROLE` when set, blank role name refused, unset runtime role not constrained**), exposure idempotency | 198 passed (regression suite total) |
| `PostgreSQL compatibility` suite (9 modules: compatibility, importer, failure injection, large rehearsal, hardening, Day 5/6) | disposable PG 16.16 schemas built only through `python -m app.migrate upgrade head` | 122 passed |
| `.github/workflows/issue145-application-pg16.yml` (`application-integration`) | the integration and regression suites above on a pinned `postgres@sha256:3950ad…` 16.16 service, exact `server_version_num = 160016` gate, evidence upload, cleanup | see PR #151 checks |
| `.github/workflows/issue145-application-pg16.yml` (`restart-runtime-reconnect`) | **isolated restart job, no `services:` block**, so it never restarts or mutates the shared service container: fetches the SHA-256-pinned 16.16 PGDG build for Ubuntu 24.04, asserts `postgres (PostgreSQL) 16.16` exactly, fails when `ISSUE145_PG_BINDIR` is unset, runs `tests/test_issue145_pg_restart.py`, and gates its JUnit XML with a minimum of 5 passes — a skip fails the job | see PR #151 checks |
| `scripts/pytest_gate.py` | gates **all four** suites independently: integration, migration/SQLite regression, PostgreSQL compatibility, and the restart/reconnection module. The JUnit XML report and the recorded pytest exit status are authoritative; the text log is diagnostic only. The job fails on a missing or non-zero recorded exit status, a missing/malformed JUnit report (including a DOCTYPE), an inconsistent report (counter/summary vs testcases), any failure, error or skip, and fewer passes than the declared minimum. The minimums are floors, not exact counts, so a suite that silently stops collecting is still caught by the structure checks | 80 / 181 / 122 / 5 minimums; negative controls in `TestPytestGate` |

Three evidence classes are kept distinct:

1. **Local execution.** The three service-container gates and the restart
   module were also run locally against PGDG 16.16 server binaries extracted
   into the workspace (the container socket was unavailable in that
   environment). The gated suites are run exactly as the workflows run them,
   with `--junitxml` plus the recorded pytest exit status passed to
   `scripts/pytest_gate.py`.
2. **Isolated restart CI job** (`restart-runtime-reconnect`). It has no
   `services:` block and owns the binaries and data directory it restarts:

   ```
   ISSUE145_PG_BINDIR=<16.16 bin> python -m pytest -q tests/test_issue145_pg_restart.py
   ```

   The job fetches the exact SHA-256-pinned 16.16 build, asserts
   `postgres (PostgreSQL) 16.16`, fails if `ISSUE145_PG_BINDIR` is unset, and
   gates the JUnit report (`restart|5|…`), so a skipped or silently
   uncollected test fails the job. The module creates its own `initdb` cluster
   in a temporary directory and restarts only that server;
   `ISSUE145_PG_ADMIN_URL` is not used by it.
3. **The three service-container database gates** (`application-integration`,
   `PostgreSQL compatibility`, `StrikeNova Status Gate`). Their PostgreSQL is a
   service container they cannot restart; they never exercise the restart
   path.

## 10. Known limitations

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
  which survive a restart; the postmaster start time is diagnostic and is never
  compared as identity. A revoked or absent `pg_control_system()` fails closed
  with the one grant that restores it — there is no weaker fallback to
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
  the application's own pool and connect listener accept the restarted server
  against the pre-restart reference, not that a specific provider's proxy
  preserves the connection semantics.
* The compatibility and importer rehearsals build their schema as Topology A
  and perform the data import as the migration identity; they do not re-prove
  the runtime-identity boundary (that lives in the integration suite).
* Topology A revokes the migrator's ordinary table/sequence privileges (§2.2)
  and PostgreSQL 16 applies them to the owner's own ACL. Inherent ownership
  powers remain: the migrator can still `ALTER`/`DROP` its own objects or
  grant a privilege back, which is why ownership separation (Topology B) and
  ADR §8.4/D-2 still matter. This is a real limitation of the Topology A
  model, not of the revoke statements.
* The TTL/timing measurement is synthetic and single-host (§7).
* No production/Neon/Render change is part of this work: no live roles, grants,
  environment variables or cutover were touched.
* The unrelated Pylint workflow still fails on its own `actions/setup-python@v3`
  setup on Ubuntu 24.04; it is not repaired here.
