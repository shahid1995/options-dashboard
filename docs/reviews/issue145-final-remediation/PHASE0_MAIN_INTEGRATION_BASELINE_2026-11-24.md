# Phase 0 — Current-main integration baseline and PR #146 port inventory

**Date:** 2026-11-24  
**Document status:** Phase 0 review artifact only — no implementation performed  
**Planning authority:** [`PR146_MAIN_INTEGRATION_PLAN_2026-11-24.md`](PR146_MAIN_INTEGRATION_PLAN_2026-11-24.md) at `ba0520cf9475cafc9a62fcdb1979d3259726daa3`  
**Current `main` baseline:** `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`  
**PR #146 implementation head:** `b0125f68ce7fc57aa0be902c868fa059c46d3842`  
**PR #149 design head:** `6968882f4bd1d3e97266bb912e397a5f555cb769`

This report establishes the verified starting state for porting the Issue #145 / ADR-017/018 implementation from PR #146 into the current `main` architecture. It is not an implementation authorization, PR acceptance, merge request, deployment plan, or permission to contact a live database, provider or environment.

---

## 1. Status and recommendation

**Phase 0 outcome: COMPLETE.** The baseline is reproducible and the port inventory is complete for the 39-file PR #146 delta.

**Integration readiness: not yet cleared for implementation.** Three facts change how Phase 1 must be sequenced and are not fully reflected in the integration plan:

1. **`main` has no PostgreSQL support at all** — no driver dependency, no PostgreSQL test module, no PostgreSQL workflow. The PostgreSQL baseline cannot be produced from `main` without changing dependency files, which Phase 0 forbids. The exact blocker is recorded in §6.3.
2. **PR #146's schema assertions belong to its own lineage.** Its `tests/test_db_migration.py` asserts **55** tables and names migrations (`c1d2e3f4a5b6`, `e9f8a7b6c5d4`, `d47aa0000001`, `d48aa0000001`) that do not exist on `main`, whose Alembic head is `b2c3d4e5f6a7` with **27** tables. The Issue #145 code itself changes no schema, so this is an assertion-level adaptation, not a migration conflict — but it must be adapted deliberately rather than copied.
3. **`main`'s own backend CI cannot pass as written**, and the full backend suite at `main` is already failing at a large, pre-existing rate (§6.2). Any new gate must be defined against this real baseline, not against the old branch's numbers.

**Recommended immediate next action:** the founder chooses the PR #149 governance route (§9), then Phase 1 ports the two-file design record, then Phase 2 begins with configuration/dependencies. No implementation should start before that decision, because the dependency change (adding a PostgreSQL driver and hashed locks) is exactly where the two routes differ in review surface.

---

## 2. Verified repository state

All refs were read from the remote (`git ls-remote`) and locally re-verified with `git rev-parse`; Pull Request metadata was read from the GitHub API on 2026-11-24.

| Item | Verified state | Match |
|---|---|---|
| `main` | `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b` | ✅ expected |
| PR #146 head (`refs/pull/146/head`, `refs/heads/issue-145-fix`) | `b0125f68ce7fc57aa0be902c868fa059c46d3842` | ✅ expected |
| PR #146 original base | `feat/strikenova-day35-portfolio-intelligence` @ `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` | ✅ expected |
| PR #149 head | `6968882f4bd1d3e97266bb912e397a5f555cb769` | ✅ expected |
| Integration-plan commit | `ba0520cf9475cafc9a62fcdb1979d3259726daa3` on `review/pr146-main-integration-plan-2026-11-24` | ✅ expected |
| PR #146 state | `open`, `draft: true`, `merged: false`, 28 commits, 39 files (+11,977 / −2,735) | unchanged |
| PR #149 state | `open`, `draft: false`, `merged: false`, 8 commits, 2 files (+1,870 / −0) | unchanged |
| PR #149 base | `main` @ `eeeebffe` | unchanged |

No important ref had moved. One infrastructure caveat was found and handled: this clone had **no `remote.origin.fetch` refspec**, so `origin/main` and friends were stale/misleading until the pinned refs were fetched explicitly (`refs/heads/…` and `refs/pull/146/head`). Anyone reproducing Phase 0 must fetch by explicit refspec, not assume `git fetch origin` updates the tracking refs.

### 2.1 Divergence between `main` and the PR #146 lineage

| Comparison | Result |
|---|---|
| Merge base of `main` and PR #146 base | `ffefa6c5fd071ece47dc1a527a2bf85cb8d1379f` |
| `main` … `9dc6e15c` | 104 commits only on `main`, 685 only on the PR lineage |
| `main` … `b0125f68` | 104 commits only on `main`, 713 only on the PR lineage |
| Files changed on `main` since the merge base | 114 |
| Overlap of those 114 with PR #146's 39 files | **only** `ARCHITECTURE.md`, `DATA.md`, `DECISIONS.md`, `INVARIANTS.md` |

The 104 `main`-only commits do **not** touch `app/db.py`, `app/config.py`, `app/main.py`, `alembic/`, or `app/services/leg_exposure.py`. That is the single most useful fact for the port: the migration/startup implementation surface on `main` is the pre-ADR-017 shape from before the split, so the PR #146 implementation files apply to a *functionally identical* baseline, while the **governance documents and the schema lineage have diverged**.

Notable `main`-only changes that must be preserved: ADR-019 and its standalone document (`fd63f91`, `c2e3c39`, merged in #148), the anonymous-registration security fix `b4eda99`, the OpenCodeReview auto-updater change, and a set of new root workflows (apisec-scan, codacy, codeql, dependency-review, npm-publish, pylint, python-publish).

---

## 3. Governance documents inspected (at `eeeebffe`)

| Document | Lines | What it establishes for this port |
|---|---|---|
| `AI.md` | 168 | Mandatory reading order, authority hierarchy, anti-drift rules, change-control protocol. §5 still states the production topology as Vercel → Render → **CockroachDB** — stale against ADR-019. |
| `AGENTS.md` | 125 | Scope control, architecture discipline, "baseline before changing", honest failure classification, no deployment without authorization. |
| `PROJECT-CONTROL.md` | 204 | GitHub as implementation authority; issue-as-contract; Draft-until-accepted; definition of done. |
| `CONTEXT.md` | 183 | §3/§6 still record CockroachDB as production persistence and SQLite as local fallback. |
| `INVARIANTS.md` | 447 | INV-020/021/022/023 (Alembic authority, migration compatibility, transaction boundaries, concurrency as correctness); INV-031/032/033 (no silent scope expansion, no weakening to pass tests, fresh evidence). INV-021 still names CockroachDB. |
| `DECISIONS.md` | 679 | Registry ADR-001…ADR-011 only. **No ADR-017/018/019 registry rows** — `main` carries ADR-019 as a standalone document without its registry entry. |
| `ARCHITECTURE.md` | 870 | §4.1 lists SQLAlchemy 2.0.43 / Alembic 1.15.2 / Python 3.13 and a "PostgreSQL / SQLite" layer diagram, while §7.1 and §13 still say CockroachDB — internally inconsistent on `main`. |
| `DATA.md` | 851 | §19 migration rules, §25 verification checklist, §26 storage topology (CockroachDB). |
| `SECURITY.md` | 746 | §17 background-task security, §18 database security (Alembic authority, integrity, sensitive columns), §20 configuration baseline. |
| `TESTING.md` | 455 | Python 3.13, pytest, layered verification, failure classification, "a skipped suite is not a pass", database-test rules. Still says "CockroachDB production integration expectations". |
| `docs/ADR-019-NEON-CLEAN-SLATE.md` | 47 | Accepted: Neon PostgreSQL is the strategic target, CockroachDB retired, no data migration, provisioning/cutover separately authorized. |

**Conclusion for Phase 2:** the current `main` governance documents are authoritative and materially *newer and longer* than the PR #146 copies (see §5). They must be amended surgically with the Issue #145 statements, never replaced.

---

## 4. Current-main implementation inventory

### 4.1 Database and migration surface

| Item | Current `main` state |
|---|---|
| `options-dashboard-project/backend/app/db.py` | 275 lines. `_engine()` (33), `get_database_path()` (71), `get_db()` (78), `_run_alembic_migrations()` (87), `init_db()` (139), `check_database_health()` (197). |
| Engine construction | Single `settings.DATABASE_URL`, else `sqlite:///backend/paper_journal.db`. SQLite gets WAL + `synchronous=NORMAL`; any non-SQLite URL goes to `create_engine(url, pool_size=5, max_overflow=10, pool_timeout=30, pool_recycle=1800, pool_pre_ping=True)`. No URL normalization, no identity check, no dialect branching. |
| Module-level engine | `engine = _engine()` and `SessionLocal = sessionmaker(...)` are created at import time (line 67–68). Any startup-time identity assertion must therefore be installed *before* first use, or must not depend on the module-level engine. |
| `_run_alembic_migrations()` | Builds `Config("alembic.ini")` with a **relative** path, sets `sqlalchemy.url` from `engine.url`, passes `engine` via `config.attributes["connectable"]`, then `command.upgrade(cfg, "head")`. |
| `init_db()` | Three steps: Alembic upgrade → `backfill_all_exposures(session)` → SQLite-only composite `CREATE INDEX IF NOT EXISTS` statements. Called once from the FastAPI lifespan. |
| Duplicate WAL listener | `_engine()` registers the SQLite WAL listener twice (lines 43–46 and 58–62). Harmless but pre-existing dead code in the function that Phase 2 will rewrite. |
| `alembic/env.py` | 143 lines. `_resolve_database_url()` prefers `config.get_main_option("sqlalchemy.url")`, then `DATABASE_URL`, then the SQLite default. `run_migrations_online()` reuses `config.attributes["connectable"]` when present, else builds an engine with `engine_from_config` + `NullPool`; always `render_as_batch=True`. **No lease admission, no PostgreSQL refusal, no connection-level identity check.** |
| Alembic revisions | 7 files: `d3eb45a2e046` (baseline) → `125e1807df8d` → `a0deb75ad22f` → `f7a3c2d1e94b` → merge `b8c9f1d2e34a` → `a1b2c3d4e5f6` → `b2c3d4e5f6a7`. `alembic heads` = **`b2c3d4e5f6a7` (single head)**; `alembic upgrade head` on a clean SQLite file yields **27 tables** (26 application tables + `alembic_version`). |
| `app/migrate.py` | **Does not exist** on `main`. |
| `app/_migration_lease.py` | **Does not exist** on `main`. |
| `app/_migration_lock.py` | **Does not exist** on `main` (it exists only in the PR lineage, where PR #146 deletes it). |
| Backfill service | `app/services/leg_exposure.py` (388 lines). `backfill_exposures()` commits internally; `backfill_all_exposures()` loops over distinct `Position.user_id` and commits once per user. Called from `init_db()` on a fresh `sessionmaker(bind=engine)()` session. No lease, no per-user transaction wrapper, no pre-commit refresh. |
| `app/config.py` | 95 lines, single `DATABASE_URL: str | None`, historical feature flags, Pydantic v1-style `class Config`. **No** migration identity, role, lease, environment or timeout settings. |
| `app/main.py` | 424 lines; `lifespan()` (304) calls `init_db()` then a token-health check. Background GEX capture loop exists but is disabled by default. |
| Models | `app/models.py` 936 lines, `app/identity.py` 811 lines. |
| Test fixtures | `tests/conftest.py` 51 lines: sets dummy env vars and **replaces `app.db.engine` / `SessionLocal` with an in-memory `StaticPool` SQLite engine at import time**. There is no hermetic per-test database for `init_db()`. |

### 4.2 Dependencies, lockfiles and Python compatibility

| Item | Current `main` state |
|---|---|
| `requirements.txt` | 9 exact pins + **two floating ranges**: `cryptography>=44.0.0`, `PyJWT>=2.8.0`. `sqlalchemy==2.0.43`, `alembic==1.15.2`, `fastapi==0.141.1`, `uvicorn[standard]==0.52.2`, `pydantic-settings==2.15.0`, `httpx==0.28.1`, `python-dotenv==1.2.2`. |
| `requirements-dev.txt` | `-r requirements.txt` + `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `pytest-cov==7.1.0`, `respx==0.23.1`. |
| Lockfiles | **None.** No `requirements.lock`, no `requirements-py313.lock`, no hash pinning anywhere. A clean install resolves the two ranges and any transitive drift. |
| PostgreSQL driver | **None declared.** No `psycopg`, `psycopg2`, `psycopg[binary]`, `asyncpg` or `pg8000` anywhere in the repository outside `docs/`. |
| Python | Declared runtime **3.13** (`ARCHITECTURE.md` §4.1, `TESTING.md` §2, CI `python-version: "3.13"`). Verified locally with CPython **3.13.15**. |

### 4.3 Existing PostgreSQL / SQLite test and CI coverage

| Item | Current `main` state |
|---|---|
| Backend test modules | 105 files under `tests/`. |
| Migration/backfill modules | `test_alembic_migrations.py`, `test_db_migration.py`, `test_broker_connection_migration.py`, `test_leg_exposure.py`, `test_candle_backfill.py`, `test_contract_metadata_backfill.py`, `test_option_candle_backfill.py`, `test_phase724_5_backfill_orchestrator.py`, `test_phase724_8a/8b_backfill_*`, `test_timestamp_serialization.py`. |
| PostgreSQL test modules | **None.** Only two modules even mention "postgres", both incidentally (`test_broker_connection_model.py`, `test_byob_credentials.py`). |
| Backend CI | `options-dashboard-project/.github/workflows/ci.yml` — installs `requirements.txt` only, runs an import smoke test, a GEX module set (which includes `test_upstox_market_feed.py` and `test_gex_reliability.py`) and three candle modules. No PostgreSQL job, no full-suite job, no migration job, no hash-verified install. |
| Root CI | `apisec-scan.yml`, `codacy.yml`, `codeql.yml`, `dependency-review.yml`, `npm-publish-github-packages.yml`, `open-code-review.yml`, `pylint.yml`, `python-publish.yml` — none runs the backend suite. |
| PostgreSQL workflows from PR #146 | `.github/workflows/postgres-compatibility.yml` and `.github/workflows/issue145-application-pg16.yml` **do not exist on `main`**. `postgres-migration-rehearsal.yml` existed at the PR base and was deleted by PR #146; it does not exist on `main` either. |

The practical consequence: **`main` currently has no executable PostgreSQL path and no PostgreSQL evidence**, so Phase 2 introduces the driver, the harness and the workflows together — there is no pre-existing `main` PostgreSQL gate to extend.

---

## 5. File-by-file port matrix (39 files)

Classification key: **R** = required for Issue #145 · **A** = required but needs adaptation · **S** = superseded by current `main` · **H** = historical evidence only · **X** = unrelated, must not be ported.

Legend for the `main` column: line count at `eeeebffe`; "—" means the path does not exist on `main`.

### 5.1 Governance and design record

| # | File | main | PR #146 | Class | Difference and minimal port |
|---|---|---|---|---|---|
| 1 | `DECISIONS.md` | 679 | 480 | **A** | PR #146's copy is an older, shorter registry that predates ADR-019 and carries ADR-003/ADR-004 supersede notes. `main` has ADR-001…011 plus ADR-019 as a standalone file with **no registry row**. Port only: ADR-017 and ADR-018 registry rows, the ADR-019 registry row (currently missing), and the 2026-11-21 founder approval note — i.e. exactly PR #149's 5-line delta. Do not take PR #146's file. |
| 2 | `INVARIANTS.md` | 447 | 153 | **S/A** | PR #146's file is a whole-file rewrite of an older, much shorter invariant set. Porting it would delete ~300 lines of current invariants. Port only targeted additions if any survive review; the lease/identity properties belong to ADR-017/018, not to a rewritten INVARIANTS.md. |
| 3 | `ARCHITECTURE.md` | 870 | 200 | **S/A** | Same shape difference. Port only the database-architecture statements needed by ADR-017/018 and ADR-019 (PostgreSQL target, guarded migration path), preserving §1–§19. |
| 4 | `DATA.md` | 851 | 161 | **S/A** | Same. Port only migration-ownership/identity statements (§19/§25/§26 area). |
| 5 | `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` | — | not in #146 | **R (via #149)** | The design record lives only in PR #149 at `6968882f`. It is not part of PR #146's 39 files. |
| 6 | `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md` | — | 546 | **A** | New operational runbook. Must be reconciled: provider-neutral PostgreSQL 16.16, ADR-019 wording (it already says Neon, CockroachDB retired), and §5.4 must document the role-name defaults for finding L3. Its measured TTL/statement-timeout rationale (§7) must be re-derived on the new branch. |
| 7–12 | `docs/reviews/issue145-final-remediation/*.md` (6 files: `ADR_CONFORMANCE`, `REMEDIATION`, `REVIEW_ACCEPTANCE`, `REVIEW_ACCEPTANCE_MIGRATION_IDENTITY`, `REVIEW_ACCEPTANCE_PRIVILEGE_MATRIX`, `REVIEW_REMEDIATION`) | — | 268/244/174/151/159/290 | **H** | Historical evidence tied to `b0125f68` and its CI run IDs. Useful as provenance; they must not be presented as evidence for the new branch, and the two superseded acceptance reports should not be resurrected as current claims. |

### 5.2 Backend implementation

| # | File | main | PR #146 | Class | Difference and minimal port |
|---|---|---|---|---|---|
| 13 | `app/db.py` | 275 | 1398 | **A** | Adds: URL normalization to psycopg 3; production config validation; `validate_migration_state`; migration-URL resolution; endpoint/alias parsing and same-endpoint refusal; configured-identity assertion; cluster/logical-target signature; per-connection `session_user`/`current_user`/role-attribute guard; Topology B owner window (`_set_owner_role`/`_reset_owner_role`); §8.3 privilege verification and migrator restrictions/backfill grants; `_run_guarded_migrations`; `MigrationWork`/`run_startup_backfill`; rewritten `init_db`. **Port in dependency order and keep `main`'s SQLite path byte-for-byte in behaviour** (WAL, absolute path, SQLite-only composite indexes, `check_database_health`). Drop the duplicated WAL listener while touching the function. `_run_alembic_migrations` must keep calling `command.upgrade(alembic_cfg, "head")` (a DB-free test asserts this string). |
| 14 | `app/config.py` | 95 | 246 | **A** | The PR copy is a **superset from its own lineage**: it also contains `EMAIL_VERIFICATION_TTL_MINUTES`, `PASSWORD_RESET_TTL_MINUTES`, `RECENT_AUTH_TTL_MINUTES`, `EMAIL_*`, `EMAIL_PROVIDER`, `BREVO_API_KEY`, `BREVO_API_URL` (base `9dc6e15`, absent on `main`). Port **only** the migration block: `STRIKENOVA_ENV`, `STRIKENOVA_MIGRATION_DATABASE_URL`, `STRIKENOVA_MIGRATION_TARGET_ALIASES`, the four lease/timeout settings with their validators, `STRIKENOVA_MIGRATOR_ROLE`, `STRIKENOVA_RUNTIME_ROLE`, `STRIKENOVA_OWNER_ROLE`, `STRIKENOVA_LEASE_NONCE`, `IS_PRODUCTION`. Do not import the email/newsletter settings. |
| 15 | `app/_migration_lease.py` | — | 2122 | **R** | New module: versioned lease DDL, `lease_enter_v1`/`lease_acquire_v1` SECURITY DEFINER functions, generations, TTL/wait/lock-timeout admission, incoming-membership enforcement, `_assert_migrator_identity` `session_user` check, pre-commit refresh. Port as a new file; verify it does not depend on lineage-only models. |
| 16 | `app/migrate.py` | — | 191 | **R** | New operator CLI (`upgrade`, `bootstrap-lease`, `verify`, `status`, `--close-barrier`). Port as a new file; adapt any docstring/reference to `main`'s revision head. |
| 17 | `app/_migration_lock.py` | — | deleted | **X** | The old renewal-thread/console lock module removed by PR #146 in favour of the lease protocol. It never existed on `main`; there is nothing to port and nothing to delete here. |
| 18 | `alembic/env.py` | 143 | 154 | **A** | Adds `resolve_migration_database_url` delegation, `_render_as_batch(url)` (SQLite-only batch), `_run_on_connection` with the unleased-PostgreSQL refusal, connection-transaction ownership, and a `pg_backend_pid` diagnostic. **Keep `main`'s `config.attributes["connectable"]` compatibility path** and its comment about in-memory SQLite; the PR file preserves it and must continue to. |
| 19 | `app/services/leg_exposure.py` | 388 | 398 | **A** | Single behavioural change: `backfill_exposures` ends with `db.flush()` instead of `db.commit()`, and `backfill_all_exposures` commits once per user. This is the ADR §7.3 transaction-ownership change. Port exactly this hunk; verify `main`'s other 370 lines are unchanged (they are). |
| 20 | `backend/.env.example` | 14 | 60 | **A** | Adds the ADR-017/018 block with placeholders only (no secrets) and a `STRIKENOVA_ENV=production` note. Port the lease/identity/role/timeout block; reconcile the `DATABASE_URL` comment, which still describes Railway rather than the ADR-019 Neon target. |

### 5.3 Dependencies and CI

| # | File | main | PR #146 | Class | Difference and minimal port |
|---|---|---|---|---|---|
| 21 | `requirements.txt` | 9 | 11 | **A** | PR #146 adds `psycopg[binary]==3.3.9`, **and also adds `sqlalchemy-cockroachdb==2.0.3`**, and pins `cryptography==50.0.2` / `PyJWT==2.15.1`. Port `psycopg[binary]` and the two exact pins; **do not port `sqlalchemy-cockroachdb`** — ADR-019 retires CockroachDB and the plan (§3) requires it not be treated as a supported target. |
| 22 | `requirements.lock` | — | 1329 | **A** | Hash-locked closure generated for **CPython 3.12** with pip's resolver. Must be **regenerated**, not copied: the dependency set changes once `sqlalchemy-cockroachdb` is dropped, and `main` declares 3.13. |
| 23 | `requirements-py313.lock` | — | 1414 | **A** | Hash-locked closure for **CPython 3.13** via `uv pip compile --generate-hashes --only-binary --python-platform x86_64-manylinux_2_28`. This is the correct lock for `main`'s runtime; regenerate after the dependency-set edit and record the generator command. |
| 24 | `scripts/pytest_gate.py` | — | 145 | **R** | Fail-closed gate over pytest logs (exit code file, no failed/error/skipped counts, `N passed` ≥ declared minimum). Reusable as-is; the **minimum counts must be re-derived** on the new branch. |
| 25 | `.github/workflows/issue145-application-pg16.yml` | — | 214 | **A** | New workflow: PG 16.16 by digest, server-version assertion `160016`, hash-locked install, integration + regression suites, `pytest_gate.py` at `71`/`160`. Adaptation needed: it pins **`python-version: "3.12"`** while `main` and the companion compatibility workflow use 3.13 — choose one supported version deliberately and record why. Also reconcile artifact names, run-id-derived credentials, cleanup and least-privilege `permissions`. |
| 26 | `.github/workflows/postgres-compatibility.yml` | — | 191 | **A** | Replaces the old workflow that exists only in the PR lineage (the `main` repository has no such file). Runs on 3.13 with `requirements-py313.lock` and a skip-failing gate. Port as a new file for `main`, re-deriving the pinned digest, hashes and any gate counts. |
| 27 | `.github/workflows/postgres-migration-rehearsal.yml` | — | deleted | **X** | Deleted by PR #146 along with `postgres-migration-rehearsal.yml`'s CockroachDB rehearsal. `main` never had it; nothing to port. |

### 5.4 PostgreSQL harness and tests

| # | File | main | PR #146 | Class | Difference and minimal port |
|---|---|---|---|---|---|
| 28 | `tests/issue145_pg_support.py` | — | 361 | **R** | Provisioning/cleanup harness for disposable PG: per-test databases and roles (`sn145_*`), `psycopg` URL handling, `run_cli`, guarded `upgrade head`, and the explicit refusal to re-enable bare Alembic. Lineage-agnostic (`upgrade head`); port as a new file. Verify role/database naming and teardown against a live disposable cluster before gating. |
| 29 | `tests/test_issue145_application_integration.py` | — | 2331 | **R** | The gated integration suite: Topology A/B, lease generation/state invariants, eight identity negative controls, startup backfill, CLI behaviour, privilege matrix, pid-sharing evidence. Lineage-agnostic. Port as a new file; **re-derive the gate count** rather than carrying `71`. |
| 30 | `tests/test_migration_identity_separation.py` | — | 281 | **R** | DB-free URL/identity contract tests plus the source assertion that the runner calls `command.upgrade(alembic_cfg, "head")`. Port; the source assertion matches `main`'s call shape. |
| 31 | `tests/test_migration_serialization.py` | — | 386 | **R** | Lease serialization/contention/ttl tests replacing the old lineage file of the same name. Port as a new file; it is larger and different in structure from the lineage version, so treat it as a new module, not a patch. |
| 32 | `tests/test_migration_large_rehearsal.py` | — | 206 | **R** | Large-rehearsal coverage over guarded `upgrade head`. Port; check runtime cost against the gate budget. |
| 33 | `tests/test_migration_failure_injection.py` | — | 121 | **R** | Failure-injection coverage (crash/contention/rollback). Port. |
| 34 | `tests/test_postgres_compatibility.py` | — | 244 | **R** | PostgreSQL compatibility suite for `requirements-py313.lock` (historical reference: 122 passed, 0 skipped on CPython 3.13.15). Port and re-derive the count. |
| 35 | `tests/test_sqlite_postgres_migration.py` | — | 204 | **R** | SQLite↔PostgreSQL migration parity using the guarded CLI. Port; this is the module that proves `main`'s SQLite behavior survives. |
| 36 | `tests/test_rehearsal_ci_database.py` | — | 147 | **R** | CI-database rehearsal behavior. Port and reconcile with the new workflow names. |
| 37 | `tests/test_migration_rehearsal_cockroachdb.py` | — | deleted | **X** | CockroachDB rehearsal removal — consistent with ADR-019, but `main` never had the file. Nothing to port; do not reintroduce. |
| 38 | `tests/test_migration_rehearsal_postgres.py` | — | deleted | **X** | Replaced by the new PostgreSQL acceptance modules above. Nothing to port. |

### 5.5 Fixtures and migrated tests

| # | File | main | PR #146 | Class | Difference and minimal port |
|---|---|---|---|---|---|
| 39 | `tests/conftest.py` | 51 | 98 | **A** | Adds the `hermetic_init_db` fixture (fresh file-based SQLite per test, monkeypatched `app.db.engine`/`SessionLocal`) and a comment forbidding an unleased-Alembic opt-in. Port the fixture: it directly addresses the `bulk_exit_records already exists` baseline failures in §6.2 — but note it **fixes those tests only if they are migrated to use it**, which is a separate, disclosable change. Keep `main`'s StaticPool swap and `reset_token_store` untouched. |
| 40 | `tests/test_db_migration.py` | 230 | 258 | **A** | The only lineage-coupled file in the delta. It replaces `assert len(tables) == 27` with `== 55` and asserts lineage-only tables (`email_verification_tokens`, `password_reset_tokens`, `pending_email_changes`, `security_events`, `broker_raw_observation`, `order_family_sync_lock`, `broker_authorizations`, `admin_controls`, `admin_audit_events`) and lineage-only revisions. **On `main` the correct assertion is 27 tables / head `b2c3d4e5f6a7`.** Port only the structural improvements (named table assertions instead of a bare count) against `main`'s actual schema. |

### 5.6 Matrix summary

Row numbering above includes row 5 (`docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`), which is the design record and is **not** in the PR #146 delta. The delta therefore covers rows 1–4 and 6–40 = **39 files**, matching the PR's `changed_files: 39`.

| Class | Count | Files |
|---|---|---|
| **R** — required, port as new/unchanged | 12 | `_migration_lease.py`, `migrate.py`, `scripts/pytest_gate.py`, `issue145_pg_support.py`, `test_issue145_application_integration.py`, `test_migration_identity_separation.py`, `test_migration_serialization.py`, `test_migration_large_rehearsal.py`, `test_migration_failure_injection.py`, `test_postgres_compatibility.py`, `test_sqlite_postgres_migration.py`, `test_rehearsal_ci_database.py` |
| **A** — required but must be adapted | 16 | `DECISIONS.md`, `ARCHITECTURE.md`, `DATA.md`, `ADR-017-018-IMPLEMENTATION-RUNBOOK.md`, `app/db.py`, `app/config.py`, `alembic/env.py`, `app/services/leg_exposure.py`, `.env.example`, `requirements.txt`, `requirements.lock`, `requirements-py313.lock`, both workflows, `conftest.py`, `test_db_migration.py` |
| **S** — superseded by `main` | 1 | `INVARIANTS.md`. The PR #146 copies of `ARCHITECTURE.md`, `DATA.md` and `DECISIONS.md` are equally superseded as whole files but are counted under **A**, because they still carry statements that must be ported selectively. |
| **H** — historical evidence | 6 | the six `docs/reviews/issue145-final-remediation/*.md` reports |
| **X** — must not be ported | 4 | `app/_migration_lock.py` (deleted by PR #146, never on `main`), `postgres-migration-rehearsal.yml`, `test_migration_rehearsal_cockroachdb.py`, `test_migration_rehearsal_postgres.py` |
| Design record outside the 39 | 1 | `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` (PR #149 only, 1865 lines) |

No file in the delta may be applied by bulk `git checkout`/`git apply`. The four governance documents and the two acceptance-report paths would regress `main` content, and `test_db_migration.py` would fail immediately.

---

## 6. Reproducible baseline at `eeeebffe`

### 6.1 Environment and commands

| Item | Value |
|---|---|
| Worktree | Isolated detached worktree at `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b` (no pre-existing user work touched) |
| Python | CPython **3.13.15** (`python3.13 -m venv`), matching CI |
| Install | `pip install -r requirements-dev.txt` — exit 0. **Resolved, not pinned:** `cryptography==50.0.2`, `PyJWT==2.15.1`, `pydantic==2.14.4`, `starlette==1.10.0`, plus 36 other packages (41 total). |
| Database | SQLite only (no `DATABASE_URL`); the repository's own conftest swap applies |
| Full suite | `env -u DATABASE_URL python -m pytest -q -p no:cacheprovider --continue-on-collection-errors` |
| Focused migration/backfill set | `pytest -q tests/test_db_migration.py tests/test_alembic_migrations.py tests/test_broker_connection_migration.py tests/test_leg_exposure.py` |

### 6.2 SQLite baseline result

**Focused set: `42 passed` in 4.22 s, exit 0.**

**Full backend suite: `114 failed, 3001 passed, 7 skipped, 2 collection errors` in 532.30 s (8 m 52 s).**

`--continue-on-collection-errors` was required: without it the run aborts at collection with **2 errors** and reports nothing:

| Collection error | Cause |
|---|---|
| `tests/test_phase724_8a_backfill_optimization.py` | `ModuleNotFoundError: No module named 'app.services.backfill_benchmark'` — the module exists nowhere in `main`'s history |
| `tests/test_upstox_market_feed.py` | `ModuleNotFoundError: No module named 'upstox_client'` — undeclared dependency (also imported by `app/services/upstox_market_feed.py`) |

Failures by module (114 total):

| Module | Failed | Dominant cause |
|---|---|---|
| `test_phase724_8c_rate_limiter.py` | 34 | 18 × `AttributeError: '_BackfillRateConfig' object has no attribute 'reduce_concurrency_threshold'` (test/impl drift); 3 stale-attr; 4 assertions; 2 `upstox_client`; 1 other; 6 unmatched |
| `test_gex_reliability.py` | 27 | `ModuleNotFoundError: upstox_client` |
| `test_phase724_8b_optimized_backfill.py` | 10 | assertions, `upstox_client`, stale `_BackfillRateConfig` |
| `test_phase724_7_production_readiness.py` | 10 | `table bulk_exit_records already exists` (Alembic replay over a `create_all` schema), assertions, `upstox_client` |
| `test_phase724_5_backfill_orchestrator.py` | 8 | assertions (`assert 0 >= 2`, `assert 0 > 0`), one `upstox_client`, others |
| `test_google_auth.py` | 8 | `_verify_google_token(token, expected_nonce=...)` signature drift between tests and implementation |
| `test_phase724_1_pipeline_foundation.py` | 6 | `table bulk_exit_records already exists` |
| `test_strategy_resolver.py` | 3 | date-dependent assertions |
| `test_phase724_6_daily_ingestion.py` | 2 | Alembic replay, assertion |
| `test_upstox_adapter.py`, `test_trade_annotations.py`, `test_phase721_persistence.py`, `test_phase10_2a_identity.py`, `test_live_verification.py`, `test_gex_capture.py` | 1 each | capability matrix, Alembic replay, absolute-engine-URL, 401 token-store, live-verification assertion, capture |

Aggregate root causes: **~33 tests + 1 collection error** from the undeclared `upstox_client`; **9** from Alembic replaying onto a `metadata.create_all()` schema (`bulk_exit_records already exists` — exactly what PR #146's `hermetic_init_db` fixture exists to prevent); **5** from the stale `_BackfillRateConfig` attribute; **~24** assertion failures in backfill/pipeline modules; **~18** rate-limiter `AttributeError`s; the remainder module-specific.

Honesty notes: this classification is from the failure bodies in the log, not from per-test triage; it is sufficient to compare later runs against the same identifiers, not to assign ownership. **These 114 failures are pre-existing at `main`; none is caused by Phase 0**, and none was suppressed, skipped or excluded. The 7 skips were not itemised (`-q` does not print reasons and no `skipif`/`pytest.mark.skip` marker exists in `tests/*.py`, so they are raised at runtime by fixtures/imports).

### 6.3 PostgreSQL baseline result — BLOCKED, with exact cause

A disposable PostgreSQL **16.16** cluster (repository-local binaries, `postgres (PostgreSQL) 16.16`) was started on `127.0.0.1:55432` for this check. It is reachable (`pg_ctl status` healthy, TCP 55432 open).

`main` cannot run **any** PostgreSQL path:

```
$ DATABASE_URL='postgresql://postgres@127.0.0.1:55432/postgres' alembic upgrade head
...
File ".../sqlalchemy/dialects/postgresql/psycopg2.py", line 696, in import_dbapi
    import psycopg2
ModuleNotFoundError: No module named 'psycopg2'
```
exit 1.

Causes, in order: (1) no PostgreSQL driver is declared in `requirements.txt`, so `create_engine` cannot load a DBAPI; (2) there is **no PostgreSQL test module** on `main`; (3) there is **no PostgreSQL workflow** on `main`. Adding `psycopg` in Phase 0 would be a dependency change, which is forbidden, so this baseline is reported as **BLOCKED**, not as "passed by omission" and not as a `main` defect to be fixed here.

Consequence for Phase 2: the PostgreSQL gate has **no `main` predecessor**. The first PostgreSQL-capable commit introduces the driver, the harness and the workflows, and its evidence cannot be compared to an earlier `main` PostgreSQL run — only to the SQLite baseline above and to the historical `b0125f68` numbers.

### 6.4 Reusability

The baseline is reusable verbatim: same worktree SHA, same Python, same install command, same pytest invocation, same sorted failure identifiers. Sorted failure identifiers are reproducible from the log with:

```
grep -E '^FAILED ' full.log | sed 's/^FAILED //; s/ - .*//' | sort > failures.txt   # 114 lines
```

---

## 7. Outstanding conflicts, risks and unresolved decisions

| ID | Severity | Item | Proposed handling |
|---|---|---|---|
| C1 | **High (scope)** | `main` has no PostgreSQL driver, harness or workflow. Phase 2 introduces all three; the "preserve existing behavior" check is therefore SQLite-only. | Sequence it as one deliberate, reviewable change (deps → harness → workflows) with the driver addition justified by ADR-019, and disclose that no `main` PostgreSQL baseline exists. |
| C2 | **High (correctness)** | PR #146's `test_db_migration.py` asserts 55 tables and lineage-only revisions; `main` is 27 / `b2c3d4e5f6a7`. | Re-author the assertions against `main`'s real schema; do not copy the count or the revision names. |
| C3 | **Medium** | PR #146's `requirements.txt` adds `sqlalchemy-cockroachdb==2.0.3`, contradicting ADR-019 and the integration plan §3. | Drop it. If any code imports it, that code is not portable to `main`. |
| C4 | **Medium** | Two lockfiles exist with different Python targets (3.12 pip, 3.13 uv), and the integration workflow pins 3.12 while `main`'s CI and companion workflow use 3.13. | Pick one supported runtime (3.13 matches `main`), regenerate the lock, drop the unused one, and record the generator command in the report. |
| C5 | **Medium** | `main`'s governance documents (AI/CONTEXT/ARCHITECTURE/INVARIANTS/DATA/TESTING) still describe CockroachDB as production persistence, contradicting accepted ADR-019. | Out of Issue #145 scope to fix broadly. Port only Issue #145 statements; record the ADR-019 document drift as a separate follow-up issue. |
| C6 | **Medium** | `main`'s `DECISIONS.md` registry lacks ADR-017, ADR-018 **and ADR-019**. | The PR #149 route (or its two-file port) adds all three rows; if the founder defers PR #149, the successor branch still needs these rows before the design record can be cited as governing. |
| C7 | **Medium** | `main`'s backend CI is red by construction (`test_upstox_market_feed.py` cannot import `upstox_client`; `test_gex_reliability.py` fails for the same reason). | Report as pre-existing; do not "fix" by editing CI in this work. Any new workflow must not inherit that module list unchanged. |
| C8 | **Low** | `main`'s dependency set has two floating ranges, so a clean install is not reproducible. | Phase 2 pins them (PR #146 already pins the resolved versions); record the resolved set used for the baseline (§6.1). |
| C9 | **Low** | Duplicate WAL event listener in `app/db.py::_engine()`. | Remove while rewriting that function; behaviour-neutral. |
| C10 | **Low** | 9 baseline failures are Alembic-replay-over-`create_all` failures that PR #146's `hermetic_init_db` fixture would prevent — but only for tests migrated to the fixture. | Treat as a separate, disclosable test-hygiene change; do not smuggle it into the Issue #145 gate or claim it as a fix. |
| C11 | **Low** | The 7 runtime skips on `main` are not itemised in the baseline. | If a new gate must fail on skips, itemise them first with `-rs` on the modules that skip. |

No conflict requires changing ADR-019, ADR-017 or ADR-018, or contacting a live environment.

---

## 8. Independent-review findings and pre-existing follow-ups

### 8.1 The three Low findings from the `b0125f68` review

| ID | Finding | Proposed handling in the successor branch | Rationale |
|---|---|---|---|
| **L1** | `alembic/env.py` can construct an unguarded engine when a caller supplies `lease_guard` but not `connection`. Unreachable in-tree; all in-tree callers pass a connection. | **Harden, don't defer.** In the ported `_run_on_connection`, refuse when `lease_guard` is supplied without a `connection` (or derive the guard requirement from the connection only), and add a DB-free negative test asserting the refusal. | One-line fail-closed change with a cheap test; deferring creates a future bypass that a later refactor could reach. |
| **L2** | Incoming membership is enforced for the migrator (both topologies) and the Topology B owner, but **not** for the runtime role. | **Decide explicitly at Phase 1, defaulting to "empty incoming membership for runtime".** Add a negative control that a runtime role which is a member of the migrator is refused, if the founder accepts that contract; otherwise record the accepted residual risk in the runbook, not silently. | This is a least-privilege question, not a bug: a runtime role that can `SET ROLE` into the migrator defeats the separation. The test is one more control in an existing matrix. |
| **L3** | `STRIKENOVA_MIGRATOR_ROLE` / `STRIKENOVA_RUNTIME_ROLE` default to `strikenova_prod_migrator` / `strikenova_production_app`, so staging/preview PostgreSQL with different role names must configure both or fail closed. | **Document + test.** Add the fail-closed rollout note to runbook §5.4 and `.env.example`, and a DB-free configuration test proving that mismatched/blank role configuration is refused. No live-environment inspection. | Fail-closed is the correct behavior; the gap is documentation and an automated guard. |

None of L1–L3 is a High or Critical blocker, and none blocks the baseline.

### 8.2 Pre-existing follow-ups (keep outside Issue #145 scope)

| Item | Evidence at `eeeebff` |
|---|---|
| Undeclared `upstox_client` | 1 collection error + ~33 failures; not in `requirements*.txt` |
| Orphaned `test_phase724_8a_backfill_optimization.py` | imports `app.services.backfill_benchmark`, which has never existed in `main`'s history |
| Stale unleased-Alembic expectations | 9 `bulk_exit_records already exists` / index-assertion failures |
| `_BackfillRateConfig.reduce_concurrency_threshold` drift | 5 failures |
| `_verify_google_token` signature drift | 8 failures |
| Date-dependent assertions | 3 in `test_strategy_resolver.py` |
| Legacy `Base.metadata.create_all()` in data tools | `app/tools/{live_verification,candle_backfill,option_candle_backfill,contract_metadata_backfill}.py` (ADR-007/INV-020 tension, pre-existing) |
| ADR-019 registry drift | `DECISIONS.md` registry lacks ADR-019 while the ADR file is accepted |
| Topology drift in control docs | AI/CONTEXT/ARCHITECTURE/INVARIANTS/DATA/TESTING still say CockroachDB |

---

## 9. PR #149 governance routes

PR #149 is open, non-Draft, unmerged, based on `main` @ `eeeebffe`, and contains exactly two files: **`DECISIONS.md` (+5 lines)** and **`docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` (+1865 lines)**. The `DECISIONS.md` delta adds the ADR-017/018 registry rows, the **missing ADR-019 row**, and the 2026-11-21 founder approval note (D-2…D-7 approved for implementation, explicitly not provisioning/cutover).

### Route A — founder accepts PR #149 as a documentation-only change first

- **Pros:** the design record and decision registry land on `main` through their own small, reviewable, Markdown-only PR; the successor integration branch then depends on an already-accepted governance state; the registry gap (C6) closes without touching implementation code; the implementation PR's diff contains only code/tests/workflows.
- **Cons:** two PRs must be sequenced and reviewed; `main` changes between the Phase 0 baseline and the integration branch (requiring a re-verified baseline SHA), and PR #149's merge would supersede the recorded `eeeebffe` starting point.
- **Effect on Phase 1:** becomes "rebase onto the post-#149 `main` SHA and cite the accepted ADR".

### Route B — the successor integration branch carries the two-file design record

- **Pros:** one integrated Draft PR, one SHA, one review surface; no dependency on a separate merge; the design record and its implementation are reviewed together, which is how ADR-017/018 were reviewed on the old branch.
- **Cons:** a 1,865-line design document plus ~12k lines of implementation in one diff makes review harder to bound; the governance change and the code change are approved atomically, so rejecting one rejects both; it bypasses the "documentation-only PR" separation that `PROJECT-CONTROL.md` and the founder's own PR #149 describe.
- **Effect on Phase 1:** port the exact two files unmodified as the first commit of the branch, then the implementation.

### Recommendation

**Route A** is the better fit for this repository's control model: PR #149 is already written, already based on `main`, already Markdown-only, and already carries the founder's approval note; landing it first keeps the decision registry truthful before code cites it, and keeps the implementation PR's review surface to code, tests and workflows. Route B remains legitimate if the founder prefers a single approval unit.

**Decision owner: the founder.** Phase 0 does not choose, merge, close, retarget or modify PR #149 or PR #146.

---

## 10. Prioritized implementation sequence for Phase 1–2

Each step is intended to be a separate, reviewable commit on a new branch from `main`, with its own verification.

1. **Governance (route A: after #149 merges; route B: first commit).** ADR-017/018 design record + registry rows, or the two-file port. No application files.
2. **Dependencies and configuration.** Add `psycopg[binary]==3.3.9`; pin `cryptography`/`PyJWT`; **do not** add `sqlalchemy-cockroachdb`; regenerate one hash lock for 3.13; add only the migration settings block to `app/config.py` with validators; add the `.env.example` block with L3's rollout note.
3. **Lease module + operator CLI.** `app/_migration_lease.py`, `app/migrate.py`, `scripts/pytest_gate.py`. DB-free tests only at this step.
4. **Guarded startup and Alembic.** `app/db.py` + `alembic/env.py` + `app/services/leg_exposure.py`, including L1's hardening. Prove SQLite parity first (focused migration/backfill set must stay at 42 passed), then PG.
5. **PostgreSQL harness and suites.** `tests/issue145_pg_support.py`, the integration module, serialization/large-rehearsal/failure-injection/compatibility/parity/CI-database modules, `hermetic_init_db`. Re-derive every gate count; do not inherit 71/160/122.
6. **Adapted assertions.** `test_db_migration.py` against `main`'s 27-table schema and head `b2c3d4e5f6a7`.
7. **CI.** Port both workflows onto `main`: one Python version, hash-verified install, pinned PG digest, version assertion `160016`, fail-closed gate, least-privilege permissions, cleanup.
8. **Documentation and evidence.** Runbook reconciled (paths, SHA, measured TTLs, §5.4 L3 note); new evidence reports for the new SHA only. Do not re-publish the old acceptance reports as current.
9. **Independent review on the new SHA**, then a new **Draft** PR to `main` citing Issue #145, PR #146 (source implementation), PR #149 (design record) and ADR-019.

---

## 11. Stop conditions for the next phase

Stop and report rather than improvise if:

- `main` moves from the SHA chosen at Phase 1 start (after route A, the post-#149 SHA), or any pinned ref moves;
- the founder's route choice differs from the one Phase 1 is about to assume;
- preserving `main`'s SQLite startup/backfill behavior requires weakening an ADR-017/018 property, or vice versa;
- `main`'s schema (27 tables / `b2c3d4e5f6a7`) differs from the recorded inventory at the time the port starts;
- the PostgreSQL baseline still cannot be produced without a dependency change that the founder has not approved;
- any step would need a live database, role, grant, credential, Neon/Render/Vercel change, deployment or cutover;
- a gate would have to be lowered, a skip accepted, or a failing test deleted to proceed.

---

## 12. Scope and exclusions observed in Phase 0

- No application source, test, migration, workflow or dependency file was changed.
- No implementation was ported. No branch was merged, retargeted, marked Ready or closed.
- PR #146 and PR #149 were read only (Git refs + GitHub API). PR #149's local branch was fetched, not modified.
- No Neon connection or provisioning. No Render, Vercel, staging or production access.
- No live database, role, grant, credential or secret operation. The disposable PostgreSQL 16.16 instance used for the driver-blocker check was repository-local, on `127.0.0.1:55432`, created for this check and not connected to any managed service; no secret was printed.
- The only repository write is this Markdown report on `review/issue145-main-integration-baseline-2026-11-24`, branched from `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`.
- The pre-existing worktree, its branch (`issue-145-fix`), and its 32 untracked artifacts were left untouched.

---

## 13. Phase 0 conclusion

**PHASE 0 COMPLETE.**

- All five pinned refs verified identical to the brief; PR #146 remains open/Draft/unmerged; PR #149 remains open/unmerged with an unchanged two-file delta.
- Current-`main` implementation, migration head, dependency set, fixtures and CI were inventoried from `eeeebffe`.
- All 39 PR #146 files are classified, with the port action, the conflict and the required tests recorded (§5).
- A clean, reproducible SQLite baseline exists: focused set 42 passed; full suite 114 failed / 3001 passed / 7 skipped / 2 collection errors in 532.30 s, with failure identifiers preserved (§6.2).
- The PostgreSQL baseline is **BLOCKED** with the exact, reproducible cause: `main` declares no PostgreSQL driver, and has no PostgreSQL test module or workflow (§6.3).
- L1–L3 have concrete dispositions, pre-existing follow-ups are separated, and the PR #149 route decision is documented with a recommendation (Route A) for the founder.

**Next action is the founder's:** choose Route A or B, confirm the gate philosophy for the new branch, and authorize Phase 1. No implementation should begin before that decision.
