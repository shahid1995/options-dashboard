# Neon Staging Privilege Proposal

**Purpose**: Propose a staging-specific least-privilege database-role architecture for the Neon `strikenova_staging` database, consistent with accepted ADR-016/017/018.

**Scope**: Read-only inspection; no roles created, altered, or granted. SQL statements are review-only proposals requiring separate Founder authorization.

**Founder-approved application target**:
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Alembic head: `d49aa0000001` (application version approval: **COMPLETE**)

**Authorization gates (separate):**
- Staging privilege model implementation: **PENDING** (separate Founder authorization)
- Neon migration execution: **PENDING** (separate Founder authorization)
- Render-Neon integration & deployment: **PENDING** (separate Founder authorization)

---

## 1. Accepted Architecture (ADR-016/017/018)

Per DECISIONS.md (accepted):

- **ADR-016 (Runtime/migration database identity separation, opt-in):** `STRIKENOVA_MIGRATION_DATABASE_URL` runs Alembic under a dedicated migration identity; runtime credential can be restricted to DML with no schema ownership/DDL. When unset, behavior is identical to historical single-identity model.
- **ADR-017 (Migration serialization via transactional lease lock):** Single-row transactional lease lock (`_migration_lock`) acquired around Alembic execution using the migration identity URL. Bootstraps on empty database. Configuration requires `MIGRATION_LOCK_TTL_SECONDS >= 2`.
- **ADR-018 (Production future-table privilege defaults, migrator-creator scope):** Default privileges bound to the migration/creator role so future tables/sequences receive runtime DML (+ USAGE) privileges. Grant is DML-only; runtime never receives CREATE/ALTER/DROP/ownership/admin/role-management privileges.

**Application support (verified):** `config.py` defines `STRIKENOVA_MIGRATION_DATABASE_URL`; `db.py` resolves migration URL via `resolve_migration_database_url()` (precedence: explicit/CLI → `STRIKENOVA_MIGRATION_DATABASE_URL` → `DATABASE_URL`), provides `_migration_engine_url()`, `_migration_lock_url()` (both use migration resolver), and `_run_alembic_migrations()` uses migration identity when configured; `alembic/env.py` delegates to `resolve_migration_database_url()`; `app/_migration_lock.py` implements transactional lease lock with bootstrap on empty DB. **No application source changes required to enable separation.**

`DATA.md` §5 describes paper-trading data, not database roles/grants. The privilege model is governed by accepted ADR-016/017/018, not `DATA.md`.

---

## 2. Actual Existing Role State (Neon `strikenova_staging` database, read-only)

| Role | LOGIN | SUPERUSER | CREATEDB | CREATEROLE | REPLICATION | BYPASSRLS | Purpose |
|---|---|---|---|---|---|---|---|
| `cloud_admin` | True | True | True | True | True | True | Neon internal — superuser |
| `neon_service` | True | False | True | True | True | True | Neon internal |
| `neon_superuser` | False | False | True | True | True | False | Neon internal |
| `strikenova_staging_owner` | True | False | True | True | **True** | **True** | Provider-created, connection-holding role |
| `pg_*`/`neon_*`/`cloud_*` | System roles | — | — | — | — | — | PostgreSQL/Neon infrastructure |

**Observations:**
- Only `strikenova_staging_owner` exists as an application-present role.
- `strikenova_staging_owner` has `replication=True, bypass_rls=True` (excessive for typical application connectivity). 
- Documented privilege-model roles (`strikenova_prod_migrator`, `strikenova_production_app`, `strikenova_prod_admin`) are **NOT present** in Neon.
- The provider-created `strikenova_staging_owner` may safely remain a provider/setup identity outside normal application runtime if replaced by properly scoped identities, but altering it is not recommended without a documented technical requirement.

---

## 3. Staging-Specific Two-Identity Model (Proposed)

Following ADR-016/017/018, staging should use two identities where possible:

1. **Migration/creator identity** (used for Alembic migrations and migration lock bootstrap/ownership) — needs schema DDL, ability to create/manage objects and run lock bootstrap on empty DB, and must be the role bound by `ALTER DEFAULT PRIVILEGES` for future tables/sequences (per ADR-018).
2. **Runtime DML identity** (used by application at runtime) — needs only CONNECT/USAGE, DML on existing/future tables, and USAGE on sequences. No DDL, no ownership, no role-management, no cluster-admin privileges.

A separate read-only login role is **not proposed by default** unless there is an actual application requirement. Administrative/break-glass access should remain through the authorized Neon provider account, not a separate broadly privileged database login role created ad hoc.

**Staging naming (do not mix prod names):** Use `strikenova_staging_migrator` / `strikenova_staging_app` (or equivalent staging-scoped names). Do not copy `strikenova_prod_*` names without explicit mapping/explanation.

---

## 4. PostgreSQL 16 SQL Requirements (Correctness)

- `TEMPORARY` is a **database privilege**, not a schema privilege. Do not propose `GRANT CREATE TEMPORARY TABLES ON SCHEMA public ...` as a schema privilege grant.
- `CREATE` on a schema does not grant ownership rights over existing tables.
- Table ownership affects ALTER/DROP and index management.
- Default privileges must be configured for the **actual migration creator role** using `ALTER DEFAULT PRIVILEGES FOR ROLE <migrator_role> IN SCHEMA public ...`
- Must address both **future tables** and **future sequences** (USAGE/SELECT/UPDATE as appropriate per ADR-018).
- Treat existing objects and future objects separately.
- Migration role must be able to bootstrap `_migration_lock` on an empty database and own/manage objects created by migrations.
- Runtime identity must receive only necessary DML and sequence privileges; no DDL, ownership, role-management, or cluster-administration.
- Do **not** propose a standalone `CREATE INDEX` privilege — correct ownership model is required.
- Do **not** label SQL as tested unless actually tested. If a disposable PG16 environment is available, rehearsal may only occur in isolated scratch environment (never Neon/CockroachDB); otherwise mark untested with exact evidence gap.

---

## 5. Review-Only Proposed SQL (Template, Not Executed)

*These are templates for review only. Actual role names, ownership, and grants must be validated against the target database state. Requires separate Founder authorization.* 

```sql
-- Migration/creator identity (staging)
CREATE ROLE strikenova_staging_migrator LOGIN;

-- Grant connection/schema usage
GRANT CONNECT ON DATABASE <staging_db_name> TO strikenova_staging_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO strikenova_staging_migrator;

-- (Optional) Ensure migrator can manage lock table; ownership comes naturally if created by it

-- Runtime DML identity (staging)
CREATE ROLE strikenova_staging_app LOGIN;

GRANT CONNECT ON DATABASE <staging_db_name> TO strikenova_staging_app;
GRANT USAGE ON SCHEMA public TO strikenova_staging_app;

-- Grant on existing objects if needed (separate from defaults)
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO strikenova_staging_app;
-- GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO strikenova_staging_app;

-- Default privileges for FUTURE objects created by migrator role (ADR-018)
ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_staging_app;
ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO strikenova_staging_app;
```

**Notes on correctness:**
- Default privileges are bound to the creator role (`FOR ROLE strikenova_staging_migrator`). If migrations run under a different role, defaults must be adjusted.
- Existing objects require explicit grants; defaults only affect future objects.
- Migration role needs `CREATE` on schema to create tables/indexes as needed; ownership model follows ADR-018.
- Do not grant `REPLICATION` or `BYPASSRLS` to application identities.
- TEMPORARY is controlled differently (database privilege) — not granted via `ON SCHEMA public` in this form.
- Administrative access remains via Neon provider account (no broad admin login role proposed here).

---

## 6. Implementation Notes & Gaps

- **Activation:** To use separate identities, set `STRIKENOVA_MIGRATION_DATABASE_URL` to the migrator connection string; runtime uses `DATABASE_URL` as the app identity. This is supported by existing code.
- **Migration lock:** Lock uses migration identity URL (ADR-017); bootstrap works on empty DB.
- **No code changes required** to enable the model; only configuration (env vars) and role/grant setup (authorized separately).
- **Testing:** Role behavior untested unless rehearsed in isolated scratch PostgreSQL 16 environment. Neon/CockroachDB must not be used for rehearsal. If unavailable, evidence gap must remain explicit.
- **Provider role:** `strikenova_staging_owner` has `replication=True, bypass_rls=True` — review necessity; do not alter without documented technical requirement.

---

## 7. Founder Decision Required

**This proposal requires separate Founder authorization.** Options:
- **Option A:** Authorize creation of staging migrator/app identities with grants per ADR-016/017/018 and configure `STRIKENOVA_MIGRATION_DATABASE_URL` / `DATABASE_URL` appropriately.
- **Option B:** Document the gap and operate with current single-identity configuration (and note security implications).

No roles have been created or modified as part of this audit.
