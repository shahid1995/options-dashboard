# Independent security acceptance review — PR #146 migration identity enforcement

**Reviewed commit (exact):** `b0125f68ce7fc57aa0be902c868fa059c46d3842`
**Branch:** `issue-145-fix` · **PR:** [#146](https://github.com/shahid1995/options-dashboard/pull/146)
**Change under review:** `12716f19b9cc40dd8ce7a26b04c9710702567530` (the migration-identity pass) on top of `5883635`
**PR state observed:** `state: open`, `draft: true`, `merged: false`, base `feat/strikenova-day35-portfolio-intelligence`
**Verdict:** **PASS** — see §10 for scope.

This review is read-only with respect to `issue-145-fix`. It creates no commit on that branch, changes no PR state, and touches no other PR, provider or database. The report itself is the only file authored, on a separate review branch based on the reviewed SHA.

---

## 1. State verification before any review work

| Check | Command | Result |
| --- | --- | --- |
| Remote head | `git ls-remote origin refs/heads/issue-145-fix` | `b0125f68ce7fc57aa0be902c868fa059c46d3842` |
| Local head | `git rev-parse HEAD` | `b0125f68ce7fc57aa0be902c868fa059c46d3842` |
| Remote head re-check immediately before publishing | `git ls-remote origin refs/heads/issue-145-fix` | unchanged, `b0125f68…` |
| PR #146 | GitHub API | open / draft / unmerged, head `b0125f68…` |
| PR #149 | GitHub API | head `6968882f4bd1d3e97266bb912e397a5f555cb769`, untouched |
| Concurrent writer | `ps -eo pid,user,etime,args` filtered for `git`/`pytest`/`alembic`/`postgres`; file mtimes under `app/`, `tests/`, `docs/`, `.github/` | no matching process; no tracked file modified in the previous 6 h |

The stated head and the repository agree. No discrepancy to report.

Pre-existing untracked evidence artifacts (`.edits/`, `.local-pg/`, `.review-verify/`, `analysis/`, `artifacts/`, the loose `pr13x-*` reports, etc.) were left exactly as found. Review work ran in a detached worktree at `b0125f68` (`/tmp/pr146-review-wt`); the main worktree was not used for writing.

## 2. Documents and contracts read in full

`AGENTS.md`, `INVARIANTS.md` (§6c/6d/6e/6f), `DECISIONS.md` (ADR-002, ADR-016, ADR-017 with its 2026-11-21 supersession note, ADR-018, ADR-019), the complete Issue #145 body (including the 2026-11-21 founder authorization that supersedes the earlier design preferences), `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md` (§2, §2.1–§2.4, §3, §4, §5.4–§5.5, §6, §7, §8.1, §9, §10), and the deliverable under review `REVIEW_ACCEPTANCE_MIGRATION_IDENTITY_2026-11-22.md`.

Implementation read end to end: `app/db.py` (1398 lines), `app/_migration_lease.py` (2122 lines), `app/migrate.py`, `alembic/env.py`, `app/main.py` lifespan, `app/services/leg_exposure.py` call surface, and the three touched test files.

## 3. Entry-point trace (every PostgreSQL migration path)

| Entry point | Path to DDL | Guarded? |
| --- | --- | --- |
| Application startup | `main.py:299` lifespan → `db.init_db()` (`db.py:1277`) → `_run_alembic_migrations` (`db.py:1145`) → `_run_guarded_migrations` (`db.py:1065`) → `_migration_engine()` (`db.py:878`) | yes |
| `python -m app.migrate upgrade head` | `migrate.py:cmd_upgrade` → `_run_alembic_migrations` | yes |
| `bootstrap-lease` / `--close-barrier` / `--upgrade-protocol` | `migrate.py:_bootstrap_engine` → `_migration_engine()` → `bootstrap_lease` → `_assert_migrator_identity` (`_migration_lease.py:1845`) | yes |
| `status` (read-only) | `_migration_engine()` + `read_protocol_info` | yes (engine), no writes |
| Preflight / lease acquisition / DDL transaction / per-user backfill | all use the single engine returned by `_migration_engine()`; backfill transactions are `work.engine.connect()` (`db.py:1230`) | yes (connect listener fires per DBAPI connection) |
| Bare `alembic upgrade head` | `alembic/env.py:run_migrations_online` → `_run_on_connection` (`env.py:80`) | refused for PostgreSQL when `lease_guard is None` (`env.py:89-96`) |
| CockroachDB | `_run_alembic_migrations` raises `45004` (`db.py:1171`) | refused outright |
| SQLite | `_run_alembic_migrations` uses `connectable = engine` (runtime), no lease (`db.py:1163`) | intentionally unguarded (single-process, single-identity) |

## 4. Independently reproduced verification

Environment built from the repository's own pins, not from the previous session's leftovers: CPython 3.12.3 virtualenv with `pip install --require-hashes -r backend/requirements.lock` (exit 0), and the disposable cluster in `.local-pg` reporting `server_version_num = 160016` (`PostgreSQL 16.16 (Ubuntu 16.16-1.pgdg24.04+1)`) on `127.0.0.1:55432`.

| # | Command (cwd `options-dashboard-project/backend`) | Result |
| --- | --- | --- |
| 1 | `pytest -q tests/test_issue145_application_integration.py` with `ISSUE145_PG_ADMIN_URL` | **71 passed**, 0 skipped, 0 failed (64.47 s) |
| 2 | `pytest -q` over the eight regression modules named in the workflow | **160 passed**, 0 skipped, 0 failed (15.19 s) |
| 3 | `pytest -q` over 19 PostgreSQL-safety modules (compatibility, sqlite→PG import ×2, rehearsal ×2, hardening, failure injection, day4/day5/day6, day38/day39/day41 concurrency, cockroachdb, production guard, alembic env, app integration, serialization, identity separation) with `TEST_DATABASE_URL` | **321 passed, 9 failed, 2 skipped** (29.46 s) |
| 4 | `pytest -q tests/test_gex_reliability.py` | 27 failed — all `ModuleNotFoundError: No module named 'upstox_client'` |
| 5 | `pytest -q tests/test_live_verification.py` | 1 failed (`TestVerifyDbRoundtrip::test_candle_roundtrip`), 36 passed |

Suites 1 and 2 are the workflow's gated suites, and they reproduce the declared minimums **exactly** — `71` and `160` are true counts at this SHA, not inflated gates. `scripts/pytest_gate.py` therefore passes at these minimums, and its own negative controls are exercised by `test_migration_serialization.py`.

**Suite 3 is deliberately broader than the previous session's subset and is not clean.** All nine failures are the same pre-existing class:

```
E  RuntimeError: refusing to run Alembic against PostgreSQL without the
   ADR-017 migration lease: use 'python -m app.migrate upgrade head'
```

They live in `tests/test_postgres_app_integration.py`, exercise their own unleased Alembic bootstrap, and are unrelated to the identity guard: the refusal is raised by `alembic/env.py:89-96`, whose unleased-PostgreSQL refusal predates the reviewed commit. The two skips are environment-gated (`test_rehearsal_synthetic.py:606` "PostgreSQL not available for rehearsal", `test_day4_postgres_foundation.py:235` "Not running against PostgreSQL"). Anyone re-running a "PostgreSQL-safety subset" should include this module and expect these nine failures; a clean subset is obtained only by excluding it.

Nothing in this review changed application code, tests, workflows, migrations or dependency files. `git status` inside the review worktree is clean apart from this report.

## 5. Does anything bypass `_migration_engine()` and its guard?

**No in-tree path does.** The reasoning, by construction rather than by comment:

1. Every PostgreSQL DDL, lease, bootstrap and backfill entry point funnels through `_migration_engine()` (`db.py:878`), which is the only place in `app/` that builds a PostgreSQL engine from the migration URL. `create_engine` appears elsewhere only for (a) the runtime engine (`db.py:44-62`), (b) the read-only logical-target comparison engine (`db.py:723`), and (c) data-only CLI tools.
2. The guard is installed **before the engine's first connection** (`db.py:891`), and the first connection is opened by `_verify_logical_target` (`db.py:693`) — so even the target proof runs through the identity guard. A wrong identity therefore fails before `MigrationLease(...)`, before `preflight()`, before `lease.acquire()`, and before any statement of the migration path.
3. The listener is registered on the engine, not on one sampled connection, and the engine uses a real pool (`pool_size=2, max_overflow=2`, `db.py:881-887`), so every pooled reconnect re-runs the guard.
4. `alembic/env.py` *can* build its own `engine_from_config` (`env.py:128-146`) with no identity guard, but only when the caller supplies neither `connection` nor `connectable`; `_run_on_connection` then refuses PostgreSQL outright (`env.py:89-96`). The only in-tree producer of `lease_guard` is `_run_guarded_migrations` (`db.py:1096`), which always sets `connection` to the same guarded connection. The CLI cannot set `config.attributes`.

The one residual, non-exploitable weakness is recorded as L1 in §8.

## 6. Identity semantics actually enforced

* **URL binding** (`db.py:527-553`): migration login must equal `STRIKENOVA_MIGRATOR_ROLE`; a blank role name is refused; the runtime login must equal `STRIKENOVA_RUNTIME_ROLE` when configured. Checked before any engine exists (`db.py:630`).
* **Live session** (`db.py:767-876`): `session_user`, `current_user`, `rolcanlogin` and the five forbidden attributes are read from `pg_roles`; `session_user` is the decisive value and cannot be changed by `SET ROLE` or inheritance. Messages name roles, never URLs.
* **Re-proof at the two decisive points** (`db.py:1045-1063`): after `RESET ROLE` inside the DDL transaction, and before the startup backfill's first write.
* **Topology B window**: `SET ROLE` is verified (`db.py:903`), `RESET ROLE` is verified to restore the migrator (`db.py:919`), the window closes before `_verify_application_privileges`, and the transaction rolls the elevation back on any failure — `SET ROLE` is transactional in PostgreSQL, so a rolled-back window cannot leak an elevated session into the pool.
* **Incoming membership** (`_migration_lease.py:1198-1215`, enforced at `1342-1360`): no role may be a member of the migrator (both topologies); in Topology B the migrator must be the owner's only member. Checked against `pg_auth_members`, which is direct membership — a transitive chain is caught one hop earlier because the intermediate role is itself a direct incoming member.
* **Lease fencing** (independent of the reviewed commit, verified for completeness): `lease_enter_v1` takes a transaction-scoped advisory lock and `SELECT … FOR UPDATE`, then verifies token hash, protocol and expiry; `run_startup_backfill` calls `enter` as the **first** statement of each user transaction and again as the **last statement before `COMMIT`** (`db.py:1240, 1259`). A stale holder therefore cannot commit after takeover, which is the CodeRabbit finding of 2026-11-18 (`db.py:476`) — that finding predates the current implementation and is answered by the pre-commit `enter`.

## 7. Do the tests exercise the real failure boundary?

Yes, and the state snapshot is meaningful rather than decorative:

* `_assert_startup_refused` (`tests/test_issue145_application_integration.py:2186`) runs the **real** subprocess `python -m app.migrate upgrade head` with the wrong migration URL, requires a non-zero exit, requires `45004` in the output, requires the expected message fragment, and then requires the snapshot to be **identical**.
* The snapshot (`:2166`) contains the lease row (owner, **generation**, protocol, expiry), `alembic_version.version_num`, `positions` count and the per-user exposure ledger, read over the disposable admin connection so that the observation does not depend on the privileges under test. `generation` is the discriminating field: an acquire-then-release would increment it, so "generation unchanged" genuinely proves *no lease was taken*, not merely that state was restored.
* The eight controls cover exactly the bypasses the commit claims to close: superuser admin login, unrelated login, login holding migrator membership (including **after** `SET ROLE`, asserting `session_user` is named in the refusal), superuser migrator role, `NOLOGIN` migrator, and the two incoming-membership directions, plus the positive control.
* `test_bad_privilege_matrix_aborts_the_guarded_upgrade` was retargeted from the §8.3 verifier message to the earlier connection-guard message. This is an **earlier** refusal, not a weaker assertion, and the test still requires non-zero exit plus `45004`.

Coverage gaps are stated in §9.

## 8. Findings

No **Critical** or **High** issue was found. Nothing here should block technical acceptance on identity grounds.

### L1 — `alembic/env.py` can construct an unguarded engine if a caller sets `lease_guard` without `connection` (Low, defense-in-depth)

`alembic/env.py:121-148`: `run_migrations_online` treats a non-`None` `lease_guard` as permission to run PostgreSQL DDL, and falls through to `engine_from_config` when `connection` and `connectable` are both absent. Such an engine would carry no `connect`-time identity guard. No in-tree caller can do this (`db.py:1096` sets both attributes, and the CLI cannot set `config.attributes`), so it is not reachable today. Hardening suggestion: in the PostgreSQL branch, require `connection` specifically and refuse `lease_guard` without it.

### L2 — incoming membership is not checked for the runtime role (Low)

`_migration_lease.py:1342-1360` closes incoming membership for the migrator and the owner only. A third login granted membership in `STRIKENOVA_RUNTIME_ROLE` could `SET ROLE` into the serving identity. Impact is bounded by design — the runtime role is DML-only, owns nothing and has no `migration_internal` access — so this is an authorization-hygiene gap, not a privilege escalation into DDL. Extending `_incoming_members` to the runtime role with an empty-allowlist rule would close it symmetrically.

### L3 — role-name defaults make the guard a rollout change for non-production deployments (Low, documentation)

`app/config.py:150-151` defaults `STRIKENOVA_MIGRATOR_ROLE` and `STRIKENOVA_RUNTIME_ROLE` to the production names. Because the runtime binding applies whenever the setting is non-empty (which the default makes always true), any existing staging or preview PostgreSQL deployment whose logins are named differently will now fail closed at startup with `45004`. That is the intended fail-closed direction, but runbook §5.4 (deployment ordering) does not say so. Worth one sentence in §5.4 and in the release note: set both variables to the deployment's actual role names before rolling.

### L4 — pre-existing: non-Alembic DDL in data CLI tools (Low, out of scope)

`app/tools/live_verification.py:550`, `candle_backfill.py:134`, `option_candle_backfill.py:97`, `contract_metadata_backfill.py:70` call `Base.metadata.create_all()`. This diverges from INVARIANT 7 / ADR-002, is acknowledged in the `db.py:1269` comment, predates this PR and is not on its migration path. It deserves its own issue. It is not a bypass of the new guard: those tools use `DATABASE_URL` (the DML-only runtime identity) and would be refused `42501` for `CREATE` on a correctly provisioned database.

### L5 — pre-existing: stale unleased-Alembic tests (Low, follow-up)

The nine failures reproduced in §4 are tests that still bootstrap schema through unleased Alembic on PostgreSQL and now hit the `env.py` refusal. They should be migrated to `python -m app.migrate upgrade head` or scoped to SQLite. Not caused by, and not fixable within, the reviewed scope.

### L6 — broader suite still has known pre-existing failures (informational)

`ModuleNotFoundError: upstox_client` (27 in `test_gex_reliability.py` today; the package is absent from every requirements file and has no resolvable distribution), a schema-drift failure in `test_live_verification.py`, and date-brittle expiry tests remain. None mention `STRIKENOVA_MIGRATOR_ROLE`, `session_user`, `current_user`, `45004` or migration identity.

## 9. Reproducibility assessment of the broader PostgreSQL 16.16 evidence

I distinguish three things that were previously discussed under one heading:

1. **The gated suites (the PR's own evidence).** Fully reproducible and reproduced here exactly: 71 and 160, zero skips, from a venv built off `requirements.lock` and a freshly started 16.16 cluster. The published evidence document `REVIEW_ACCEPTANCE_MIGRATION_IDENTITY_2026-11-22.md` correctly scopes its §5 to these suites plus a 3.13 compatibility subset. It does **not** claim a full backend-suite pass, and the PR body does not either. No overclaim was found.
2. **The PostgreSQL-safety subset.** Reproducible, but the result depends entirely on which modules are in the list. My 19-module list yields 321 passed / 9 failed / 2 skipped, with all failures being the pre-existing unleased-Alembic refusal. A "454 passed, 0 skipped, 0 failed" figure is therefore only true of a subset that excludes `test_postgres_app_integration.py`. That is a scoping choice, not a false statement, but the label "PostgreSQL-safety subset" should carry its module list.
3. **The broad full-suite run (~6603 passed / 58 failed / 10 errors / 43 skipped, with `test_upstox_market_feed.py` excluded).** **Not reproducible from the repository.** Its logs were written to `/tmp` in the previous session and no longer exist (`/tmp/full_pg_suite.log`, `/tmp/head_failures.txt`, `/tmp/base_failures.txt`, `/tmp/baseline_modules.log`, `/tmp/pg_safety_rs.log` are all absent), and the run exceeds a single command budget here. The *method* is sound in principle — run the failing modules at the pre-guard base `5883635` in a detached worktree and diff the sorted per-test identifiers in both directions (`comm -13` / `comm -23`), so an empty diff proves neither side added a failure — but it is not re-executable from anything committed, and it excludes one module and leaves 43 skips unclassified. It should not be cited as a complete backend-suite pass, and the PR does not cite it as one.

What I can state with first-hand evidence is narrower and sufficient for the question the guard raises: **every module the commit touches, and every gated module, passes; the failures I can reproduce are all in classes whose error text predates the guard and never mentions identity.**

## 10. Verdict and readiness

**PASS** — for the change under review, the migration-identity enforcement at `b0125f6`.

* No Critical or High issue was found, and no connection or engine path reachable from the application, the guarded CLI or a bare Alembic invocation can execute PostgreSQL DDL as a principal other than the configured migrator.
* The refusal provably precedes lease acquisition, DDL and backfill, and the tests prove it with a generation-bearing state snapshot rather than a nominal assertion.
* The gated evidence (`71` / `160`) is independently reproduced from a clean environment at this exact SHA, with zero skips, and the gates are true minimums.
* Three Low findings (L1–L3) are hardening/documentation items; L4–L6 are pre-existing and belong to follow-up issues, not to this branch.

**Scope statement.** This review audited the identity-enforcement commit and the migration/lease/backfill orchestration it sits in. It did not re-derive the full 39-file ADR-017/018 implementation, the notification/background-job surfaces, or the frontend. Findings L1–L3 and the follow-ups in L5–L6 should be tracked, but none of them is a reason to withhold technical acceptance.

**Is PR #146 ready for the founder's acceptance decision?** Yes — on technical grounds, at `b0125f68ce7fc57aa0be902c868fa059c46d3842`, with the Low findings and the pre-existing failures recorded as follow-up work. The PR remains open, Draft and unmerged; this review neither merges it nor marks it Ready. The final acceptance, merge and any deployment decision remain the founder's, and Neon/Render/production remain unauthorized.

**Independent-review signals.** OpenCodeReview did not review this head (its own runner failed), SonarCloud's latest PR-146 analysis is still `eeeebffe…` from 2026-11-20, and CodeRabbit is disabled for the base branch. None of those signals is treated as a pass in this document, and this review is not a substitute for the founder's decision.
