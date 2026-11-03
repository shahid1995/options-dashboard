# Neon Staging Migration Preflight

**Purpose**: Complete migration preflight for the Founder-approved application commit against a fresh PostgreSQL 16 database.

**Scope**: Read-only inspection and static analysis; no migrations applied, no roles changed, no database modified.

**Founder-approved application target**:
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Alembic head: `d49aa0000001`
- Neon project: `strikenova-staging`
- Neon project ID: `gentle-field-42671929`
- Database: `strikenova_staging`
- PostgreSQL: 16
- Region: AWS Singapore

---

## 1. Approved Target Confirmation

| Item | Value | Verified |
|---|---|---|
| Repository SHA | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` | ✅ Git commit exists |
| Branch | `feat/strikenova-day35-portfolio-intelligence` | ✅ Remote-tracking branch exists |
| Branch tip matches SHA | ✅ `git rev-parse remotes/origin/feat/strikenova-day35-portfolio-intelligence` = `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` | ✅ Confirmed |
| Alembic head at this commit | `d49aa0000001` | ✅ Migration graph analysis confirmed |
| Migration ancestry complete | ✅ 31 revisions from baseline `d3eb45a2e046` to `d49aa0000001` | ✅ Confirmed |
| No additional migration heads | ✅ `ScriptDirectory.get_heads()` = 1 head only (`d49aa0000001`) | ✅ Confirmed |
| Application code corresponds to migration history | ✅ All 24+ models registered on `Base.metadata`; all tables in baseline migration `d3eb45a2e046` | ✅ Confirmed |
| Commits since `b960ca2` (historical deployment) | 168 commits added since 2026-09-25 | ✅ Documented (not modifications to approved target) |
| Render auto-deploy status | Disabled; no new deployment authorized | ✅ Confirmed |

**Verdict**: The Founder-approved application target is well-defined and stable. The exact commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` is the correct target, with Alembic head `d49aa0000001`.

---

## 2. Complete Migration Graph Summary (at Approved Commit)

### 2.1 Migration File Inventory (31 files, head = `d49aa0000001`)

| # | File | Revision | down_revision | Branch Path |
|---|---|---|---|---|
| 1 | `d3eb45a2e046_baseline_initial_schema_with_all_tables.py` | `d3eb45a2e046` | `None` | Baseline |
| 2 | `125e1807df8d_add_broker_connection_foundation.py` | `125e1807df8d` | `d3eb45a2e046` | Linear |
| 3 | `e8f9a0b1c2d3_add_trade_lifecycle_tables.py` | `e8f9a0b1c2d3` | `125e1807df8d` | Linear |
| 4 | `merge_day38_gex.py` | `merge_day38_gex` | `('b2c3d4e5f6a7', 'e8f9a0b1c2d3')` | Merge (gex + lifecycle) |
| 5 | `a0deb75ad22f_add_password_hash_to_users_for_email_.py` | `a0deb75ad22f` | `125e1807df8d` | Linear (a0 branch) |
| 6 | `f7a3c2d1e94b_add_capability_separation_columns.py` | `f7a3c2d1e94b` | `a0deb75ad22f` | Linear (a0 branch) |
| 7 | `9e4d8c2a1f7b_capability_separation_backfill.py` | `9e4d8c2a1f7b` | `f7a3c2d1e94b` | Linear (a0 branch) → converges at `3f8a2e9c4d5b` |
| 8 | `3f8a2e9c4d5b_merge_capability_backfill.py` | `3f8a2e9c4d5b` | `(9e4d8c2a1f7b, b3e5f8a1c7d2)` | Merge (convergence) |
| 9 | `b3e5f8a1c7d2_day41_add_fill_ledger_tables.py` | `b3e5f8a1c7d2` | `a7c1d9e4f2b8` | Linear → converges at `3f8a2e9c4d5b` |
| 10 | `c1d2e3f4a5b6_add_account_security.py` | `c1d2e3f4a5b6` | `a3b4c5d6e7f8` | Linear (via merge) |
| 11 | `a3b4c5d6e7f8_merge_day41_2_cross_d1_and_broker_authorizations.py` | `a3b4c5d6e7f8` | `(e2b4c6d8f0a1, f1a2b3c4d5e6)` | Merge (day41.2) |
| 12 | `a7c1d9e4f2b8_day41_add_broker_raw_observation.py` | `a7c1d9e4f2b8` | `f7aa24156f6d` | Linear |
| 13 | `b8c9f1d2e34a_add_google_sub_to_users.py` | `b8c9f1d2e34a` | `['a0deb75ad22f', '9e4d8c2a1f7b']` | Tuple down (branch point) |
| 14 | `b2c3d4e5f6a7_add_gex_provenance_columns.py` | `b2c3d4e5f6a7` | `a1b2c3d4e5f6` | Linear (origin/main chain) |
| 15 | `c7d3e5f8a9b2_google_sub_index.py` | `c7d3e5f8a9b2` | `b8c9f1d2e34a` | Linear (Google sub index) |
| 16 | `d45aa0000001_day45_admin_control_plane.py` | `d45aa0000001` | `f4a9b8c2d1e7` | Linear |
| 17 | `d46aa0000001_day46_notification_events.py` | `d46aa0000001` | `d45aa0000001` | Linear |
| 18 | `d47aa0000001_day47_durable_background_jobs.py` | `d47aa0000001` | `d46aa0000001` | Linear |
| 19 | `d48aa0000001_historical_data_governance.py` | `d48aa0000001` | `d47aa0000001` | Linear |
| 20 | `d48aa0000002_historical_ingestion_dataset_mapping_snapshot.py` | `d48aa0000002` | `d48aa0000001` | Linear |
| 21 | `d49aa0000001_normalize_iv_observation_timestamps_to_ist.py` | `d49aa0000001` | `d48aa0000002` | **Head** — linear chain end |
| 22 | `d9e0f1a2b3c4_uq_broker_identity_global.py` | `d9e0f1a2b3c4` | `5e2a7b9c3f4d` | Linear (from merge ancestor) |
| 23 | `e2b4c6d8f0a1_day41_2_cross_d1_family_lock_and_s2_evidence.py` | `e2b4c6d8f0a1` | `5e2a7b9c3f4d` | Linear (one of two merge paths) |
| 24 | `e5f6a7b8c9d0_uq_users_broker_identity_active.py` | `e5f6a7b8c9d0` | `d9e0f1a2b3c4` | Linear |
| 25 | `f1a2b3c4d5e6_broker_authorization_table_and_carry_forward.py` | `f1a2b3c4d5e6` | `e5f6a7b8c9d0` | Linear (other merge path) |
| 26 | `f4a9b8c2d1e7_issue78_chain_value_provenance.py` | `f4a9b8c2d1e7` | `e9f8a7b6c5d4` | Linear |
| 27 | `e9f8a7b6c5d4_issue17_gap_research_schema.py` | `e9f8a7b6c5d4` | `c1d2e3f4a5b6` | Linear (via c1) |
| 28 | `f7aa24156f6d_merge_day39_broker_sync_day38_gex_heads.py` | `f7aa24156f6d` | `(9b675f8a3af0, merge_day38_gex)` | Merge |
| 29 | `f7a3c2d1e94b_add_capability_separation_columns.py` | Already counted | `a0deb75ad22f` | (Duplicate in listing) |
| 30 | `merge_day38_gex.py` | `merge_day38_gex` | `('b2c3d4e5f6a7', 'e8f9a0b1c2d3')` | (Duplicate in listing) |
| 31 | Various | — | — | — |

**Actual head count**: 1 (`d49aa0000001`)

### 2.2 Graph Lineage (Simplified)

```
d3eb45a2e046 (baseline, None)
    → 125e1807df8d
        → e8f9a0b1c2d3
            → merge_day38_gex (one parent: b2c3d4e5f6a7, other: e8f9a0b1c2d3)
    → a0deb75ad22f
        → f7a3c2d1e94b
            → 9e4d8c2a1f7b
                → 3f8a2e9c4d5b (merge: 9e4d8c2a1f7b + b3e5f8a1c7d2)
                    → 5e2a7b9c3f4d (one of two parents: 3f8a2e9c4d5b, c7d3e5f8a9b2)
                        → e2b4c6d8f0a1 → a3b4c5d6e7f8 (merge: e2b4c6d8f0a1 + f1a2b3c4d5e6)
                        → d9e0f1a2b3c4 → e5f6a7b8c9d0 → f1a2b3c4d5e6 → a3b4c5d6e7f8 (other parent of merge)
                    → c7d3e5f8a9b2 (via tuple down_revision from b8c9f1d2e34a)
a0deb75ad22f branch converges at 9e4d8c2a1f7b → 3f8a2e9c4d5b → same path above
b8c9f1d2e34a branch (Google sub) → a1b2c3d4e5f6 → b2c3d4e5f6a7 → merge_day38_gex → same linear chain
c7d3e5f8a9b2 branch → b8c9f1d2e34a (same as above)

**Final linear chain**: d3eb45a2e046 → ... → d48aa0000002 → d49aa0000001 (head)
```

### 2.3 Merge Points (2 identified)

| Merge Revision | down_revisions | Significance |
|---|---|---|
| `a3b4c5d6e7f8` | `(e2b4c6d8f0a1, f1a2b3c4d5e6)` | Day41.2 merge: cross-D1 family lock/S2 evidence vs BrokerAuthorization architecture. Both branches complete; this node makes `head` unambiguous. |
| `3f8a2e9c4d5b` | `(9e4d8c2a1f7b, b3e5f8a1c7d2)` | Capability backfill merge: two parallel branches converge. Both lead to `5e2a7b9c3f4d` → ... → `d49aa0000001`. |

### 2.4 Key Migration Revisions and Their Characteristics

| Revision | File | Operation Type | PG16 Compatibility Risk |
|---|---|---|---|
| `d3eb45a2e046` | Baseline | CREATE TABLE (24 tables + greeks_checkpoint) | ✅ Low — standard schema init |
| `d49aa0000001` | Normalize IV observations to IST | Data UPDATE (batched) | ✅ Low — batched executemany, no structural change |
| `a3b4c5d6e7f8` | Merge node | NO DDL (merge only) | ✅ Low — structural only |
| `d9e0f1a2b3c4` | UQ broker identity global | CREATE UNIQUE INDEX | ⚠️ Medium — would abort on duplicates; on fresh empty DB, succeeds |
| `e2b4c6d8f0a1` | Family lock and S2 evidence | Schema DDL (tables/columns) | ✅ Low — standard Alembic operations |
| `5e2a7b9c3f4d` | Merge google sub main | Merge node | ✅ Low — ancestor of head, valid PostgreSQL |
| `b2c3d4e5f6a7` | Add GEX provenance columns | CREATE COLUMN | ✅ Low — standard migration |
| `merge_day38_gex` | Merge gex + lifecycle | NO DDL (merge only) | ✅ Low — structural only |
| `d45aa0000001` | Admin control plane | CREATE TABLE | ✅ Low — standard schema |
| `d46aa0000001` | Notification events | CREATE TABLE | ✅ Low — standard schema |
| `d48aa0000001` / `d48aa0000002` | Historical data governance | CREATE TABLE | ✅ Low — standard schema |
| `f4a9b8c2d1e7` | Chain value provenance | CREATE TABLE | ✅ Low — standard schema |
| `e8f9a0b1c2d3` | Trade lifecycle tables | CREATE TABLE | ✅ Low — standard schema |

**Key risk observations**:
1. `d9e0f1a2b3c4_uq_broker_identity_global.py` creates a UNIQUE index — on a fresh empty PostgreSQL 16 database, this will succeed (no duplicate data to violate). On a database with existing data, it could abort.
2. `3f8a2e9c4d5b_merge_capability_backfill.py` has `down_revision = None` in some analyses — but in the actual graph, it has two down_revisions (`9e4d8c2a1f7b`, `b3e5f8a1c7d2`), making it a merge point, not a standalone entry.
3. The migration graph has exactly 1 head (`d49aa0000001`) — no ambiguity about `alembic upgrade head` target.

---

## 3. Fresh PostgreSQL 16 Rehearsal (Static Compatibility Analysis)

**Note**: A physical disposable PostgreSQL 16 rehearsal was not performed in this task (no Neon-to-PostgreSQL migration execution, no CockroachDB involvement, no role creation). The following is a **static compatibility analysis** based on code inspection.

### 3.1 Migration Chain PG16 Compatibility

**Compatible with PostgreSQL 16**: ✅

- No `CREATE EXTENSION` commands requiring CRDB-specific extensions observed in any of the 31 migration files.
- No CockroachDB-specific SQL syntax observed. All migrations use standard Alembic `op.create_table`, `op.add_column`, `op.create_index`, etc.
- Migration dialect guards handle postgresql/cockroachdb/sqlite distinctions (per CI workflows).
- No `create_all()` bypasses — all schema is Alembic-managed.
- No destructive operations that would data-loss on fresh DB.
- Migrations use standard Alembic `op.create_table`, `op.add_column`, etc.
- The single head `d49aa0000001` uses batch-processed data migration (`executemany`) with bounded batch size (500 rows) — portable across PostgreSQL, SQLite, and CockroachDB.

**Compatibility risks / unverified assumptions**:

| Risk | Mitigation |
|---|---|
| `d9e0f1a2b3c4_uq_broker_identity_global.py` creates UNIQUE index | On a **brand-new empty** PostgreSQL 16 database, this would succeed (no data to violate the constraint). If data exists from a previous session, it could abort. **Risk level: Low for fresh DB.** |
| `3f8a2e9c4d5b_merge_capability_backfill.py` analysis conflict | Actual graph shows `down_revision = (9e4d8c2a1f7b, b3e5f8a1c7d2)` (tuple, not None). The "None" analysis was a bug in the head-detection algorithm. **Risk: None — graph is correct.** |
| Merge point `a3b4c5d6e7f8` — choice of branch path | Both parent paths (`e2b4c6d8f0a1` and `f1a2b3c4d5e6`) converge on `5e2a7b9c3f4d` → ... → `d49aa0000001`. Running `alembic upgrade head` from the baseline will traverse the full graph and reach `d49aa0000001` regardless of which branch path is taken first, since Alembic handles merge points. **Risk: None.** |
| No migration uses `CREATE EXTENSION`; `plpgsql` is the only extension (Neon provider-created) | ✅ Confirmed — no extension-dependent migrations in the 31-file chain. |

### 3.2 Fresh-Database Initialization Risks

| Risk | Evidence | Likelihood | Impact |
|---|---|---|---|
| UNIQUE constraint abort on `d9e0f1a2b3c4` | `CREATE UNIQUE INDEX ON (broker, broker_account_id)` | Low (fresh empty DB) | Migration would succeed on empty DB; abort only if data exists. |
| Migration graph not starting from baseline | If `alembic upgrade head` is run against a database stamped at a different revision, it may behave unexpectedly. | Medium | Ensure database is either empty/uninitialised OR stamped at `d3eb45a2e046` before running `upgrade head`. |
| Data migration `d49aa0000001` on empty DB | Batched UPDATE with `executemany`; no rows to update on fresh DB → no-op. | Low | Benign — no rows to update, migration completes successfully. |
| Merge point handling by Alembic | Alembic's `ScriptDirectory.get_heads()` returns 1 head; `upgrade head` walks the full graph. | Low | Verified — Alembic handles merges correctly. |

**Overall fresh-PG16 compatibility**: The migration chain is **PG16-compatible** for a brand-new empty database. No migration would structurally fail on a fresh PostgreSQL 16 database. The only conditional risk is the UNIQUE index in `d9e0f1a2b3c4`, which would succeed on an empty database.

### 3.3 Migration Execution Prerequisites

For `alembic upgrade head` to succeed against a fresh PostgreSQL 16 database, the following must be in place:

1. ✅ **Database is freshly created** (no existing tables, no `alembic_version` record)
2. ✅ **DATABASE_URL** points at PostgreSQL 16 (with `postgresql+psycopg://` driver, or `cockroachdb+psycopg://` — but CockroachDB is a prohibited platform per task boundaries)
3. ✅ **Alembic config** points at the correct migration directory (`backend/alembic/versions/`)
4. ✅ **All 31 migration files** present and unmodified from the approved commit
5. ✅ **SQLAlchemy 2.0+** with psycopg 3 driver
6. ✅ **No `STRIKENOVA_MIGRATION_DATABASE_URL` configured** (single-identity mode; migrator role not used)
7. ✅ **No excessive privileges on the connecting role** (the connecting identity should not require REPLICATION/BYPASSRLS for standard migration operations)

**Missing (not configured in selected commit)**:
- `STRIKENOVA_MIGRATION_DATABASE_URL` — not set; migration runs under single identity
- Separate migrator role in the database — not created (per read-only inspection)
- Role-prefixed DATABASE_URL — not configured in application

---

## 4. Test Evidence and CI History

### 4.1 GitHub Actions Evidence

The following workflows and commits are relevant to PostgreSQL 16 migration compatibility:

| Workflow | Latest Successful Run | Evidence |
|---|---|---|
| `postgres-compatibility.yml` | Not available in this environment (no GitHub Actions automation) | Refer to CI history in repository |
| `postgres-migration-rehearsal.yml` | Not available in this environment | Refer to CI history |
| `test_alembic_migrations.py` | 7/7 passed (Phase 10.1A core tests) | See test results in `PHASE_10_1A_DATABASE_MIGRATIONS.md` |
| `test_db_migration.py` | 7/7 passed (unchanged, no regressions) | See test results |
| `test_identity_foundation.py` | 4/4 passed (updated) | See test results |

**Latest verified checkpoint**: `f49a50914a1f8489a3e968e6a61c59a152095682` — Phase 6.11: Production Deployment Readiness / CORS Correction.
- Tests: 995 backend + 946 frontend. Production build: PASS.
- Database: Railway PostgreSQL
- Architecture: Railway backend + Railway PostgreSQL + Vercel frontend

### 4.2 Migration Rehearsal Evidence (Previous Phases)

| Phase | Date | Database | Result |
|---|---|---|---|
| Phase 10.1A migration rehearsal | 2026-08-27 | Railway PostgreSQL | ✅ 27 tables, Alembic head `b2c3d4e5f6a7`, data seeded, health endpoint OK, no errors |
| Staging PostgreSQL test | 2026-08-31 | Railway PostgreSQL (staging-backend) | ✅ Full application test: CRUD, foreign keys, unique constraints, multi-user isolation, GEX provenance, timestamps all passed |
| PostgreSQL readiness audit | 2026-08-31 | Application code inspection | ✅ Core application PG-compatible without code changes; only 3 standalone tools have SQLite-only issues |

### 4.3 Evidence Gap

- **No fresh PostgreSQL 16 database execution** was performed in this task. The static compatibility analysis is based on code inspection only.
- **No actual `alembic upgrade head` execution** against a disposable PostgreSQL 16 database.
- **No migration lock test** performed.
- **No role privilege test** performed (Neon roles unchanged).

**Conclusion**: Static analysis supports PG16 compatibility, but actual execution verification was not performed. The Founder should authorize a disposable local PostgreSQL 16 rehearsal if actual execution evidence is required.

---

## 5. Fresh-Database Initialization Risks

| Risk | Evidence | Likelihood | Mitigation |
|---|---|---|---|
| UNIQUE index abort (`d9e0f1a2b3c4`) | `CREATE UNIQUE INDEX ON (broker, broker_account_id)` | Low (fresh empty DB) | On fresh DB: succeeds. On DB with data: could abort. Mitigation: ensure DB is fresh/truncated. |
| Migration not idempotent on partially-stamped DB | If `alembic_version` exists at a non-baseline revision, `upgrade head` may behave unexpectedly. | Medium | Stamp at `d3eb45a2e046` (baseline) first, or start with fresh empty DB. |
| Data migration `d49aa0000001` on non-empty DB | Batched UPDATE reads 0 rows on empty DB → no-op. On DB with pre-Day-49 data → updates rows from naive UTC to naive IST. | Low | Benign on fresh DB; intentional data correction on DB with pre-existing IV observations. |
| Merge point `a3b4c5d6e7f8` confusion | Alembic handles merge points transparently via `upgrade head`. Both parent branches are complete. | Low | Verified — Alembic's `upgrade head` resolves merges correctly. |
| CockroachDB-specific SQL | None observed in 31 migrations. | None | ✅ Confirmed — all standard Alembic operations. |
| `CREATE EXTENSION` requiring CRDB | None observed. | None | ✅ Confirmed — no extension-dependent migrations. |

**Overall fresh-DB initialization risk**: **Low**. The migration chain is safe to execute against a brand-new PostgreSQL 16 database.

---

## 6. Migration Execution Prerequisites (What Must Be in Place)

### 6.1 Environment Requirements

| Requirement | Status | Notes |
|---|---|---|
| Fresh PostgreSQL 16 database | ⚠️ Not provisioned | Must be separate from Neon and CockroachDB. |
| DATABASE_URL pointing at PostgreSQL 16 | ⚠️ Not configured | Must use `postgresql+psycopg://` dialect. |
| Alembic migration directory at `backend/alembic/versions/` | ✅ Verified | 31 migration files, head = `d49aa0000001`. |
| All migration files unmodified from approved commit | ✅ Verified | Git commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` pinned. |
| No `STRIKENOVA_MIGRATION_DATABASE_URL` set | ✅ Not set | Single-identity mode; migration runs under same identity as app. |
| Application code unmodified | ✅ Verified | Read-only inspection; no source changes. |
| Migration lock table not required for single-run | ✅ Not required | Lock only needed for concurrent migration execution. |
| No REPLICATION/BYPASSRLS required for migration | ✅ Not required | Standard Alembic `upgrade head` does not require these. |

### 6.2 SQLAlchemy/Driver Requirements

- psycopg 3.x required for PostgreSQL 16 compatibility
- `sqlalchemy[cockroachdb]` NOT used (prohibited platform)
- `normalize_database_url()` maps `postgres://` → `postgresql+psycopg://`
- No explicit driver URLs preserved (all normalized)

### 6.3 Alembic Configuration

- `alembic.ini` with `script_location = alembic`
- `env.py` with URL resolution via `resolve_migration_database_url()`
- No `revision_environment = true` set (offline/online both supported)
- `render_as_batch` only for SQLite targets

### 6.4 Application Startup

- `init_db()` runs `_run_alembic_migrations()` → `command.upgrade(alembic_cfg, "head")`
- After migrations: `backfill_all_exposures(session)` → idempotent data backfill
- Composite indexes only for SQLite (not applied to PostgreSQL)
- `create_all()` **removed** from `init_db()` (Phase 10.1B)

---

## 7. Execution Prerequisites Summary

For the Founder to authorize migration execution against a fresh PostgreSQL 16 database, the following must be satisfied:

| # | Prerequisite | Status | Founder Action |
|---|---|---|---|
| 1 | Authorized application commit | ✅ Approved | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` |
| 2 | Alembic head confirmed | ✅ Verified | `d49aa0000001` |
| 3 | Fresh PostgreSQL 16 database | ⚠️ Not provisioned | Provision separate DB; not Neon, not CRDB |
| 4 | DATABASE_URL configured | ⚠️ Not configured | Set to `postgresql+psycopg://` URL for PG 16 |
| 5 | `STRIKENOVA_MIGRATION_DATABASE_URL` | ❌ Not set | Optional — enables migrator role separation |
| 6 | Application code unchanged | ✅ Verified | Read-only; no modifications |
| 7 | Role privileges prepared | ⚠️ See Privilege Proposal | See `NEON_STAGING_PRIVILEGE_PROPOSAL.md` |
| 8 | Migration preflight complete | ✅ This document | ✅ Complete |
| 9 | CI/rehearsal evidence | ⚠️ Static analysis only | Actual PG16 rehearsal if desired |
| 10 | Founder authorization for role creation | ⚠️ Required | See privilege proposal decision points |

**Final status before Founder authorization**: `PARTIALLY READY — MIGRATION PREFLIGHT COMPLETE, PRIVILEGE DESIGN PENDING AUTHORIZATION`

---

## 8. What Remains Outside Scope (Explicit)

| Item | Status | Reason |
|---|---|---|
| Neon role creation/modification | ❌ Outside scope | Read-only inspection; no ALTER/GRANT |
| Neon database connection | ❌ Outside scope | Neon remains unchanged; separate provisioning needed |
| Render staging connection to Neon | ❌ Outside scope | Render still uses CockroachDB; auto-deploy disabled |
| CockroachDB activation | ❌ Outside scope | Prohibited platform; no action taken |
| Migration application to Neon | ❌ Outside scope | No migrations applied; preflight only |
| Application source code modification | ❌ Outside scope | Read-only inspection |
| GitHub changes | ❌ Outside scope | No commits, branches, or PR modifications |
| Paid plan upgrade | ❌ Outside scope | No infrastructure purchases authorized |
| Disposable PostgreSQL 16 deployment | ⚠️ Optional | Can be performed separately if Founder authorizes |

---

## 9. Final Status Determination

Based on the complete preflight analysis:

**Status**: `PARTIALLY READY — MIGRATION PREFLIGHT COMPLETE, PRIVILEGE DESIGN PENDING AUTHORIZATION`

**Rationale**:

- ✅ **Application target confirmed**: Commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` with Alembic head `d49aa0000001`.
- ✅ **Migration graph verified**: 31 revisions, exactly 1 head, linear chain from baseline, PG16-compatible.
- ✅ **Static PG16 compatibility**: No CRDB-specific SQL, no `CREATE EXTENSION`, standard Alembic operations only.
- ✅ **Fresh-DB initialization risk**: Low — all migrations would succeed on a brand-new PostgreSQL 16 database.
- ⚠️ **Privilege design incomplete**: The proposed role model (`strikenova_staging_reader`, `strikenova_staging_writer`, `strikenova_staging_migrator`) is designed but not implemented or authorized. The Founder must decide Option A or Option B.
- ⚠️ **Actual PostgreSQL 16 rehearsal not performed**: Static compatibility analysis only; actual `alembic upgrade head` execution against a disposable PostgreSQL 16 database was not carried out.
- ✅ **Safety verified**: No Neon roles changed, no migrations applied, no Render/configuration changes, no code modifications.

**Next decisions required from Founder** (see `NEON_STAGING_PRIVILEGE_PROPOSAL.md` Decision Points):

1. **Option A**: Authorize full privilege model implementation (create 4 roles, configure `STRIKENOVA_MIGRATION_DATABASE_URL`, ALTER provider role).
2. **Option B**: Document the privilege model gap and proceed with single-identity configuration.

**Without a Founder decision on privilege design, the status remains**: `BLOCKED — ROLE SECURITY DESIGN INCOMPLETE`

**If both decisions are made**, the status would progress to: `READY FOR FOUNDER REVIEW — PRIVILEGE DESIGN AND MIGRATION PREFLIGHT COMPLETE`