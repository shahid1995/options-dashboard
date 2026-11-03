# Neon Staging Privilege Proposal

**Purpose**: Propose a staging-specific least-privilege database-role architecture for the Neon `strikenova_staging` database, in advance of migration execution.

**Scope**: Read-only inspection; no roles created, altered, or granted.

**Founder-approved application target**:
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Alembic head: `d49aa0000001`

---

## 1. Actual Existing Role State (Neon `strikenova_staging` database, read-only inspection)

| Role | LOGIN | SUPERUSER | CREATEDB | CREATEROLE | REPLICATION | BYPASSRLS | Purpose |
|---|---|---|---|---|---|---|---|
| `cloud_admin` | True | True | True | True | True | True | Neon internal — superuser |
| `neon_service` | True | False | True | True | True | True | Neon internal |
| `neon_superuser` | False | False | True | True | True | False | Neon internal |
| `strikenova_staging_owner` | True | False | True | True | **True** | **True** | Provider-created, connection-holding role |
| All `pg_*` / `neon_*` / `cloud_*` roles | System roles | — | — | — | — | — | PostgreSQL/Neon infrastructure |

**Key observation**: The only application-present role is `strikenova_staging_owner`, which has `replication=True` and `bypass_rls=True` — attributes that exceed what is needed for a staging connection-holder. The three documented privilege-model roles (`strikenova_prod_migrator`, `strikenova_production_app`, `strikenova_prod_admin`) are **NOT present** in `pg_roles`.

**No accepted database-role contract was located** in the repository. `DATA.md` §5 describes paper-trading data, not database login roles or grants. No DECISIONS.md, INVARIANTS.md, or ADRs referencing database privilege model were found.

---

## 2. Repository Requirements (from approved commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`)

### 2.1 Application Database Identity Usage

The application currently uses a **single-identity architecture**:

- **`DATABASE_URL`** is the sole connection string for all operations:
  - Application request-handling (FastAPI routes)
  - Startup-time Alembic migration (`init_db()` → `alembic upgrade head`)
  - Migration lease locking (`_migration_lock_url()`)
  - Session management, data CRUD, health checks

- No separate `STRIKENOVA_MIGRATION_DATABASE_URL` is configured in the selected commit.
- The `resolve_migration_database_url()` function in `db.py` falls back to `DATABASE_URL` when `STRIKENOVA_MIGRATION_DATABASE_URL` is not set.
- Migration identity separation (ADR-016/ADR-017) is designed but **not activated** — the application operates under single-identity semantics.

### 2.2 Migration Architecture

- Alembic is the sole authoritative schema management mechanism.
- Startup sequence: `alembic upgrade head` → idempotent data backfill → SQLite-only composite indexes.
- Migration lock uses `_migration_lock_url()` which resolves to the same `DATABASE_URL` when `STRIKENOVA_MIGRATION_DATABASE_URL` is unset.
- The migration chain at the approved commit has exactly 1 head: `d49aa0000001`.
- The migration graph (31 revisions) is linear from baseline `d3eb45a2e046` through all intermediate revisions, with merge points at `a3b4c5d6e7f8` and `3f8a2e9c4d5b`.

### 2.3 Code Review Key Files

- `backend/app/db.py`: Engine creation, URL normalization, `validate_production_config()`, `validate_migration_state()`, `resolve_migration_database_url()`, `_run_alembic_migrations()`, `init_db()`
- `backend/alembic/env.py`: URL resolution, model imports, `run_migrations_offline()`/`run_migrations_online()`
- `backend/alembic.ini`: Alembic configuration, `sqlalchemy.url` resolved dynamically
- `backend/alembic/versions/`: 31 migration files, head = `d49aa0000001`
- `backend/app/models.py`: SQLAlchemy models (24 tables + `greeks_checkpoint` CLI-only)
- `backend/app/identity.py`: User, UserSession models

### 2.4 Identity Separation Capability

The application **does not currently support** separate identities for:
- Runtime application connections
- Migration execution
- Migration locking

All database operations use the same `DATABASE_URL` identity. If a separate migrator role is created, the application would need `STRIKENOVA_MIGRATION_DATABASE_URL` configured and the migration code path updated to use it — which exceeds the "do not modify application source code" constraint.

---

## 3. Proposed Staging-Specific Role Architecture

**Proposal**: Design a least-privilege role model for Neon staging. This is a **proposed design**, not an accepted policy. The Founder must authorize implementation.

### 3.1 Proposed Identities

| # | Proposed Name | Purpose | LOGIN | Required Memberships | Database Privileges | Schema Privileges | Table Privileges | Sequence Privileges | Ownership | Required Default Privileges | Prohibited Privileges | App Compatible w/o Code Change? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `strikenova_staging_reader` | Read-only application data access | Yes | Public role (granted via NEON_CONNECTION) | SELECT on all tables | USAGE on all schemas | SELECT on all tables | N/A | None | No SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS | ✅ Yes (if app only reads) |
| 2 | `strikenova_staging_writer` | Application DML (INSERT/UPDATE/DELETE) | Yes | Public role | INSERT, UPDATE, DELETE on all tables | USAGE on all schemas | INSERT, UPDATE, DELETE on all tables | N/A | None | No SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS | ✅ Yes (standard app DML) |
| 3 | `strikenova_staging_migrator` | Schema DDL via Alembic migration identity | Yes (short-lived) | Temporary, revoked after migration | CREATE TABLE, CREATE INDEX, CREATE VIEW, ALTER, DROP on all schemas | USAGE on all schemas | All schema-level privileges | N/A | None | No REPLICATION/BYPASSRLS; no SUPERUSER/CREATEDB/CREATEROLE | ⚠️ Conditional (requires `STRIKENOVA_MIGRATION_DATABASE_URL`) |
| 4 | `strikenova_staging_admin` | Break-glass access through Neon account only | Yes (via Neon owner) | Neon provider account only | All privileges (rate-limited by Neon provider) | All schemas | All tables | All sequences | Table/row ownership as needed | No REPLICATION/BYPASSRLS for app identities | ✅ Yes (via Neon account, not DB role) |

### 3.2 Privilege Rationale

**Why separate roles?**

1. **Application DML guardrails**: A `strikenova_staging_writer` role with only INSERT/UPDATE/DELETE privileges prevents accidental DDL execution through the application connection. This is the primary security benefit — the app cannot `CREATE TABLE`, `CREATE INDEX`, or `ALTER TABLE` through its normal connection path.

2. **Migration separation**: A `strikenova_staging_migrator` role enables schema DDL when `STRIKENOVA_MIGRATION_DATABASE_URL` is configured, so the runtime credential does not need schema-DDL/ownership rights. This is the ADR-016/ADR-017 design pattern — but it requires the application to be configured with a separate migration URL, which the selected commit does not activate.

3. **No REPLICATION/BYPASSRLS on application identities**: Neither `strikenova_staging_reader` nor `strikenova_staging_writer` should have `replication=True` or `bypass_rls=True`. These attributes are unnecessary for application connectivity and increase attack surface. The current `strikenova_staging_owner` has both, which is excessive.

4. **Break-glass via Neon account, not DB role**: Administrative access should remain through the authorized Neon provider account, not through a database role with unrestricted privileges. This preserves the audit trail and limits exposure.

### 3.3 SQL Proposals (Review-Only, Not Executed)

**3.3.1 Create staging reader role**

```sql
-- Create a reader role for application data access
-- Does NOT have REPLICATION or BYPASSRLS
-- Does NOT have CREATE/DROP/ALTER capabilities
CREATE ROLE strikenova_staging_reader LOGIN;

-- Grant SELECT on all tables in the public schema
GRANT CONNECT ON DATABASE TO strikenova_staging_reader;
GRANT USAGE ON SCHEMA public TO strikenova_staging_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO strikenova_staging_reader;

-- Set default privileges for future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO strikenova_staging_reader;
```

**3.3.2 Create staging writer role**

```sql
-- Create a writer role for application DML
-- Does NOT have REPLICATION or BYPASSRLS
-- No CREATE/DROP/ALTER capabilities
CREATE ROLE strikenova_staging_writer LOGIN;

-- Grant CONNECT and USAGE
GRANT CONNECT ON DATABASE TO strikenova_staging_writer;
GRANT USAGE ON SCHEMA public TO strikenova_staging_writer;

-- Grant Data manipulation on all tables
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO strikenova_staging_writer;

-- Set default privileges for future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_staging_writer;
```

**3.3.3 Create staging migrator role (conditional)**

```sql
-- Create a migrator role for Alembic schema operations
-- Only usable when STRIKENOVA_MIGRATION_DATABASE_URL is configured
-- Does NOT have REPLICATION or BYPASSRLS
-- No SUPERUSER, CREATEDB, CREATEROLE
CREATE ROLE strikenova_staging_migrator LOGIN;

-- Grant schema DDL capabilities needed by Alembic
GRANT CONNECT ON DATABASE TO strikenova_staging_migrator;
GRANT USAGE ON SCHEMA public TO strikenova_staging_migrator;

-- Grant DDL privileges for migration operations
-- These are table-specific and schema-specific, not global
GRANT CREATE ON SCHEMA public TO strikenova_staging_migrator;
GRANT CREATE TEMPORARY TABLES ON SCHEMA public TO strikenova_staging_migrator;

-- For index creation during migration:
-- GRANT CREATE INDEX ON ALL TABLES IN SCHEMA public TO strikenova_staging_migrator (if supported)

-- Note: Does NOT grant ownership, SUPERUSER, CREATEDB, CREATEROLE, REPLICATION, or BYPASSRLS
```

**3.3.4 Revoke excessive privileges from provider role**

```sql
-- Note: This requires ALTER ROLE, which is outside audit scope.
-- Documented for Founder awareness.

-- The provider-created strikenova_staging_owner currently has:
-- replication=True, bypass_rls=True

-- These attributes are excessive for a staging connection-holder.
-- Modifying them is outside the read-only audit scope.
-- Founder should decide whether to ALTER the provider role after
-- the new staging roles are implemented.
```

### 3.4 Application Compatibility Assessment

| Role | App Compatible w/o Code Change? | Required Config Change | Rationale |
|---|---|---|---|
| `strikenova_staging_reader` | ✅ Yes | None (if app only reads) | App can connect with read-only credentials; no source changes needed if app DML is not required |
| `strikenova_staging_writer` | ✅ Yes | None (if app does standard DML) | App already uses INSERT/UPDATE/DELETE via ORM; no code changes needed |
| `strikenova_staging_migrator` | ⚠️ Conditional | ⚠️ Set `STRIKENOVA_MIGRATION_DATABASE_URL` | Requires application to configure separate migration URL. The selected commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` does NOT set this variable. Without it, the migrator role cannot be used through the application path. |
| `strikenova_staging_admin` | ✅ Yes (via Neon account) | None | Access through Neon provider account, not a database role. No code changes needed. |

**Critical finding**: The selected commit operates under **single-identity semantics**. The `strikenova_staging_writer` and `strikenova_staging_reader` roles are compatible without code changes **only if** the application connects using a role-prefixed connection string or if the application's ORM/middleware automatically sets the search path and role. If the application uses a bare `DATABASE_URL` without role configuration, the connection will use the default `strikenova_staging_owner` role (or `postgres` depending on Neon setup).

**Recommendation**: The application would need either:
- (a) Role configuration in the DATABASE_URL (`postgresql+psycopg://user:password@host/db?role=strikenova_staging_writer`), or
- (b) Middleware that sets `SET ROLE strikenova_staging_writer` after connection,

Neither of which is present in the selected commit. The privilege model design assumes future configuration, not immediate code changes.

---

## 4. Implementation Prerequisites (Founder-Authorized)

| Prerequisite | Status | Founder Action Required |
|---|---|---|
| Authorize staging application commit | ✅ Approved | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` |
| Authorize privilege model design | ⚠️ Proposed | Review and approve Option A or Option B below |
| Configure `STRIKENOVA_MIGRATION_DATABASE_URL` | ❌ Not configured | Required for migrator role usage |
| Apply new roles to Neon database | ❌ Not done | Requires separate ALTER/GRANT operations |
| Remove `replication=True, bypass_rls` from `strikenova_staging_owner` | ❌ Not done | Requires ALTER ROLE (Founder-authorized) |
| Migration preflight verification | ⚠️ In progress | See `NEON_STAGING_MIGRATION_PREFLIGHT.md` |

---

## 5. Proposer's Decision Points (Founder Must Authorize)

### Option A: Implement Full Privilege Model

Create all four roles (`strikenova_staging_reader`, `strikenova_staging_writer`, `strikenova_staging_migrator`, `strikenova_staging_admin`) with the documented grants. This enables:
- Proper separation between migration DDL and application DML
- Read/write isolation for application connections
- Break-glass access through Neon account only
- ADR-016/ADR-017 migration identity separation (when `STRIKENOVA_MIGRATION_DATABASE_URL` is configured)

**Required Founder action**: Authorize role creation and ALTER of provider role.

### Option B: Document Actual Configuration

Acknowledge that the privilege model is not applicable to this Neon staging setup and document the actual role configuration in use (currently only `strikenova_staging_owner` exists with `replication=True, bypass_rls=True`). The application operates under single-identity semantics without role separation.

**Required Founder action**: Authorize documentation of the gap and either (i) accept the current configuration, or (ii) authorize the implementation work to add the privilege model in a future phase.

### Without a Founder decision on either option, the status remains:

`BLOCKED — ROLE SECURITY DESIGN INCOMPLETE`

**Proposed next step**: Founder to review this proposal and authorize Option A or Option B before migration preflight can be completed.

---

**End of Neon Staging Privilege Proposal**