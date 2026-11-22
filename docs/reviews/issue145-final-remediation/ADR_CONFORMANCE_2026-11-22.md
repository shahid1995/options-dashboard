# ADR-017/018 conformance corrections after the remediation review

**Date:** 2026-11-22
**PR:** #146 — **Draft, open, unmerged** (https://github.com/shahid1995/options-dashboard/pull/146)
**Branch:** `issue-145-fix`
**Starting HEAD:** `65ab3ea52de2a018332ddd1f1bd36bc5b2b639b9`
**Code commit:** `840e4f14bbfe2762dc93495cdff869bca86aa62a`
**This report:** the commit that adds this file (documentation only)

Architecture authority remains the approved ADR-017/018 (PR #149). Nothing in
this change alters the lease protocol, the lease functions, the barrier, the
protocol versions or the role model; it restores a privilege contract the
implementation had documented incorrectly.

---

## 1. Finding 1 (P1) — the ADR §8.3 Topology A revokes were missing

### What was wrong

The runbook claimed "Topology A cannot revoke table privileges from its own
creator: an object's owner always holds them." That is wrong for **ordinary**
table and sequence privileges: PostgreSQL 16 applies a `REVOKE` to the owner's
own ACL, and a later `INSERT`/`UPDATE`/`DELETE` by that owner fails `42501`.
Only *ownership* (the `ALTER`/`DROP` right and the ability to grant a privilege
back) cannot be revoked. Because the implementation believed the revokes were
impossible, Topology A kept the full owner privilege set on the tables the
startup backfill touches, which contradicts ADR §8.3.

### What changed

`app/db.py`

* `_TOPOLOGY_A_RESTRICTED_TABLES` / `_TOPOLOGY_A_RESTRICTED_SEQUENCES` declare
  the ADR §8.3 objects and the exact privileges to remove.
* `apply_migrator_privilege_restrictions(conn)` issues, as the migration
  identity and with the configured `STRIKENOVA_MIGRATOR_ROLE` (never a
  hard-coded production role name):

  ```sql
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE public."positions"              FROM "<migrator>";
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE public."paper_orders"           FROM "<migrator>";
  REVOKE UPDATE, DELETE, TRUNCATE          ON TABLE public."strategy_leg_exposures" FROM "<migrator>";
  REVOKE ALL                               ON SEQUENCE public.strategy_leg_exposures_id_seq FROM "<migrator>";
  GRANT  USAGE                             ON SEQUENCE public.strategy_leg_exposures_id_seq TO   "<migrator>";
  ```

  Missing objects are skipped via `to_regclass`, and the sequence is resolved
  with `pg_get_serial_sequence`, so a renamed/recreated sequence is still
  restricted.
* `_run_guarded_migrations` calls it **inside the protected DDL transaction**,
  after `command.upgrade(...)` and `_reset_owner_role(conn)` and before the
  pre-commit `lease.enter(conn)`. The revokes therefore commit atomically with
  the schema, are visible to the startup backfill that runs immediately after,
  and are re-applied by **every** later `upgrade head` — a subsequent migration
  cannot silently restore the privileges.
* Topology B is untouched: when `STRIKENOVA_OWNER_ROLE` is set the function
  returns without doing anything, and Topology B keeps its owner window,
  `INHERIT FALSE, SET TRUE, ADMIN FALSE` membership and four explicit grants.

`tests/issue145_pg_support.py`

* New `grant_importer_write_privileges()` used only by `provision_schema_ready`
  (the compatibility/importer rehearsals). Those suites are *importer* tests
  that copy legacy rows **as** the migration identity, so on a throwaway
  database they explicitly re-grant the write privileges the importer needs.
  The application never re-grants; the fixture concession is documented in the
  helper docstring and does not weaken any production assertion.

### Verified privilege matrix (Topology A, PostgreSQL 16.16)

`test_topology_a_restricted_privilege_matrix` reads the effective privileges
with `has_table_privilege` / `has_sequence_privilege` and then executes the
statements:

| Object | `SELECT` | `INSERT` | `UPDATE` | `DELETE` | `TRUNCATE` |
| --- | --- | --- | --- | --- | --- |
| `public.positions` | **true** | **false** | **false** | **false** | **false** |
| `public.paper_orders` | **true** | **false** | **false** | **false** | **false** |
| `public.strategy_leg_exposures` | **true** | **true** | **false** | **false** | **false** |
| `public.strategy_leg_exposures_id_seq` | `SELECT` **false** | — | — | — | `USAGE` **true** |

Denied statements raise `42501` **for the owning migrator**:
`INSERT INTO public.positions SELECT …`, `UPDATE public.positions`,
`DELETE FROM public.positions`, `TRUNCATE public.positions`, the same three on
`paper_orders`, `UPDATE`/`DELETE`/`TRUNCATE` on `strategy_leg_exposures`, and
`SELECT last_value FROM public.strategy_leg_exposures_id_seq`. The allowed
reads succeed and `nextval()` works (`USAGE`), which is what
`INSERT … RETURNING id` needs.

`test_topology_a_restricted_backfill_completes` then runs the **real** startup
path (`db_module.init_db()`, i.e. guarded Alembic + protected per-user backfill
as the same restricted identity): three seeded legacy users produce exactly one
`strategy_leg_exposures` row each, a later startup adds the fourth without
duplicates, and the matrix is re-checked after that second upgrade — every
denied privilege is still `false` and `INSERT` on `strategy_leg_exposures` is
still `true`.

### Ownership, stated accurately

The revokes remove ordinary privileges only. The Topology A migrator still
**owns** those objects and can therefore `ALTER`/`DROP` them, or grant a
privilege back to itself. That is inherent ownership, not a revocable
privilege, and it is why ownership separation (Topology B) and ADR §8.4/D-2
still exist. This limitation is now documented in the runbook (§2.2, §10)
instead of being used to justify skipping the contract.

## 2. Finding 2 (P2) — the regression suite was not gated

`issue145-application-pg16.yml` gated `issue145-application.log` (skips, pass
summary, minimum count) but only checked the **exit code** of
`issue145-regression.log`. An all-skipped or empty regression run would have
left the job green.

### What changed

* New `scripts/pytest_gate.py` — a single fail-closed gate for any number of
  suites. For every suite it requires the recorded exit code (present and `0`),
  the pytest log (present), no `N failed` / `N error(s)`, **no** `N skipped`, an
  `N passed` summary, and `N >= minimum`. It emits `::error::` annotations and
  exits `1` on any violation, `2` on a malformed invocation.
* The workflow now calls it once for both suites:

  ```yaml
  python scripts/pytest_gate.py \
    --suite "integration|24|issue145-application.log|issue145-application.rc" \
    --suite "regression|144|issue145-regression.log|issue145-regression.rc"
  ```

  Both suites are still uploaded as sanitized evidence, and the gate script is
  uploaded with them so the decision is reproducible from the artifact.

### How the minimums were derived

They are the exact counts this commit runs, measured, not rounded thresholds:

| Suite | Modules | Count |
| --- | --- | --- |
| integration | `test_issue145_application_integration.py` | **24** (was 22; +2 Topology A tests) |
| regression | `test_migration_serialization.py`, `test_db_migration.py`, `test_migration_identity_separation.py`, `test_alembic_migrations.py`, `test_alembic_single_head.py`, `test_alembic_env_resolution.py`, `test_day5_alembic_authority.py`, `test_leg_exposure.py` | **144** (was 135; +9 gate tests) |

An exact floor is the defensible choice: adding tests keeps the gate green,
while deleting or silently skipping coverage lowers the count and fails the job
until the workflow is changed deliberately in the same commit.

### Negative controls (deterministic, no real test altered)

`tests/test_migration_serialization.py::TestPytestGate` (9 tests) drives the
script against synthetic logs in `tmp_path`:

| Control | Expected |
| --- | --- |
| `24 passed, 1 warning` + rc 0 | exit 0 |
| `22 skipped` | exit 1 (`skipped`) |
| `collected 0 items` (no summary) | exit 1 (`no 'N passed' summary`) |
| `21 passed, 1 failed` | exit 1 |
| rc `1` with a clean summary | exit 1 (`pytest exited 1`) |
| no `.rc` file | exit 1 (`no recorded pytest exit code`) |
| `21 passed` with minimum 22 | exit 1 (`only 21 test(s) passed`) |
| missing log | exit 1 (`no pytest log`) |
| green integration + `135 skipped` regression | exit 1, integration still reported |

The same script was also run manually against the real counts and against
synthetic skipped / missing-summary / bad-exit-code logs (`0`, `1`, `1`, `1`
as expected). In CI the gate step (`Gate on the actual test results`, step 10)
ran and passed in run 44993700695.

## 3. Finding 3 (P3) — documentation

* `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md`: the incorrect "Topology A
  cannot revoke" sentence is replaced by a new §2.2 that states what the
  revokes are, where in the lifecycle they run, that PostgreSQL applies them to
  the owner's own ACL, and that inherent ownership powers remain. §5.1 now says
  no manual revoke step is needed, §8 records the restricted Topology A
  matrix, §9 carries the new counts, and §10 restates the ownership limitation
  accurately.
* `app/config.py`, `app/db.py`: the ADR-016 "optional identity separation /
  single-identity deployments unaffected" wording is replaced by the actual
  contract — on PostgreSQL `STRIKENOVA_MIGRATION_DATABASE_URL` is mandatory,
  must differ from `DATABASE_URL`, and the resolver's `DATABASE_URL` fallback is
  explicitly documented as SQLite-only because
  `assert_guarded_migration_configuration()` refuses an unguarded PostgreSQL
  migration with `45004`. No code path was added or relaxed.

## 4. Verification

PostgreSQL 16.16 only (`server_version_num = 160016`, `16.16 (Ubuntu
16.16-1.pgdg24.04+1)`), disposable `sn145_*` databases and roles, no Neon,
Render, production, staging or live role/grant touched.

| Command | Result |
| --- | --- |
| `pytest -q tests/test_migration_serialization.py` | **55 passed** (was 46; +9 gate tests) |
| `pytest -q tests/test_issue145_application_integration.py` | **24 passed** (was 22; includes both new Topology A tests) |
| regression suite (8 modules, workflow invocation) | **144 passed** |
| PostgreSQL compatibility suite (9 modules) | **122 passed, 0 skipped** |
| `python scripts/pytest_gate.py` on the real counts | exit 0, `24 passed (minimum 24)` / `144 passed (minimum 144)` |
| same script on synthetic skipped / missing-summary / rc=1 logs | exit 1 in every case |
| `python -m compileall -q app tests scripts` | clean |
| YAML parse of both workflows | clean |
| `ruff check` (0.17.6) on the changed files | 32 findings, all pre-existing style rules on lines untouched by this change (the repository configures no ruff profile); `scripts/pytest_gate.py` itself is clean |

### CI evidence for the code commit (`840e4f1`)

| Workflow | Event | Run | Result |
| --- | --- | --- | --- |
| Issue #145 ADR-017/018 application integration (PostgreSQL 16.16) | pull_request | [44993700695](https://github.com/shahid1995/options-dashboard/actions/runs/44993700695) | success — gate step 10 passed |
| PostgreSQL compatibility | pull_request | [44993700707](https://github.com/shahid1995/options-dashboard/actions/runs/44993700707) | success |

Evidence here is pinned to the commit that produced it, never to the branch
head, because the head legitimately moves. While this report was being written
the branch advanced from `840e4f1` → `01574c8` (this report, documentation
only) → `3785469` (CI-only hardening by the founder: per-ref concurrency, pip
caching, credential scoping). Both workflows were re-run on `3785469` and
passed: application integration run `44993950117`, PostgreSQL compatibility run
`44993950031`. Commits that only change Markdown cannot alter these results.

The application job's steps 8 and 9 are the two suites and step 10 is the new
gate, followed by cleanup (step 11) and the sanitized evidence upload (step 12),
all `success`.

This report's own commit is documentation only and re-runs both workflows; its
results appear in PR #146's checks.

## 5. Residual limitations and explicitly unverified items

* **Concurrent first-ever bootstrap** remains evidenced by a contention test
  that accepts `55P03`, not by a two-process proof of an empty database.
* **Importer rehearsals re-grant write privileges** on their own disposable
  database (see §1). That is a fixture concession, not a production path, and
  the §8.3 matrix is asserted separately against an unmodified database.
* **No Neon or production-class verification.** TTL and statement-timeout
  defaults still come from the synthetic single-host measurement.
* **No live role, grant, Render, deployment or cutover change.**
* **Pylint workflow still fails** on its own `actions/setup-python@v3` setup on
  Ubuntu 24.04 (pre-existing, unrelated, not repaired here). `compileall` is a
  syntax check only; the static check actually run for this change is `ruff`
  (locally, no repository profile), reported above.
* **PR #146 remains Draft, open and unmerged**; independent acceptance review is
  still required.
