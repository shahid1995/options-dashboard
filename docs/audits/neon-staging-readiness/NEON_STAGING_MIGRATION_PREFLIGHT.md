# Neon Staging Migration Preflight

**Purpose**: Complete migration preflight for the Founder-approved application commit against a fresh PostgreSQL 16 database.

**Scope**: Read-only inspection and static analysis; no migrations applied, no roles changed, no database modified.

**Founder-approved application target** (application version approval: **COMPLETE**):
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Alembic head: `d49aa0000001`
- Target: Existing empty Neon PostgreSQL 16 database `strikenova_staging` (project `gentle-field-42671929`) — not an unrelated newly provisioned DB

**Authorization gates (separate):**
- Application version: **COMPLETE**
- Staging privilege model: **PENDING** (separate Founder authorization)
- Neon migration execution: **PENDING** (separate Founder authorization)
- Render-Neon integration & deployment: **PENDING** (separate Founder authorization)

---

## 1. Approved Target Confirmation

| Item | Value | Verified |
|---|---|---|
| Repository SHA | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` | ✅ Git commit exists |
| Branch | `feat/strikenova-day35-portfolio-intelligence` | ✅ Remote-tracking branch exists |
| Alembic head at this commit | `d49aa0000001` | ✅ Migration graph analysis confirmed |
| Migration ancestry complete | ✅ 31 revisions from baseline `d3eb45a2e046` to `d49aa0000001` | ✅ Confirmed |
| Multiple heads exist in repo graph | ⚠️ Yes (repo has multiple heads), target head is `d49aa0000001` | ✅ Confirmed |
| Application code corresponds to migration history | ✅ Models registered on Base.metadata; all tables covered | ✅ Confirmed |
| Render auto-deploy status | Disabled; no new deployment authorized | ✅ Confirmed |
| Render uses CockroachDB (not Neon) | ✅ Verified | ✅ Confirmed |

---

## 2. Migration History (Corrected from Exact Git Trees)

| Commit | Migration files | Relevant head | Notes |
|---|---:|---|---|
| Historical Render deployment `b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba` | 27 | `d46aa0000001` (on extended line; repo has multiple heads) | Does **not** contain `d47aa0000001`, `d48aa0000001`, `d48aa0000002`, `d49aa0000001` |
| Approved staging target `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` | 31 | `d49aa0000001` | Adds 4 migrations: `d47aa0000001` → `d48aa0000001` → `d48aa0000002` → `d49aa0000001` extending linearly from `d46aa0000001`. Graph reconstruction handled tuple/list `down_revision` values and all merge parents. |

**Final head:** `d49aa0000001` (single head on the target upgrade path via `alembic upgrade head`).

---

## 3. PostgreSQL 16 Compatibility (Static Analysis)

- No `CREATE EXTENSION` requiring CRDB-specific extensions; no CRDB-specific SQL observed.
- Standard Alembic operations throughout.
- Dialect guards handle PostgreSQL/CockroachDB/SQLite.
- Data migration `d49aa0000001` is batched and idempotent-style; on empty DB it is a no-op.
- `d9e0f1a2b3c4` creates UNIQUE index on `(broker, broker_account_id)` — succeeds on fresh empty DB; could abort if duplicates exist.

**Compatibility:** The chain is PG16-compatible for a fresh empty database.

---

## 4. CI/Rehearsal Evidence (Qualification)

Existing GitHub Actions runs succeeded at commit `a016887f61289bc9c64edaabed45d3e1559038f9`:
- PostgreSQL compatibility: [https://github.com/shahid1995/options-dashboard/actions/runs/37575586698](https://github.com/shahid1995/options-dashboard/actions/runs/37575586698)
- PostgreSQL and CockroachDB migration rehearsal: [https://github.com/shahid1995/options-dashboard/actions/runs/37575586664](https://github.com/shahid1995/options-dashboard/actions/runs/37575586664)

**Qualification:**
- Relevant to approved target because app and migration files are unchanged between `a016887` and `9dc6e15` (only 2 intervening documentation/governance commits).
- Not performed at exact `9dc6e15` SHA.
- Not a Neon-specific execution or Neon role-privilege rehearsal.
- No migration has been applied to Neon.

---

## 5. Prerequisites for Migration Execution (Not Yet Authorized)

| # | Prerequisite | Status | Notes |
|---|---|---|---|
| 1 | Authorized application commit | **COMPLETE** | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` (head `d49aa0000001`) |
| 2 | Alembic head confirmed | ✅ Verified | `d49aa0000001` |
| 3 | Target database identified | ✅ Existing empty Neon DB | `strikenova_staging` on project `gentle-field-42671929` (existing empty DB) |
| 4 | Connection config | ⚠️ Pending | Use application's `DATABASE_URL` (and optionally `STRIKENOVA_MIGRATION_DATABASE_URL` per ADR-016). Do **not** invent `NEON_DATABASE_URL`. |
| 5 | Role privileges | ⚠️ Pending separate auth | See `NEON_STAGING_PRIVILEGE_PROPOSAL.md`; may use existing identity or new scoped identities |
| 6 | Migration lock / ownership understood | ✅ Documented | ADR-017 lock uses migration identity; ADR-018 requires migrator-creator default privileges if using two-identity model |
| 7 | Render/Neon integration | ⚠️ Pending separate auth | Render currently uses CockroachDB; no integration/deployment changes authorized |
| 8 | Migration execution authorization | ⚠️ Pending separate auth | No migrations applied to Neon |

**Final status before execution:** `PREFLIGHT COMPLETE — APPLICATION APPROVED; PRIVILEGE/MIGRATION/INTEGRATION AUTHORIZATIONS PENDING`

---

## 6. What Remains Outside Scope (Explicit)

| Item | Status | Reason |
|---|---|---|
| Neon role creation/modification | Outside scope | No roles created/granted/altered |
| Neon migrations/schema init | Outside scope | No migrations applied |
| CockroachDB activation/changes | Outside scope | Prohibited; Render uses CockroachDB but no changes made |
| Render env/deployment changes | Outside scope | No changes; auto-deploy disabled |
| Application source/migration/config changes | Outside scope | Read-only |
| GitHub branch/PR/status changes | Outside scope | No changes |
| Merging/marking PRs ready | Outside scope | Not authorized |

---

**End of Migration Preflight**
