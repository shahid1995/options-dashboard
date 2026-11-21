# T-01…T-27 Acceptance Matrix — Static Review Report

**Branch:** `review/issue145-t01-t27-harness-evidence`
**Base:** `main` at `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`
**ADR source:** `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` at pinned commit `ae2226ef592dc48d5e9619bd2198de33285911df`
**PR #149:** open, unmerged, one file — no changes
**Harness:** `run_t01_to_t27.py` (SQL copied from the pinned ADR: §6.1, §6.3–§6.6, §10.2, §9.2.1)

The commit that publishes this report is identified in the publication
response, not inside the file it publishes.

---

## Result: no runtime verification was performed

PostgreSQL 16.16 is unavailable in this environment. No scenario was executed
against a database. Every one of the 27 public outcomes is **NOT RUN**.

The only execution performed here was the harness's safety gate and
unavailability path:

```
$ python3 docs/reviews/issue145-t01-t27-harness/run_t01_to_t27.py
Safety gate: REFUSED — destructive safety gate refused: ALLOW_DESTRUCTIVE_TESTS
  must be exactly '1' (got ''); HARNESS_CLUSTER_LABEL must be
  'issue145-disposable' (got '')
No connection was opened and no database or role was touched.
...
Total: 27
PASS: 0
FAIL: 0
INVALID_SETUP: 0
NOT RUN: 27
INCONCLUSIVE: 0
$ echo $?
2

$ ALLOW_DESTRUCTIVE_TESTS=1 HARNESS_CLUSTER_LABEL=issue145-disposable \
    python3 docs/reviews/issue145-t01-t27-harness/run_t01_to_t27.py
Safety gate: ACCEPTED (cluster label 'issue145-disposable')
PostgreSQL availability: UNAVAILABLE — PostgreSQL unreachable (OperationalError ...)
...
Total: 27
PASS: 0
FAIL: 0
INVALID_SETUP: 0
NOT RUN: 27
INCONCLUSIVE: 0
$ echo $?
0
```

Those runs demonstrate that the gate refuses before connecting without the
explicit opt-in, and that the runner exits cleanly and classifies all scenarios
NOT RUN when PostgreSQL is absent. They are not evidence about the lease
protocol.

---

## Per-scenario outcomes

| # | Scenario | Outcome | Prerequisite not met here |
|---|---|---|---|
| T-01 | First installation from a genuinely empty database | NOT RUN | PostgreSQL 16.16 |
| T-02 | First installation, twice concurrently | NOT RUN | Two backends |
| T-03 | Concurrent acquire on an unowned lease | NOT RUN | Two backends |
| T-04 | W1 enters twice; second worker refused | NOT RUN | PostgreSQL 16.16 |
| T-05 | GUC forgery cannot substitute for the token | NOT RUN | PostgreSQL 16.16 |
| T-06 | Foreign valid-format token is refused | NOT RUN | PostgreSQL 16.16 |
| T-07 | Row-lock contention with the correct token | NOT RUN | PostgreSQL 16.16 |
| T-08 | Two workers run `alembic upgrade head` | NOT RUN | Alembic chain + concurrency |
| T-09 | Concurrent backfill of three users | NOT RUN | Backfill implementation |
| T-10 | Two `enter` calls per backfill transaction | NOT RUN | Backfill implementation |
| T-11 | TTL genuinely exceeded by the database clock | NOT RUN | PostgreSQL 16.16 |
| T-12 | Terminate W1's backend inside a protected transaction | NOT RUN | PostgreSQL 16.16 |
| T-13 | Interrupted backfill and recovery | NOT RUN | Backfill implementation |
| T-14 | Version mismatch and closed barrier | NOT RUN | PostgreSQL 16.16 |
| T-15 | Topology A — runtime APP has no lease privilege | NOT RUN | PostgreSQL 16.16 |
| T-16 | Topology B — non-inherited membership and grants | NOT RUN | PostgreSQL 16.16 |
| T-17 | `alembic upgrade head` directly | NOT RUN | Alembic binary |
| T-18 | `python -m app.migrate upgrade head` | NOT RUN | Application runner |
| T-19 | Injected backfill and release failures | NOT RUN | Blocker session |
| T-20 | Supported command on a database already at head | NOT RUN | Application command |
| T-21 | Operator close-barrier sequence | NOT RUN | Operator commands |
| T-22 | `p_lock_timeout_ms = 0` / `p_ttl_seconds = 0` | NOT RUN | PostgreSQL 16.16 |
| T-23 | Stale protocol-1 worker after upgrade to 2 | NOT RUN | `bootstrap-lease --upgrade-protocol` flow |
| T-24 | Cross-version concurrency | NOT RUN | `*_v2` function set + concurrency |
| T-25 | Plain bootstrap verify refuses a mismatch | NOT RUN | `bootstrap-lease` command |
| T-26 | Explicit upgrade, refusal and cancellation | NOT RUN | Operator commands |
| T-27 | `enter`/`acquire` refuse an unready protocol | NOT RUN | PostgreSQL 16.16 |

## Totals

| Outcome | Count |
|---|---|
| PASS | 0 |
| FAIL | 0 |
| INVALID_SETUP | 0 |
| NOT RUN | 27 |
| INCONCLUSIVE | 0 |
| **Total** | **27** |

Every scenario has exactly one category. NOT RUN is never counted as FAIL, and
no scenario was counted from an earlier run.

When scenarios do execute, the runner classifies them separately: failed
assertions and unexpected PostgreSQL SQLSTATEs are `FAIL`, database or identity
provisioning failures are `INVALID_SETUP`, and only genuinely absent
prerequisites are `NOT RUN`. Database teardown runs in a `finally` path, and a
teardown error is recorded on its own line without replacing the scenario's
outcome.

---

## What the static review did establish

- The four `CREATE FUNCTION` bodies and both `CREATE TABLE` bodies, loaded from
  the Python module, are identical to the pinned ADR fenced blocks.
- `lease_enter_v1` declares `v_prev` and `v_exp`, saves the caller's
  `lock_timeout`, applies `p_lock_timeout_ms`, and restores the saved value
  after the lease row lock.
- No SQL string contains a literal backslash-n.
- Every harness connection is opened through one helper that sets
  `autocommit=True`, so each `with conn.transaction():` block is a real
  BEGIN/COMMIT instead of a savepoint inside an accidental implicit transaction.
- `run_sql()` contains no `commit()`/`rollback()`; scenarios own their
  transactions. Every call passes the connection first and only `params` /
  `fetch_mode` keywords.
- Bootstrap runs as one transaction in the ADR's order: preflight, create,
  revoke set, full verification while the protocol reads `closed` / `installing`,
  activation to `open` / `ready`, then the protocol-info assertion. A DB-free
  ordering assertion enforces it, so verification cannot move after activation.
- The verifier compares constraint definitions through `pg_get_constraintdef()`,
  complete function result signatures (including the five-column protocol-info
  result), ACLs expanded with `acldefault(...)`, per-privilege effective
  privileges for schema, tables, sequences and functions, transitive role
  membership, `rolsuper` / `rolbypassrls`, and both global and schema-scoped
  default ACLs. It issues no GRANT or REVOKE.
- The runner calls `prepare_scenario_database()` and `drop_scenario_db()` for
  each scenario, the latter in a `finally` path.
- The destructive gate refuses before connecting without `ALLOW_DESTRUCTIVE_TESTS=1`
  and `HARNESS_CLUSTER_LABEL=issue145-disposable`, and refuses a target holding
  foreign databases, a production-like fixed-name role, or an unexpected role
  membership.
- The runner probes `SHOW server_version` / `server_version_num` and requires
  `160016`; `SELECT 1` alone is not used to establish the version.

## Execution model and safety gate

Every harness connection is `autocommit=True`; each atomic operation is an
explicit `with conn.transaction():` block. This makes the two facts T-11 and T-12
assert true in the code rather than incidental: the application table creation is
committed before the lease is acquired, and `lease_enter_v1` is the first
statement of each protected transaction.

Because the harness drops fixed-name databases and mutates cluster-level role
membership, it requires `ALLOW_DESTRUCTIVE_TESTS=1` and
`HARNESS_CLUSTER_LABEL=issue145-disposable`, then validates the target before any
mutation. It creates three cluster roles if absent
(`strikenova_prod_migrator`, `strikenova_production_app`,
`strikenova_prod_owner`), the owner→migrator membership, and the `issue145_t01`…
`issue145_t27` databases, dropping each database after its scenario. The roles and
their membership survive, because dropping a database does not drop roles. None
of this was exercised here: no database was contacted.

## What remains unverified

Everything about runtime behaviour: SQLSTATE values, lock timeouts, backend
termination, privilege enforcement, expiry against the database clock, and every
other acceptance observation. No PASS is claimed for any of T-01…T-27.

## Historical, superseded evidence

The original publication on this branch (12 PASS / 13 FAIL / 2 INVALID_SETUP)
came from an earlier harness that did not implement this protocol faithfully and
inferred SQLSTATE from exception text. Those numbers are preserved in Git
history as historical and superseded; they are not current results.
