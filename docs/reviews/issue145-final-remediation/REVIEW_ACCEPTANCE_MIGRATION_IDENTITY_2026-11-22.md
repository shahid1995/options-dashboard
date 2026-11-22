# Issue #145 — the migration connection must *be* the configured migrator

**Branch:** `issue-145-fix` · **PR:** [#146](https://github.com/shahid1995/options-dashboard/pull/146) (Draft, open, unmerged)
**Starting HEAD verified:** `5883635765a2f3c66616392e663ea2c44d4a8152`
**Code commit:** `12716f19b9cc40dd8ce7a26b04c9710702567530`
**Docs commit:** the commit that adds this report, directly on top of `12716f1`
**Scope:** close the gap between "the migration login differs from the runtime login" and "the migration connection is the configured migrator". No ADR redesign, no Neon/Render/Vercel/production/staging contact, no role, grant, credential or provider change outside disposable PostgreSQL 16.16.

---

## 1. Workspace protection

* `git ls-remote origin refs/heads/issue-145-fix` reported `5883635…` before any edit, and the local branch matched it; the remote did not move during the pass.
* The tracked tree was clean. The only untracked paths were the pre-existing evidence artefacts (`.edits/`, `.local-pg/`, `.review-verify/`, `analysis/`, `artifacts/`, older reports). They were preserved, not added to either commit.
* No concurrent writer was active on the branch.
* `AGENTS.md` was followed: inspect the current shape of every file before editing, treat a comment as a claim rather than proof, keep the scope fence, and never weaken an assertion to make a suite pass.

## 2. The defect that remained

`assert_guarded_migration_configuration` proved four URL properties — PostgreSQL dialect, distinct login, same database name, same endpoint. It never proved the migration login **was** `STRIKENOVA_MIGRATOR_ROLE`. Consequences:

| Configuration | Old result | Why it is wrong |
| --- | --- | --- |
| `STRIKENOVA_MIGRATION_DATABASE_URL` uses the cluster `postgres` superuser | accepted when it differed from the runtime login | DDL and backfill ran as a superuser, so no revoke or matrix check bounded it |
| the URL uses an unrelated ordinary login | accepted | migrations ran as a principal nobody configured |
| the URL uses a login granted membership in the migrator | accepted | that login could `SET ROLE` the migrator at will |
| the migrator role itself is `SUPERUSER` | refused only later, by the matrix verifier | privilege inspection ran *after* DDL had already executed |

A successful privilege check of the migrator role is not evidence about the connection: the verifier reads catalogs for a role name, while the session may be a different login.

## 3. What is now enforced

### 3.1 The URLs name the configured identities (`app/db.py`)

A fifth URL property is checked before any engine, connection, lease or DDL exists, refusing `45004`:

* the migration URL login must equal `STRIKENOVA_MIGRATOR_ROLE`; a blank role name is refused;
* the runtime URL login must equal `STRIKENOVA_RUNTIME_ROLE` when that is configured (an unset runtime role does not constrain it).

Messages name the observed and the configured role, never the URL or its password.

### 3.2 Every actual connection is proven (`app/db.py`)

`_install_migration_identity_guard` registers a SQLAlchemy `connect` listener on the migration engine *before its first connection*. Each new DBAPI connection reads, from the server:

```sql
SELECT session_user, current_user, r.rolcanlogin, r.rolsuper, r.rolcreaterole,
       r.rolcreatedb, r.rolbypassrls, r.rolreplication
FROM pg_roles r WHERE r.rolname = session_user
```

and refuses `45004` while the connection is still being created when the session login is not the configured migrator, when `current_user` differs, when the role is not a `LOGIN`, or when it carries `SUPERUSER`, `CREATEROLE`, `CREATEDB`, `BYPASSRLS` or `REPLICATION`. `lease.acquire()`, the DDL transaction, every per-user backfill transaction and every pooled reconnect are covered — not one sampled connection.

### 3.3 The identity is re-proven at the two points that matter

* `_verify_application_privileges` now calls `_assert_migration_session_identity` before delegating to the §8.3 verifier. It is invoked (a) inside the DDL transaction **after** `RESET ROLE`, and (b) before the startup backfill's first write. In both places the check requires `session_user` **and** `current_user` to be the migrator.
* `bootstrap-lease` and `--close-barrier` extend `_assert_migrator_identity`: `session_user` must be the migrator, so a login that can assume the role cannot bootstrap as it.

### 3.4 Topology B's approved exception is preserved

Inside the DDL window `current_user` becomes the NOLOGIN owner via `SET ROLE`; `session_user` remains the migrator throughout. `RESET ROLE` is verified to restore the migrator, and the identity check that runs immediately afterwards proves the elevation did not outlive the window — before verification, before `COMMIT`, and before any backfill write.

### 3.5 Incoming membership (`app/_migration_lease.py`)

`verify_application_privileges` closed only the *outgoing* membership matrix (what the migrator, runtime and owner are members of). It now also refuses `45004` when:

* any role is a member of the migration identity (both topologies) — such a login can inherit or `SET ROLE` the migrator;
* in Topology B, any role other than the migrator is a member of the owner role — the migrator must be the owner's only approved member.

The migration identity must also be a `LOGIN` role.

## 4. New negative controls

Eight integration tests run the real path against disposable PostgreSQL 16.16 (`tests/test_issue145_application_integration.py`):

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_startup_refuses_the_superuser_login_before_any_work` | migration URL = the `postgres` admin URL | `45004`, "not the configured migration identity", state unchanged |
| `test_startup_refuses_an_unrelated_login_before_any_work` | fresh ordinary `LOGIN` role | `45004`, state unchanged |
| `test_startup_refuses_a_login_holding_migrator_membership` | fresh `LOGIN` granted the migrator `WITH INHERIT TRUE, SET TRUE` | `45004`, state unchanged; after `SET ROLE` to the migrator the session check still refuses on `session_user` |
| `test_startup_refuses_a_superuser_migration_role` | URL names the migrator, `ALTER ROLE … SUPERUSER` | `45004`, "has SUPERUSER" — refused at connection, before the lease |
| `test_session_identity_accepts_the_migrator_connection` | the configured migrator | accepted |
| `test_verifier_refuses_a_non_login_migration_identity` | a `NOLOGIN` role passed as the migrator | `45004`, "is not a LOGIN role" |
| `test_verifier_refuses_unexpected_incoming_migrator_membership` | third login granted the Topology A migrator | `45004`, names the unexpected member |
| `test_verifier_refuses_unexpected_incoming_owner_membership` | third login granted the Topology B owner | `45004`, "Topology B allows exactly one member" |

"State unchanged" is asserted against a snapshot of the **lease row** (ownership, generation, protocol version, expiry), `alembic_version.version_num` and the application data (`positions` count and the per-user `strategy_leg_exposures` ledger) taken before and after the refused run — proving the refusal happens before lease acquisition, DDL or backfill rather than somewhere in the run.

Four DB-free tests were added to `tests/test_migration_identity_separation.py` for the URL-level binding (migration login must be the configured migrator, runtime login must be the configured runtime identity, blank migrator role refused, unset runtime role not constrained). Existing hermetic fixtures were updated to name their roles, which is the new contract rather than a relaxed one.

One existing test changed meaning deliberately: `test_bad_privilege_matrix_aborts_the_guarded_upgrade` used to accept a superuser migrator until the §8.3 verifier; the invalid identity is now refused one step earlier by the connection guard, and the test asserts that earlier `45004` refusal.

No test was removed, skipped, `xfail`ed or given a suppression, and no assertion was weakened. Gate minimums were raised, not lowered:

* integration `63` → **`71`**;
* regression `156` → **`160`**.

## 5. Local evidence (disposable PostgreSQL 16.16, `server_version_num = 160016`)

| Check | Command scope | Result |
| --- | --- | --- |
| Application integration | `tests/test_issue145_application_integration.py` | **71 passed** (80.3 s) |
| Regression | `test_migration_serialization`, `test_db_migration`, `test_migration_identity_separation`, `test_alembic_*`, `test_day5_alembic_authority`, `test_leg_exposure` | **160 passed** (16.3 s) |
| Impacted files alone | `test_migration_serialization.py` / `test_migration_identity_separation.py` | 55 / 23 passed |
| PostgreSQL compatibility on CPython **3.13.15** | the nine modules the compatibility workflow runs | **122 passed, 0 skipped** (32.2 s) |
| Gate script | `scripts/pytest_gate.py` with `71` / `160` | `integration: 71 passed (minimum 71)`, `regression: 160 passed (minimum 160)`, exit 0 |
| Byte-compile | `python -m compileall -q app tests` | clean |
| Lint | `ruff check` on the five touched Python files | 39 findings, **all on pre-existing lines** (`app/db.py` 19 = its baseline count; the two findings my first draft introduced were fixed, not suppressed) |
| Cleanup | `sn145_` databases/roles after the run | none left; the cluster was stopped |

The 3.13 run used the existing hash-locked `requirements-py313.lock` closure (interpreter 3.13.15). No production, staging, Neon or provider credential is read by any of these suites.

## 6. CI evidence on `12716f19b9cc40dd8ce7a26b04c9710702567530`

| Workflow / check | Run | Conclusion |
| --- | --- | --- |
| Issue #145 application integration (push) | [45004364137](https://github.com/shahid1995/options-dashboard/actions/runs/45004364137) | **success** — gate `integration|71`, `regression|160` |
| Issue #145 application integration (PR) | [45004360586](https://github.com/shahid1995/options-dashboard/actions/runs/45004360586) | **success** |
| PostgreSQL compatibility (push) | [45004364112](https://github.com/shahid1995/options-dashboard/actions/runs/45004364112) | **success** — Python 3.13, `--require-hashes -r requirements-py313.lock` |
| PostgreSQL compatibility (PR) | [45004360553](https://github.com/shahid1995/options-dashboard/actions/runs/45004360553) | **success** |
| Status tracker and master plan validation | [45004364159](https://github.com/shahid1995/options-dashboard/actions/runs/45004364159) | success (2 Node-20 deprecation annotations, no test results) |
| Vercel Preview Comments | check run `137152540185` | success |
| CodeRabbit | check run `137152567155` | **skipped** — "reviews are disabled for this base branch" |
| OpenCodeReview Analysis | [45004362251](https://github.com/shahid1995/options-dashboard/actions/runs/45004362251) | **failure at its own `Run OpenCodeReview` step** (exit 1) — no review was produced, so this pass is **unverified** by that tool |
| SonarCloud (PR 146) | `/api/qualitygates/project_status` | gate **OK** (reliability A, security A, maintainability A, 0.5 % duplication) — but the latest PR-146 analysis is for revision `eeeebffe…` dated 2026-11-20, i.e. **not the current head**; no analysis of `12716f1` was observed |

The failing third-party checks are reported as they are: OpenCodeReview failed inside its own runner (the job log's only failure annotation is "Process completed with exit code 1"; its last observed cause on this branch was a provider HTTP 429 and there is no free alternative review route here), and CodeRabbit did not run because reviews are disabled for the base branch. Neither is presented as a pass, and neither was worked around by adding `NOSONAR`, broad exclusions or any other suppression.

## 7. Files changed by `12716f1`

* `options-dashboard-project/backend/app/db.py` — configured-identity URL binding, `_MIGRATION_IDENTITY_SQL`, `_migration_identity_problems`, `_assert_migration_session_identity`, `_install_migration_identity_guard`, identity re-proof inside `_verify_application_privileges`.
* `options-dashboard-project/backend/app/_migration_lease.py` — `_incoming_members`, incoming-membership refusals and the migrator `LOGIN` check in `verify_application_privileges`; `session_user` in `_assert_migrator_identity`.
* `options-dashboard-project/backend/tests/test_issue145_application_integration.py` — the eight controls above, the extended state snapshot helper, and the corrected superuser-refusal expectation.
* `options-dashboard-project/backend/tests/test_migration_identity_separation.py` — four URL-binding tests plus role-name fixtures.
* `options-dashboard-project/backend/tests/test_migration_serialization.py` — role-name fixtures for the hermetic identity rules.
* `.github/workflows/issue145-application-pg16.yml` — gate minimums `71` / `160`.
* `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md` — §2.3 property 5, new §2.4 (live connection identity), §8.1 incoming-membership row and controls, §9 counts.

## 8. Remaining limitations

* The guard proves the **authenticated session**; it does not prevent an out-of-band superuser or a holder of valid credentials from changing roles, grants or catalogs after the fact. It bounds what the application's own connections may do, not what a privileged operator may do.
* Identity is proven from the session's own server-side view (`session_user`/`current_user`/`pg_roles`). A malicious server could lie; that is outside the threat model of every check in this PR.
* Topology B still needs its approved `SET ROLE` window: inside it `current_user` is the owner. Only `session_user` distinguishes the migrator there, which is exactly what is asserted.
* The endpoint-equivalence proof and its `42501`/`42883`-only fallback from the previous pass are unchanged, including their documented limitation for two unrelated clusters that share the probabilistic fingerprint.
* Concurrent first installation and the two-process contention proof remain as scoped in runbook §10; this pass adds no stress or throughput claim.
* No Neon, Render, Vercel, staging or production system was contacted, no credential was read or printed, and no role or grant outside the disposable test cluster was changed.
* `12716f1` is not independently reviewed by OpenCodeReview (its run failed) and SonarCloud has not analysed it; those two signals must not be read as acceptance.

## 9. PR state

PR [#146](https://github.com/shahid1995/options-dashboard/pull/146) remains **open, Draft and unmerged**; its description was updated to this head's results. PR #149 was not touched.
