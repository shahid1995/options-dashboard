# Issue #145 — final ADR-018 privilege and target-identity acceptance pass

**Branch:** `issue-145-fix` · **PR:** [#146](https://github.com/shahid1995/options-dashboard/pull/146) (Draft, open, unmerged)
**Starting HEAD:** `f56f99c6a4267528833662bf73e05b65d5051aea`
**Scope:** the four remaining independent-review concerns. No ADR redesign, no Neon/Render/production contact, no role, grant, credential or provider change outside disposable PostgreSQL 16.16.

---

## 1. Target identity — deterministic evidence where PostgreSQL provides it

### What was wrong

`_logical_target_signature` compared `current_database()`, `pg_postmaster_start_time()` and the `pg_database` OID, and its own docstring admitted that unrelated clusters could coincidentally share those values. The alias declaration was validated, but the server-side proof was only probabilistic.

### What PostgreSQL 16 actually offers, measured

| Candidate | Readable without superuser | Stable | Cluster-unique | Verdict |
| --- | --- | --- | --- | --- |
| `pg_control_system().system_identifier` | **yes** — `proacl` is NULL, i.e. the default `=X/<owner>`, so `PUBLIC` has `EXECUTE` | yes, created by `initdb`, survives restarts | **yes** | used as primary evidence |
| `current_setting('cluster_name')` | yes | only if an operator sets it | no — default is `''` | rejected, not an identifier |
| `current_database()` / `pg_database.oid` | yes | yes | no | kept as fallback only |
| `pg_postmaster_start_time()` | yes | until restart | no | kept as fallback only |
| `inet_server_addr()` / port | yes | yes | no | rejected: route, not cluster |

Measured on the disposable cluster used for this pass (PGDG `16.16-1.pgdg24.04+1`):

* a plain `LOGIN` role with no grants read `system_identifier` successfully;
* two clusters started from separate `initdb` runs reported `7710572404024957629` and `7710894975794810555`;
* `cluster_name` was `''` on both, confirming it proves nothing by default.

### What changed

`app/db.py`

* new `_cluster_system_identifier(conn)` — reads `system_identifier` and returns `None` when the role is refused, instead of raising;
* `_logical_target_signature` now returns `(database, postmaster_start_time, database_oid, system_identifier | None)`;
* `_verify_logical_target` compares the identifier **first** and refuses (`45004`, "different PostgreSQL cluster identifiers") when both are present and differ;
* when either side cannot read it (a provider revoked `pg_control_system()`), the probabilistic fingerprint is still compared and a **warning naming the fallback** is logged. The weaker evidence is never reported as the stronger one.

The alias declaration still only *permits* the comparison: an undeclared different host is refused before any connection, and a declared alias must still prove the same cluster.

### Tests (`tests/test_issue145_application_integration.py`)

| Test | Proves |
| --- | --- |
| `test_both_identities_read_one_cluster_system_identifier` | both the runtime and the migrator read a non-null identifier and the two signatures are equal |
| `test_logical_target_verification_uses_the_cluster_identifier` | `_migration_engine()` takes the deterministic branch (no fallback warning) |
| `test_logical_target_refuses_a_different_cluster_identifier` | two identifiers → `45004`, message names the cluster identifiers, no URL |
| `test_logical_target_falls_back_explicitly_without_the_identifier` | a revoked function degrades to the fingerprint *and* logs the warning |

---

## 2. The privilege verifier now covers the whole matrix

`app/_migration_lease.py::verify_application_privileges` (still read-only, still fail-closed with `45004`, still never repairs).

### Added checks

**Runtime role**

* effective `CREATE` on schema `public` (previously unchecked — the runtime could create objects there);
* `CREATEDB`, `CREATEROLE`, `BYPASSRLS`, `REPLICATION` attributes;
* **no membership at all** — previously only `migrator`/`owner` were checked, so `GRANT pg_read_all_data` or a bridge role was invisible;
* `TRUNCATE`, `REFERENCES`, `TRIGGER` on all three application tables;
* `UPDATE` on the exposure id sequence.

**Owner role (Topology B)**

* `CREATEROLE`, `CREATEDB`, `BYPASSRLS`, `REPLICATION` (on top of the existing `NOLOGIN` / no-superuser rules);
* no membership of any role, plus an explicit `pg_has_role(owner, migrator, 'MEMBER')` negative;
* no effective `USAGE`/`CREATE` on `migration_internal` and no `EXECUTE` on its functions.

**Migrator role**

* `BYPASSRLS`/`REPLICATION` (Topology A/B rules otherwise unchanged);
* `REFERENCES` and `TRIGGER` on every application table are now examined, not just DML and `TRUNCATE`.

### What enforcement changed, not just detection

`app/db.py::apply_migrator_privilege_restrictions` (Topology A) now also revokes `REFERENCES` and `TRIGGER` from the migrator. Before this pass those two privileges were silently present on all three tables (`relacl` showed `=rxt`); with the verifier examining them, the migration and the verifier now converge on the same §8.3 matrix instead of the verifier passing an unchecked corner. Both revokes are idempotent and re-applied by every `upgrade head`.

### Negative controls added (all rejected by the real verifier with `45004`)

Runtime `CREATE` on `public` · runtime `CREATEDB` · runtime `CREATEROLE` · runtime `BYPASSRLS` · runtime sequence `UPDATE` · runtime membership of `pg_read_all_data` · runtime access to `migration_internal` through an intermediate role · owner membership of an intermediate role · owner membership of `pg_read_all_data` · owner `CREATEROLE` · owner `CREATEDB` · owner `USAGE` on `migration_internal`.

Plus:

* `test_verifier_refuses_owner_membership_of_migrator` — PostgreSQL refuses the direct `GRANT migrator TO owner` cycle (the migrator is already a member of the owner); the test asserts that refusal and that the valid installation still verifies, documenting why the `pg_has_role` check is defence-in-depth.
* `test_guarded_upgrade_refuses_runtime_public_create` — the real `python -m app.migrate upgrade head` path refuses to commit an installation the DDL cannot repair (the second end-to-end enforcement proof, alongside the existing superuser one).

Positive controls for both topologies remain and still pass.

---

## 3. Preserved results

| Suite | Command | Result |
| --- | --- | --- |
| Target-identity contract | `pytest -q tests/test_migration_identity_separation.py` | **19 passed** |
| Application integration | `pytest -q tests/test_issue145_application_integration.py` | **55 passed** (was 37) |
| Regression | the 8-module workflow suite | **156 passed** (unchanged) |
| PostgreSQL compatibility | the 9-module workflow suite | **122 passed, 0 skipped** (unchanged) |
| Lease/protocol contract | `tests/test_migration_serialization.py` | **55 passed** (unchanged) |

Gate minimums were raised deliberately, never lowered: `integration|55`, `regression|156` in `.github/workflows/issue145-application-pg16.yml` and in the `pytest_gate.py` docstring.

Static checks: `python -m compileall` clean; `ruff check` on the four touched files reports only pre-existing findings in the pre-existing code; the workflow YAML parses.

---

## 4. Security gate

### SonarCloud `githubactions:S8544`

The rule is "python dependencies should be locked to verified versions". The finding was on `python -m pip install -r requirements.txt -r requirements-dev.txt`: exact pins and `--only-binary :all:` were already in place, but a `-r` requirements file does not tell the analyser that the *resolved closure* is locked.

Fixed by adding `options-dashboard-project/backend/requirements.lock`:

* the full 44-package closure of `requirements.txt` + `requirements-dev.txt`, resolved once with pip for CPython 3.12 / Linux x86_64;
* every version an exact `==` pin, every published file of that version carrying its `sha256`;
* CI installs only the lock: `python -m pip install --only-binary :all: --require-hashes -r requirements.lock`.

Verified locally in a clean virtualenv: the hash-checked install succeeds and the resulting environment imports FastAPI, SQLAlchemy, psycopg, Alembic and pytest at the pinned versions. Hash-checking mode fails closed if the lock and the requirement files ever diverge, and refuses any artifact whose bytes were not verified.

No `NOSONAR`, no exclusion, no weakened check. The remaining rating is reported as CI reports it.

### OpenCodeReview

Still **unverified**. The last run failed on OpenRouter HTTP 429 and produced no review; no permitted free route was available in this pass. CodeRabbit is skipped because reviews are disabled for this PR's base. Neither is reported as passed.

---

## 5. Boundaries honoured

* Branch re-fetched and verified at `f56f99c…` before any edit; no other agent's worktree was written to.
* Only disposable PostgreSQL 16.16 (PGDG binaries, `127.0.0.1:55432` plus a throwaway second cluster used solely to prove the identifier differs).
* No Neon, Render, production or staging connection; no secret read or echoed.
* No live role/grant/credential/provider change.
* PR #146 left open, Draft and unmerged; PR #149 untouched.

## 6. Final commit

See the PR #146 head for the exact SHA of this pass; the evidence table above is the local result of that commit.
