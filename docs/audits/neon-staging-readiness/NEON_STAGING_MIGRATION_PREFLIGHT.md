# Neon Staging Migration Preflight

**Purpose:** Migration preflight for the Founder-approved application commit against the existing empty PostgreSQL 16 Neon database.

**Scope:** Documentation and static analysis only. No migrations applied, no roles changed, no database contacted.

**Founder-approved application target** (application version approval: **COMPLETE**):
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Expected Alembic head: `d49aa0000001`
- Target: existing empty Neon PostgreSQL 16 database `strikenova_staging` (project `gentle-field-42671929`) — **not** an unrelated newly provisioned database

**Authorization gates (separate):**
| # | Gate | Status |
|---|---|---|
| 1 | Application version | **COMPLETE** |
| 2 | Staging privilege implementation | **PENDING** |
| 3 | Neon migration execution | **PENDING** |
| 4 | Render–Neon integration & deployment | **PENDING** |

---

## 1. Approved Target Confirmation

| Item | Value | Basis |
|---|---|---|
| Repository SHA | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` | Commit exists; was the branch tip at publication (`git ls-remote`, 2026-11-03) |
| Branch | `feat/strikenova-day35-portfolio-intelligence` | Remote-tracking branch confirmed |
| Alembic head at this commit | `d49aa0000001` | **Static graph reconstruction** from the pinned tree (Alembic CLI not executed) |
| Head count at this commit | **Exactly 1** (`d49aa0000001`) | Static reconstruction: no revision lists `d49aa0000001` as a parent |
| Migration files at this commit | **31** | `git ls-tree -r` on the pinned tree |
| Migration ancestry | Complete from baseline `d3eb45a2e046` to `d49aa0000001` | Static reconstruction |
| `alembic upgrade head` ambiguity | **None** — single head at this tree | Static reconstruction; not executed |
| Application code alignment | Models registered on `Base.metadata`; tables covered by the chain | Static inspection |
| Render auto-deploy | Disabled; no deployment authorized | Render service metadata |
| Render database provider | CockroachDB (not Neon) | Render startup logs, 2026-11-02 |

---

## 2. Migration History (Reconstructed from Exact Git Trees)

| Commit | Migration files | Heads (static reconstruction) | Notes |
|---|---:|---|---|
| Historical Render deployment `b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba` | **27** | `d46aa0000001` — **exactly 1 head** | Does **not** contain `d47aa0000001`, `d48aa0000001`, `d48aa0000002`, or `d49aa0000001`. Corroborated by ADR-016's validation record ("the full 27-revision chain from empty to `d46aa0000001`"). |
| Approved staging target `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` | **31** | `d49aa0000001` — **exactly 1 head** | Adds four migrations extending the chain linearly: `d46aa0000001` → `d47aa0000001` → `d48aa0000001` → `d48aa0000002` → `d49aa0000001`. No file removed; one existing file (`e5f6a7b8c9d0`) changed content without changing its `revision`/`down_revision`. |

**Why `merge_day38_gex` is not a head.** `f7aa24156f6d` declares `down_revision = ('9b675f8a3af0', 'merge_day38_gex')`, and `a7c1d9e4f2b8` declares `down_revision = f7aa24156f6d`. `merge_day38_gex` (parents `b2c3d4e5f6a7`, `e8f9a0b1c2d3`) therefore has a descendant and is not a head at either pinned tree. Earlier revisions that reported it as a head mis-parsed a tuple-form `down_revision`.

**Method and limitation.** Heads were derived by parsing `revision`, `down_revision`, and `depends_on` as Python literals (string, tuple, and list forms), collecting every parent, and treating a revision as a head only when no other revision names it. No migration file in either tree declares `depends_on`. **The Alembic CLI and `ScriptDirectory.get_heads()` were not executed.** No `alembic heads`, `alembic history`, or `alembic upgrade` was run, and no database was contacted.

**Target head for upgrade:** `d49aa0000001`. At this tree there is exactly one head, so `alembic upgrade head` is unambiguous. This is a static conclusion and does not authorize executing it.

---

## 3. PostgreSQL 16 Compatibility (Static Analysis)

- No `CREATE EXTENSION` requiring CockroachDB-only extensions; no CockroachDB-only SQL on the PostgreSQL path.
- Standard Alembic operations throughout (`op.create_table`, `op.add_column`, `op.execute`).
- Dialect guards distinguish PostgreSQL / CockroachDB / SQLite.
- `d49aa0000001` normalizes IV observation timestamps to IST in batches and is a no-op on an empty database.
- `d9e0f1a2b3c4` creates a global UNIQUE index on `(broker, broker_account_id)`: succeeds on a fresh empty database; would abort if duplicates exist.
- `e5f6a7b8c9d0` drops the legacy `uq_users_broker_identity` constraint. Between the two pinned trees this file gained a CockroachDB branch; the PostgreSQL 16 branch is unchanged (`ALTER TABLE users DROP CONSTRAINT IF EXISTS …`).
- Schema `public` in PostgreSQL 15+ no longer grants `CREATE` to `PUBLIC`; the migration identity needs explicit `USAGE, CREATE` (see `NEON_STAGING_PRIVILEGE_PROPOSAL.md` §4.7).

**Compatibility conclusion:** the chain is PostgreSQL 16-compatible for a fresh empty database, by static inspection only. No migration was executed.

---

## 4. CI / Rehearsal Evidence (Qualified)

Successful GitHub Actions runs exist at commit `a016887f61289bc9c64edaabed45d3e1559038f9`:
- PostgreSQL compatibility — [run 37575586698](https://github.com/shahid1995/options-dashboard/actions/runs/37575586698)
- PostgreSQL and CockroachDB migration rehearsal — [run 37575586664](https://github.com/shahid1995/options-dashboard/actions/runs/37575586664)

**Qualifications — read these before citing the runs:**
- The application and migration files are identical between `a016887` and `9dc6e15`: all 31 migration files have identical blob SHAs, and the two intervening commits changed only six governance/documentation files. *(This was re-derived from the exact tree comparison.)*
- **Not** executed at the exact `9dc6e15` SHA.
- **Not** a Neon-specific run, and **not** a Neon role-privilege rehearsal.
- The CI runs covered the 27-revision chain that existed at `a016887`; the four revisions added by `9dc6e15` were present in the tree (`d47aa0000001` through `d49aa0000001`) but were **not** part of any migration execution in those runs.
- No migration has been applied to Neon.

---

## 5. Prerequisites for Migration Execution (Not Yet Authorized)

| # | Prerequisite | Status | Notes |
|---|---|---|---|
| 1 | Authorized application commit | **COMPLETE** | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`, expected head `d49aa0000001` |
| 2 | Alembic head determined | ✅ Static reconstruction | `d49aa0000001`, single head at this tree; CLI not executed |
| 3 | Target database identified | ✅ Identified | Existing empty Neon `strikenova_staging`, project `gentle-field-42671929` |
| 4 | Connection configuration | ⚠️ Pending | Use `DATABASE_URL` (runtime) and, if two-identity is authorized, `STRIKENOVA_MIGRATION_DATABASE_URL` (migrator). Do **not** invent `NEON_DATABASE_URL`. |
| 5 | Role privileges | ⚠️ Pending gate 2 | See `NEON_STAGING_PRIVILEGE_PROPOSAL.md` |
| 6 | Migration lock / ownership understood | ✅ Documented | ADR-017 lease uses the migration identity; ADR-018 binds defaults to the migrator/creator |
| 7 | Render–Neon integration | ⚠️ Pending gate 4 | Render currently uses CockroachDB; no integration change authorized |
| 8 | Migration execution authorization | ⚠️ Pending gate 3 | No migration applied to Neon |
| 9 | Provider/role credentials provisioned | ⚠️ Pending gate 2 | Must come from an approved provider/secret-management process; never committed |

**Status before execution:** `PREFLIGHT COMPLETE — APPLICATION VERSION APPROVED; PRIVILEGE, MIGRATION, AND INTEGRATION AUTHORIZATIONS PENDING`

---

## 6. Explicitly Outside Scope

| Item | Status | Reason |
|---|---|---|
| Neon role creation / modification | Not performed | No roles created, granted, altered, or dropped |
| Neon migrations / schema initialization | Not performed | No migration applied; nothing written |
| CockroachDB activation or change | Not performed | Prohibited; Render remains on CockroachDB unchanged |
| Render environment / deployment change | Not performed | Auto-deploy disabled; no change authorized |
| Application source, migration, or config change | Not performed | Read-only |
| GitHub branch / PR / status change | Not performed | No branch or PR state changed; nothing merged |
| Credentials | Not accessed | No secret read, printed, or committed |

---

**End of Migration Preflight**
