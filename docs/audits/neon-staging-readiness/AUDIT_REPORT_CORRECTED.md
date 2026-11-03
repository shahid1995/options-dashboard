# Corrective Independent Audit: Render Provider, Migration Graph, Founder Approval, and Neon Role Verification

**Document status:** Final corrected audit (this pass removes residual defects left by the previous two correction passes).

**Scope of this document:** Documentation only. No database connection or mutation, no role creation/alteration/grant, no migration execution, no secret access, no environment-variable change, no infrastructure or provider change, and no pull-request state change were performed in producing it.

## Pinned references used throughout

| Label | Value | Source qualification |
|---|---|---|
| Repository | `shahid1995/options-dashboard` | — |
| Historical Render deployment commit | `b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba` (2026-09-24) | Render deployment history (`dep-dar4go17lnhs73a0mfm0`, deployed 2026-09-25) |
| Founder-approved application target | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` (2026-10-19), branch `feat/strikenova-day35-portfolio-intelligence` | Branch tip re-verified at publication via `git ls-remote` (2026-11-03) |
| Approved expected Alembic head | `d49aa0000001` | Static graph reconstruction from the exact pinned tree |
| Neon project / database | `gentle-field-42671929` / `strikenova_staging` (PostgreSQL 16) | Neon project metadata from the earlier audit pass |
| Render service | `strikenova-api-staging` (`srv-daj4vetg1s2s739ecvfg`) | Render service metadata from the earlier audit pass |

---

## 0. Correction ledger for this pass

Each row records an obsolete statement that existed in the previous revision of this report and its correction.

| # | Defect in the previous revision | Correction | Evidence |
|---|---|---|---|
| D-1 | "No Founder authorization established"; "application target not authorized"; "staging application commit not identifiable" | **Application version approval is COMPLETE** — branch `feat/strikenova-day35-portfolio-intelligence`, commit `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`, expected head `d49aa0000001` | `git ls-remote` at publication returns `9dc6e15…` as that branch's tip; `b960ca2` is an ancestor of `9dc6e15` (168 commits ahead) |
| D-2 | "`b960ca2` contains 31 migration files" | `b960ca2` contains **27** migration files | `git ls-tree -r` on the exact tree |
| D-3 | A 31-row inventory presented as the `b960ca2` inventory | The 31-row inventory belongs to `9dc6e15`; `b960ca2` has its own 27-row inventory (§C.1, §C.2) | Tree-by-tree enumeration |
| D-4 | `b960ca2` contains `d47aa0000001`, `d48aa0000001`, `d48aa0000002`, `d49aa0000001` | Those four revisions are **absent** at `b960ca2` and first appear at `9dc6e15` | Set difference of revision IDs between the two trees |
| D-5 | `b960ca2` has head `d49aa0000001` | `b960ca2` has head **`d46aa0000001`** | Graph reconstruction (§C.4) |
| D-6 | Two heads at each pinned commit (`d46aa0000001`/`d49aa0000001` plus `merge_day38_gex`) | **Exactly one head each**: `d46aa0000001` at `b960ca2`, `d49aa0000001` at `9dc6e15` | §C.4; `merge_day38_gex` has a child |
| D-7 | "`merge_day38_gex` is a merge revision with no children" | `merge_day38_gex` **has a child**: `f7aa24156f6d` declares `down_revision = ('9b675f8a3af0', 'merge_day38_gex')` | `f7aa24156f6d_merge_day39_broker_sync_day38_gex_heads.py` |
| D-8 | "No new migration files were added between `b960ca2` and the current HEAD" | **Four** migration files were added | §C.2 |
| D-9 | "`3f8a2e9c4d5b` has `down_revision = None`" | `3f8a2e9c4d5b` has `down_revision = ('9e4d8c2a1f7b', 'b3e5f8a1c7d2')` | `3f8a2e9c4d5b_merge_capability_backfill.py` |
| D-10 | "Graph analysis using `ScriptDirectory.get_heads()`" | `ScriptDirectory.get_heads()` and the Alembic CLI were **never executed**; every head result here is static reconstruction | §C.7 |
| D-11 | "No accepted database-role contract"; "`DATA.md` §5 describes paper-trading data"; "no ADRs or decision documents" | **Accepted architecture exists**: ADR-016/017/018 are *Accepted* in `DECISIONS.md`, and `DATA.md` §5 is titled "Production privilege model (database `strikenova`)" | `DECISIONS.md` (ADR-016, ADR-017, ADR-018), `DATA.md` §5 |
| D-12 | Absence of `strikenova_prod_*` identities from staging graded `FAIL` | Those identities are **production-database** identities for `strikenova`; staging identities are a separate *proposed* pair | `DATA.md` §5; §G |
| D-13 | Recommendation asking the Founder to choose among migration revisions | Obsolete — the application target is already approved (`9dc6e15`, head `d49aa0000001`) | §E |
| D-14 | `PARTIALLY CORRECTED — …` status labels; "without both decisions" | Replaced by the four-gate status in §A and §K | — |
| D-15 | "Candidate 2: `b2c3d4e5f6a7` (origin/main head)" | `b2c3d4e5f6a7` is an **Alembic revision ID** (`add_gex_provenance_columns`), not a commit; `origin/main` tip is a different SHA | `git cat-file -t b2c3d4e5f6a7` fails; `git ls-remote origin refs/heads/main` returns `e62f196f7d3530943a8669c6ded41c74b55bf77d` |

---

## A. Executive Summary

**Authorization state (four separate items — do not conflate them):**

| # | Item | Status |
|---|---|---|
| 1 | Application version approved for staging | **COMPLETE** — `feat/strikenova-day35-portfolio-intelligence` @ `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`, expected head `d49aa0000001` |
| 2 | Staging privilege implementation (role creation/grants) | **PENDING** — separate Founder authorization required |
| 3 | Neon migration execution | **PENDING** — separate Founder authorization required |
| 4 | Render–Neon integration and deployment | **PENDING** — separate Founder authorization required |

Application-version approval authorizes *which code* is intended. It does **not** authorize creating roles, applying migrations, or changing Render.

**Substantive findings:**

1. **Render staging database provider.** Render staging currently runs on **CockroachDB** (`sqlalchemy_cockroachdb` driver, monthly RU limit exceeded, connections rejected). Neon has **not** been connected to Render. The Neon project `gentle-field-42671929` was provisioned independently of the Render service.

2. **Migration graph, historical deployment (`b960ca2`).** 27 migration files, **exactly one head: `d46aa0000001`**. This corroborates ADR-016, whose validation record describes the migrator identity running "the full 27-revision chain from empty to `d46aa0000001`".

3. **Migration graph, approved target (`9dc6e15`).** 31 migration files, **exactly one head: `d49aa0000001`**. The four added revisions extend the approved head linearly: `d46aa0000001` → `d47aa0000001` → `d48aa0000001` → `d48aa0000002` → `d49aa0000001`.

4. **No ambiguity at the target.** Because the approved tree has one head, `alembic upgrade head` resolves to `d49aa0000001` unambiguously. This is a static conclusion; nothing was executed.

5. **Neon role state.** The earlier read-only catalog inspection found only the provider-created `strikenova_staging_owner` as an application-present role. That role carries `replication=True` and `bypass_rls=True`. The catalog was **not** re-read during this documentation-only pass.

6. **Accepted architecture is not missing.** ADR-016 (identity separation via `STRIKENOVA_MIGRATION_DATABASE_URL`), ADR-017 (transactional lease lock `_migration_lock`), and ADR-018 (default privileges scoped to the migrator/creator role) are all *Accepted* in `DECISIONS.md`. `DATA.md` §5 is the accepted production privilege model for database `strikenova`. What is missing for staging is *implementation* of a staging-scoped identity pair, not architecture or decision authority.

7. **PostgreSQL 16 compatibility.** The approved chain is PG16-compatible by static inspection: no CockroachDB-only SQL in the PostgreSQL path, standard `op.create_table` / `op.add_column` / `op.execute` operations. One file changed between the two pinned trees (`e5f6a7b8c9d0`) but only to split the CockroachDB branch from the PostgreSQL branch; the PostgreSQL path is unchanged (`ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_broker_identity`).

---

## B. Render Provider Verification

| Field | Value | Source |
|---|---|---|
| Service | `strikenova-api-staging` | Render service metadata |
| Service ID | `srv-daj4vetg1s2s739ecvfg` | Render service metadata |
| Repository | `shahid1995/options-dashboard` | Render service metadata |
| Configured branch | `feat/strikenova-day35-portfolio-intelligence` | Render service metadata |
| Auto-deploy | Disabled | Render service metadata |
| Region | Singapore | Render service metadata |
| Plan | Free | Render service metadata |
| Last successful deployment | `dep-dar4go17lnhs73a0mfm0` on 2026-09-25 | Render deployment history |
| Deployed commit SHA | `b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba` | Render deployment history |
| Commit message | Merge PR #98, Day-46 alert transaction boundary | Render deployment history |
| Database driver at startup | `sqlalchemy_cockroachdb` | Render startup logs, Nov 2, 2026 |
| CockroachDB connection status | Monthly RU limit exceeded; connections rejected | Render logs, Nov 2, 2026 |
| Neon connected to Render | **No** | Neon project exists independently; no Render configuration change is authorized or recorded |

**Key distinction.** The historical successful deployment (2026-09-25) used CockroachDB at `b960ca2`. The configured branch has since advanced 168 commits to the approved target `9dc6e15`, but auto-deploy remains disabled and no new deployment is authorized. Moving Render onto Neon is gate 4 of §A and is not performed.

---

## C. Migration Graph

Both graphs below were reconstructed by enumerating `alembic/versions/*.py` in each pinned tree, parsing `revision`, `down_revision`, and `depends_on` as Python literals (string, tuple, and list forms all handled), collecting every parent from every declaration, and treating a revision as a head only when no other revision names it.

### C.1 Inventory at the historical deployment — `b960ca2` (27 files)

| # | File | Revision | `down_revision` |
|---|---|---|---|
| 1 | `125e1807df8d_add_broker_connection_foundation.py` | `125e1807df8d` | `d3eb45a2e046` |
| 2 | `3f8a2e9c4d5b_merge_capability_backfill.py` | `3f8a2e9c4d5b` | `('9e4d8c2a1f7b', 'b3e5f8a1c7d2')` |
| 3 | `5e2a7b9c3f4d_merge_google_sub_main.py` | `5e2a7b9c3f4d` | `('3f8a2e9c4d5b', 'c7d3e5f8a9b2')` |
| 4 | `9b675f8a3af0_day39_add_broker_sync_idempotency_and_.py` | `9b675f8a3af0` | `e8f9a0b1c2d3` |
| 5 | `9e4d8c2a1f7b_capability_separation_backfill.py` | `9e4d8c2a1f7b` | `f7a3c2d1e94b` |
| 6 | `a0deb75ad22f_add_password_hash_to_users_for_email_.py` | `a0deb75ad22f` | `125e1807df8d` |
| 7 | `a1b2c3d4e5f6_correct_trading_status_backfill.py` | `a1b2c3d4e5f6` | `b8c9f1d2e34a` |
| 8 | `a3b4c5d6e7f8_merge_day41_2_cross_d1_and_broker_authorizations.py` | `a3b4c5d6e7f8` | `('e2b4c6d8f0a1', 'f1a2b3c4d5e6')` |
| 9 | `a7c1d9e4f2b8_day41_add_broker_raw_observation.py` | `a7c1d9e4f2b8` | `f7aa24156f6d` |
| 10 | `b2c3d4e5f6a7_add_gex_provenance_columns.py` | `b2c3d4e5f6a7` | `a1b2c3d4e5f6` |
| 11 | `b3e5f8a1c7d2_day41_add_fill_ledger_tables.py` | `b3e5f8a1c7d2` | `a7c1d9e4f2b8` |
| 12 | `b8c9f1d2e34a_add_google_sub_to_users.py` | `b8c9f1d2e34a` | `['a0deb75ad22f', '9e4d8c2a1f7b']` |
| 13 | `c1d2e3f4a5b6_add_account_security.py` | `c1d2e3f4a5b6` | `a3b4c5d6e7f8` |
| 14 | `c7d3e5f8a9b2_google_sub_index.py` | `c7d3e5f8a9b2` | `b8c9f1d2e34a` |
| 15 | `d3eb45a2e046_baseline_initial_schema_with_all_tables.py` | `d3eb45a2e046` | `None` (baseline) |
| 16 | `d45aa0000001_day45_admin_control_plane.py` | `d45aa0000001` | `f4a9b8c2d1e7` |
| 17 | `d46aa0000001_day46_notification_events.py` | `d46aa0000001` | `d45aa0000001` |
| 18 | `d9e0f1a2b3c4_uq_broker_identity_global.py` | `d9e0f1a2b3c4` | `5e2a7b9c3f4d` |
| 19 | `e2b4c6d8f0a1_day41_2_cross_d1_family_lock_and_s2_evidence.py` | `e2b4c6d8f0a1` | `5e2a7b9c3f4d` |
| 20 | `e5f6a7b8c9d0_uq_users_broker_identity_active.py` | `e5f6a7b8c9d0` | `d9e0f1a2b3c4` |
| 21 | `e8f9a0b1c2d3_add_trade_lifecycle_tables.py` | `e8f9a0b1c2d3` | `125e1807df8d` |
| 22 | `e9f8a7b6c5d4_issue17_gap_research_schema.py` | `e9f8a7b6c5d4` | `c1d2e3f4a5b6` |
| 23 | `f1a2b3c4d5e6_broker_authorization_table_and_carry_forward.py` | `f1a2b3c4d5e6` | `e5f6a7b8c9d0` |
| 24 | `f4a9b8c2d1e7_issue78_chain_value_provenance.py` | `f4a9b8c2d1e7` | `e9f8a7b6c5d4` |
| 25 | `f7a3c2d1e94b_add_capability_separation_columns.py` | `f7a3c2d1e94b` | `a0deb75ad22f` |
| 26 | `f7aa24156f6d_merge_day39_broker_sync_day38_gex_heads.py` | `f7aa24156f6d` | `('9b675f8a3af0', 'merge_day38_gex')` |
| 27 | `merge_day38_gex.py` | `merge_day38_gex` | `('b2c3d4e5f6a7', 'e8f9a0b1c2d3')` |

**Absent at this tree:** `d47aa0000001`, `d48aa0000001`, `d48aa0000002`, `d49aa0000001`.

**Head:** `d46aa0000001` (exactly one).

### C.2 Inventory at the approved target — `9dc6e15` (31 files)

The 27 files in §C.1 are all still present. One of them changed content (`e5f6a7b8c9d0`, see §C.6) without changing its `revision` or `down_revision`. Four files are new:

| # | File | Revision | `down_revision` |
|---|---|---|---|
| 28 | `d47aa0000001_day47_durable_background_jobs.py` | `d47aa0000001` | `d46aa0000001` |
| 29 | `d48aa0000001_historical_data_governance.py` | `d48aa0000001` | `d47aa0000001` |
| 30 | `d48aa0000002_historical_ingestion_dataset_mapping_snapshot.py` | `d48aa0000002` | `d48aa0000001` |
| 31 | `d49aa0000001_normalize_iv_observation_timestamps_to_ist.py` | `d49aa0000001` | `d48aa0000002` |

**Added head chain:** `d46aa0000001` → `d47aa0000001` → `d48aa0000001` → `d48aa0000002` → `d49aa0000001`.

**Head:** `d49aa0000001` (exactly one).

### C.3 Graph structure

Six revisions carry multiple parents and create the graph's only branches:

- `b8c9f1d2e34a` — list form `['a0deb75ad22f', '9e4d8c2a1f7b']`: joins the password-hash path and the capability-separation path.
- `3f8a2e9c4d5b` — tuple form `('9e4d8c2a1f7b', 'b3e5f8a1c7d2')`: joins the capability-separation path and the fill-ledger path.
- `5e2a7b9c3f4d` — tuple form `('3f8a2e9c4d5b', 'c7d3e5f8a9b2')`: joins the capability-backfill merge and the Google-sub index path.
- `a3b4c5d6e7f8` — tuple form `('e2b4c6d8f0a1', 'f1a2b3c4d5e6')`: joins the two Day-41 branches.
- `merge_day38_gex` — tuple form `('b2c3d4e5f6a7', 'e8f9a0b1c2d3')`: joins the GEX-provenance path and the trade-lifecycle path.
- `f7aa24156f6d` — tuple form `('9b675f8a3af0', 'merge_day38_gex')`: closes the Day-38/Day-39 merge pair into a single line.

After `f7aa24156f6d` the graph is a single line:

```
f7aa24156f6d → a7c1d9e4f2b8 → b3e5f8a1c7d2 → 3f8a2e9c4d5b
    → 5e2a7b9c3f4d → a3b4c5d6e7f8 → c1d2e3f4a5b6 → e9f8a7b6c5d4
    → f4a9b8c2d1e7 → d45aa0000001 → d46aa0000001
    → [ d47aa0000001 → d48aa0000001 → d48aa0000002 → d49aa0000001 ]   (approved target only)
```

`merge_day38_gex` is therefore **not** a head at either pinned tree: it is a parent of `f7aa24156f6d`, which is in turn a parent of `a7c1d9e4f2b8`.

**`depends_on`:** no migration file in either tree declares `depends_on` (the symbol is absent or `None` throughout), so it contributes no additional edges.

### C.4 Heads

| Pinned tree | Migration files | Heads | Count |
|---|---:|---|---:|
| `b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba` (historical deployment) | 27 | `d46aa0000001` | **1** |
| `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` (approved target) | 31 | `d49aa0000001` | **1** |

The previously reported "seven heads" count and the intermediate "two heads (`…`, `merge_day38_gex`)" count are both invalid. `merge_day38_gex` was misread as childless because its consumer (`f7aa24156f6d`) declares it as a *tuple* member rather than a bare string.

### C.5 Ancestry of the approved head

Walked from the approved head towards the root; `(merge of A, B)` marks a revision whose `down_revision` names more than one parent:

```
d49aa0000001
→ d48aa0000002 → d48aa0000001 → d47aa0000001 → d46aa0000001
→ d45aa0000001 → f4a9b8c2d1e7 → e9f8a7b6c5d4 → c1d2e3f4a5b6
→ a3b4c5d6e7f8            (merge of e2b4c6d8f0a1, f1a2b3c4d5e6)
     e2b4c6d8f0a1 → 5e2a7b9c3f4d
     f1a2b3c4d5e6 → e5f6a7b8c9d0 → d9e0f1a2b3c4 → 5e2a7b9c3f4d
→ 5e2a7b9c3f4d            (merge of 3f8a2e9c4d5b, c7d3e5f8a9b2)
     3f8a2e9c4d5b         (merge of 9e4d8c2a1f7b, b3e5f8a1c7d2)
          9e4d8c2a1f7b → f7a3c2d1e94b → a0deb75ad22f → 125e1807df8d → d3eb45a2e046
          b3e5f8a1c7d2 → a7c1d9e4f2b8 → f7aa24156f6d
     c7d3e5f8a9b2 → b8c9f1d2e34a   (merge of a0deb75ad22f, 9e4d8c2a1f7b)
          → a1b2c3d4e5f6 → b2c3d4e5f6a7 → merge_day38_gex
f7aa24156f6d              (merge of 9b675f8a3af0, merge_day38_gex)
     9b675f8a3af0 → e8f9a0b1c2d3 → 125e1807df8d
     merge_day38_gex      (merge of b2c3d4e5f6a7, e8f9a0b1c2d3)
```

Single root: `d3eb45a2e046` (`down_revision = None`, the only such file). Single leaf: `d49aa0000001` at the approved target, `d46aa0000001` at the historical deployment.

### C.6 What changed between the two pinned trees

- 27 → 31 migration files: **four** added (§C.2), **none** removed.
- Exactly **one** existing file changed content: `e5f6a7b8c9d0_uq_users_broker_identity_active.py`. The change splits the dialect branches so CockroachDB drops the backing unique index (`DROP INDEX IF EXISTS users@uq_users_broker_identity CASCADE`) while PostgreSQL keeps `ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_broker_identity`. `revision` and `down_revision` are unchanged, so the graph is unaffected and the PostgreSQL 16 path is unchanged.
- No file's `revision` or `down_revision` changed between the two trees.

### C.7 Method and limitations

- Method: static reconstruction from the exact objects in each pinned Git tree. `revision`, `down_revision`, and `depends_on` parsed as Python literals; string, tuple, and list forms all handled; every parent collected; heads derived as revisions referenced by no other revision.
- **The Alembic CLI was not run.** `ScriptDirectory.get_heads()`, `alembic heads`, `alembic history`, and `alembic upgrade` were **not executed** at any point in this audit. No statement in this report should be read as a result of executing Alembic.
- No database was contacted. No `alembic_version` state was read or written.

---

## D. Pinned Commit Comparison

| Item | Historical deployment `b960ca2` | Approved target `9dc6e15` |
|---|---|---|
| Branch | `feat/strikenova-day35-portfolio-intelligence` (configured on Render) | `feat/strikenova-day35-portfolio-intelligence` |
| Commit SHA | `b960ca2f20f0c15bf2e59f3d894ccb66d8b0f3ba` | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` |
| Commit date | 2026-09-24 | 2026-10-19 |
| Commit message | Merge pull request #98 from `shahid1995/fix/day46-alert-transaction-boundary` | Merge pull request #132 from `shahid1995/docs/chatgpt-markdown-only-boundary-131` |
| Relationship | Ancestor of the approved target | Descendant of the historical deployment |
| Commits between them | — | 168 |
| Migration files | 27 | 31 |
| Alembic heads (static) | `d46aa0000001` (1) | `d49aa0000001` (1) |
| Migration files added after this point | 4 (`d47aa0000001`, `d48aa0000001`, `d48aa0000002`, `d49aa0000001`) | — |
| PostgreSQL 16 compatibility | Compatible by static inspection | Compatible by static inspection |
| Deployed to Render | Yes — `dep-dar4go17lnhs73a0mfm0`, 2026-09-25, on CockroachDB | **No** — auto-deploy disabled, no deployment authorized |
| Application approval | Historical provenance only | **Founder-approved application version** |

**Branch-tip qualification.** At the time this document was written, `git ls-remote origin refs/heads/feat/strikenova-day35-portfolio-intelligence` returned `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`, so the approved commit *was* the live branch tip at publication. That is a point-in-time observation, not a standing guarantee: the approved target is identified by commit SHA, not by "whatever the branch tip is".

**Open pull requests.** PR #130 (draft, Day-50 feature branch), #136, and #137 (security) were not inspected for migration content in this pass and were not modified in any way.

---

## E. Revision Provenance (historical context only)

Earlier revisions of this report asked the Founder to select among "candidate revisions". That question is obsolete: the application version is approved (`9dc6e15`, expected head `d49aa0000001`). The following is retained only as provenance for the historical deployment.

### E.1 `5e2a7b9c3f4d` — old Render/CockroachDB staging head

- Merge revision, `down_revision = ('3f8a2e9c4d5b', 'c7d3e5f8a9b2')`.
- **Not a head** at either pinned tree: it has multiple descendants, including `e2b4c6d8f0a1`, `d9e0f1a2b3c4`, and the whole path through `a3b4c5d6e7f8` to `d49aa0000001`.
- It was the last revision deployed to the old Render staging environment on CockroachDB. The CockroachDB deployment context is not an authorized recovery target; the revision itself is ordinary PostgreSQL-compatible Alembic.
- Position: ancestor of the approved head `d49aa0000001`.

### E.2 `b2c3d4e5f6a7` — Alembic revision, **not** a commit

- `revision = b2c3d4e5f6a7`, `down_revision = a1b2c3d4e5f6`, file `b2c3d4e5f6a7_add_gex_provenance_columns.py`.
- One of the two parents of `merge_day38_gex`; **not a head**.
- The previous revision of this report described it as "origin/main head" with "3 migration files tracked". That was a category error: `b2c3d4e5f6a7` is not a Git object (`git cat-file -t` fails), and the actual `origin/main` tip is `e62f196f7d3530943a8669c6ded41c74b55bf77d`. No claim about `origin/main`'s migration inventory is made here, because it was not established in this pass.

### E.3 `d49aa0000001` — approved head

- `down_revision = d48aa0000002`; data-normalization migration (IV observation timestamps to IST), batched, and a no-op on an empty database.
- Single head at `9dc6e15`, reached by the linear chain from `d46aa0000001`.
- This is the approved expected head. It is not a *deployment* target statement — applying it is gate 3 of §A and is not authorized.

---

## F. Neon Role State (earlier read-only observation)

**Source and date qualification.** The catalog below was read in the *earlier* audit pass with a read-only `pg_roles` query against Neon project `gentle-field-42671929`, database `strikenova_staging`. That inspection has no separately recorded timestamp in these artifacts; the only dated evidence surviving from the same pass is the Render startup-log inspection dated **2026-11-02**. **The Neon role catalog was not re-read during this documentation-only pass**, and nothing here should be read as a fresh verification. A re-read requires a separate, authorized task with database connectivity.

| Role | LOGIN | SUPERUSER | CREATEDB | CREATEROLE | REPLICATION | BYPASSRLS | Purpose |
|---|---|---|---|---|---|---|---|
| `cloud_admin` | True | True | True | True | True | True | Neon internal — superuser |
| `neon_service` | True | False | True | True | True | True | Neon internal |
| `neon_superuser` | False | False | True | True | True | False | Neon internal |
| `strikenova_staging_owner` | True | False | True | True | **True** | **True** | Provider-created, connection-holding role |
| `pg_*` / `neon_*` / `cloud_*` | — | — | — | — | — | — | PostgreSQL/Neon infrastructure |

**Observations, stated with their correct weight:**

1. `strikenova_staging_owner` is the only application-present role, and it is provider-created. It carries `replication=True` and `bypass_rls=True`, which exceed what a staging application connection needs. Its `CREATEROLE=True` also means it can create the proposed staging identities if that gate is authorized.
2. The production identities defined in `DATA.md` §5 (`strikenova_production_app`, `strikenova_prod_migrator`, `strikenova_prod_runtime`, `strikenova_prod_admin`) are **not** expected in staging. They are the accepted identities for the *production* database `strikenova`; their absence here is not an implementation failure and must not be graded as one.
3. There is no broad break-glass *database login* for staging, and that is not a defect: for staging, break-glass administration is intended to remain through the authorized Neon provider account. (Production's `strikenova_prod_admin` break-glass identity is a production-only artifact of `DATA.md` §5.)

**Grading (corrected):**

| Item | Classification | Rationale |
|---|---|---|
| `strikenova_staging_owner` privilege breadth | `WARNING` | Provider-created role holds `replication`, `bypass_rls`, `createdb`, `createrole`. Not alterable under read-only authorization. |
| Staging identity separation implemented | `PENDING` | Proposed in `NEON_STAGING_PRIVILEGE_PROPOSAL.md`; requires separate authorization. Not a "failure". |
| `strikenova_prod_*` absent from staging | `NOT APPLICABLE` | Production-database identities; wrong database to test them in. |
| No staging break-glass database login | `ACCEPTED BY DESIGN` | Provider-account administration is the intended staging model. |
| System roles (`cloud_admin`, `neon_service`, `neon_superuser`) | `EXPECTED` | Neon platform infrastructure. |

---

## G. Accepted Database Architecture vs Proposed Staging Identities

The previous revisions of this report claimed that no accepted privilege architecture, no ADRs, and no decision documents existed. That was incorrect. The repository contains `DECISIONS.md`, `INVARIANTS.md`, `SECURITY.md`, and `PROJECT-CONTROL.md` at the repository root, and the relevant decisions are *Accepted*:

| Record | Title | Status | Content relevant here |
|---|---|---|---|
| ADR-016 | Runtime/migration database identity separation (opt-in) | **Accepted** | `STRIKENOVA_MIGRATION_DATABASE_URL` runs Alembic under a dedicated migration identity; the runtime credential can then be restricted to DML with no schema ownership. Unset ⇒ historical single-identity behaviour. Validation record: migrator identity ran the full 27-revision chain from empty to `d46aa0000001`. |
| ADR-017 | Migration serialization via transactional lease lock | **Accepted** | Single-row lease (`_migration_lock`) acquired around Alembic execution using the migration identity; bootstraps on an empty database; `MIGRATION_LOCK_TTL_SECONDS >= 2`. Manual CLI path remains operator-controlled. |
| ADR-018 | Production future-table privilege defaults (migrator-creator scope) | **Accepted** | Default privileges bound to the migration/creator role for future tables and sequences; DML-only grant; runtime never receives CREATE/ALTER/DROP/ownership/admin/role-management. |

`DATA.md` §5 is titled **"Production privilege model (database `strikenova`)"** — verified against live production metadata on 2026-09-26. It documents four production identities (`strikenova_production_app` DML-only, `strikenova_prod_migrator` DDL, `strikenova_prod_runtime` NOLOGIN owner, `strikenova_prod_admin` break-glass), the two `pg_default_acl` rows bound to `strikenova_prod_migrator`, `_migration_lock` ownership, and the warning that defaults do not follow a creator-role change. It is a database-role and grant contract, not paper-trading data. The previous claim that §5 covers paper-trading data was false and is withdrawn.

**Distinct layers that must not be conflated:**

| Layer | Names | Status |
|---|---|---|
| Accepted architecture (role *model*) | ADR-016 / ADR-017 / ADR-018 | **Accepted** in `DECISIONS.md` |
| Accepted production identities (database `strikenova`) | `strikenova_production_app`, `strikenova_prod_migrator`, `strikenova_prod_runtime`, `strikenova_prod_admin` | Documented as the live production state in `DATA.md` §5 |
| Proposed staging identities (database `strikenova_staging`) | `strikenova_staging_migrator`, `strikenova_staging_app` | **Proposed**, not created; see `NEON_STAGING_PRIVILEGE_PROPOSAL.md` |
| Provider-created staging owner | `strikenova_staging_owner` | Exists (Neon-created) |

The gap is therefore **implementation of staging-scoped identities**, not the existence of accepted architecture. Application support already exists: `app/config.py` defines `STRIKENOVA_MIGRATION_DATABASE_URL`; `app/db.py` resolves the migration URL through `resolve_migration_database_url()` (precedence: explicit/CLI → `STRIKENOVA_MIGRATION_DATABASE_URL` → `DATABASE_URL`) and uses it for the migration engine, the lock, and `_run_alembic_migrations()`; `alembic/env.py` delegates to the same resolver; `app/_migration_lock.py` implements the ADR-017 lease. **No application source change is required to enable separation.**

---

## H. Findings (Ranked)

### Critical

| # | Finding | Evidence | Impact | Owner |
|---|---|---|---|---|
| C-1 | Render staging is on CockroachDB, not Neon | Startup logs show `sqlalchemy_cockroachdb`; RU limit exceeded; connections rejected | Render cannot serve against Neon until gate 4 is authorized and executed | Founder (gate 4) |
| C-2 | Staging identity separation is not implemented | Only provider-created `strikenova_staging_owner` exists; no `strikenova_staging_migrator` / `strikenova_staging_app` | Application would run with a role holding `replication`, `bypass_rls`, `createdb`, `createrole` | Founder (gate 2) |

### High

| # | Finding | Evidence | Impact | Owner |
|---|---|---|---|---|
| H-1 | `strikenova_staging_owner` carries privileges beyond staging need | `replication=True`, `bypass_rls=True`, `createdb=True`, `createrole=True` (earlier read-only observation) | Larger blast radius if the staging connection string leaks | Founder (gate 2) |
| H-2 | Two prior reports miscounted graph heads | Static reconstruction: 1 head each (`d46aa0000001`, `d49aa0000001`); `merge_day38_gex` has child `f7aa24156f6d` | Correction required for `alembic upgrade head` reasoning | Corrected in §C |

### Medium

| # | Finding | Evidence | Impact |
|---|---|---|---|
| M-1 | Prior reports misidentified `DATA.md` §5 and denied the ADRs | `DATA.md` §5 is the production privilege model; ADR-016/017/018 are Accepted | Corrected in §G; governance was never missing |
| M-2 | Prior reports described `ScriptDirectory.get_heads()` as executed | No Alembic command was ever run in any audit pass | Corrected in §C.7; all head results are static |
| M-3 | Prior reports presented a revision ID as a commit | `b2c3d4e5f6a7` is an Alembic revision; `git cat-file -t` fails | Corrected in §E.2 |

### Low

| # | Finding | Evidence | Impact |
|---|---|---|---|
| L-1 | List-form `down_revision` in `b8c9f1d2e34a` | `['a0deb75ad22f', '9e4d8c2a1f7b']` | Naive string-only parsing misses this edge and inflates head counts; handled by literal parsing |
| L-2 | `d9e0f1a2b3c4` creates a global UNIQUE index | `CREATE UNIQUE INDEX` on `(broker, broker_account_id)` | Succeeds on an empty database; would abort on duplicates. No data exists in the target. |
| L-3 | `d9e0f1a2b3c4`→`e5f6a7b8c9d0` drop path is dialect-split | PostgreSQL keeps `DROP CONSTRAINT IF EXISTS`; CockroachDB uses `DROP INDEX … CASCADE` | PostgreSQL 16 path unaffected by the change between the two trees |

### Informational

| # | Finding | Evidence | Impact |
|---|---|---|---|
| I-1 | 168 commits between the pinned commits | `git rev-list --count b960ca2..9dc6e15` | Auto-deploy disabled; no deployment authorized |
| I-2 | No `depends_on` declarations in either tree | Literal parse of every migration file | Graph edges come only from `down_revision` |
| I-3 | Six multi-parent revisions | `b8c9f1d2e34a`, `3f8a2e9c4d5b`, `5e2a7b9c3f4d`, `a3b4c5d6e7f8`, `merge_day38_gex`, `f7aa24156f6d` | Explains why head counting is error-prone without full parsing |

---

## I. Authorizations Outstanding

The application version is decided. Three authorizations remain, each separate:

1. **Staging privilege implementation** — authorize creating `strikenova_staging_migrator` and `strikenova_staging_app` with grants per ADR-016/017/018, and decide whether to alter the provider role's attributes. Proposal and review-only SQL: `NEON_STAGING_PRIVILEGE_PROPOSAL.md`. Login credentials must come from an approved provider/secret-management process; no plaintext secret may be committed.
2. **Neon migration execution** — authorize `alembic upgrade head` against the existing empty `strikenova_staging` database. Instrument: `NEON_STAGING_EXECUTION_PLAN.md`, gate 3.
3. **Render–Neon integration and deployment** — authorize changing Render's database configuration and redeploying. Not performed; auto-deploy remains disabled.

No authorization exists for: touching production, modifying CockroachDB, altering Neon project settings, or modifying any pull request.

---

## J. Safety Verification

- [x] Neon remains unmodified — no migrations applied, no `alembic_version` written, no schema objects created
- [x] No roles created, altered, or granted; no passwords set
- [x] No Neon project settings changed
- [x] No CockroachDB cluster activated or modified
- [x] No Render environment variable, setting, branch, or deployment changed
- [x] No production database or service contacted
- [x] No application source code, migration file, test, or configuration modified
- [x] No credentials, connection strings, tokens, or secrets read, printed, or committed
- [x] No branch created or deleted; PR #130/#136/#137 untouched; nothing merged

---

## K. Final Status

`APPLICATION VERSION APPROVED (COMPLETE) — STAGING PRIVILEGE IMPLEMENTATION, NEON MIGRATION EXECUTION, AND RENDER-NEON INTEGRATION PENDING SEPARATE FOUNDER AUTHORIZATION — NO DATABASE OR INFRASTRUCTURE ACTIONS TAKEN`

**Basis:**

1. Application version: `feat/strikenova-day35-portfolio-intelligence` @ `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`, expected head `d49aa0000001`. **Complete.**
2. Migration graph: 27 files / 1 head (`d46aa0000001`) at `b960ca2`; 31 files / 1 head (`d49aa0000001`) at `9dc6e15`; four files added; `merge_day38_gex` is not a head. Static reconstruction only — Alembic was not executed.
3. Architecture: ADR-016/017/018 Accepted; `DATA.md` §5 is the accepted production privilege model. Staging identity separation remains unimplemented and separately gated.
4. Render remains on CockroachDB with auto-deploy disabled; Neon is not connected.

**End of Corrective Independent Audit Report.**
