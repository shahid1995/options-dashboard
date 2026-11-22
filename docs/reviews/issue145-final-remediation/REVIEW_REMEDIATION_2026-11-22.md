# Issue #145 — independent review remediation (ADR-017/018)

**Branch:** `issue-145-fix` · **PR:** #146 (Draft, open, unmerged) ·
**Architecture:** approved ADR-017/018 (PR #149),
`docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`
**Baseline commit:** `1e4719c`
**Status:** implementation and verification complete; evidence below is pinned
to the commits that produced it.

This report answers the four independent-review findings (P1 target identity,
P1 privilege enforcement, P2 deterministic contention, security-gate triage).
It does not redesign the lease protocol or reopen accepted decisions.

---

## 1. Findings, root causes and changes

### 1.1 P1 — the validator proved inequality, not a shared target

**Finding.** `assert_guarded_migration_configuration` compared identity tuples
(`backend, username, host, port, database`). It rejected an *identical* tuple
but never established that the two URLs reach the **same logical database**:
a migration URL with a different host or a different database name passed, and
so did SQLite or CockroachDB as a "different" migration target. A different
password for the same login was also treated as separation.

**Root cause.** The check answered "are these different connections?" while
ADR-018 requires "are these two logins on the same database?" — the inverse
question. Nothing verified the target at connect time.

**Change** (`app/db.py`, `app/config.py`):

* `_MigrationTarget` + `_parse_migration_target()` parse every URL into
  `backend, role, host, port, database` and fail closed (`45004`) on a blank,
  malformed, unparseable or partial URL. Passwords are not part of the model.
* `assert_guarded_migration_configuration()` now enforces, in order: both URLs
  PostgreSQL; distinct logins; identical database name; identical endpoint,
  or a *declared* equivalent endpoint. A PostgreSQL migration URL with a
  non-PostgreSQL runtime URL is refused as having no proven target.
* `STRIKENOVA_MIGRATION_TARGET_ALIASES` (new setting) is an explicit,
  validated `hostA=hostB` list. Malformed entries, self-aliases, wildcards and
  port mismatches are configuration errors, not silently ignored.
* `_verify_logical_target()` opens both URLs and compares what the **server**
  reports — `current_database()`, `pg_postmaster_start_time()` and the
  `pg_database` OID. It runs inside `_migration_engine()`, i.e. before
  `preflight`, `lease.acquire()` and any DDL.
* No error path embeds a URL or password: messages name the setting and the
  attribute only (`same PostgreSQL login`, `different PostgreSQL databases`,
  `different PostgreSQL endpoints`, `database name`, `port`).

**Supported topology.** One cluster, one database, two logins. A second route
to the same endpoint (Neon pooled `ep-x-pooler…` vs direct `ep-x…`) is accepted
only when explicitly declared in `STRIKENOVA_MIGRATION_TARGET_ALIASES`; the
declaration is then *proven* by the connection-time comparison. There is no
implicit suffix heuristic and no wildcard. Documented in runbook §2.3.

**Tests** (`tests/test_migration_identity_separation.py`, DB-free, 12 added):

| Case | Outcome |
| --- | --- |
| Same database, distinct runtime/migrator logins | accepted |
| Same login, different password | refused — "same PostgreSQL login" |
| Different database name | refused — "different PostgreSQL databases" |
| Different endpoint, no declaration | refused — "different PostgreSQL endpoints" |
| Declared alias for one endpoint | accepted |
| Declared alias with a different port | refused |
| Neon pooled vs direct route | refused without declaration; accepted with it |
| PostgreSQL runtime + SQLite migration URL | refused |
| PostgreSQL runtime + CockroachDB migration URL | refused |
| Missing / blank / malformed URL | refused |
| PostgreSQL migration URL + non-PostgreSQL runtime | refused |
| Malformed alias configuration | refused |

Every refusal assertion also checks that neither password nor the URL text
appears in the exception message.

### 1.2 P1 — the application privilege matrix was tested, not enforced

**Finding.** `verify_installation()` validated the private
`migration_internal` infrastructure. The approved §8.3 matrix for the
application objects (`positions`, `paper_orders`, `strategy_leg_exposures`,
the exposure sequence), the role attributes and the runtime boundary were only
asserted by tests, and Topology B's four grants were applied out of band after
the migration rather than by it.

**Root cause.** Two gaps: no read-only verifier existed for the application
objects, and the guarded DDL path had no privilege post-condition — a REVOKE
was assumed to be effective rather than read back.

**Change** (`app/_migration_lease.py`, `app/db.py`):

* `verify_application_privileges()` — read-only, fail-closed (`45004`). Reads
  `pg_roles`, `pg_auth_members`, `pg_class` ownership, `has_table_privilege`
  one privilege at a time, `has_sequence_privilege`, `has_schema_privilege`,
  `pg_default_acl` with `aclexplode`, and `pg_has_role` for transitive
  membership. It never grants, revokes or repairs.
* Enforcement points: after the Alembic chain and the topology step, **before**
  the pre-commit `enter` (so a bad matrix aborts the migration and nothing
  commits); and again at the start of `run_startup_backfill`, before the first
  write (so the backfill never starts on a wrong installation).
* `apply_migrator_backfill_grants()` — Topology B's four §8.3 grants are now
  installed by the guarded migration inside the verified owner window, before
  `RESET ROLE`, and re-applied on every upgrade. Topology A keeps its revokes
  (also re-applied every upgrade). Both topologies therefore converge on the
  same matrix by construction.

**Effective matrices asserted** (from catalogs, not from issued statements):

| Table | migrator SELECT | INSERT | UPDATE | DELETE | TRUNCATE |
| --- | --- | --- | --- | --- | --- |
| `positions` | true | false | false | false | false |
| `paper_orders` | true | false | false | false | false |
| `strategy_leg_exposures` | true | true | false | false | false |

Sequence: `USAGE = true`, `SELECT = false`, `UPDATE = false`.
Runtime: `SELECT/INSERT/UPDATE/DELETE` on all three, `TRUNCATE = false`,
sequence `USAGE = true`, and zero effective access (schema, tables, functions)
to `migration_internal`, no membership in migrator or owner.

**Negative controls** (integration suite, each on a disposable database):

| Invalid setup | Result |
| --- | --- |
| migrator `SUPERUSER` | `45004` "is a superuser" |
| `GRANT UPDATE ON positions` to the migrator | `45004` "migrator UPDATE on public.positions is True" |
| `GRANT SELECT` on the exposure sequence | `45004` "has SELECT on the exposure id sequence" |
| extra membership path (`GRANT migrator TO app`) | `45004` "runtime role … is a member" |
| `GRANT USAGE ON SCHEMA migration_internal` to runtime | `45004` "effectively has USAGE" |
| revoked runtime default privilege | `45004` "missing runtime table privileges ['DELETE']" |
| Topology B membership `INHERIT TRUE` | `45004` "INHERIT TRUE" |
| Topology B owner `LOGIN` | `45004` "Topology B requires NOLOGIN" |
| runtime membership in the owner | `45004` "is a member of …_owner" |
| superuser migrator before `upgrade head` | the **guarded upgrade itself** exits non-zero with `45004` and "application privilege verification failed" |

Positive controls: a valid Topology A and a valid Topology B installation both
pass the verifier.

### 1.3 P2 — the two-process test did not prove overlap

**Finding.** `_concurrent_startups` launched two `Popen` workers back to back
and only checked that both exited 0. Nothing observed that they overlapped
inside the protected section; a fast first process could finish before the
second started.

**Change** (`tests/test_issue145_application_integration.py`,
`test_two_process_startup_contends_on_lease_row_lock`):

1. Provision and bootstrap a disposable PG 16.16 database; run the guarded
   chain so the schema is at head; seed legacy data.
2. Take `ACCESS EXCLUSIVE` on `public.strategy_leg_exposures` from the test
   connection. Worker A (real `init_db()`) enters its protected DDL transaction
   (the §8.3 revokes) while already holding the lease row lock, and blocks.
3. Observe in `pg_stat_activity` that worker A is `active` with
   `wait_event_type = 'Lock'`, and that it holds a granted lock on
   `migration_internal.migration_lease`.
4. Start worker B (same real `init_db()` path) and observe, in the same
   observer connection, B `active` with `wait_event_type = 'Lock'` **and** at
   least one ungranted row in `pg_locks` for B's PID — while A is still alive.
5. Only then release the test lock. A finishes (revokes, verification, backfill),
   releases the lease, B proceeds; both exit 0.
6. Assert exactly-once exposure rows for both users, no SQLSTATE `23505`
   anywhere in either process's output, and that the lease is unowned at the end.

There is no test-only seam in the application path: the test only uses a
PostgreSQL table lock to make the overlap deterministic, and the failure mode
is a timeout, so a run where B never contends fails.

### 1.4 Security-quality gate

SonarCloud, PR #146 analysis on `1e4719c`: **7 open new-code issues**, all
`VULNERABILITY/MAJOR`, and all from this PR's own diff (annotations retrieved
from the check run). No source-level application vulnerability was reported.

| Rule | Location | Disposition |
| --- | --- | --- |
| `githubactions:S8541` ×2 | `issue145-application-pg16.yml:84-85` | **Fixed** — both `pip` invocations now pass `--only-binary :all:`; verified locally that the full requirement set resolves and installs with the flag (`pip install --only-binary :all: -r requirements.txt -r requirements-dev.txt` → exit 0), so no sdist build script can run in CI. |
| `githubactions:S8544` ×2 | same lines | **Partly fixed, one finding accepted** — the pip installer is pinned exactly (`pip==26.3`), every version in both requirement files is now an exact pin (the three ranged entries were pinned to the versions the suites verify against), and the install is `--only-binary`. One `S8544` still fires on the `-r requirements.txt -r requirements-dev.txt` command, because the GitHub-Actions sensor classifies any `-r` install as unlocked without reading the referenced files; pinning them changed nothing. `# NOSONAR` was tried and is **not** honoured by this sensor, so no suppression is in the tree. Closing it properly needs a hash-locked lockfile for every CI platform, a repo-wide dependency change outside this review. |
| `python:S8786` ×3 | `scripts/pytest_gate.py:38-40` | **Fixed** — the three counting regexes (`(\d+) passed`, `(\d+) (?:failed\|error\|errors)`, `(\d+) skipped`) were replaced by a linear token scan over the log. Semantics unchanged; the gate's negative-control tests still pass. |

No rule, file or gate is suppressed: no `NOSONAR`, no `sonar.issue.ignore`,
no exclusion. After the push the PR's new-code finding count went **7 → 1**
(the regex and binary-install findings are gone); the remaining `S8544` keeps
new-code Security Rating at **C**. That is reported as the honest outcome: the
workflow's direct dependencies are exact-pinned and install binary-only, but
transitive resolution is still performed by pip at install time, which is what
the rule is about, and no lockfile is in scope here.

**Security rating after remediation:** C (one `VULNERABILITY/MAJOR` finding,
`githubactions:S8544`, on the `-r` install line). No suppression was used to
obtain a higher rating.

**OpenCodeReview.** The run on the current head failed because the OpenRouter
free-model daily limit returned HTTP 429; it produced zero tokens and zero
comments, so it is **not** a review result. No credits were purchased and no
paid route was used. Retried only if a permitted free route becomes available;
until then it is **unverified**, not passed.

---

## 2. Files changed

| File | Change |
| --- | --- |
| `options-dashboard-project/backend/app/db.py` | target model/parsing, alias validation, connect-time target proof, Topology B grants, two enforcement call sites |
| `options-dashboard-project/backend/app/_migration_lease.py` | `verify_application_privileges` and its catalog helpers |
| `options-dashboard-project/backend/app/config.py` | `STRIKENOVA_MIGRATION_TARGET_ALIASES` |
| `options-dashboard-project/backend/scripts/pytest_gate.py` | linear log scanning (no backtracking regex), minimums 37/156 |
| `options-dashboard-project/backend/tests/test_migration_identity_separation.py` | 12 URL-contract tests |
| `options-dashboard-project/backend/tests/test_issue145_application_integration.py` | 13 tests: verifier positive/negative controls, guarded-upgrade refusal, deterministic contention |
| `.github/workflows/issue145-application-pg16.yml` | binary-only, pinned installs; gate minimums 37/156 |
| `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md` | §2.3 target proof, §8.1 enforcement, updated evidence and limitations |

No application behaviour other than the required fail-closed checks changed;
the lease protocol, bootstrap modes and SQLSTATE contract are untouched.

---

## 3. Verification (disposable PostgreSQL 16.16 only)

Environment: locally extracted PGDG 16.16 binaries
(`server_version_num = 160016`), throwaway cluster on `127.0.0.1:55432`,
`ISSUE145_PG_ADMIN_URL=postgresql://postgres@127.0.0.1:55432/postgres`.
No Neon, Render, production, staging, role, grant or credential was touched.

| Suite | Command | Result |
| --- | --- | --- |
| URL contract | `pytest -q tests/test_migration_identity_separation.py` | 19 passed (7 existing + 12 new) |
| Application integration | `pytest -q tests/test_issue145_application_integration.py` | **37 passed** |
| DB-free lease contract | `pytest -q tests/test_migration_serialization.py` | **55 passed** |
| Regression suite (8 modules) | the workflow's exact command | **156 passed** |
| PostgreSQL compatibility | the workflow's exact command (9 modules) | **122 passed**, 0 skipped |
| Static | `python -m compileall -q app scripts tests`; `ruff check` on changed files | compile clean; only pre-existing style findings remain in the touched files (import sorting, `BLE001` in pre-existing handlers) |

Counts are deliberate: 24 → 37 integration (13 new) and 144 → 156 regression
(12 new). No threshold was lowered, no test was skipped and no assertion was
weakened; the two exact minimums in `pytest_gate.py` and the workflow were
updated together with the tests.

The deterministic contention test was run repeatedly as part of the full
suite; it fails closed by timeout if the second worker is not observed
waiting.

---

## 4. Cleanup

Each test provisions and drops its own database and roles
(`sn145_<hex>_{owner,migrator,app}`). After the runs the local cluster held no
`sn145_%` database or role and the scratch server was stopped. CI's cleanup
step remains the last job step and drops any leftover `sn145_%` resources.

---

## 5. Remaining limitations / not verified

* No Neon, Render, production or staging verification of any kind; the URL
  proof was exercised against one local cluster plus declared aliases. A
  provider-side endpoint alias must still be declared explicitly.
* The alias mechanism trusts an operator declaration, then proves it at
  connect time. Two unrelated clusters that coincidentally share database name,
  postmaster start time *and* database OID are not defended against; that is
  the documented residual risk, not a silent assumption.
* Timing/throughput are not measured here; the deterministic test proves
  contention, not performance.
* SonarCloud new-code Security Rating remains **C** because of the single
  `S8544` `-r`-install finding described in §1.4. A hash-locked lockfile is the
  real remediation and is not part of this review.
* OpenCodeReview remains **unverified**: its last run failed with HTTP 429 on a
  free model and produced no review output. No paid route was used.
* The unrelated Pylint workflow still fails on its own `actions/setup-python@v3`
  setup; this PR does not repair it.
