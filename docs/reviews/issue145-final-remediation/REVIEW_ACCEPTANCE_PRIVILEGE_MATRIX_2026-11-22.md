# Issue #145 — privilege-matrix closure and identity-fallback narrowing

**Branch:** `issue-145-fix` · **PR:** [#146](https://github.com/shahid1995/options-dashboard/pull/146) (Draft, open, unmerged)
**Starting HEAD verified:** `5eb2bf2139c8665b77ede0616e5b71f3497741ee`
**Remediation commit:** `c872a58e71cdc53f480d267b5da1082c935d9ecb` *(the report commit is a follow-up on top of it)*
**Scope:** the remaining gaps from the independent acceptance review. No ADR redesign, no Neon/Render/Vercel/production/staging contact, no role, grant, credential or provider change outside disposable PostgreSQL 16.16.

---

## 1. Workspace protection

* `git ls-remote` reported `refs/heads/issue-145-fix = 5eb2bf2…` before any edit, and the local branch matched it.
* The tracked working tree was clean; the only untracked paths were pre-existing evidence artefacts (`.edits/`, `.local-pg/`, `analysis/`, `artifacts/`, older reports), which were left untouched and are **not** part of the commit.
* No other writer was active on the branch: the remote ref did not move between the start of the pass and the push, and every file edited here was read from this worktree first.
* `AGENTS.md` was followed: inspect, do not assume, stay inside the stated scope, record rather than silently widen.

## 2. Default privileges are now enforced exactly

`app/_migration_lease.py::_application_default_acl_problems` previously checked only the *missing* direction. It now fails closed in both directions, on the actual object creator, scoped to `public`:

| Object | Required | Now rejected |
| --- | --- | --- |
| Tables (`r`) | exactly `SELECT`, `INSERT`, `UPDATE`, `DELETE` | any missing privilege **and** any excess privilege (`TRUNCATE`, `REFERENCES`, `TRIGGER`, …) |
| Sequences (`S`) | exactly `USAGE` | missing `USAGE` **and** excess `SELECT` or `UPDATE` |
| Other object types | none | a runtime default on any other object type |
| Scope | `IN SCHEMA public`, `defaclrole` = the real creator | `<global>`, another schema, or `PUBLIC` |

Messages name the exact surplus set (`excess table privileges ['TRUNCATE']`, `excess sequence privileges ['SELECT']`), so a failure is diagnosable rather than generic.

## 3. The sequence matrix is `USAGE`-only, in the verifier, the fixture and the runbook

`_sequence_problems` used to apply different rules per identity: the migrator was refused `SELECT` and `UPDATE`, the runtime only `UPDATE`. Both identities are now held to the accepted `USAGE`-only matrix (`USAGE` authorises `nextval`/`currval`; `SELECT` exposes `last_value` and `UPDATE` authorises `setval`).

Contradictions found and corrected:

* `tests/issue145_pg_support.py` — `GRANT USAGE, SELECT ON SEQUENCES` → `GRANT USAGE ON SEQUENCES`;
* `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md` §3 step 3 — `USAGE/SELECT ON SEQUENCES` → `USAGE` only;
* runbook §8.1 runtime-sequence row — now states `USAGE` true, `SELECT` false, `UPDATE` false;
* runbook §9 — the integration row and the gate minimums.

Left deliberately unchanged: `grant_importer_write_privileges` still grants `ALL` on one sequence, because that helper is the legacy **importer** rehearsal running as the migrator and `setval()` requires `UPDATE`. It is a documented, test-only concession on a throwaway database, outside the §8.3 runtime matrix, and the verifier is asserted by the integration suite. The accepted design was not widened to make any test pass.

## 4. Negative controls (real PostgreSQL 16.16, real verifier, `45004`)

| New test | Installed state | Refusal asserted |
| --- | --- | --- |
| `test_verifier_refuses_runtime_default_truncate` | default `TRUNCATE` on future public tables | `excess table privileges ['TRUNCATE']` |
| `test_verifier_refuses_runtime_default_references_and_trigger` | default `REFERENCES` + `TRIGGER` on future tables | `excess table privileges ['REFERENCES', 'TRIGGER']` |
| `test_verifier_refuses_runtime_default_sequence_select` | default `SELECT` on future sequences | `excess sequence privileges ['SELECT']` |
| `test_verifier_refuses_runtime_default_sequence_update` | default `UPDATE` on future sequences | `excess sequence privileges ['UPDATE']` |
| `test_verifier_refuses_runtime_sequence_select_on_the_exposure_sequence` | `GRANT SELECT` on the existing exposure sequence | `<runtime> has SELECT on the exposure id sequence` |

The existing runtime-sequence `UPDATE` control and both positive topology tests (A and B) still pass, so the additions reject only the overgrants they install.

## 5. The identity fallback is narrowed

`app/db.py::_cluster_system_identifier` treated **every** `DBAPIError` as "the function is unavailable" and returned `None`, which silently swapped deterministic evidence for the probabilistic fingerprint.

Now only two SQLSTATEs may use the fallback, because they are the documented availability cases:

| SQLSTATE | Meaning | Behaviour |
| --- | --- | --- |
| `42501` | `insufficient_privilege` — a hardened installation revoked PUBLIC `EXECUTE` | return `None`, caller compares the fingerprint and logs the weaker-evidence warning |
| `42883` | `undefined_function` — the server does not provide `pg_control_system()` | same |
| anything else (e.g. `08006` connection failure, timeouts) | unexpected | **raise `45004`**: "cannot read … refusing to fall back to the probabilistic cluster fingerprint on an unexpected database error" |

New tests:

* `test_cluster_identifier_only_degrades_for_documented_refusals` — `42501` and `42883` return `None`;
* `test_cluster_identifier_unexpected_error_fails_closed` — `08006` raises `45004`, names the SQLSTATE and the refusal;
* `test_logical_target_warns_when_the_identifier_is_revoked` — the **real** documentation case: `REVOKE EXECUTE ON FUNCTION pg_control_system() FROM PUBLIC` on the disposable cluster, the identifier is unreadable, `_verify_logical_target` still proves the target by fingerprint and logs the warning, then the grant is restored.

Error text contains only the SQLSTATE and a description — no URL, host, user or password.

**What this evidence does and does not establish.** `pg_control_system().system_identifier` is a 64-bit value created by `initdb`; it is stable across restarts and identical for two connections to one cluster, so it *deterministically* distinguishes two clusters. It does **not** identify a Neon branch, project, compute endpoint or pooled route — those are provider concepts PostgreSQL does not expose. No Neon connection was made to test this, and no such claim is made.

## 6. CI dependency installation is hash-locked on both interpreters

`postgres-compatibility.yml` kept Python 3.13 coverage but installed `requirements-dev.txt` with no hash verification, while the new `requirements.lock` is a CPython 3.12/Linux x86_64 closure.

Added `options-dashboard-project/backend/requirements-py313.lock`:

* the full resolved closure of `requirements.txt` + `requirements-dev.txt` for CPython 3.13 / `x86_64-manylinux_2_28`, binary distributions only, generated with uv's resolver against a real CPython 3.13 interpreter;
* every version an exact pin, every file of that version hashed;
* installed with `python -m pip install --only-binary :all: --require-hashes -r requirements-py313.lock`, plus the same exact `pip==26.3` pin the 3.12 job uses.

Validated, not assumed: a clean CPython **3.13.15** virtualenv was created, the lock installed with `--require-hashes --only-binary :all:` (exit 0), the environment imported FastAPI/SQLAlchemy/psycopg/Alembic/pytest/PyJWT, and the full **9-module PostgreSQL compatibility suite ran green on 3.13.15 (122 passed)**.

The two lockfiles are deliberately separate: one closure cannot be installed reproducibly on the other interpreter. Neither job installs an unhashed requirements file any more, and this report does not describe a path as hash-locked unless it is.

## 7. The concurrency test now states what PostgreSQL observes

`lease_enter_v1` takes `pg_advisory_xact_lock(hashtextextended('strikenova:migration_internal:migration_guard', 0))` **before** it selects the lease row `FOR UPDATE`. A second worker therefore queues on the advisory guard and never reaches the row lock. The previous test was named after the row lock and asserted only "some ungranted lock", which overstated the mechanism.

Renamed and tightened to `test_two_process_startup_contends_on_the_advisory_guard_lock`:

* worker B has an **ungranted advisory lock whose 64-bit key equals the guard key** (computed in SQL from `hashtextextended(...)`, compared against `(classid << 32) | objid` as an unsigned value);
* worker A **holds the granted advisory guard lock** and the granted lease row lock;
* worker B has **no lock entry at all** for `migration_internal.migration_lease`, which is the positive proof that it is blocked before the row lock rather than on it.

The end-to-end success, exactly-once exposure ledger and no-`23505` assertions are unchanged. The test still does not claim row-lock contention, throughput or deployment timing.

## 8. Local verification (disposable PostgreSQL 16.16, `server_version_num = 160016`)

| Suite | Command | Result |
| --- | --- | --- |
| Application integration | `pytest -q tests/test_issue145_application_integration.py` | **63 passed** (was 55; +5 default/excess controls, +3 identity controls) |
| Regression | the 8-module workflow suite | **156 passed** (unchanged) |
| PostgreSQL compatibility (Python 3.13.15, new lock) | the 9-module workflow suite | **122 passed, 0 skipped** |
| Target-identity contract + lease contract | `test_migration_identity_separation.py`, `test_migration_serialization.py` | **74 passed** (19 + 55) |
| Gate | `scripts/pytest_gate.py --suite "integration\|63\|…" --suite "regression\|156\|…"` | `integration: 63 passed (minimum 63)`, `regression: 156 passed (minimum 156)`, exit 0 |

Gate minimums were **raised**, never lowered: `integration|63`, `regression|156` in `.github/workflows/issue145-application-pg16.yml` and in the `pytest_gate.py` docstring.

Static checks: `python -m compileall` clean on the package and the touched tests; both changed workflow YAML files parse; `ruff check` on the five touched Python files reports 35 findings, all on lines that predate this pass (none in the added code).

Cleanup: 0 leftover `sn145_` databases and roles; the local cluster was stopped after the run.

## 9. CI evidence for `c872a58e71cdc53f480d267b5da1082c935d9ecb`

Each workflow ran twice for this commit (`pull_request` and `push`); both runs of both workflows succeeded.

| Workflow / check | Run | Result |
| --- | --- | --- |
| `issue145-application-pg16.yml` (PostgreSQL 16.16 lease integration) | [45001856378](https://github.com/shahid1995/options-dashboard/actions/runs/45001856378) (PR), [45001850878](https://github.com/shahid1995/options-dashboard/actions/runs/45001850878) (push) | **success** — hash-locked 3.12 install, `pytest_gate` passed with `integration|63` and `regression|156`, which fails the job on any non-zero rc, any failed/errored test, any skip, a missing summary, or fewer passes than the minimum. Its single annotation is the Node-20 action deprecation warning, not a test result |
| `postgres-compatibility.yml` | [45001856274](https://github.com/shahid1995/options-dashboard/actions/runs/45001856274) (PR), [45001850938](https://github.com/shahid1995/options-dashboard/actions/runs/45001850938) (push) | **success** — CPython 3.13.18, installed from `requirements-py313.lock` with `--require-hashes --only-binary :all:` (visible in the job log), 0 failed, 0 skipped, ≥120 passes enforced by the gate |
| `strikenova-status-gate.yml` | [45001856318](https://github.com/shahid1995/options-dashboard/actions/runs/45001856318) | **success** |
| Semgrep Code | scan [274856219](https://semgrep.dev/orgs/business_ikon_personal_org/projects/6755353/scans/274856219) | **success**, 0 annotations |
| Vercel | Preview Comments | **success**, 0 unresolved feedback |
| SonarCloud Code Analysis | [PR #146 dashboard](https://sonarcloud.io/dashboard?id=shahid1995_options-dashboard&pullRequest=146) | **gate OK**, **0 new issues** (`/api/issues/search?sinceLeakPeriod=true` → `total: 0`), `new_security_rating = 1` (**A**), reliability and maintainability also 1, 0.6% duplication, 100% of security hotspots reviewed |
| OpenCodeReview Analysis | [45001853260](https://github.com/shahid1995/options-dashboard/actions/runs/45001853260) | **failure — no review produced, unverified** |
| CodeRabbit | — | **skipped** (reviews disabled for the base branch) — no review |

CI and local counts are reported separately on purpose: CI proves the gates pass at the configured minimums; the exact local counts are in §8.

## 10. Gates that are not passes

* **OpenCodeReview Analysis** — the job is triggered by `pull_request_target` and runs a third-party review bot; on this commit it failed again at its own step (run 45001853260, last known cause: provider HTTP 429). It is **not** reported as passed, and this report does not substitute for it.
* **CodeRabbit** — reviews are disabled for this PR's base branch, so the check is skipped. **No review was produced**; skipped is not passed.
* **SonarCloud** — the gate status and issue count in §9 are read from the SonarCloud API for `pullRequest=146`, not inferred from a GitHub check badge. `0 new issues` with `new_security_rating = 1` is the current state; the previous pass's `S8544` finding is gone because both CI installs are now hash-locked.

## 11. Remaining limitations

* No Neon, Render, production or staging contact: nothing here proves a provider-specific branch/compute identity, and `system_identifier` is explicitly not claimed to do so.
* The fingerprint fallback remains probabilistic; it is now reachable only through the two documented SQLSTATEs, but two unrelated clusters that coincidentally share database name, postmaster start time and database OID are still indistinguishable in that mode.
* Default-privilege checks cover what `pg_default_acl` reports for the configured creator in `public`; a default attached to a *different* role that later creates an application object is a provisioning error the verifier cannot see from the runtime's catalogs.
* The concurrency proof is deterministic for contention on the advisory guard, not a stress or throughput measurement.
* Topology A still leaves inherent owner powers (`ALTER`/`DROP`/re-grant) with the migrator; ownership separation remains the stronger model.
* The unrelated Pylint workflow still fails on its own `actions/setup-python@v3` step; it is not repaired here.

## 12. State

PR #146 is left **open, Draft and unmerged**; PR #149 is untouched. No secrets were read, printed or copied; every credential used was generated per-run on the disposable cluster.
