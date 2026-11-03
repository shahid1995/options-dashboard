# Neon Staging Execution Plan

**Purpose**: Provide a precise, numbered execution plan that explains precisely what would happen after separate Founder authorization for Neon staging initialization and migration.

**Scope**: Read-only planning document; no roles created, no migrations applied, no infrastructure changed.

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

## 1. Prerequisite: Founder Decisions Required

Before this execution plan can be invoked, the Founder must authorize two separate decisions:

### Decision 1: Authorized Staging Application Commit (Choose One)

| Option | Description | Required Action |
|---|---|---|
| **A1** | `d49aa0000001` — HEAD revision, 31-revision chain | Authorize `alembic upgrade head` target = `d49aa0000001` |
| **A2** | `b2c3d4e5f6a7` — origin/main head, separate minimal chain | Authorize different revision target (not recommended) |
| **A3** | `5e2a7b9c3f4d` — old Render staging head on CockroachDB | Authorize revision with prohibited platform caveat |

**Default recommendation**: **A1** (`d49aa0000001`) — single unambiguous head, PG16-compatible, linear chain from baseline.

### Decision 2: Privilege Model Implementation (Choose One)

| Option | Description | Required Action |
|---|---|---|
| **B1** | Create the three documented privilege-model roles | `strikenova_prod_migrator`, `strikenova_production_app`, `strikenova_prod_admin` <br> + configure `STRIKENOVA_MIGRATION_DATABASE_URL` <br> + ALTER `strikenova_staging_owner` to remove `replication=True, bypass_rls=True` |
| **B2** | Document the privilege model gap; keep single-identity configuration | Accept that only `strikenova_staging_owner` exists; no role creation; application operates under single-identity semantics |

**Default recommendation**: **B1** — implements the audit task's security requirements. **B2** acceptable if role separation is not a priority.

**Both decisions are required** before this execution plan can proceed. Without both, the status remains `BLOCKED`.

---

## 2. Execution Plan (Numbered Sequence)

This plan assumes **Both Decisions Authorized** (A1 + B1). Each step identifies which actions require Founder authorization and which are technical implementation.

### Step 1: Neon Role Setup (Founder-authorized actions)

| # | Action | Authorized By | Technical Implementation |
|---|---|---|---|
| 1 | Create `strikenova_prod_migrator` role in Neon `strikenova_staging` database | Founder (Decision B1) | `CREATE ROLE strikenova_prod_migrator LOGIN;` <br> `GRANT CONNECT ON DATABASE TO strikenova_prod_migrator;` <br> `GRANT USAGE ON SCHEMA public TO strikenova_prod_migrator;` <br> `GRANT CREATE, CREATE TEMPORARY TABLE ON SCHEMA public TO strikenova_prod_migrator;` |
| 2 | Create `strikenova_production_app` role in Neon `strikenova_staging` database | Founder (Decision B1) | `CREATE ROLE strikenova_production_app LOGIN;` <br> `GRANT CONNECT ON DATABASE TO strikenova_production_app;` <br> `GRANT USAGE ON SCHEMA public TO strikenova_production_app;` <br> `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO strikenova_production_app;` <br> `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_production_app;` |
| 3 | Create `strikenova_prod_admin` role in Neon `strikenova_staging` database | Founder (Decision B1) | `CREATE ROLE strikenova_prod_admin LOGIN;` <br> Intended for break-glass access through Neon provider account <br> No DB-level privileges beyond what Neon provider grants (rate-limited) |
| 4 | ALTER `strikenova_staging_owner` to remove `replication=True, bypass_rls=True` | Founder (Decision B1) | `ALTER ROLE strikenova_staging_owner NOREPLICATION NOCREATEROLE NOCREATEDB NOSUPERUSER;` <br> *(Note: This modifies the provider role; Founder authorization required.)* |
| 5 | Configure `STRIKENOVA_MIGRATION_DATABASE_URL` in application settings | Founder (Decision B1) | Set to a dedicated migration URL if migrator role separation is desired <br> *Not strictly required if app uses single identity* |

**Step 1 Complete Checklist**:
- [ ] All 4 roles created in Neon database
- [ ] `strikenova_staging_owner` privileges reduced (replication/bypass_rls removed)
- [ ] `STRIKENOVA_MIGRATION_DATABASE_URL` configured (if Option B1 chosen)
- [ ] No `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION`, or `BYPASSRLS` on application identities

---

### Step 2: Database Migration (Technical execution, Founder authorizes Step 1 first)

| # | Action | Performed By | Technical Details |
|---|---|---|---|
| 6 | Provision fresh PostgreSQL 16 database | Founder/Operator | Create a new PostgreSQL 16 database **separate from Neon** <br> (or use an existing empty PG 16 instance). <br> **NOT** the Neon `strikenova_staging` database until Step 1 is complete. |
| 7 | Configure `DATABASE_URL` for fresh PG 16 | Technical operator | `postgresql+psycopg://user:password@host:port/dbname` <br> Ensure URL uses `postgresql+psycopg://` dialect (normalized from `postgresql://` or `postgres://`). |
| 8 | Set application role (if B1 chosen) | Technical operator | `SET ROLE strikenova_prod_migrator;` <br> **OR** `SET ROLE strikenova_production_app;` <br> Depending on which identity the app should use for this migration run. |
| 9 | Run `alembic upgrade head` | Technical operator | `cd /path/to/options-dashboard-project <br> ALBETIC env.py <br> python -m alembic upgrade head` <br> This migrates from baseline `d3eb45a2e046` to head `d49aa0000001`. <br> **Expected**: "Alembic migrations applied successfully" in logs. |
| 10 | Verify Alembic version stamped | Technical operator | `python -m alemc current` <br> **Expected**: `d49aa0000001` <br> Confirms migration chain was stamped correctly. |
| 11 | Run post-migration health checks | Technical operator | `/health` endpoint ✅ `{"status":"ok"}` <br> Verify 27 tables exist (per `init_db()`) <br> Verify `alembic_version` table contains `d49aa0000001`. |

**Step 2 Complete Checklist**:
- [ ] Fresh PostgreSQL 16 database provisioned (separate from Neon)
- [ ] `alembic upgrade head` executed successfully to `d49aa0000001`
- [ ] `alembic current` shows `d49aa0000001`
- [ ] Application health endpoint returns `ok`
- [ ] 27 tables verified in database
- [ ] `alembic_version` record stamped

---

### Step 3: Independent Schema and Permission Verification (Read-only verification)

| # | Action | Performed By | Verification Details |
|---|---|---|---|
| 12 | Spot-check table schemas | Technical operator | Verify key tables exist with expected columns: <br> `users` (id, email, status, etc.) <br> `positions` (id, user_id, symbol, etc.) <br> `strategy_executions` (id, user_id, etc.) <br> `trades` (id, user_id, etc.) |
| 13 | Verify migration identity privileges | Technical operator | If `STRIKENOVA_MIGRATION_DATABASE_URL` configured: <br> Confirm migrator role can run `alembic upgrade head` again (idempotent). <br> If single-identity: Confirm app connection role has sufficient privileges for schema already at head. |
| 14 | Verify no unexpected objects | Technical operator | Check for tables/columns/sequences that should not be present after migration from the approved commit only. |
| 15 | Verify migration lock state | Technical operator | `SELECT * FROM alembic_version;` <br> Confirm single row with `d49aa0000001`. <br> No stale lock records. |
| 16 | Run relevant existing tests | Technical operator | `pytest tests/test_alembic_migrations.py -v` <br> `pytest tests/test_db_migration.py -v` <br> Expect: all Pass (no regressions from migration). |

**Step 3 Complete Checklist**:
- [ ] Key tables verified with expected schemas
- [ ] Migration identity privileges confirmed (or single-identity verified)
- [ ] No unexpected database objects
- [ ] Migration lock state clean
- [ ] Existing tests pass (no regressions)

---

### Step 4: Render Staging Connection Configuration (Founder-authorized, separate from Neon)

| # | Action | Authorized By | Technical Details |
|---|---|---|---|
| 17 | Configure Render staging to use Neon (if desired) | Founder | This step addresses the Render/CockroachDB mismatch identified in the audit. <br> **Requirements**: <br> - Render service `srv-daj4vetg1s2s739ecvfg` must be authorized to connect to Neon <br> - Migration credentials (or `STRIKENOVA_MIGRATION_DATABASE_URL`) must be set in Render environment <br> - SSL configuration for Neon connection <br> - Connection pooling choice for production workload |
| 18 | Set Render environment variables for Neon | Founder (if authorizing Render-Neon integration) | `NEON_DATABASE_URL` <br> `STRIKENOVA_MIGRATION_DATABASE_URL` <br> `FREEBUFF_CLIENT_ID` / `FREEBUFF_ISSUER` (for auth) <br> `APP_ORIGIN` (development/production origin) |
| 19 | Deploy new Render configuration | Founder (if authorizing) | Render dashboard → Deploy changes <br> **Note**: Auto-deploy currently disabled on staging; manual deploy required. |
| 20 | Run Render staging smoke tests | Founder/Technical operator | Verify staging service starts with Neon backend <br> Run `/health` endpoint <br> Verify database connectivity <br> Confirm no CockroachDB errors |

**Step 4 Complete Checklist** (if Render-Neon integration authorized):
- [ ] Render environment variables set for Neon connection
- [ ] Render service deployed with Neon backend
- [ ] Smoke tests pass (service starts, health endpoint OK, DB connectivity)
- [ ] No CockroachDB errors in logs

**Step 4 is OPTIONAL** — the Founder may authorize Render-Neon integration separately, or may keep Render on CockroachDB with a different migration approach. This step is not required for Neon staging initialization alone.

---

### Step 5: Staging Deployment and Smoke Tests (Founder-authorized)

| # | Action | Performed By | Verification Details |
|---|---|---|---|
| 21 | Deploy application to staging environment | Founder/Operator | Deploy the application code tied to commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` <br> to the staging environment with the Neon backend configured. |
| 22 | Verify application startup | Technical operator | `railway service restart` <br> Or equivalent. <br> Check logs for: <br> - "Alembic migrations applied successfully" <br> - No errors from `init_db()` <br> - No "production database configuration is required" RuntimeError |
| 23 | Run browser smoke test checklist | Founder/Operator | (Manual or available browser automation) <br> 1. Landing page visible <br> 2. Auth flow works <br> 3. Dashboard loads with data <br> 4. GEX snapshots visible <br> 5. Paper trading accounts visible <br> 6. Portfolio data loads <br> 7. Settings accessible <br> 8. Logout works <br> 9. Re-login works <br> 10. `/health` returns `ok` |
| 24 | Monitor for 24+ hours (conditional) | Founder/Operator | Observe for errors, check `railway logs --service staging-backend | grep -i error` <br> Verify `GET /health` returns `ok` repeatedly <br> Verify no new error patterns emerge. |

**Step 5 Complete Checklist**:
- [ ] Application deployed to staging with Neon backend
- [ ] Application startup clean (no init_db errors)
- [ ] Smoke test checklist passed (or documented gaps)
- [ ] 24+ hour monitoring completed (if authorized) with no errors

---

## 3. Authorization Boundary Summary

| # | Action | Requires Founder Authorization? |
|---|---|---|
| 1-5 | Neon role setup (create roles, ALTER provider role, configure migration URL) | ✅ Yes (both Decisions A + B required) |
| 6-11 | Database migration (provision PG 16, run alembic, verify) | ✅ Yes (Steps 1 must complete first) <br> ✅ Technical execution after authorization |
| 12-16 | Schema/permission verification (read-only checks) | ❌ No (technical verification only) |
| 17-20 | Render-Neon integration (optional) | ✅ Yes (separate authorization) |
| 21-25 | Staging deployment and smoke tests | ✅ Yes (Founder authorizes deployment) |

---

## 4. Clear Identification of New Authorization Requirements

The following actions in this execution plan require **a new explicit Founder authorization** — they go beyond the two prerequisite decisions (A and B already discussed in this document):

| # | Action | Why It Requires New Authorization |
|---|---|---|
| 6 | Provision fresh PostgreSQL 16 database | Creates a new database resource; not Neon, not CockroachDB; requires operator access. |
| 8 | Set application role for migration | Changes the database identity the app uses; could affect all subsequent connections. |
| 17-20 | Render-Neon integration | Modifies Render service configuration; changes the Render/CockroachDB status quo; introduces new service dependencies. |
| 21 | Deploy application to staging | Triggers a deployment; changes the running service; requires confidence in all prior steps. |
| 22-25 | Smoke tests and monitoring | Validates the complete end-to-end flow; requires the prior steps to be correctly executed. |

**All other steps are either technical implementations (after authorization) or read-only verification.**

---

## 5. Final Status Matrix

| Condition | Status |
|---|---|
| Both Founder decisions authorized (A1 + B1) | `READY FOR EXECUTION` |
| Decision A authorized, Decision B pending | `BLOCKED — ROLE SECURITY DESIGN INCOMPLETE` |
| Decision B authorized, Decision A pending | `BLOCKED — APPLICATION IDENTITY NOT AUTHORIZED` |
| Neither decision authorized | `BLOCKED — REQUIRED VERIFICATION ENVIRONMENT UNAVAILABLE` |
| Render-Neon integration not authorized | `PARTIALLY READY — Neon staging initialized, Render separate` |
| Full execution completed | `READY — Neon staging operational with migration applied` |

---

## 6. Execution Prerequisites Checklist (Before Step 6)

| # | Prerequisite | Status | Action Required |
|---|---|---|---|
| 1 | Founder Decision A (staging commit) | ❌ Not authorized | Authorize `d49aa0000001` or alternative |
| 2 | Founder Decision B (privilege model) | ❌ Not authorized | Authorize Option A (create roles) or Option B (document gap) |
| 3 | Neon database accessible | ✅ Verified | Read-only inspection complete |
| 4 | Repository state verified | ✅ Verified | Commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` confirmed |
| 5 | Migration graph verified | ✅ Verified | 1 head: `d49aa0000001`, 31 revisions, PG16-compatible |
| 6 | Disposable PG 16 database ready | ❌ Not provisioned | Provision separate PostgreSQL 16 instance |
| 7 | `STRIKENOVA_MIGRATION_DATABASE_URL` configured | ❌ Not set | Configure if Option B1 chosen |
| 8 | Application code unchanged | ✅ Verified | Read-only; no modifications |
| 9 | Safety conditions verified | ✅ Verified | No Neon modifications, no migrations applied, no role changes |

**All prerequisites must be satisfied before this execution plan can be invoked.** The Founder must review and authorize the required decisions before proceeding.

---

**End of Neon Staging Execution Plan**