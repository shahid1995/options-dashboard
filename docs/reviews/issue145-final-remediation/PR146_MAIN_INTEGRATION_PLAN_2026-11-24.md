# PR #146 → current main: Issue #145 integration plan

**Date:** 2026-11-24  
**Document status:** Planning / review artifact only  
**Current main baseline:** `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`  
**PR #146 implementation head:** `b0125f68ce7fc57aa0be902c868fa059c46d3842`  
**PR #146 base:** `feat/strikenova-day35-portfolio-intelligence` at `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`  
**PR #149 design head:** `6968882f4bd1d3e97266bb912e397a5f555cb769`

This plan reconciles the independently reviewed Issue #145 implementation with the current `main` / Neon architecture. It is **not** an implementation authorization, PR acceptance, merge request, deployment plan, or permission to contact a live database/provider.

## 1. Executive recommendation

**Do not retarget or merge PR #146 as-is. Do not merge the entire `issue-145-fix` branch into `main`.**

Use a new integration branch based on the verified current `main` commit, and port only the Issue #145 / ADR-017/018 implementation delta after reconciling each changed file against the newer `main` baseline. Open a new Draft PR to `main` only after the new branch has its own tests and CI evidence.

Keep PR #146 open, Draft and unmerged as the historical implementation/evidence source until the successor integration has been independently reviewed and the founder decides what to do with the original PR. Keep PR #149 open and unmerged unless the founder separately authorizes its merge.

The identity-remediation PASS at `b0125f68` remains a **scoped technical review of that implementation on its current base**. It does not establish that the full PR can be safely merged into current `main`, nor that the resulting application has been verified against Neon.

## 2. Verified repository snapshot

The following state was read from GitHub on 2026-11-24. The current `main` reference was compared directly with `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b` and was identical.

| Item | Verified state |
|---|---|
| `main` | `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b` |
| ADR-019 | Accepted; `docs/ADR-019-NEON-CLEAN-SLATE.md` is on `main` |
| PR #148 | Merged; its only file change was the standalone ADR-019 file |
| PR #149 | Open, non-Draft, unmerged; head `6968882f4bd1d3e97266bb912e397a5f555cb769`, base `main` at `eeeebffe` |
| PR #149 file delta | Two files: `DECISIONS.md` and new `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` |
| PR #146 | Open, Draft, unmerged; head `b0125f68`, base `feat/strikenova-day35-portfolio-intelligence` at `9dc6e15c` |
| PR #146 file delta | 28 commits, 39 files, 11,977 additions and 2,735 deletions relative to its stated base |
| PR #146 base branch | Still exists at the exact base SHA `9dc6e15c` |
| Issue #145 | Contains the founder authorization update dated 2026-11-21 approving D-2 through D-7 for implementation |

Primary references:
- [Issue #145](https://github.com/shahid1995/options-dashboard/issues/145)
- [PR #146](https://github.com/shahid1995/options-dashboard/pull/146)
- [PR #149](https://github.com/shahid1995/options-dashboard/pull/149)
- [Accepted ADR-019 on main](https://github.com/shahid1995/options-dashboard/blob/main/docs/ADR-019-NEON-CLEAN-SLATE.md)
- [PR #146's 28-commit implementation delta](https://github.com/shahid1995/options-dashboard/compare/9dc6e15c7b7c112ccb81b3be9c1609be66c92414...b0125f68ce7fc57aa0be902c868fa059c46d3842)
- [Divergence between main and issue-145-fix](https://github.com/shahid1995/options-dashboard/compare/main...issue-145-fix)

## 3. Why a direct retarget is unsafe

There are two different comparisons, and they must not be confused:

1. **The PR's own delta:** `9dc6e15c...b0125f68` is 28 commits ahead, zero behind. That is the 39-file PR diff reviewed for #146.
2. **The whole implementation branch against main:** `main...issue-145-fix` is divergent, with 713 commits ahead and 104 behind; the common ancestor is `ffefa6c5fd071ece47dc1a527a2bf85cb8d1379f`.

The second result includes the old feature-branch history inherited by the Issue #145 base. It is not a statement that the Issue #145 PR itself contains 713 new commits. It does show why changing PR #146's base to `main` would expose a vastly different comparison and is not an acceptable integration shortcut.

The current `main` code baseline is also materially older/different in the migration area. Direct GitHub reads of `main` show:
- `options-dashboard-project/backend/app/db.py` still has the earlier single-`DATABASE_URL` migration path.
- `options-dashboard-project/backend/alembic/env.py` has no ADR-017 lease admission/refusal contract.
- `options-dashboard-project/backend/app/_migration_lease.py` does not exist on `main`.
- `options-dashboard-project/backend/app/migrate.py` does not exist on `main`.
- `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` and `docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md` do not exist on `main`.
- `DECISIONS.md` on `main` has no ADR-017/ADR-018 registry entries.

By contrast, PR #149 is based directly on current `main` and provides the founder-approved design record in exactly two files. The Issue #145 update independently records the founder's D-2 through D-7 authorization. Keep those two facts distinct: implementation is authorized by the Issue update, while the full ADR/design text has not yet landed in `main`.

## 4. Target architecture and non-negotiable boundaries

The integration must preserve all of the following:

1. **ADR-019 governs the database target.** Neon PostgreSQL is the clean-slate strategic production target. CockroachDB is retired as the strategic target; no CockroachDB data recovery/export/migration is required.
2. **ADR-019 is not provisioning approval.** Neon production provisioning, role creation, live grants, migrations against Neon, Render changes, deployment and cutover remain unauthorized.
3. **D-2 through D-7 remain the Issue #145 implementation contract:** runtime has zero access to `migration_internal`; a dedicated migrator performs Alembic and startup backfill; no renewal thread or handoff design; PostgreSQL migrations use the guarded CLI; bootstrap is narrow and verified; per-user backfill has lease entry and mandatory pre-commit refresh.
4. Preserve both ownership topologies already covered by the implementation unless a new founder decision changes them.
5. Preserve the actual-connection identity controls reviewed at `b0125f68`: configured URL identity binding, server-observed `session_user` / `current_user`, role-attribute checks, per-connection enforcement, Topology B restoration, and incoming-membership checks for migrator/owner.
6. Do not reintroduce a GUC/session-variable ownership proof, a renewal worker, direct unleased PostgreSQL Alembic execution, or a session-level fixed advisory-lock substitute.
7. SQLite's existing local/test behavior must remain intact.

The accepted ADR-019 explicitly requires staging/production separation and clean-database readiness evidence. A successful disposable PostgreSQL test is not evidence that a Neon project or production database exists.

## 5. Recommended integration sequence

### Phase 0 — freeze and inventory current main

Create a dedicated integration branch from the exact current `main` SHA, not from `issue-145-fix` or the old feature branch.

Before editing:
- confirm the branch still starts at `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`;
- read the current Issue #145 body, PR #149 design, ADR-019, current `AGENTS.md`, `DECISIONS.md`, `INVARIANTS.md`, and `TESTING.md`;
- inventory the current Alembic head/revisions, SQLAlchemy/driver pins, current startup path, backfill contract and test fixtures;
- capture a clean `main` test baseline and its failure identifiers before changes;
- preserve any pre-existing user work and untracked artifacts.

Do not assume the old implementation branch's root control documents or fixtures are interchangeable with current main.

### Phase 1 — establish the design record

PR #149's two-file delta is the exact existing governance source:
- `DECISIONS.md`
- `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`

Preferred governance route: have the founder decide whether PR #149 should be accepted/merged as its own documentation PR before implementation integration. This plan does not merge it.

If the founder prefers one integrated successor PR instead, port those same two files (and no duplicate ADR/design document) as the first logical change in the new integration branch. Do not replace the newer `main` versions of the control documents with stale copies from PR #146.

PR #146 changed `ARCHITECTURE.md`, `DATA.md`, `DECISIONS.md` and `INVARIANTS.md` relative to its old base. Reconcile only the intended Issue #145 statements against the current files; preserve unrelated current-main content.

### Phase 2 — port the implementation as small, reviewable changes

Use the PR #146 delta as implementation evidence, not as a blanket merge instruction. Port and review in dependency order:

1. **Configuration and dependencies:** dedicated migration/runtime role settings, PostgreSQL driver support, reproducible dependency declarations and hash locks. Reconcile them with current-main dependency versions and supported Python versions; do not replace current lockfiles blindly.
2. **Lease protocol and operator interface:** add the versioned lease implementation and guarded `app.migrate` CLI, with narrowly scoped bootstrap/verify/status behavior.
3. **Startup and Alembic integration:** adapt current `app/db.py` and `alembic/env.py` so every PostgreSQL migration path is guarded and bare PostgreSQL Alembic refuses to proceed without the lease.
4. **Backfill fencing:** carry the lease through DDL and per-user backfill transactions, with entry as the first protected statement and refresh immediately before each user transaction commits. Keep SQLite's existing behavior.
5. **Privilege and identity enforcement:** apply the complete effective privilege matrix, actual authenticated-connection identity checks and the Topology B owner-role window/restoration checks. Confirm runtime exclusion from lease objects/functions.
6. **Tests and harness:** port the PostgreSQL 16.16 provisioning, negative identity controls, serialization/failure-injection tests and the gated regression set. Adapt fixtures to current main rather than copying stale assumptions.
7. **CI:** add/update only the workflows required for the current-main integration, preserving least-privilege workflow permissions, pinned PostgreSQL 16.16, hash-locked dependencies, exact test-count gates and cleanup.
8. **Documentation/evidence:** carry forward the implementation runbook and final evidence reports after reconciling all paths, revision references and claims with the new integration SHA.

Do not bulk-copy the 39-file diff without file-by-file review. In particular, PR #146's root governance-file edits were written against the older feature base.

### Phase 3 — PostgreSQL/Neon compatibility reconciliation

- Keep the migration implementation PostgreSQL 16-compatible and provider-neutral; Neon is the intended managed host, but no Neon endpoint may be contacted during this work.
- Review all assumptions about direct versus pooled routes, endpoint aliases, backend PID/transaction behavior, advisory locks, role attributes, default ACLs, SECURITY DEFINER ownership/search paths, transaction semantics and role-membership options against PostgreSQL and Neon documentation.
- Ensure CockroachDB is not treated as a supported target of the new lease protocol or its acceptance suite. PR #146 removes the old CockroachDB-specific migration-rehearsal test and its old PostgreSQL rehearsal module; verify that the new PostgreSQL acceptance coverage supersedes the removed lease scenarios rather than relying on those deletions alone.
- Do not infer that existing staging roles/configuration match the new production model. Role provisioning and staging/production configuration remain separately authorized work.

### Phase 4 — verification gates on the new integration SHA

The old-branch numbers are reference targets, not inherited passes. Run them again on the new branch and publish results for its exact final SHA.

Minimum acceptance evidence:
- PostgreSQL 16.16 application integration, including both Topology A and Topology B, all identity/role negative controls, lease generation/state invariants, actual Alembic DDL, startup backfill and operator CLI behavior.
- The Issue #145 gated integration and regression suites at their current non-skipped pass counts (historical reference on `b0125f68`: 71 and 160). Update gates deliberately only when meaningful coverage changes.
- PostgreSQL compatibility on the supported Python versions with the correct hash lock (historical reference: 122 passed, 0 skipped on CPython 3.13.15).
- SQLite/migration/auth regressions relevant to current main.
- Failure injection, contention, crash/recovery, lease takeover, idempotent backfill and cleanup evidence.
- CI status on the final integration SHA; do not cite runs from `b0125f68` as CI for the new branch.
- Full-suite baseline comparison with sorted test identifiers, explicit skip reasons and exact module lists.

The independent review reproduced 71 integration and 160 regression passes on the old branch. It also ran a broader 19-module PostgreSQL-safety set with 321 passed, 9 failed and 2 skipped: the nine failures were stale direct/unleased-Alembic test paths, and the two skips were environment-gated. A prior claim of 454 passed/0 skipped applied to a narrower module selection. The previous 6,603-pass full-suite log was not retained, one module was excluded, and 43 skips were not classified. Do not describe that run as a complete clean-suite pass. The missing `upstox_client` import and other baseline failures must remain visible and be compared against the new main baseline rather than suppressed.

### Phase 5 — independent review and publication

After the new branch passes its own verification:
- request a separate read-only review of the exact new integration SHA;
- publish the complete diff, evidence and any remaining findings in GitHub;
- open a new **Draft** PR targeting `main`;
- link Issue #145, PR #146 as the source implementation, PR #149 as the design record, ADR-019 and the independent identity review;
- explain precisely which old-branch commits/files were ported, adapted, omitted or superseded.

Do not retarget PR #146. Do not close it automatically. The founder decides whether it remains a historical reference or is closed after the successor PR is accepted.

## 6. Independent review findings to carry forward

The independent review of `b0125f68` found no Critical or High identity-boundary blocker and recorded these Low items:

| ID | Finding | Integration disposition |
|---|---|---|
| L1 | Alembic can construct an unguarded engine if a caller supplies `lease_guard` without `connection`; no in-tree caller currently does so. | Prefer defense-in-depth hardening in the new Alembic integration, or document an explicit founder-approved deferral. Test the actual refusal. |
| L2 | Incoming membership is checked for migrator and Topology B owner, not for runtime. | Decide whether runtime inbound membership must be empty under the least-privilege contract; add a negative test if required, otherwise record the accepted residual risk. |
| L3 | Production role names are config defaults, so staging/preview PostgreSQL deployments with different role names must configure both identities explicitly or fail closed. | Document this in the current deployment runbook and configuration examples; add a DB-free configuration test. No live environment inspection or change is authorized. |

Other follow-ups (stale unleased-Alembic tests, missing `upstox_client`, schema drift/date-brittle tests, and non-Alembic `create_all()` calls in legacy data tools) should be tracked independently. Do not widen the Issue #145 integration silently to fix unrelated product/test debt.

## 7. Explicit stop conditions and authorization boundary

Stop and report rather than improvising if:
- current `main` has moved from the recorded SHA before the integration branch is created or work begins;
- required models, Alembic revisions, dependency contracts or current-main behavior differ materially from this plan;
- a proposed port requires changing the accepted ADR-019 target or founder-approved D-2…D-7 design;
- the new branch requires changing an existing live database, role, grant, credential, provider or deployment to make tests pass.

This plan authorizes **no**:
- merge, Ready transition, branch retargeting or PR closure;
- Neon provisioning/connection, live role/grant creation or migration;
- Render, Vercel, staging or production configuration/deployment;
- production cutover, credential rotation, or data operation.

## 8. Current disposition

**Recommended route:** new integration branch from current `main`; port the Issue #145 delta only; re-run all relevant evidence on that branch; independent review; then a new Draft PR to `main`.

**Not recommended:** retargeting PR #146, merging the 713-ahead divergent branch, bulk-copying old governance files, or treating the existing `b0125f68` tests as proof of a successful integration to `main`.

PR #146 remains open/Draft/unmerged. PR #149 remains open/unmerged. ADR-019 remains the accepted strategic database target. No Neon, Render, staging or production operation was performed in preparing this plan.
