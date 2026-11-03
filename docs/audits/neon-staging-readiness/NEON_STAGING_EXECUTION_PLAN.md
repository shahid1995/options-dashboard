# Neon Staging Execution Plan

**Purpose**: Numbered execution plan for Neon staging initialization/migration, showing what happens AFTER separate Founder authorization. No steps are executed by this documentation task.

**Scope**: Planning only; no database changes, no role changes, no deployments.

**Founder-approved application target** (application version approval: **COMPLETE**):
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Alembic head: `d49aa0000001`

**Target database:** Existing empty Neon PostgreSQL 16 database `strikenova_staging` (project `gentle-field-42671929`), AWS Singapore. **Not** a newly provisioned unrelated database.

**Authorization gates (all separate, explicit authorization required):**
1. **Application version** — **COMPLETE** (9dc6e15, head d49aa0000001)
2. **Staging privilege model** — **PENDING**
3. **Neon migration execution** — **PENDING**
4. **Render-Neon integration & deployment** — **PENDING** (Render currently uses CockroachDB; auto-deploy disabled)

**Configuration:** Use application's actual settings `DATABASE_URL` and optionally `STRIKENOVA_MIGRATION_DATABASE_URL` (ADR-016). Do **not** invent `NEON_DATABASE_URL`. Hosting platform is **Render** (remove any Railway references).

---

## Pre-Execution Checks (No Actions Taken)

- [x] Migration history corrected from exact Git trees (b960ca2: 27 files, 1 head `d46aa0000001`; 9dc6e15: 31 files, 1 head `d49aa0000001` via 4 added migrations `d47aa0000001`→`d48aa0000001`→`d48aa0000002`→`d49aa0000001`). `f7aa24156f6d` points to `merge_day38_gex` (has child) so not a head. Graph reconstructed with string/tuple/list `down_revision`, all merge parents, `depends_on`. Alembic CLI/`ScriptDirectory.get_heads()` not executed; static reconstruction only.
- [x] Application code supports `STRIKENOVA_MIGRATION_DATABASE_URL` (no code changes needed)
- [x] ADR-016/017/018 accepted and understood
- [x] CI evidence qualified (runs at a016887, not exact 9dc6e15; not Neon-specific)
- [x] No Neon/CockroachDB/Render changes made

---

## Execution Plan (Only If All Gates Authorized)

### Gate 1: Privilege Model (if using two-identity model) — Requires separate authorization
1. **Review proposal** — Review `NEON_STAGING_PRIVILEGE_PROPOSAL.md` and decide Option A or B.
2. **Create scoped roles** (if Option A) — Create staging migrator and app roles with correct grants per ADR-016/017/018. Use staging-scoped names (no mixing prod names). Apply defaults with `ALTER DEFAULT PRIVILEGES FOR ROLE <migrator> ...` for tables and sequences. Do **not** grant `REPLICATION`/`BYPASSRLS` to app identities. Do **not** propose invalid SQL (`CREATE TEMPORARY TABLES ON SCHEMA` as schema privilege). Do **not** alter provider role `strikenova_staging_owner` unless documented technical requirement exists.
3. **Configure env vars** (if two-identity) — Set `STRIKENOVA_MIGRATION_DATABASE_URL` to migrator connection, `DATABASE_URL` to runtime app connection. Understand migration lock uses migration identity.
4. **Verify roles** (read-only) — Confirm grants without modifying data.

### Gate 2: Migration Execution — Requires separate authorization  
5. **Confirm target DB** — Confirm connecting to existing empty Neon `strikenova_staging` DB (not scratch). Distinguish from any disposable PG16 rehearsal.
6. **Pre-migration check** — Ensure no `alembic_version` table or app tables exist (fresh empty DB). If not empty, do **not** proceed without explicit authorization and understanding.
7. **Run migrations** — From `options-dashboard-project/backend/`, run `alembic upgrade head`. Use migration identity if configured (ADR-016); lock will serialize (ADR-017). Bootstrap on empty DB is supported.
8. **Verify** — Confirm head is `d49aa0000001`, key tables exist. Do **not** guess unverified table counts.

### Gate 3: Render-Neon Integration & Deployment — Requires separate authorization
9. **Review Render config** — Render currently uses CockroachDB; changing to Neon requires separate authorization. No changes made now.
10. **Update env vars on Render** (if authorized) — Point to Neon appropriately; do **not** assume automatic cutover.
11. **Deploy** (if authorized) — Follow Render's process; auto-deploy currently disabled.
12. **Smoke tests** — Verify app connects with scoped runtime identity, migrations applied correctly.

### Gate 4: Post-Deploy Verification
13. **Validate identity separation** (if enabled) — Runtime identity cannot perform DDL; migrator owns objects per ADR-018.
14. **Check lock behavior** — Understand startup lock behavior.
15. **Document outcome** — Record results, any issues, and remaining gaps.

---

## Critical Notes

- **Each action needs explicit authorization.** This plan does not authorize or execute anything.
- **Target is existing empty Neon DB.** Distinguish from disposable PostgreSQL rehearsal.
- **Use correct env var names:** `DATABASE_URL`, `STRIKENOVA_MIGRATION_DATABASE_URL`. No `NEON_DATABASE_URL`.
- **Platform:** Render (not Railway). Remove Railway restart/logging instructions.
- **Correct SQL only.** Follow PostgreSQL 16 semantics; no invalid grants.
- **Separate gates.** Application approval is complete; others are separate.
- **No unverified table counts.** Do not prescribe specific table counts.
- **Migration lock & ownership:** Explain per ADR-017/018.
- **Rehearsal separation:** If disposable PG16 rehearsal performed, clearly mark as separate from real Neon DB.

---

## What This Plan Does NOT Do

- Create roles or grants
- Run migrations
- Change Render configuration
- Deploy
- Modify application code
- Touch production databases

---

**End of Execution Plan**
