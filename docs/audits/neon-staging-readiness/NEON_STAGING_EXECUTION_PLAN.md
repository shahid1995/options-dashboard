# Neon Staging Execution Plan

**Purpose:** Numbered plan for Neon staging privilege setup and migration, describing what happens **after** separate Founder authorization. **No step in this plan has been executed.**

**Scope:** Planning only. No database change, no role change, no deployment, no credential access.

**Founder-approved application target** (application version approval: **COMPLETE**):
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Expected Alembic head: `d49aa0000001`

**Target database:** existing empty Neon PostgreSQL 16 database `strikenova_staging` (project `gentle-field-42671929`), AWS Singapore. **Not** a newly provisioned unrelated database, and **not** a disposable rehearsal instance.

**Authorization gates — all separate, each requiring explicit authorization:**

| # | Gate | Status |
|---|---|---|
| 1 | Application version | **COMPLETE** (`9dc6e15`, expected head `d49aa0000001`) |
| 2 | Staging privilege implementation | **PENDING** |
| 3 | Neon migration execution | **PENDING** |
| 4 | Render–Neon integration & deployment | **PENDING** (Render currently uses CockroachDB; auto-deploy disabled) |

**Configuration names:** the application's own settings — `DATABASE_URL` (runtime) and optionally `STRIKENOVA_MIGRATION_DATABASE_URL` (migration identity, ADR-016). Do **not** invent `NEON_DATABASE_URL`.

**Platform:** **Render**. There are no Railway instructions in this plan.

---

## Pre-Execution Checks (Already Satisfied — No Actions Taken)

- [x] Migration history reconstructed from the exact pinned trees: `b960ca2` = 27 files, 1 head `d46aa0000001`; `9dc6e15` = 31 files, 1 head `d49aa0000001`, adding `d47aa0000001` → `d48aa0000001` → `d48aa0000002` → `d49aa0000001`.
- [x] `merge_day38_gex` confirmed **not** a head: `f7aa24156f6d` declares it as a tuple `down_revision`, and `a7c1d9e4f2b8` descends from `f7aa24156f6d`.
- [x] Graph parsed for string, tuple, and list `down_revision`; all merge parents collected; `depends_on` checked (none declared).
- [x] **Alembic CLI / `ScriptDirectory.get_heads()` not executed** — all head results are static reconstruction.
- [x] Application already supports `STRIKENOVA_MIGRATION_DATABASE_URL`; no application source change needed.
- [x] ADR-016/017/018 read and confirmed *Accepted*; `DATA.md` §5 is the accepted production privilege model.
- [x] CI evidence qualified: the PostgreSQL job of the migration-rehearsal workflow runs `tests/test_migration_rehearsal_postgres.py`, which invokes `command.upgrade(cfg, "head")` against a real `postgres:16` container and therefore applied the full 31-revision chain to the single head `d49aa0000001`. Runs were at `a016887`, not at the exact `9dc6e15` SHA; the migration trees are byte-identical at both commits (31 files, same blob SHAs; the two intervening commits changed only six governance/documentation files). The 27-revision figure belongs to the historical deployment `b960ca2`. Neither run was Neon-specific, and neither applied any migration to Neon (the container is ephemeral).
- [x] No Neon, CockroachDB, or Render change made.

---

## Gate 2 — Staging Privilege Implementation (requires separate authorization)

1. **Review the proposal.** Read `NEON_STAGING_PRIVILEGE_PROPOSAL.md` and choose option A, B, or C. Nothing below runs before that decision.
2. **Provision credentials out of band.** Obtain passwords for `strikenova_staging_migrator` and `strikenova_staging_app` from the approved Neon/provider process or the platform secret store. Never write a password, connection string, or token into a file, SQL script, log, or chat.
3. **Create the two login roles** (review-only SQL in the proposal, §6). Do not grant `REPLICATION`, `BYPASSRLS`, `CREATEDB`, `CREATEROLE`, `SUPERUSER`, or object ownership to the runtime role.
4. **Grant schema access.** `USAGE, CREATE` on schema `public` to the migrator; `USAGE` on `public` and `CONNECT` on the database to the app. In PostgreSQL 16, `public` no longer grants `CREATE` to `PUBLIC`, so this is explicit.
5. **Install default privileges for the migrator** (ADR-018): `ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_staging_app;` and `… GRANT USAGE ON SEQUENCES TO strikenova_staging_app;`
   - Choose exactly one arrangement (proposal §4.6 / §4.8). They are alternatives, not sequential steps:
     - **A (recommended):** connect directly as `strikenova_staging_migrator` and run the statements **without** `FOR ROLE`.
     - **B:** run as an administrative role with an `INHERIT TRUE` membership path to the migrator, using the `FOR ROLE` form. This sets the migrator's defaults for future objects but does **not** make objects created in that session count as the migrator's.
     - **C:** run as an administrative role with `SET TRUE` on every membership edge, execute `SET ROLE strikenova_staging_migrator;`, then run the statements as the migrator. `INHERIT TRUE` alone does not permit `SET ROLE`.
   - `INHERIT TRUE` and `SET TRUE` are independent: the `FOR ROLE` inherited-privilege check needs `INHERIT TRUE`; `SET ROLE` needs `SET TRUE`. `WITH INHERIT TRUE, SET TRUE` satisfies both. A membership grant requires the grantor to hold `ADMIN` on it (or be a superuser).
   - Defaults affect **future** objects only. Existing objects need explicit `GRANT … ON ALL TABLES/SEQUENCES IN SCHEMA`.
6. **Configure environment variables** (only if two-identity is authorized): `STRIKENOVA_MIGRATION_DATABASE_URL` → migrator connection; `DATABASE_URL` → runtime app connection.
7. **Verify read-only** using the checks in the proposal §8 (`pg_roles`, `pg_default_acl` / `\ddp`, `has_schema_privilege`, membership options). Confirm the app identity cannot create in `public`.
8. **Do not alter `strikenova_staging_owner`** unless a documented technical requirement and its own authorization exist. It currently holds `replication`, `bypass_rls`, `createdb`, `createrole`.

**Gate 2 exit criteria:** migrator and app roles exist with the intended attributes; two `pg_default_acl` rows bound to the migrator; the app identity is DML-only; credentials live only in the secret store.

---

## Gate 3 — Neon Migration Execution (requires separate authorization)

9. **Confirm the target.** Confirm the connection resolves to the existing empty Neon `strikenova_staging` database — not a scratch or rehearsal instance. Record the database name and project, never the credential.
10. **Confirm emptiness.** Verify no `alembic_version` table and no application tables exist. If the database is not empty, stop and obtain explicit instruction; do not stamp, force, or downgrade on assumption.
11. **Choose the executing identity.** Either connect directly as `strikenova_staging_migrator`, or connect as an authorized administrative role and `SET ROLE strikenova_staging_migrator` for the session.
    - `SET ROLE` requires a membership path with `SET TRUE` on **every** edge; `INHERIT TRUE` alone is insufficient.
    - After `SET ROLE`, `current_user` is the migrator, so object creation and the ADR-018 default privileges apply as the migrator.
    - **Objects must be created by the migrator:** membership alone — without a direct migrator connection or a prior `SET ROLE` — does not make the migrator's defaults apply to objects created under a different current role.
12. **Run the migration.** From `options-dashboard-project/backend/`, run `alembic upgrade head`. The single head at this tree is `d49aa0000001`, so the target is unambiguous. With the two-identity model the ADR-017 lease serializes execution and bootstraps on an empty database.
13. **Verify.** Confirm `alembic_version` reports `d49aa0000001`, that the key tables exist, and that a table created by the migrator carries the runtime grants from the defaults. Do **not** assert an unverified table count.

**Gate 3 exit criteria:** `alembic_version` = `d49aa0000001`; runtime identity can DML and cannot DDL; `_migration_lock` owned by the migrator and unlocked.

---

## Gate 4 — Render–Neon Integration & Deployment (requires separate authorization)

14. **Review the Render configuration.** Render currently uses CockroachDB. Changing the database requires its own authorization; no change is made by this plan.
15. **Update Render environment variables** (if authorized): point `DATABASE_URL` at the runtime identity and `STRIKENOVA_MIGRATION_DATABASE_URL` at the migrator identity through Render's secret store. Do not assume an automatic cutover.
16. **Deploy** (if authorized). Auto-deploy is currently disabled; a deployment must be triggered deliberately.
17. **Smoke test** against the deployed service: application connects with the runtime identity, startup migration is a no-op at head, and the ADR-017 lease behaves as documented.

**Gate 4 exit criteria:** the service serves traffic from Neon using the runtime identity; startup migration reports "already current"; no credential appears in logs.

---

## Gate 5 — Post-Deploy Verification

18. **Verify identity separation** — the runtime identity cannot perform DDL; tables are owned by the migrator; defaults remain bound to the migrator.
19. **Verify lease behaviour** — one instance migrates, concurrent instances wait or fail closed.
20. **Document the outcome** — record commands, observed values, and any residual gaps. Record the provider role's retained attributes as an accepted or open risk.

---

## Critical Notes

- **Every step needs explicit authorization for its own gate.** Application approval (gate 1) authorizes nothing else.
- **Target is the existing empty Neon database.** Distinguish it from any disposable PostgreSQL 16 rehearsal.
- **Correct env var names only:** `DATABASE_URL`, `STRIKENOVA_MIGRATION_DATABASE_URL`. No `NEON_DATABASE_URL`.
- **Platform is Render.** No Railway restart or logging instructions apply.
- **PostgreSQL 16 semantics only.** `TEMPORARY` is a database privilege, not a schema privilege; there is no standalone `CREATE INDEX` grant; defaults never apply retroactively.
- **Creation role matters.** Defaults bound to the migrator are inert unless objects are actually created by the migrator.
- **No unverified table counts.** Report only what is observed.
- **Never commit or log credentials.** Passwords come from the secret store.

---

## What This Plan Does NOT Do

- Create or alter roles, or grant privileges
- Run migrations or write `alembic_version`
- Change Render configuration or deploy
- Modify application source, migrations, or configuration
- Touch production or CockroachDB
- Read, print, or store credentials

---

**Status:** `PLAN ONLY — NO STEP EXECUTED — GATES 2, 3, AND 4 PENDING SEPARATE FOUNDER AUTHORIZATION`

**End of Execution Plan**
