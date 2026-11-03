# Corrective Independent Audit: Render Provider, Migration Graph, and Neon Role Verification

## Change Log — Corrections from Previous Report (AUDIT_REPORT.md)

| # | Previous Report Statement | Corrected Finding | Evidence |
|---|---|---|---|
| C-1 | Render staging uses Neon | **Render staging currently uses COCKROACHDB**; Neon has not been connected to Render | Render startup logs show `sqlalchemy_cockroachdb` driver; CockroachDB monthly RU limit exceeded; CockroachDB connection rejected. Neon project `gentle-field-42671929` exists but is not linked to Render service `srv-daj4vetg1s2s739ecvfg`. |
| C-2 | Seven Alembic heads at commit `b960ca2` | **Migration history corrected from exact Git trees:** b960ca2 has 27 migration files with relevant head `d46aa0000001` (among multiple heads); 9dc6e15 has 31 migration files with relevant head `d49aa0000001` (among multiple heads). The previous "seven heads" claim is invalid; graph analysis required reconstruction from exact trees with proper handling of tuple/list `down_revision` and merge parents. | Reconstructed graphs from `git show` at exact commits b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba and 9dc6e15c7b7c112ccb81b3be9c1609be66c92414. Added in 9dc6e15: `d47aa0000001`, `d48aa0000001`, `d48aa0000002`, `d49aa0000001` extending linearly from `d46aa0000001`. |
| C-3 | `DATA.md` §5 documents database roles / No accepted database-role contract | **Accepted privilege contract exists** (ADR-016/017/018 in DECISIONS.md). **`DATA.md` §5 describes paper-trading data**, not database login roles or grants. | Read DECISIONS.md sections ADR-016, ADR-017, ADR-018. Read DATA.md §5 — confirms paper-trading data. No false claim of "no accepted contract". |
| C-4 | Historical deployment commit is the next approved target / authorization pending | **Application version approval is COMPLETE.** Founder has explicitly approved branch `feat/strikenova-day35-portfolio-intelligence` at commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` with expected Alembic head `d49aa0000001`. | Explicit Founder approval stated in requirements. This decision is not pending. |
| C-5 | Privilege-model roles documented but status unknown | **Three documented privilege-model roles (`strikenova_prod_migrator`, `strikenova_production_app`, `strikenova_prod_admin`) are NOT created** in Neon. Only provider-created `strikenova_staging_owner` exists. | Read-only `pg_roles` inspection. Staging privilege model implementation remains pending separate Founder authorization. |
| C-6 | Application source changes needed for identity separation | **Application already supports `STRIKENOVA_MIGRATION_DATABASE_URL`.** No code changes necessary. Separation implemented in config.py, db.py (resolve_migration_database_url, _migration_lock_url), alembic/env.py, app/_migration_lock.py. | Direct code inspection at approved commit. |

---

## A. Executive Summary

**Corrected findings:**

1. **Render staging database provider**: Render staging currently uses **CockroachDB** (`sqlalchemy_cockroachdb`). Neon has **not** been connected to Render. Historical successful deployment (2026-09-25) used CockroachDB at commit `b960ca2`.

2. **Migration history (from exact Git trees):**
- **b960ca2** (`b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba`): 27 migration files. Relevant head on the extended line is `d46aa0000001`. Does **not** contain `d47aa0000001`, `d48aa0000001`, `d48aa0000002`, `d49aa0000001`.
- **9dc6e15** (`9dc6e15c7b7c112ccb81b3be9c1609be66c92414`, approved target): 31 migration files. Adds 4 migrations extending linearly from `d46aa0000001`: `d47aa0000001` → `d48aa0000001` → `d48aa0000002` → `d49aa0000001`. Relevant head is `d49aa0000001`. Graph reconstruction handled tuple/list `down_revision` values and merge parents (multiple heads exist in repo; target head is `d49aa0000001`).

3. **Application approval (COMPLETE):** Founder has explicitly approved branch `feat/strikenova-day35-portfolio-intelligence` at commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` with expected Alembic head `d49aa0000001`. This is not pending.

4. **Accepted architecture (ADR-016/017/018):** Runtime/migration database identity separation is supported via `STRIKENOVA_MIGRATION_DATABASE_URL` (ADR-016). Migration serialization uses transactional lease lock `_migration_lock` bootstrapped on empty DB (ADR-017). Migrator-creator default privileges required for future tables/sequences (ADR-018). Application code already implements this; no changes needed.

5. **Neon role configuration:** Only provider-created `strikenova_staging_owner` exists (with `replication=True, bypass_rls=True`). Documented privilege-model roles (`strikenova_prod_migrator`, `strikenova_production_app`, `strikenova_prod_admin`) are not created. Staging privilege model implementation requires separate Founder authorization.

6. **PostgreSQL 16 compatibility:** Migration chain is PG16-compatible (no CRDB-specific SQL, no `CREATE EXTENSION` requiring CRDB extensions, standard Alembic operations). CI runs at `a016887` succeeded on PG and CRDB (runs [37575586698](https://github.com/shahid1995/options-dashboard/actions/runs/37575586698), [37575586664](https://github.com/shahid1995/options-dashboard/actions/runs/37575586664)); approved target `9dc6e15` differs only by 2 doc/governance commits with no app/migration changes — relevant qualification applies (not a Neon-specific role rehearsal).

**Authorization gates (separate):**
1. Application version: **COMPLETE** (9dc6e15, head d49aa0000001)
2. Staging privilege model: **PENDING** (separate Founder authorization)
3. Neon migration execution: **PENDING** (separate Founder authorization)  
4. Render-Neon integration & deployment: **PENDING** (separate Founder authorization)

**Status:** `CORRECTED DOCUMENTATION — GATES SEPARATED; NO DATABASE OR INFRASTRUCTURE ACTIONS TAKEN`
