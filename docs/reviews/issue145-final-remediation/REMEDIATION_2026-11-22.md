# Issue #145 / ADR-017/018 — final remediation of the implementation PR

**Date:** 2026-11-22
**Implementation PR:** #146 — **Draft, open, unmerged** (not approved for merge)
**Branch:** `issue-145-fix` (base `feat/strikenova-day35-portfolio-intelligence` @ `9dc6e15c`)
**Architecture authority:** PR #149 / `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`; accepted strategic database target: Neon PostgreSQL (ADR-019).

This report closes the independent review findings against the ADR-017/018
implementation. It records what changed, why, the evidence for each finding, and
what remains unverified. Nothing here was merged, deployed, or run against Neon,
Render, production, staging, or any live role/grant configuration.

---

## 1. Baseline and final commits

| Ref | SHA | Meaning |
| --- | --- | --- |
| Starting HEAD | `1845e54d6f4ddb826ffb44c2bcc7b6ee566fca33` | expected starting point; `origin/issue-145-fix` matched it |
| Code remediation | `3390234334156b98b64b94dd27245493d361a540` | findings 1–7 fixed (schema built through the guarded CLI, topologies, ACL, TTL rule, workflow triggers, docs) |
| CI false-green guard | `e0651941d043fd283ab7a9fdb4bcddd269f06823` | job gates fail on skipped tests / too few passing tests |
| Report + harness hardening | the commit that adds this file | provisioning cleans up after a failed setup; this report |

The code fixes (`3390234`) and the gate fixes (`e065194`) were each verified by a
fresh push to `issue-145-fix`; the workflows were green on both commits (§8).
The report commit changes only Markdown plus one test-harness `try/except`
(§3), and re-runs the same workflows.

## 2. Scope and prohibitions observed

* No merge of PR #146 or PR #149; PR #146 stayed **Draft** throughout.
* No duplicate implementation PR, no push to `main`, no history rewrite.
* No Neon provisioning/connection, no live role or grant change, no Render
  change, no deploy, no cutover, no production/staging credential use.
* No secret was read, printed, copied or committed.
* All PostgreSQL work used **disposable PostgreSQL 16.16** instances with
  uniquely named `sn145_*` databases and roles that are dropped afterwards.

## 3. Changed files and rationale

| File | Change | Why |
| --- | --- | --- |
| `backend/tests/issue145_pg_support.py` | `provision(topology=...)` with independent **Topology A** and **Topology B** provisioning; `install_lease`, `run_guarded_upgrade`, `provision_schema_ready`, `migrator_engine` helpers; failure-safe cleanup; `grant_backfill_privileges` refuses Topology A; misleading `apply_topology_a_revokes` deleted | F1/F3: one provisioning harness, two genuinely different topologies, schema always built by the supported CLI |
| `backend/alembic/env.py` | removed the unleased-Allembic escape hatch; the PostgreSQL refusal is unconditional again | F1: the guard must not be bypassable for tests |
| `backend/tests/conftest.py` | removed the `STRIKENOVA_ALEMBIC_SCHEMA_REHEARSAL` default | F1: no global test-wide bypass |
| `backend/tests/test_postgres_compatibility.py` | `postgres_target`/`postgres_engine` fixtures provision a disposable database and run `python -m app.migrate upgrade head`; new `test_schema_is_built_by_the_guarded_migration_path` | F1 + F2 |
| `backend/tests/test_sqlite_postgres_migration.py` | same guarded fixture; `prepare_schema` is SQLite-only and raises on PostgreSQL | F1 + F2 |
| `backend/tests/test_rehearsal_ci_database.py` | same guarded fixture; `_prepare_schema` is SQLite-only | F1 + F2 |
| `backend/tests/test_migration_failure_injection.py` | three rollback-injection tests kept, retargeted to the guarded fixture | F1 + F2 |
| `backend/tests/test_migration_large_rehearsal.py` | large rehearsal kept, retargeted to the guarded fixture | F1 + F2 |
| `backend/app/_migration_lease.py` | `_schema_acl_problems`: `pg_namespace.nspacl` + `aclexplode` + `acldefault('n', nspowner)`, effective `has_schema_privilege`, superuser and schema-owner membership checks; wired into `verify_installation` | F4 |
| `backend/app/config.py` | `model_validator` requiring `MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS < MIGRATION_LEASE_TTL_SECONDS * 1000` (equality refused) | F5 |
| `backend/tests/test_migration_serialization.py` | boundary tests for the timeout/TTL rule (below, equal, above, non-positive, non-positive TTL) | F5 |
| `backend/tests/test_issue145_application_integration.py` | independent Topology A fixture; Topology A runtime-DML/DDL-denied test; Topology B role-shape/default-privilege test; schema-ACL positive and three negative controls; schema attestations in the zero-access test; short-TTL test now sets a sub-TTL statement timeout | F3, F4, F5 |
| `.github/workflows/issue145-application-pg16.yml` | triggers on push to `issue-145-fix`/test branch, on PRs whose base is `feat/strikenova-day35-portfolio-intelligence`, and `workflow_dispatch`; real exit statuses captured; gate fails on skips and on fewer than 22 passing tests | F6 |
| `.github/workflows/postgres-compatibility.yml` | pinned `postgres@sha256:3950ad…` (16.16), exact `server_version_num = 160016` gate, `ISSUE145_PG_ADMIN_URL`, evidence upload, cleanup, and a gate that fails on non-zero status, any skip, or fewer than 120 passing tests | F2, F6 |
| `DECISIONS.md` | ADR-003/ADR-004 marked **superseded by ADR-019** (Neon PostgreSQL; CockroachDB retired, historical evidence retained) | F7 |
| `INVARIANTS.md` | Neon PostgreSQL target; no migration-URL fallback; creator-role/`IN SCHEMA public` default privileges for both topologies | F7 |
| `DATA.md` | environments table (Neon target, CockroachDB historical/retired); migration-lease data domain | F7 |
| `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md` | topology switch and semantics, strict TTL rule, updated ACL/verification section, measurement provenance, evidence counts | F3, F4, F5, F7 |
| `backend/.env.example` | owner role empty by default (Topology A) with the Topology B alternative explained; strict timeout rule; Neon/PostgreSQL wording | F7 |

No migration file, model, or production code path was changed beyond
`_migration_lease.py` (verification only) and `config.py` (validation only).

## 4. Prior findings and closure evidence

| # | Finding | Closure | Evidence |
| --- | --- | --- | --- |
| F1 (P1) | PostgreSQL compatibility suites failed because they built schemas with a bare Alembic run, and an uncommitted test-only escape hatch weakened the guard | Escape hatch deleted; **all** PostgreSQL schemas in tests now come from `python -m app.migrate upgrade head` under the lease and the separate migration identity; `alembic/env.py` refusal is unconditional | `test_bare_alembic_upgrade_fails_closed` (integration); new `test_schema_is_built_by_the_guarded_migration_path`; 122 compatibility tests pass |
| F2 (P1) | 10 compatibility tests failing/erroring at PR head | All 10 retained and adapted (§5); none deleted, none xfailed | run 44999476675 (before) → 44991445664 (after) |
| F3 (P1) | `pg_topology_a` only blanked a setting; it did not demonstrate Topology A | Topology A now provisioned independently (no owner role, no membership, defaults for the migrator); Topology B asserted via `pg_auth_members` (`INHERIT FALSE, SET TRUE, ADMIN FALSE`), `NOLOGIN` owner, owner-created tables, creator-scoped defaults | `test_two_concurrent_startups_serialize_and_backfill_exactly_once`, `test_topology_a_creator_objects_are_runtime_usable`, `test_topology_b_owner_role_shape_and_default_privileges`, `test_migrator_minimum_privileges_and_owner_window` |
| F4 (P2) | `migration_internal` schema ACL not adequately verified | `verify_installation` now inspects `pg_namespace.nspacl` (and `acldefault('n', …)`) for PUBLIC and the runtime role, plus effective `USAGE`/`CREATE`, superuser status and schema-owner membership | `test_schema_acl_verification_positive_and_negative_controls`, `test_runtime_identity_has_zero_access_to_migration_infrastructure` |
| F5 (P2) | Statement timeout only checked for positivity | Cross-field rule rejects equality and larger values; defaults (300 s / 150 000 ms) preserved | `TestConfigurationValidation` boundary cases (§7) |
| F6 (P2) | Integration workflow ran on the wrong branch/PR base | Triggers corrected; a PR against `feat/strikenova-day35-portfolio-intelligence` now runs it; a fresh run was produced from the real PR branch | PR-event runs 44991445717 (integration) and 44991445664 (compatibility) |
| F7 (P3) | PR/control documents described the retired design and CockroachDB as the production target | PR #146 and PR #149 descriptions rewritten; DECISIONS/INVARIANTS/DATA/runbook/.env.example reconciled with the code and with ADR-019 | this file + PR bodies |

## 5. Compatibility test disposition

Every test that was failing at the PR head is retained; the only change is how
its PostgreSQL target is prepared (guarded CLI, disconnected-from-superuser
engine) and, for the importer rehearsals, that the import runs as the migration
identity that owns the objects.

| Previously failing test | Disposition |
| --- | --- |
| `test_synthetic_rehearsal_uses_current_alembic_schema_and_verifies_full_report` | retained; SQLite source still built by Alembic, PostgreSQL target by the guarded CLI; full report assertions unchanged |
| `test_migration_failure_rolls_back_all_imported_rows` | retained; injected insert failure and empty-target assertions unchanged |
| `test_migration_failure_during_later_table_rolls_back_earlier_tables` | retained; same rollback contract |
| `test_migration_failure_during_sequence_repair_rolls_back_data` | retained; same rollback contract |
| `test_large_realistic_rehearsal_migrates_and_verifies_without_data_loss` | retained; 10 000-row dataset, fingerprint and no-loss assertions unchanged |
| `test_full_alembic_schema_exists_on_postgres` | retained |
| `test_postgres_schema_supports_identity_and_gex_provenance` | retained |
| `test_postgres_partial_unique_default_connection_is_enforced` | retained |
| `test_postgres_unique_constraint_allows_multiple_non_default_connections` | retained |
| `test_rehearsal_migrates_sqlite_rows_into_postgres` | retained |

No test was removed. The suites that exercised the **retired renewal-thread**
mechanism were already deleted in the earlier implementation commit and are not
part of this remediation. The superuser `TEST_DATABASE_URL` remains in the
workflow only for unrelated legacy suites (e.g. the Day 6 baseline, which
provisions its own database); none of the ADR-017/018 or import-rehearsal tests
use it, and no test engine connects as the cluster superuser.

## 6. Ownership topologies and privilege matrices

### Topology A (`STRIKENOVA_OWNER_ROLE` empty — the default)

| Assertion | Expected | Test |
| --- | --- | --- |
| Owner role exists | no | `pg_roles` count 0 |
| Migrator role memberships | 0 | `pg_auth_members` |
| `positions` / probe table owner | migrator | `pg_tables.tableowner` |
| Runtime DML on creator table | allowed | INSERT/SELECT/UPDATE/DELETE |
| Runtime `ALTER`/`DROP`/`TRUNCATE` | `42501` | psycopg errors |
| Runtime `CREATE TABLE` in `public` | `42501` | psycopg error |
| Runtime sequence `USAGE` | yes (default privilege) | `has_sequence_privilege` |
| Runtime `migration_internal` access | `42501` | psycopg error |

An object's owner inherently holds every privilege on it, so §8.3 revokes are a
Topology B control; the runbook says so explicitly.

### Topology B (`STRIKENOVA_OWNER_ROLE` set)

| Assertion | Expected | Evidence |
| --- | --- | --- |
| Owner role | `NOLOGIN`, not superuser, no CREATEROLE/CREATEDB | `pg_roles` |
| Migrator → owner membership | `INHERIT FALSE, SET TRUE, ADMIN FALSE` | `pg_auth_members` |
| Application object owner | the owner role | `pg_tables` |
| Default privileges | bound to the owner, `IN SCHEMA public` only | `pg_default_acl` + `aclexplode` |
| Migrator default privileges | none | `pg_default_acl` |
| Runtime DML on owner-created table | allowed | INSERT/SELECT |
| Runtime `ALTER`/`DROP`/`SET ROLE owner` | `42501` | psycopg errors |
| Migrator table rights (`positions`, `paper_orders`, `strategy_leg_exposures`) | SELECT only / SELECT only / SELECT+INSERT; sequence USAGE without SELECT | `has_table_privilege`, `has_sequence_privilege` |
| Owner window | DDL works inside `SET ROLE`; lease functions still `42501`; `RESET ROLE` restores the migrator | `test_migrator_minimum_privileges_and_owner_window` |
| Backfill identity | migration identity, never the owner; lease row lock held | `test_backfill_uses_migrator_identity_and_per_user_transactions` |

### `migration_internal` schema ACL (`pg_namespace.nspacl`)

| Control | Result |
| --- | --- |
| Valid installation | verify passes, `installed_version` unchanged |
| `GRANT USAGE … TO PUBLIC` | verify fails `45004`, message names PUBLIC/USAGE |
| `GRANT CREATE … TO PUBLIC` | verify fails `45004`, message names PUBLIC/CREATE |
| `GRANT USAGE … TO <runtime role>` | verify fails `45004`, message names the runtime role |
| After revoking each grant | verify passes again, zero PUBLIC schema entries, runtime `USAGE`/`CREATE` both false |

## 7. Timeout/TTL boundary tests

Rule: `MIGRATION_BACKFILL_STATEMENT_TIMEOUT_MS < MIGRATION_LEASE_TTL_SECONDS * 1000`
(strictly below; equality refused), validated in `Settings`.

| Case | Result |
| --- | --- |
| Defaults (300 s / 150 000 ms) | accepted |
| 299 999 ms with 300 s TTL | accepted |
| 300 000 ms with 300 s TTL | `ValidationError` (“strictly below”) |
| 300 001 ms with 300 s TTL | `ValidationError` |
| timeout 0 / −1 / −150 000 | `ValidationError` (positive) |
| TTL 0 / −1 / 0.0005 | `ValidationError` |
| Test using a 3 s TTL | sets a 2 500 ms statement timeout (otherwise correctly refused) |

## 8. Verification evidence

### Local, disposable PostgreSQL 16.16 (`server_version_num = 160016`, `server_version = 16.16 (Ubuntu 16.16-1.pgdg24.04+1)`)

| Command | Result |
| --- | --- |
| `pytest -q tests/test_issue145_application_integration.py` (`ISSUE145_PG_ADMIN_URL=…`) | **22 passed** in 29.34 s |
| `pytest -q tests/test_migration_serialization.py` | **46 passed** |
| `pytest -q` compatibility list (9 modules) | **122 passed** in 34.64 s, 0 skipped |
| `pytest -q` SQLite/migration regression list (8 modules) | **135 passed** |
| `python -m compileall` (app, tests, alembic) | clean |
| YAML parse of both workflows | clean |

Measured per-user backfill (50 users × 2 positions, same run):
`p50=6.0ms p99=18.2ms max=18.2ms ttl=300s statement_timeout=150000ms`.
This is a synthetic, single-host measurement — **not** a Neon or production
result. The historical 35.3 ms p99 measurement from the earlier revision is
retained as historical, and the TTL/statement-timeout derivation uses the slower
of the two.

### GitHub Actions on the real PR branch

| Workflow | Commit | Event | Run | Result |
| --- | --- | --- | --- | --- |
| Issue #145 ADR-017/018 application integration (PostgreSQL 16.16) | `3390234` | push | [44991087146](https://github.com/shahid1995/options-dashboard/actions/runs/44991087146) | success |
| Issue #145 ADR-017/018 application integration (PostgreSQL 16.16) | `3390234` | pull_request | [44991091140](https://github.com/shahid1995/options-dashboard/actions/runs/44991091140) | success |
| PostgreSQL compatibility | `3390234` | push | [44991087224](https://github.com/shahid1995/options-dashboard/actions/runs/44991087224) | success |
| PostgreSQL compatibility | `3390234` | pull_request | [44991091160](https://github.com/shahid1995/options-dashboard/actions/runs/44991091160) | success |
| Issue #145 application integration (with skip/under-count gates) | `e065194` | push | [44991441771](https://github.com/shahid1995/options-dashboard/actions/runs/44991441771) | success |
| Issue #145 application integration (with skip/under-count gates) | `e065194` | pull_request | [44991445717](https://github.com/shahid1995/options-dashboard/actions/runs/44991445717) | success |
| PostgreSQL compatibility (with skip/under-count gates) | `e065194` | push | [44991441594](https://github.com/shahid1995/options-dashboard/actions/runs/44991441594) | success |
| PostgreSQL compatibility (with skip/under-count gates) | `e065194` | pull_request | [44991445664](https://github.com/shahid1995/options-dashboard/actions/runs/44991445664) | success |

Baseline failure for comparison (PR head `1845e54`): run
[44999476675](https://github.com/shahid1995/options-dashboard/actions/runs/44999476675)
— 111 passed, 5 failed, 5 errors.

The `pull_request` runs prove finding F6 is closed: the workflows now execute
for a PR whose base is `feat/strikenova-day35-portfolio-intelligence`.

Both gates fail on: non-zero pytest status, any skipped test, an explicit
`failed`/`error` line, or fewer passing tests than the suite really has (≥ 22
integration, ≥ 120 compatibility). A skipped-everything run can no longer look
green.

## 9. Cleanup evidence

* Both workflows run a `Drop leftover test databases and roles` step with
  `if: always()`; it prints `cleanup: dropped N database(s), N role(s)` and the
  step succeeded on every run above.
* The local disposable cluster ends with **0** `sn145_*` databases and **0**
  `sn145_*` roles after the full integration and compatibility runs.
* One leaked provisioning (from a run that failed before the fixture received
  its handle) was found and dropped manually during this remediation; the
  provisioning helper now rolls back its own partial resources on any failure,
  so that leak cannot recur.

## 10. Residual limitations and explicitly unverified items

* **Not verified against Neon or any production-class instance.** All evidence
  is from disposable PostgreSQL 16.16; TTL/timeout defaults are synthetic
  measurements (runbook §7).
* **No live role/grant configuration was created or changed.** The Topology A/B
  matrices are proven on disposable roles only.
* **Sites/environments not exercised:** no Render change, no deploy, no cutover,
  no Neon provisioning.
* **Pylint workflow** still fails on its own `actions/setup-python@v3` setup on
  Ubuntu 24.04. It is unrelated to this change and was not repaired here.
* **Concurrent first-ever bootstrap** of a genuinely empty database is covered by
  a dedicated contention test accepting `55P03`, not by a claimed proof of two
  simultaneous first installations.
* **Superseded by the ADR conformance round:** the earlier claim that Topology A
  cannot revoke privileges from its own creator was wrong. PostgreSQL 16 applies
  a `REVOKE` to the owner's own ACL, and the guarded migration now enforces the
  §8.3 Topology A matrix in both topologies; only inherent ownership powers
  (`ALTER`/`DROP`, re-granting) remain. See
  [`ADR_CONFORMANCE_2026-11-22.md`](ADR_CONFORMANCE_2026-11-22.md) and runbook
  §2.2.
* **This report's own commit** re-runs both workflows; its results are visible in
  PR #146's checks. The code evidence above is pinned to `3390234` / `e065194`.
* **PR #146 remains Draft and is not approved for merge.** Independent acceptance
  review is still required.
