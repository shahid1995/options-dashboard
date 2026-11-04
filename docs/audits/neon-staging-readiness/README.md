# Neon Staging Readiness Artifacts

**Repository:** `shahid1995/options-dashboard`
**Branch:** `docs/neon-staging-readiness-artifacts`
**Destination:** `docs/audits/neon-staging-readiness/`

**Scope:** Documentation only. No application source, migration, test, configuration, workflow, governance file, database role, database object, provider setting, deployment, credential, or pull-request state was changed in producing this set.

## Artifacts

| File | Bytes (UTF-8) | Logical lines | Purpose |
|---|---:|---:|---|
| `README.md` | 6796 | 62 | This index: publication metadata, migration graph, authorization gates |
| `AUDIT_REPORT_CORRECTED.md` | 34291 | 405 | Corrective audit: Render provider, migration graph at both pinned trees, Founder approval, Neon role observation, accepted architecture, findings |
| `NEON_STAGING_PRIVILEGE_PROPOSAL.md` | 25580 | 316 | Proposed staging identity pair with full PostgreSQL 16 role-membership semantics and review-only SQL |
| `NEON_STAGING_MIGRATION_PREFLIGHT.md` | 10978 | 125 | Preflight: inventories, head results, PG16 compatibility, qualified CI evidence, prerequisites |
| `NEON_STAGING_EXECUTION_PLAN.md` | 10921 | 124 | Numbered plan for gates 2–5 (privilege, migration, Render integration, post-deploy) |

## Authorization state

| # | Gate | Status |
|---|---|---|
| 1 | Application version | **COMPLETE** — `feat/strikenova-day35-portfolio-intelligence` @ `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`, expected head `d49aa0000001` |
| 2 | Staging privilege implementation | **PENDING** — separate Founder authorization |
| 3 | Neon migration execution | **PENDING** — separate Founder authorization |
| 4 | Render–Neon integration & deployment | **PENDING** — separate Founder authorization (Render currently uses CockroachDB; auto-deploy disabled) |

Gate 1 approves *which application version*. It does not authorize creating roles, applying migrations, or changing Render.

## Migration graph (static reconstruction from the exact pinned trees)

| Commit | Migration files | Heads | Notes |
|---|---:|---|---|
| `b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba` (historical Render deployment — commit date 2026-09-24, deployed 2026-09-25) | **27** | `d46aa0000001` — exactly 1 | Does **not** contain `d47aa0000001`, `d48aa0000001`, `d48aa0000002`, `d49aa0000001`. |
| `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` (approved target, 2026-10-19) | **31** | `d49aa0000001` — exactly 1 | Adds four revisions extending the chain: `d46aa0000001` → `d47aa0000001` → `d48aa0000001` → `d48aa0000002` → `d49aa0000001`. |

**Merge relationships that decide the head count:**

- `f7aa24156f6d` has `down_revision = ('9b675f8a3af0', 'merge_day38_gex')`.
- `merge_day38_gex` has parents `b2c3d4e5f6a7` and `e8f9a0b1c2d3`.
- `a7c1d9e4f2b8` has parent `f7aa24156f6d`.
- Therefore `merge_day38_gex` has a descendant and is **not** a head at either tree.
- `3f8a2e9c4d5b` has parents `9e4d8c2a1f7b` and `b3e5f8a1c7d2` (it is not a root).
- `b8c9f1d2e34a` declares its `down_revision` as a **list**: `['a0deb75ad22f', '9e4d8c2a1f7b']`.

Parsing handled string, tuple, and list `down_revision` forms, all merge parents, and `depends_on` (none declared). **The Alembic CLI and `ScriptDirectory.get_heads()` were not executed** — every head result here is static reconstruction from the pinned Git trees, and no database was contacted.

## Key facts

- **Render** runs on CockroachDB (`sqlalchemy_cockroachdb`); Neon is not connected to it. Historical deployment `b960ca2` — commit dated **2026-09-24**, actually deployed **2026-09-25** (`dep-dar4go17lnhs73a0mfm0`). The two dates differ: the commit date is the Git authoring/commit timestamp, the deployment date is when Render released `dep-dar4go17lnhs73a0mfm0`. Auto-deploy disabled.
- **Neon:** project `gentle-field-42671929`, database `strikenova_staging` (PostgreSQL 16). The earlier read-only catalog inspection found only the Neon-created `strikenova_staging_owner` as an application-present role, with `replication`, `bypass_rls`, `createdb`, and `createrole` set. The catalog was **not** re-read during this documentation pass.
- **Architecture:** ADR-016 (identity separation via `STRIKENOVA_MIGRATION_DATABASE_URL`), ADR-017 (`_migration_lock` lease), and ADR-018 (default privileges bound to the migrator/creator) are **Accepted** in `DECISIONS.md`. `DATA.md` §5 is the accepted production privilege model for database `strikenova`. Application support already exists; no source change is required.
- **Proposed staging identities** (`strikenova_staging_migrator`, `strikenova_staging_app`) are distinct from the accepted production identities (`strikenova_production_app`, `strikenova_prod_migrator`, `strikenova_prod_runtime`, `strikenova_prod_admin`) and from the provider-created `strikenova_staging_owner`.
- **CI:** runs at `a016887f61289bc9c64edaabed45d3e1559038f9` — [37575586698](https://github.com/shahid1995/options-dashboard/actions/runs/37575586698), [37575586664](https://github.com/shahid1995/options-dashboard/actions/runs/37575586664). The PostgreSQL job of the migration-rehearsal workflow runs `tests/test_migration_rehearsal_postgres.py`, which invokes `command.upgrade(cfg, "head")` against a real `postgres:16` service container; that job completed successfully. Because `a016887` and `9dc6e15` have byte-identical migration trees (31 files, identical blob SHAs, single head `d49aa0000001`) and the two intervening commits changed only six governance/documentation files, the rehearsal applied the full 31-revision chain. Qualified: the runs were at commit `a016887`, not the exact `9dc6e15` SHA; neither was Neon-specific; neither applied any migration to Neon (the container is ephemeral). The 27-revision figure belongs to the historical Render deployment `b960ca2`, not to these runs.

## Publication record

- **Baseline for this correction pass:** `cc00e87d4bcf13d190e3ca9025aa6c01d4b357e1` (parent `059c1c752d0fe82089831002838b6bdd4b0e8c68`).
- **Correction commit:** the commit that introduced this revision of these five files, on `docs/neon-staging-readiness-artifacts`. A Git commit cannot embed its own SHA, so that SHA is recorded in the commit itself and in `git log -1` rather than in a file it contains; it is also reported in the publication report for this pass.
- **Byte and line counts** above are measured on the published UTF-8 files of this revision (logical lines = newline-separated content lines, ignoring the single trailing newline).

**Status:** INDEPENDENT AUDIT CORRECTIONS APPLIED — CI CHAIN EVIDENCE ACCURATE — POSTGRESQL 16 INHERIT/SET DISTINCTIONS CORRECT — SQL ORDERING AND EXECUTION PLAN ALIGNED — READ-ONLY PLAN VERIFIED — FIVE REMOTE FILES VERIFIED — NO DATABASE OR INFRASTRUCTURE ACTIONS TAKEN
