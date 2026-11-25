# Neon Staging Gates 2 & 3 — Replacement Execution Evidence

> **Record status:** RETROSPECTIVE REPLACEMENT — NOT THE ORIGINAL EXECUTION ARTIFACT  
> **Compiled:** 2026-11-05  
> **Target:** Neon project `strikenova-staging`, branch `main`, database `strikenova_staging`  
> **Outcome:** Gate 2 and Gate 3 reported complete by FreeBuff and independently corroborated by read-only PostgreSQL catalog queries.  
>
> **Important provenance limitation:** The original temporary artifact, `artifacts/neon-gates-2-3-evidence.md` (previously reported as 3,501 bytes), was not committed and was not present in a subsequently provisioned workspace. A repository-wide and workspace search reported no copy in the accessible files or Git history. Its original SHA-256 cannot be recovered. This document is a newly compiled replacement from contemporaneous execution reports retained in the project conversation, the live read-only database queries performed independently after migration, and the published readiness documents. It is not a byte-for-byte restoration and does not claim to reproduce the original terminal transcript.

## 1. Executive summary

The staging privilege setup and application migration were executed by FreeBuff on 2026-11-05. FreeBuff reported successful direct password authentication for the two application identities, successful completion of the approved Gate 2 privilege operator, and a successful application startup invocation for Gate 3.

A separate read-only Neon SQL session independently inspected the live database after execution. It confirmed:

- Both application roles are login-capable and have no elevated PostgreSQL role attributes.
- Neither application role is a member of `neon_superuser` or another PostgreSQL role.
- The migrator owns 56 application tables and 33 sequences.
- The migrator has the three approved default-privilege entries.
- The application role has effective DML rights on all 56 tables and sequence USAGE on all 33 sequences, while it cannot CREATE in schema `public`.
- `public.alembic_version` contains `d49aa0000001`.
- The ADR-017 migration-lock sentinel row is present and released (all lease fields are NULL).
- No application functions or non-internal triggers remained in schema `public` at the independent post-migration check.
- The approved PUBLIC database CONNECT and TEMPORARY posture remains in place.

The final credential-cleanup report records that the temporary project-scoped Neon API key was revoked and rejected with HTTP 401; the project secret store was emptied; database connection variables were absent; and the temporary operator harness was deleted. The same underlying VM instance could not be proven to have restarted, so fresh VM/session identity is recorded as UNKNOWN rather than PASS.

No production, CockroachDB, Render, Vercel, application-source, migration-source, or deployment changes were part of this operation.

## 2. Target and source identity

| Item | Verified target |
|---|---|
| Neon project | `strikenova-staging` |
| Neon project ID | `gentle-field-42671929` |
| Region | AWS Singapore — `aws-ap-southeast-1` |
| PostgreSQL | 16 (FreeBuff reported server version 16.15) |
| Branch | `main` |
| Branch ID | `br-old-lab-azh0l76s` |
| Database | `strikenova_staging` |
| Database owner | `strikenova_staging_owner` |
| Direct compute endpoint | `ep-shy-sea-az899irl.c-3.ap-southeast-1.aws.neon.tech` (recorded by FreeBuff; no connection string or credential is included here) |
| Pooling | Disabled for the operator connections; FreeBuff confirmed `pooler_enabled=false` |
| Approved repository | `shahid1995/options-dashboard` |
| Application branch | `feat/strikenova-day35-portfolio-intelligence` |
| Approved application commit | `9dc6e15c7b7c112ccb81b3be9c1609be66c92414` |
| Migration files | 31 |
| Static migration graph | One head: `d49aa0000001`; root `d3eb45a2e046` |

FreeBuff re-fetched the remote application branch and reported that it still resolved to the approved commit. It parsed all 31 migration files, including string, tuple and list `down_revision` forms, and reported one head with no dangling parents or duplicate revision IDs.

The published migration preflight independently documents that the 31-file tree at the approved commit has a single statically reconstructed head, `d49aa0000001`. The earlier PostgreSQL 16 rehearsal was against a disposable PostgreSQL container, not Neon; it is preparation evidence and is not represented here as proof of a Neon migration.

## 3. Evidence provenance and confidence

This replacement deliberately separates operator-reported execution from independent live-database corroboration.

| Evidence ID | Source | What it supports | Limitation |
|---|---|---|---|
| E1 | FreeBuff execution reports retained in the project conversation | CLI/toolchain setup, generated password handling, direct role authentication, SQL execution, Gate 2 probes, application startup exit status, post-checks, cleanup actions | Original transient scripts and terminal log are no longer available in the fresh workspace; this document does not reproduce their full output |
| E2 | Independent read-only PostgreSQL catalog queries executed through a separate connected Neon SQL session on 2026-11-05 | Current role attributes and memberships, default ACLs, database/schema ACLs, object counts and ownership, Alembic revision, lock row, runtime privileges | Read-only post-execution corroboration; does not reproduce the operator's original two-session behavioral probe transcript |
| E3 | Published readiness documents on `docs/neon-staging-readiness-artifacts` at `fcc929814477e341327a3387bfd1f2acd1b7c10d` | Approved target, migration chain, Arrangement A, ADR-016/017/018 rationale, SQL correctness constraints, pre-execution test plan | These documents were preparation documents and still contain historical “not executed / pending” status language; they are not the Gate 2/3 execution record |
| E4 | FreeBuff final cleanup report retained in the conversation | API-key revocation response, secret-store cleanup metadata, variable absence, harness deletion, repository scan summary | Workspace session identity was not conclusively established; some cleanup claims remain operator-reported |
| E5 | Fresh repository/workspace investigation | Original report's absence from accessible workspace and Git refs; no prior committed copy | Original file and its SHA-256 are unavailable |

### Published readiness references

- [Readiness README](https://github.com/shahid1995/options-dashboard/blob/docs/neon-staging-readiness-artifacts/docs/audits/neon-staging-readiness/README.md)
- [Privilege proposal](https://github.com/shahid1995/options-dashboard/blob/docs/neon-staging-readiness-artifacts/docs/audits/neon-staging-readiness/NEON_STAGING_PRIVILEGE_PROPOSAL.md)
- [Execution plan](https://github.com/shahid1995/options-dashboard/blob/docs/neon-staging-readiness-artifacts/docs/audits/neon-staging-readiness/NEON_STAGING_EXECUTION_PLAN.md)
- [Migration preflight](https://github.com/shahid1995/options-dashboard/blob/docs/neon-staging-readiness-artifacts/docs/audits/neon-staging-readiness/NEON_STAGING_MIGRATION_PREFLIGHT.md)
- [PostgreSQL 16 privilege rehearsal](https://github.com/shahid1995/options-dashboard/blob/docs/neon-staging-readiness-artifacts/docs/audits/neon-staging-readiness/POSTGRESQL_16_PRIVILEGE_REHEARSAL.md)

The readiness documents' original “Gates 2 and 3 pending” labels describe their state when those documents were authored. This replacement execution record records the later 2026-11-05 execution and post-execution verification. The readiness documents themselves were not edited in this documentation-only change.

## 4. Role configuration and membership

### 4.1 Application roles

FreeBuff reported that the roles already existed and were not deleted or recreated. It generated two independent passwords in-process and applied them through `ALTER ROLE … WITH LOGIN PASSWORD …` as the existing owner identity, passing SQL through standard input to the Neon CLI's PostgreSQL client. Passwords, password hashes and connection strings are intentionally omitted.

Independent PostgreSQL catalog inspection after that operation returned:

| Role | LOGIN | SUPERUSER | CREATEDB | CREATEROLE | REPLICATION | BYPASSRLS |
|---|---:|---:|---:|---:|---:|---:|
| `strikenova_staging_migrator` | true | false | false | false | false | false |
| `strikenova_staging_app` | true | false | false | false | false | false |

FreeBuff separately reported successful direct, non-pooled password-authenticated sessions for both roles, each returning the intended `current_user` and `current_database`. This direct-authentication result is operator-reported; the independent catalog query did not retrieve or test either password.

### 4.2 Memberships — precise observed state

Independent `pg_auth_members` queries established:

- There are **zero membership edges where either application role is the member**. Neither `strikenova_staging_migrator` nor `strikenova_staging_app` is a member of `neon_superuser` or another role.
- The existing `strikenova_staging_owner` is a member of `neon_superuser`.
- The owner role is also recorded as a member of `strikenova_staging_migrator` and `strikenova_staging_app`, with `ADMIN OPTION=true`, `INHERIT=false`, and `SET=false` on those two edges.
- The owner role's observed attributes include `CREATEDB=true`, `CREATEROLE=true`, `REPLICATION=true`, and `BYPASSRLS=true`; `SUPERUSER=false`.

These owner-role attributes and membership edges are recorded as observed, not generalized to the application roles. The owner is a provider-created elevated identity and FreeBuff reports it was left unchanged. The owner-to-application-role membership edges were not modified by this operation. They are explicitly disclosed here because an earlier shorthand statement that “all role memberships are empty” would be inaccurate for the full membership graph.

## 5. Fresh preflight before writes

FreeBuff reported that the fresh target preflight, performed before the approved writes, found:

- Correct project, branch, database, owner and direct compute endpoint.
- Both application roles present.
- No application relations, functions, triggers, Alembic version table or migration-lock table.
- No unexpected user-defined functions requiring review.
- Runtime app role unable to CREATE in schema `public`.
- Database-level PUBLIC CONNECT and TEMPORARY retained.
- Approved application branch and migration graph unchanged.

The earlier read-only setup inspection had also recorded provider-owned `cloud_admin` default ACLs. The independent post-migration read below confirms those ACL rows remain present with the previously recorded ACL values.

## 6. Gate 2 — privilege implementation

**FreeBuff operator result: PASS.** Independent post-execution catalog and effective-privilege verification: **PASS** for the queried properties below.

### 6.1 Existing database and schema privileges

Independent effective-privilege queries confirmed:

| Privilege | Migrator | App |
|---|---:|---:|
| Database CONNECT | true | true |
| Schema `public` USAGE | true | true |
| Schema `public` CREATE | true | false |
| Database TEMPORARY | true | true |

The database ACL retained PUBLIC CONNECT and PUBLIC TEMPORARY. The app's TEMPORARY access is therefore retained through the approved PUBLIC posture; it is not evidence of an exclusive application-role grant.

### 6.2 Three migrator default-privilege entries

Independent `pg_default_acl` inspection returned exactly three entries for `strikenova_staging_migrator`:

| Scope | Object type | Observed ACL | Meaning |
|---|---|---|---|
| `public` | `r` (tables) | `{strikenova_staging_app=arwd/strikenova_staging_migrator}` | App receives SELECT, INSERT, UPDATE, DELETE on future migrator-created tables |
| `public` | `S` (sequences) | `{strikenova_staging_app=U/strikenova_staging_migrator}` | App receives USAGE on future migrator-created sequences |
| Global | `f` (functions) | `{strikenova_staging_migrator=X/strikenova_staging_migrator}` | Migrator retains its own EXECUTE; there is no PUBLIC EXECUTE entry |

The three defaults were installed while connected directly as `strikenova_staging_migrator`, consistent with Arrangement A and ADR-018 creator-role scoping. The global function default intentionally has no `IN SCHEMA` clause; PostgreSQL's global default EXECUTE privilege for PUBLIC must be revoked globally.

FreeBuff reported that its first attempt did not persist because of a transaction/savepoint handling mistake. It verified that the first attempt left no migrator default ACL rows or probe functions, corrected the connection's autocommit/commit semantics, reran the operation, and then verified exactly the three durable rows. The independent catalog query corroborated the final durable state.

### 6.3 Runtime privileges on created objects

Independent queries executed as `strikenova_staging_app` confirmed:

| Effective permission | Granted objects | Total objects | Result |
|---|---:|---:|---|
| SELECT | 56 tables | 56 | PASS |
| INSERT | 56 tables | 56 | PASS |
| UPDATE | 56 tables | 56 | PASS |
| DELETE | 56 tables | 56 | PASS |
| Sequence USAGE | 33 sequences | 33 | PASS |

The app role also had SELECT permission on `public.alembic_version` and `public._migration_lock`. The app role's `public` schema CREATE privilege remained false.

### 6.4 Provider-owned defaults

Independent post-migration inspection found the previously recorded `cloud_admin` default ACL entries still present:

- `public` sequences: `{neon_superuser=r*w*U*/cloud_admin}`
- `public` tables: `{neon_superuser=a*r*w*d*D*/cloud_admin}`

These provider-created defaults were not changed by the approved migrator-default configuration.

### 6.5 Behavioral probes

FreeBuff reported the Gate 2 function probe lifecycle was executed across separate migrator and app sessions:

1. Migrator created a uniquely named probe function.
2. Its ACL was inspected.
3. App invocation was denied with expected SQLSTATE `42501`.
4. Migrator dropped the probe function and cleanup was checked.

FreeBuff also reported executing the approved TEMPORARY probe. Independent catalog inspection after execution found zero remaining user-defined functions and confirmed the app's effective TEMPORARY privilege is true. The original transient operator's detailed probe output was not retained with the lost artifact, so this replacement does not claim to reproduce that full transcript.

## 7. Gate 3 — application migration

**FreeBuff operator result: PASS.** Independent post-execution database state: **PASS** for the revision, object ownership, counts and lock state queried.

FreeBuff ran the application's approved startup path from `options-dashboard-project/backend/` at the exact approved application commit:

```powershell
python -c "from app.db import init_db; init_db()"
```

The operator reported exit status **0**. This path calls `app.db.init_db()`, runs the ADR-017 lease-guarded migration mechanism through the dedicated migration URL, and then invokes the exposure backfill through the runtime engine. A standalone Alembic-only command was not used as the sole migration step.

### 7.1 Alembic revision

Independent SQL read confirmed:

| Item | Result |
|---|---|
| `public.alembic_version` | Present |
| Current revision | `d49aa0000001` |
| Expected revision | `d49aa0000001` |
| Result | PASS |

### 7.2 Created schema objects and ownership

Independent catalog queries found:

| Object type | Count | Owner |
|---|---:|---|
| Tables | 56 | `strikenova_staging_migrator` |
| Sequences | 33 | `strikenova_staging_migrator` |
| Total counted public tables and sequences | 89 | `strikenova_staging_migrator` |

The independent query found zero user-defined functions and zero non-internal triggers in schema `public` after migration.

### 7.3 ADR-017 migration lease

Independent query found the sentinel row in `public._migration_lock`:

| Field | Observed value |
|---|---|
| `_id` | true |
| `locked_by` | NULL |
| `acquired_at` | NULL |
| `expires_at` | NULL |

This represents a released lease, not a stale active lease. FreeBuff re-read the ADR-017 implementation after initially misclassifying the cleared sentinel row; the implementation confirms that release clears the lease fields. The independent database query confirms all three lease fields are NULL.

### 7.4 Backfill limitation

The application startup path invokes `backfill_all_exposures()` after migration using the runtime engine. FreeBuff reported successful completion of `init_db()` with no startup error. The original transient evidence does not retain a separate detailed backfill row-count transcript, so this replacement does not claim an independently measured backfill count.

## 8. Credential cleanup

### 8.1 Temporary Neon API key

The temporary project-scoped Neon API key was initially still active after the first attempted cleanup. FreeBuff's later verification reported that the key was successfully revoked and that an authenticated request to Neon `/api/v2/auth` returned **HTTP 401 Unauthorized** at 2026-11-05 11:06:44 UTC. A subsequent branch-list request with the same stale in-memory value also returned 401.

This is FreeBuff-reported provider-side verification; the original token is not retained in this document or repository.

### 8.2 Secret-store and process environment

The final cleanup report records:

- The FreeBuff project secret-injection directory contained zero files after the secret-store cleanup.
- `NEON_API_KEY` was absent in subsequent checks after the secret source was emptied.
- `DATABASE_URL` was absent.
- `STRIKENOVA_MIGRATION_DATABASE_URL` was absent.
- No database password, DSN, API key, password hash, or token value is reproduced here.

The same underlying VM instance could not be conclusively distinguished from a brand-new workspace session: host/machine/boot markers and the long-lived VM agent were unchanged. Therefore session identity is **UNKNOWN**, not PASS. The secret source was reported empty and the environment-variable presence checks returned ABSENT; those are the evidence-supported cleanup results.

### 8.3 Temporary tooling and scan

FreeBuff reported:

- `/tmp/snx-g2` deleted after preserving sanitized evidence.
- Approximately 207 MB of temporary harness/toolchain files removed.
- Neon CLI, isolated virtual environment and operator scripts absent after cleanup.
- Database connection variables absent.
- No surviving migration/psql processes.
- A final scan covering 29,185 files reported zero API-key values, zero `npg_` token-shaped values, and zero DSNs with non-placeholder credentials. Placeholder DSNs in test fixtures and documentation were identified separately.
- Shell history contained no database exports or credentials.

The final repository/worktree check reported zero tracked modifications and no cleanup commit or PR before this replacement publication.

## 9. Scope and exclusions

The operation was confined to the Neon staging target and the approved application commit.

The following were not changed or performed:

- Production database or production data.
- CockroachDB production topology.
- Render service configuration or deployment.
- Vercel configuration or deployment.
- Application source code, tests, migrations or configuration.
- GitHub application source, branch protection or rulesets.
- PR #130 or any unrelated existing PR.
- Neon provider owner role configuration.
- Any Gate 4 Render–Neon integration or deployment.
- Any post-migration database mutation during credential cleanup.

This document is the only file proposed by the associated documentation PR.

## 10. Overall status

| Workstream | Status | Evidence basis |
|---|---|---|
| Gate 1 — read-only target/migration preflight | PASS | FreeBuff preflight plus pinned-source and live-target checks |
| Gate 2 — roles and base privileges | PASS | FreeBuff execution report plus independent role/schema/database catalog checks |
| Gate 2 — migrator default privileges | PASS | Independent exact three-row `pg_default_acl` query |
| Gate 2 — function/TEMPORARY probes | PASS as operator-reported | FreeBuff operator report; final catalog corroboration |
| Gate 3 — application startup migration | PASS as operator-reported | FreeBuff command/exit result plus independent Alembic/object/lock queries |
| Post-migration runtime permissions | PASS | Independent effective privilege counts as app role |
| Temporary Neon API key revoked | PASS as operator-reported | FreeBuff reported HTTP 401 after revocation |
| Secret environment cleanup | PASS as operator-reported | FreeBuff reported all three variables absent and project secret source empty |
| Fresh VM/session identity | UNKNOWN | Shared VM markers did not prove a new instance |
| Production / Render / Vercel / CockroachDB unchanged | PASS as operator-reported | Operator scope report; no such actions were authorized |

**Conclusion:** The available retained evidence supports successful completion of Neon staging Gates 2 and 3, with independent database-state corroboration. This is a retrospective replacement record, not the missing original operator artifact. The explicit session-identity uncertainty and the observed owner-role membership edges are retained rather than silently resolved.

---

*Prepared from retained execution reports and independent read-only verification on 2026-11-05. No credential values, password hashes, full connection strings, or private endpoint credentials are included.*
