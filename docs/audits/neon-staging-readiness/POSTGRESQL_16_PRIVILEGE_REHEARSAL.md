# PostgreSQL 16 Privilege Rehearsal — Gate 2 Preparation

**Status — two attempts on 2026-11-04:**

| Attempt | Environment | Outcome |
|---|---|---|
| 1 — local FreeBuff host | no usable container runtime | `BLOCKED`, zero SQL executed (§1–§16) |
| 2 — GitHub-hosted `ubuntu-latest` + disposable `postgres:16` | workflow run `41813028902`, commit `165f70e` | `EXECUTED`: A 43/43, B 61/61, C 62/62, D1 35/35 (+12 observations), D2 5/5, **0 failed assertions** (§17–§29) |

Attempt 2 does not overwrite attempt 1's history. §1–§16 are the original blocked record and are preserved verbatim; §17–§29 are the executed GitHub-hosted rehearsal. Neither attempt validates the proposal end-to-end, and neither authorizes Gate 2 implementation.

**Corrective pass, 2026-11-04:** §30–§41 re-verify the run of record from GitHub, correct statements in Part II that the evidence does not support, and add the `TEMP` and `CONNECT` investigations. §41 lists every correction. Where Part II and Part III disagree, Part III governs.

**Post-publication review pass, 2026-11-05:** §42–§43 record corrections to the *verification plan and reporting wording* raised by an independent review of the published set. No rehearsal result, count, or limitation changes; Part IV governs where it and an earlier part disagree about a procedure.

---

## Part I — Initial local attempt (BLOCKED, zero SQL executed)

**Date:** 2026-11-04
**Scope:** Isolated validation of the PostgreSQL 16 role-membership, `INHERIT`, `SET ROLE`, default-privilege, and ownership behaviour described in `NEON_STAGING_PRIVILEGE_PROPOSAL.md` §4 and §6.
**Authorization:** Gate 2 preparation only. No Neon, CockroachDB, Render, staging, or production system was contacted. Nothing was committed or pushed.

---

## 1. Repository and documentation baseline

| Item | Value | How verified |
|---|---|---|
| Repository | `shahid1995/options-dashboard` | `git remote -v` |
| Branch | `docs/neon-staging-readiness-artifacts` | `git branch --show-current` |
| Local HEAD | `f56d9922d89a465ad94343f51fcc60f0f41d74bf` | `git rev-parse HEAD` |
| Remote HEAD | `f56d9922d89a465ad94343f51fcc60f0f41d74bf` | `git ls-remote origin refs/heads/docs/neon-staging-readiness-artifacts` |
| Expected HEAD | `f56d9922d89a465ad94343f51fcc60f0f41d74bf` | Matches — **no divergence** |
| Tracked working tree | clean | `git status --short` (only pre-existing untracked `.wt-*` directories belong to other work; untouched) |

Governance inspected before any action: root `AGENTS.md` (no nested `AGENTS.md` exists at depth ≤ 4), `.specify/memory/constitution.md`, plus `INVARIANTS.md` / `PROJECT-CONTROL.md` as referenced authority. Nothing in those documents prohibits a disposable local PostgreSQL rehearsal; the constitution's evidence-before-assertion and credential-safety principles are the reason this report records no results that were not actually produced.

Proposal sections read in full: §1–§10, with §4.1–§4.8 and the §6 SQL as the objects under test.

## 2. Preflight result

`BLOCKED` — not on repository state, but on environment capability. The request requires a **genuinely disposable PostgreSQL 16 instance**, and prohibits installing software or changing machine configuration (task §4). This environment offers no such instance and no permitted route to create one.

## 3. Environment evidence

| Requirement | Result | Evidence |
|---|---|---|
| Container runtime reachable | **No** | A root-owned `dockerd` is already running on this host (`/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock`, started before this task), but `/usr/bin/docker version` → `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`. The daemon exists; this process may not use it. |
| Docker socket ownership | root-only for us | `srw-rw---- 1 root docker` — our uid `129488032` is in group `freebuff` only; `getent group docker` → `docker:x:103:ubuntu` |
| Elevation | **Unavailable** | `sudo -n true` → `The "no new privileges" flag is set, which prevents sudo from running as root` |
| Process capabilities | **None** | `/proc/self/status`: `CapEff: 0000000000000000`, `NoNewPrivs: 1` |
| Rootless container tooling | **Absent** | `rootlesskit`, `newuidmap`, `newgidmap`, `slirp4netns`, `fuse-overlayfs`, `dockerd-rootless.sh` — none found; `podman` not installed |
| Alternative Docker endpoint | **No daemon** | `docker -H tcp://127.0.0.1:36421 version` → `Error response from daemon: 404: Page Not Found` (the port is not a Docker API); `curl --unix-socket /var/run/docker.sock http://localhost/_ping` → no response |
| PostgreSQL 16 binaries installed | **No** | `ls /usr/lib/postgresql` → empty; `find / -maxdepth 6 -name postgres -o -name pg_ctl` → no results; `which psql` → not found |
| PostgreSQL server already running | **No** | `ss -ltnp` shows only DNS (53), SSH (22), and one loopback HTTP port; `ps aux \| grep -i postgres` → no match |
| Embedded/vendored PostgreSQL in repo | **No** | No `@embedded-postgres` in any `node_modules`; no `initdb` or `PG_VERSION` under the repository |
| Network egress (for context only) | available | TCP connect to `deb.debian.org:80` succeeded — deliberately **not** used, because obtaining PostgreSQL that way means installing software |

Conclusion: there is no Docker/Podman daemon this process may use, no PostgreSQL 16 server or client, no ability to start a daemon (empty capability set, `NoNewPrivs=1`, no sudo), and no permitted software-installation route. Task §4 directs: *"If PostgreSQL 16 cannot be made available safely, stop and report `BLOCKED`. Do not substitute…"* — that stop condition is met.

## 4. What was executed

**Nothing.** This is the central fact of this report:

- No `initdb`, no `pg_ctl start`, no `postgres` process.
- No SQL statement of any kind against any database, local or remote.
- No `CREATE ROLE`, `GRANT`, `ALTER DEFAULT PRIVILEGES`, `SET ROLE`, `RESET ROLE`, `CREATE TABLE`, `CREATE SEQUENCE`, or `CREATE FUNCTION`.
- No `psql`, no client connection attempt, no catalog query.
- No SQL test script was authored, so no SHA-256 of executed SQL material can be provided (task §6). Providing a hash for a script that never ran would misrepresent evidence.

Every entry in the matrices below is therefore `NOT RUN`, and every A/B/C status is `BLOCKED`.

## 5. Material extracted for testing (unexecuted)

The rehearsal was to exercise the proposal's §6 SQL as written, in its documented order:

| Part | Content | Role under test |
|---|---|---|
| 1 | `CREATE ROLE strikenova_staging_migrator LOGIN;` `CREATE ROLE strikenova_staging_app LOGIN;` | bootstrap administrator |
| 2 | `GRANT CONNECT ON DATABASE … TO` migrator and app | bootstrap administrator |
| 3 | `GRANT USAGE, CREATE ON SCHEMA public TO migrator;` `GRANT USAGE ON SCHEMA public TO app;` | bootstrap administrator |
| 4 | `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public` / `GRANT USAGE ON ALL SEQUENCES IN SCHEMA public` to app | bootstrap administrator |
| 5 | `GRANT strikenova_staging_migrator TO <admin_role> WITH INHERIT TRUE, SET TRUE;` (prerequisite for B/C only) | superuser or holder of `ADMIN` on that membership |
| 6 | **A:** `ALTER DEFAULT PRIVILEGES IN SCHEMA public` (tables, sequences) | migrator, connected directly |
| 7 | **B:** `ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator IN SCHEMA public` (tables, sequences) | administrator with `INHERIT TRUE` path |
| 8 | **C:** `SET ROLE strikenova_staging_migrator;` → `ALTER DEFAULT PRIVILEGES IN SCHEMA public` (tables, sequences) → `RESET ROLE;` | administrator with `SET TRUE` on every edge |

Note for the record: the proposal configures table and sequence default privileges only. It contains no function default privileges, so the task's "function permissions wherever the proposal configures them" clause has no corresponding proposal statement to test.

## 6. Test matrix — all entries NOT RUN

### 6.1 Alternative A — direct migrator connection

| # | Assertion | Expected | Actual | Status |
|---|---|---|---|---|
| A1 | Connect directly as `strikenova_staging_migrator` | connection succeeds | — | NOT RUN |
| A2 | `session_user` = `current_user` = migrator | both migrator | — | NOT RUN |
| A3 | Migrator performs documented migration operations without unintended memberships | succeeds, DDL as owner | — | NOT RUN |
| A4 | §6 Part 6 default privileges execute as written | two `pg_default_acl` rows bound to migrator | — | NOT RUN |
| A5 | Create table/sequence as migrator | objects owned by migrator | — | NOT RUN |
| A6 | App role's effective privileges on those objects | DML + sequence USAGE; no DDL/ownership | — | NOT RUN |
| A7 | App role lacks unintended DDL, ownership, `CREATEROLE`, `SUPERUSER` | all false | — | NOT RUN |

### 6.2 Alternative B — `INHERIT TRUE` + `FOR ROLE`

| # | Assertion | Expected | Actual | Status |
|---|---|---|---|---|
| B1 | Membership `GRANT … WITH INHERIT TRUE, SET TRUE` succeeds from authorized grantor | succeeds | — | NOT RUN |
| B2 | Positive: admin with `INHERIT TRUE` path runs `ALTER DEFAULT PRIVILEGES FOR ROLE` | succeeds | — | NOT RUN |
| B3 | Negative: `INHERIT FALSE` edge / no membership | fails | — | NOT RUN |
| B4 | Negative: membership path lacking `INHERIT` on an intermediate edge | fails | — | NOT RUN |
| B5 | Actor identity during B2 (`session_user`, `current_user`) | session_user = admin, current_user = admin | — | NOT RUN |
| B6 | Inheritance does not make admin equivalent to migrator for ownership | objects created by admin owned by admin | — | NOT RUN |
| B7 | Objects created as admin vs as migrator use different default ACLs | defaults apply per creating role | — | NOT RUN |
| B8 | Admin-created objects do **not** receive the migrator's default ACLs | no app grants from migrator defaults | — | NOT RUN |

### 6.3 Alternative C — `SET ROLE`

| # | Assertion | Expected | Actual | Status |
|---|---|---|---|---|
| C1 | `SET ROLE` succeeds with `SET TRUE` on every edge | succeeds; `current_user` = migrator | — | NOT RUN |
| C2 | Negative: `SET FALSE` on the final edge | `SET ROLE` fails | — | NOT RUN |
| C3 | Negative: multi-hop path with one non-`SET` intermediate edge | `SET ROLE` fails | — | NOT RUN |
| C4 | `INHERIT` without `SET` does not permit `SET ROLE` | fails | — | NOT RUN |
| C5 | `SET` without `INHERIT` permits `SET ROLE` | succeeds | — | NOT RUN |
| C6 | `session_user` unchanged, `current_user` = migrator after `SET ROLE` | as stated | — | NOT RUN |
| C7 | §6 Part 8 statements in documented order | succeeds; defaults bound to migrator | — | NOT RUN |
| C8 | Objects created under migrator identity are owned by migrator | ownership = migrator | — | NOT RUN |
| C9 | `RESET ROLE` restores admin identity | `current_user` = admin | — | NOT RUN |

### 6.4 Cross-checks

| # | Assertion | Expected | Actual | Status |
|---|---|---|---|---|
| X1 | `pg_roles` attributes: `LOGIN` true; `SUPERUSER`/`CREATEDB`/`CREATEROLE`/`REPLICATION`/`BYPASSRLS` false | as stated | — | NOT RUN |
| X2 | Grantor authority sufficient (`ADMIN` option or superuser) | grant succeeds only for authorized grantor | — | NOT RUN |
| X3 | `INHERIT` vs `SET` are independent | both directions demonstrated | — | NOT RUN |
| X4 | Default privileges affect future objects only | pre-existing objects unprivileged for app | — | NOT RUN |
| X5 | Ownership ≠ effective grant ≠ membership | three distinct results recorded | — | NOT RUN |
| X6 | `has_table_privilege` / `has_sequence_privilege` / `has_schema_privilege` confirm behaviour | matches intended model | — | NOT RUN |
| X7 | App role has no migration or administrative capability | all false | — | NOT RUN |
| X8 | §6 ordering executable exactly as written | no reordering needed | — | NOT RUN |

## 7. Membership and `INHERIT` / `SET` results

None. No positive or negative membership case was exercised, and no SQLSTATE was produced. The PG16 `INHERIT`/`SET`/`ADMIN` distinction therefore remains **documentation-derived and unverified by execution**, exactly as `NEON_STAGING_PRIVILEGE_PROPOSAL.md` §4.6 and §9.2 already state.

## 8. Default-privilege and ownership results

None. No `pg_default_acl` row was created or inspected; no object was created; no ownership was observed.

## 9. Effective application-role permission results

None. No privilege-check function was queried.

## 10. SQLSTATE and error details from intentional negative tests

None — no negative test was executed. The only error captured in this session is the environment failure `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`, which is an environment blocker, not a PostgreSQL result.

## 11. Limitations, unexpected results, and proposal ambiguities

1. **Environment blocker (primary).** No usable container runtime, no PostgreSQL 16 binaries, no privilege to install either. This is the sole reason for `BLOCKED`.
2. **Nothing about the proposal's correctness can be inferred from this report.** A blocked rehearsal is not weak evidence for or against §4/§6; it is no evidence.
3. **`CREATE ROLE … LOGIN` without a password** (proposal §6 Part 1) is consistent with §7's out-of-band credential provisioning, but on a real server it produces a role that cannot authenticate by password until a credential is set. Untested; flagged only so the gap is not lost.
4. **`GRANT … ON ALL TABLES IN SCHEMA public`** (Part 4) against zero tables emits a notice and is a no-op; this is expected from the PostgreSQL manual, not observed here.
5. **No function default privileges** are configured anywhere in §6, while the task's scope mentions function permissions. If the application creates functions as the migrator, PostgreSQL's default `EXECUTE` grant to `PUBLIC` governs runtime execution unless explicitly revoked. This is a **hypothesis for future testing**, not a finding; no correction is proposed on this basis.

## 12. Conclusion on technical support for each alternative

| Alternative | Conclusion |
|---|---|
| A — direct migrator connection | **NOT VALIDATED** (blocked; no execution) |
| B — `INHERIT TRUE` + `FOR ROLE` | **NOT VALIDATED** (blocked; no execution) |
| C — `SET ROLE` | **NOT VALIDATED** (blocked; no execution) |

The proposal is **not validated, and not invalidated**. Its §9 "Explicit Gaps" entries 1 and 2 remain accurate.

## 13. Recommended documentation corrections

**None applied, and none proposed as a correction**, because a correction requires executed evidence and this rehearsal produced none. The observations in §11 items 3–5 above are questions for a future rehearsal, not documented defects.

Specifically: `NEON_STAGING_PRIVILEGE_PROPOSAL.md` §9.2 ("No rehearsal … was used to validate §4 or §6") and the `PROPOSAL — REVIEW ONLY — UNEXECUTED — UNTESTED` status line remain true and were **not** modified.

> *(Superseded 2026-11-04: §9.2 and that status line **have since been corrected**, because a rehearsal now exists and because the function default needed a fix. See §41 C7 for the exact wording change. This paragraph is left as written so the Part I record stays intact.)*

## 14. Requirements to unblock

Any **one** of the following, under separate authorization, would allow this rehearsal to run as specified:

1. **Grant this environment Docker access** — the host already runs `dockerd`; adding this agent uid (`129488032`) to the `docker` group, or exposing an authorized Docker endpoint, would be sufficient. Then `docker run --rm postgres:16` with no port publishing and no named volume satisfies task §4 directly.
2. **Authorize a rootless container runtime** — install `rootlesskit` + `podman`/`docker` rootless prerequisites with a subuid mapping for this uid (none currently exists for it in `/etc/subuid`; only `ubuntu` and `freebuff-git` are listed).
3. **Authorize installing PostgreSQL 16 locally** — explicitly lifting the §4 "do not install software" restriction. Even then, this process has `CapEff=0` and no root, so a system package install is impossible; only an unprivileged, workspace-local extraction of official PostgreSQL 16 packages would work, which is a deviation from §4's preferred container method and should be authorized deliberately, not assumed.
4. **Provide a disposable, isolated PostgreSQL 16 instance** reachable only by this task (non-shared, credentials generated for the rehearsal, destroyed afterwards).

Until one of these is authorized, every Gate 2 privilege claim remains documentation-derived.

## 15. Cleanup evidence

| Item | Result | Evidence |
|---|---|---|
| Containers created | none | no Docker API call succeeded — every `docker` invocation failed at the socket with `permission denied`; the pre-existing host `dockerd` (pid 1356, started Oct 31, before this task) was neither used nor configured |
| Databases / data directories created | none | no `initdb` run; no `PG_VERSION` anywhere in the workspace |
| Listening ports created | none | listener set unchanged: DNS 53, SSH 22, one loopback HTTP port |
| Persistent volumes / external resources | none | nothing provisioned |
| Temporary scripts | none written | no SQL scripts were authored |
| Repository files changed | one new untracked file (this report) | `git status --short` shows `?? docs/audits/neon-staging-readiness/POSTGRESQL_16_PRIVILEGE_REHEARSAL.md` plus pre-existing unrelated `?? .wt-*` directories |
| Branch / HEAD | unchanged | `f56d9922d89a465ad94343f51fcc60f0f41d74bf`, tracked tree clean |
| Existing audit documents | unmodified | proposals, preflight, execution plan, audit report, README all byte-identical to `f56d992` |

## 16. Safety confirmation

- No connection to Neon, project `gentle-field-42671929`, or database `strikenova_staging`.
- No connection to CockroachDB Cloud, Render, Vercel, or any shared/staging/production database.
- No application, staging, or production connection string was read, used, or inspected; no environment variable of that kind was examined.
- No credential, password, token, or secret was read, printed, or written.
- No application source, migration, test, CI workflow, dependency, proposal, audit report, or execution-plan file was created or modified — the only new file is this report.
- No Alembic migration was executed against any database.
- No commit, push, branch creation, PR, or branch-protection change was made.
- No provider resource was activated or created, and no paid service was used.

**Part I final status: `BLOCKED — NO POSTGRESQL 16 RUNTIME AVAILABLE — A/B/C NOT VALIDATED — ZERO SQL EXECUTED`**

---

# Part II — GitHub-hosted attempt (executed, 2026-11-04)

## 17. Authorization and scope of the second attempt

A separate authorization permitted exactly one temporary branch carrying exactly two new files, used only to run the isolated rehearsal on a standard GitHub-hosted runner. Nothing else was authorized, and nothing else was done.

| Item | Value |
|---|---|
| Temporary branch | `temp/pg16-privilege-rehearsal-20261104` (created from `f56d9922d89a465ad94343f51fcc60f0f41d74bf`, deleted after the run) |
| Commit of record | `165f70e92b71b4b1bf5f00aa0ae6694ee08542fe` |
| Files added (only these) | `.github/workflows/pg16-privilege-rehearsal.yml`, `tools/pg16-privilege-rehearsal.py` |
| `git diff --name-status f56d992..165f70e` | `A  .github/workflows/pg16-privilege-rehearsal.yml`, `A  tools/pg16-privilege-rehearsal.py` — no modification, no deletion |
| SHA-256 of harness at `165f70e` | `6a9bd0535edf7344c06861a90a95174a79af69a7ec74608e1bcf4e684bdedab4` |
| SHA-256 of workflow at `165f70e` | `5d6ae99d4f4f6b0e85fdfb130c83eb0c38606bcc83e3ad47e36d1501f530400e` |
| Branch history | `9ef8407` → `802c9a6` → `4604bde` → `69b1d39` → `720fb1f` → `736a7cc` → `165f70e` |

No PR was opened, nothing was merged, and `main` and `docs/neon-staging-readiness-artifacts` were never written to.

## 18. Workflow and isolation design

| Requirement | Implementation |
|---|---|
| Trigger | `push` restricted to `refs/heads/temp/pg16-privilege-rehearsal-20261104` (plus `workflow_dispatch`, added after the first push enqueued no run; the manual trigger turned out to be unusable — GitHub only dispatches workflows that exist on the default branch — so every run of record came from a push) |
| Runner | `ubuntu-latest`, standard GitHub-hosted; no larger or paid runner, no self-hosted runner |
| Database | one `postgres:16` service container, no volume, no published port, `POSTGRES_HOST_AUTH_METHOD: trust` so no credential exists in the repository at all |
| Second container | one further disposable `postgres:16` on `127.0.0.1:5544`, created and destroyed inside a single step, its bootstrap password supplied through a temporary `--env-file` deleted in the same step, used only for the §7 login diagnostic |
| Permissions | `permissions: contents: read`; no `GITHUB_TOKEN` write, no secrets, no provider credentials |
| Actions used | `actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4` — the same reviewed major versions the repository's existing workflows use (pinned to tags, not SHAs; noted as a limitation) |
| Artifact | `pg16-privilege-rehearsal-evidence`, `retention-days: 7` |
| Forbidden paths | no cache requirement, no deploy/release step, no application migration, no application test suite, no CockroachDB service, no network route to Neon, Render, Vercel or any application database |
| Failure behaviour | the job exits non-zero if any assertion mismatches or the harness cannot complete (`python tools/pg16-privilege-rehearsal.py` is the failing step) |

## 19. Environment of record

| Fact | Value |
|---|---|
| Workflow run | `41813028902` (run number 5), conclusion **`success`**, head SHA `165f70e92b71b4b1bf5f00aa0ae6694ee08542fe` |
| Job completion | `2026-11-04T10:16:41Z` |
| PostgreSQL | `PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit` |
| `server_version` / `server_version_num` | `16.15 (Debian 16.15-1.pgdg13+2)` / `160015` |
| Server settings recorded | `server_encoding=UTF8`, `standard_conforming_strings=on`, `password_encryption=scram-sha-256` |
| Image | `postgres:16`, digest `sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a` (from the runner's `docker pull` output; see limitation L2) |
| `pg_hba.conf` — rehearsal service | `local all all trust`, `host all all 127.0.0.1/32 trust`, `host all all ::1/128 trust`, replication rows `trust`, `host all all all trust` |
| `pg_hba.conf` — login-diagnostic container | same, except `host all all all scram-sha-256` |
| Artifact | id `13313880536`, name `pg16-privilege-rehearsal-evidence`, 13 970 bytes, digest `sha256:b17f26677b8a01dba1922bad260f71a5ba6b9715da0189b59ff9af1e879808e6`, expires `2026-11-11T10:16:36Z` |
| Log archive downloaded | `shahid1995_options-dashboard_run_41813028902_logs.zip` — every number in §22–§24 is quoted from it |

Both `pg_hba.conf` files were read from the server catalogs during the run and are reproduced above as recorded, not as configured.

## 20. Run history

| Run | Head SHA | Conclusion | Why it ended that way |
|---|---|---|---|
| `41810161190` | `4604bde` | failure | A/B/C all passed; two D1 probes failed and the D2 diagnostic crashed on a harness bug (`check()` called with a missing argument) |
| `41810893244` | `69b1d39` | failure | A/B/C passed; D1-200b returned `[]`, and D2 reproduced the `TypeError: check() missing 1 required positional argument: 'actual'` |
| `41811686633` | `720fb1f` | failure | D2 fixed and passing; two D1 probes were written around guessed expectations and disagreed with PostgreSQL 16 |
| `41812470136` | `736a7cc` | **success** | A 43/43, B 61/61, C 62/62, D1 35/35, D2 5/5 |
| `41813028902` | `165f70e` | **success** — run of record | Identical verdicts; log markers for recorded observations corrected, server/container facts printed |

The three failures were harness defects, not proposal results. They were not resolved by relaxing an assertion: the two D1 probes in run 3 asserted a posture the harness had invented, and the fix was to convert them into explicitly `OBSERVED` records (excluded from the failure count and reported as measurements) while leaving every genuine assertion untouched. Run 1's count of D1 failures and run 2's `[]` are the same phenomenon that §23 explains.

## 21. Substitutions

Only identifiers were substituted; no proposal SQL text, clause, or order was altered. Substitutions are declared in the harness `SUBSTITUTIONS` list and reproduced here.

| Proposal | Rehearsal | Reason |
|---|---|---|
| database `strikenova_staging` | `rehearsal_a`, `rehearsal_b`, `rehearsal_c` | the three alternatives run in separate, mutually invisible databases |
| role `strikenova_staging_migrator` | `rehearsal_<alt>_migrator` | per-alternative isolation |
| role `strikenova_staging_app` | `rehearsal_<alt>_app` | per-alternative isolation |
| proposal's `<admin_role>` placeholder | `rehearsal_<alt>_admin` (+ `_noset`, `_noinherit`, `_nomember` variants in alternative C) | the proposal leaves the administrator name open; the variants exist to test `INHERIT`/`SET` independently |
| schema `public` | `public` (unchanged) | the default-privilege scope is part of what is under test |
| diagnostic database and schemas | database `rehearsal_func`, schemas `public` and `jobs`, roles `rehearsal_f_migrator` / `rehearsal_f_app` / `rehearsal_f_probe` | the function diagnostic requires comparing a schema-scoped default with a global default in an isolated arrangement (database and schema names corrected 2026-11-04 — see §41 C6) |
| table/sequence names | `rehearsal_orders`, `rehearsal_orders_id_seq` | nothing in the proposal depends on the names |

Statement order was preserved exactly: Parts 1–5 (role creation, `CONNECT`, schema grants, bulk `ALL TABLES`/`ALL SEQUENCES` grants, membership grant) run first in each database, then Part 6 for A, Part 7 for B, Part 8 for C.

## 22. Executed result matrix

`A: 43/43 passed, observed=0, failed=0, verdict=PASS`
`B: 61/61 passed, observed=0, failed=0, verdict=PASS`
`C: 62/62 passed, observed=0, failed=0, verdict=PASS`
`D1: 35/35 passed, observed=12, failed=0, verdict=PASS`
`D2: 5/5 passed, observed=0, failed=0, verdict=PASS`
`Total failed assertions: 0`

Every intentional negative case raised `InsufficientPrivilege` (`SQLSTATE 42501`).

### 22.1 Alternative A — direct migrator connection (43 assertions)

| Id | Assertion | Recorded actual | Status |
|---|---|---|---|
| A-100 | connect directly as the migrator; identity | `('rehearsal_a_migrator', 'rehearsal_a_migrator')` | PASS |
| A-X10 | role attributes `(rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls)` for both roles | `(True, False, False, False, False, False)` | PASS |
| A-X20 | migrator database privileges `CONNECT`, `CREATE` | `(True, False)` | PASS |
| A-X21 | migrator schema privileges `USAGE`, `CREATE` on `public` | `(True, True)` | PASS |
| A-X22 | app `USAGE` on `public`, no `CREATE` | `(True, False)` | PASS |
| A-X30 | Part 6 default privileges materialised | `rows=2, owners=['rehearsal_a_migrator'], namespaces=['public'], objtypes=['S', 'r']` | PASS |
| A-X31 | the app role appears in every stored default-ACL row | `True` | PASS |
| A-X60 / A-X61 | owner of the migrated table / memberships held by the app | `rehearsal_a_migrator` / `0` | PASS |
| A-X4SE … A-X4TR | app table privileges | `SELECT/INSERT/UPDATE/DELETE True`; `TRUNCATE False`, `REFERENCES False`, `TRIGGER False` | PASS |
| A-X5US … A-X5UP | app sequence privileges | `USAGE True`; `SELECT False`, `UPDATE False` | PASS |
| A-N01/A-N02 | app `INSERT` and `UPDATE` on the migrated table | `OK` | PASS |
| A-N03 | app `CREATE TABLE` in `public` | `42501: permission denied for schema public` | PASS |
| A-N04/A-N05 | app `ALTER`/`DROP` the migrator's table | `42501: must be owner of table rehearsal_orders` | PASS |
| A-N06 | app `CREATE ROLE` | `42501: permission denied to create role` | PASS |
| A-N07 | app `SET ROLE` to the migrator | `42501: permission denied to set role "rehearsal_a_migrator"` | PASS |
| A-N08 | app `ALTER DEFAULT PRIVILEGES` | `42501: permission denied to change default privileges` | PASS |
| A-N09 | app grants the migrator membership | `42501: permission denied to grant role` | PASS |
| A-N10 | app `CREATE SCHEMA` in the database | `42501: permission denied for database rehearsal_a` | PASS |

### 22.2 Alternative B — `INHERIT TRUE` + `ALTER DEFAULT PRIVILEGES FOR ROLE` (61 assertions)

| Id | Assertion | Recorded actual | Status |
|---|---|---|---|
| B-201, B-301, B-302 | Part 7 statements execute in the documented order as the documented actor | `OK` | PASS |
| B-X30 | Part 7 defaults materialised for the migrator | `rows=2, owners=['rehearsal_b_migrator'], namespaces=['public'], objtypes=['S', 'r']` | PASS |
| B-X20 / B-X21 | migrator database `CONNECT`/`CREATE`, schema `USAGE`/`CREATE` | `(True, False)` / `(True, True)` | PASS |
| B-X22 | app schema `USAGE` / `CREATE` | `(True, False)` | PASS |
| B-500 | admin identity before any membership edge | `('rehearsal_b_admin', 'rehearsal_b_admin')` | PASS |
| B-501 | admin without membership changes the migrator's default privileges | `42501: permission denied to change default privileges` | PASS |
| B-512 | `FOR ROLE` form attempted while the membership carries `INHERIT FALSE` | `42501: permission denied to change default privileges` | PASS |
| B-513 / B-522 | admin without `SET` on the edge attempts `SET ROLE` | `42501: permission denied to set role "rehearsal_b_migrator"` | PASS |
| B-531 | admin holding only the `INHERIT TRUE` path changes the migrator's defaults via `FOR ROLE` | `OK` | PASS |
| B-541 | table created by the administrator is owned by the administrator, not the migrator | `rehearsal_b_admin` | PASS |

Objects created by the administrator did **not** receive the migrator's default privileges — B's model applies only to objects the migrator creates, which the proposal's §4.3/§4.5 text already states and which the run confirms.

### 22.3 Alternative C — explicit `SET ROLE` (62 assertions)

| Id | Assertion | Recorded actual | Status |
|---|---|---|---|
| C-201 | Part 8 executes in the documented order | `OK` | PASS |
| C-X30 | Part 8 defaults materialised | `rows=2, owners=['rehearsal_c_migrator'], namespaces=['public'], objtypes=['S', 'r']` | PASS |
| C-501 | `SET ROLE` with `SET TRUE` on every edge | `OK` | PASS |
| C-502 | membership options observed on the edge | `(True, True)` | PASS |
| C-512 | actor/grantor relationship for the membership | `('rehearsal_c_admin', 'rehearsal_c_migrator')` | PASS |
| C-541 | `SET FALSE` edge: `SET ROLE` denied | `42501: permission denied to set role "rehearsal_c_migrator"` | PASS |
| C-542 | same role: `FOR ROLE` default-privilege path still permitted | `OK` | PASS |
| C-551 | `INHERIT FALSE` + `SET TRUE`: `SET ROLE` still permitted | `OK` | PASS |
| C-554 | same role: inherited authority to change the migrator's defaults refused | `42501: permission denied to change default privileges` | PASS |
| C-560 | no membership at all: `SET ROLE` denied | `42501: permission denied to set role` | PASS |
| C-X20 / C-X21 / C-X22 | attributes and app limits identical to A and B | `(True, False)` / `(True, True)` / `(True, False)` | PASS |

`INHERIT` and `SET` are confirmed independent in both directions, which is what `NEON_STAGING_PRIVILEGE_PROPOSAL.md` §4.6 asserts: `INHERIT TRUE, SET FALSE` permits the `FOR ROLE` form but not `SET ROLE`; `INHERIT FALSE, SET TRUE` permits `SET ROLE` but not the inherited `FOR ROLE` authority. `session_user` stayed the administrator while `current_user` became the migrator under `SET ROLE`, and `RESET ROLE` restored the administrator identity.

## 23. Function `EXECUTE` diagnostic (D1) — the substantive finding

The proposal configures table and sequence default privileges only. It contains no function default privileges, and §11 item 5 of Part I flagged the consequence as a hypothesis. That hypothesis is now measured.

Setup: database `rehearsal_func`, schemas `public` and `jobs`, migrator `rehearsal_f_migrator`, app `rehearsal_f_app`, plus a third non-superuser `rehearsal_f_probe` used to watch one ACL entry move.

| Step | Test ids | Observed |
|---|---|---|
| 1. Function created by the migrator with the proposal's defaults in place | D1-101, D1-400 | `proacl` is `NULL` and the app's `EXECUTE` succeeded |
| 2. App-role effective privilege on it | D1-102, D1-103 | `has_function_privilege = True`; the app executed it and returned a row |
| 3. `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` in an otherwise empty arrangement | D1-200, D1-200b, D1-203, D1-204 | `pg_default_acl` for the migrator in `public` is **`[]`** — no row stored; the next function still has `proacl` `<NULL: built-in default ACL>` and the app still executed it |
| 4. A schema-scoped `GRANT EXECUTE … TO rehearsal_f_probe` | D1-213b, D1-215, D1-216, D1-217 | the stored row is `{rehearsal_f_probe=X/rehearsal_f_migrator}`, but the created function's `proacl` is `{=X/rehearsal_f_migrator,rehearsal_f_migrator=X/rehearsal_f_migrator,rehearsal_f_probe=X/rehearsal_f_migrator}` — the probe **and** the app both executed it |
| 5. Repeating the schema-scoped `REVOKE EXECUTE … FROM PUBLIC` after that row exists | D1-218b, D1-220, D1-221 | the row is unchanged (`{rehearsal_f_probe=X/…}`) and the app still executed the new function |
| 6. Global `ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` (no `IN SCHEMA`) | D1-303, D1-303b, D1-304, D1-305 | stored rows `{rehearsal_f_migrator=X/rehearsal_f_migrator}` for the global entry (namespace `-`, object type `f`) and for the `public` entry; created functions in `public` and in `jobs` get `proacl` without a `PUBLIC` entry (`fn_global_jobs` in `jobs`: `{rehearsal_f_migrator=X/rehearsal_f_migrator}`; `fn_global_public` in `public`, with the case-2 probe grant still merged: `{rehearsal_f_migrator=X/…,rehearsal_f_probe=X/…}`), and the app is refused with `42501: permission denied for function …` in both schemas |

**Finding D1.** Under PostgreSQL 16.15 as written, §6 leaves newly created functions executable by `PUBLIC` and therefore by the application role. A schema-scoped `REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` **cannot** remove the built-in `PUBLIC` `EXECUTE` grant: schema-scoped default-ACL rows are merged onto `acldefault()`, so `PUBLIC`'s built-in `EXECUTE` is re-added after the stored row is applied, and when no row exists at all the `REVOKE` stores nothing (`[]`). Only the **global** form of the statement produces an ACL without `PUBLIC` and actually denies the application role.

This is recorded as a measurement. The proposal was **not** modified, no corrective SQL is proposed here, and the rehearsal deliberately does not decide whether function creation is in scope for the migration.

## 24. Login diagnostic (D2)

Two separate containers, so the authentication mechanism is observed rather than assumed.

| Id | Check | Recorded actual | Status |
|---|---|---|---|
| D2-100 | `CREATE ROLE rehearsal_nopw LOGIN;` with no password on the trust service | `OK` | PASS |
| D2-101 | connect as that role on the trust cluster | connected, `current_user = rehearsal_nopw` | PASS |
| D2-201 | `CREATE ROLE rehearsal_nopw_scram LOGIN;` on the second container | `OK` | PASS |
| D2-202 | connect as that role where `pg_hba.conf` says `host all all all scram-sha-256` | `OperationalError: … failed: fe_sendauth: no password supplied` (no SQLSTATE; the failure is client-side before a statement is sent) | PASS |
| D2-203 | catalog check that no credential exists | `no credential stored`; `rolcanlogin = true`, `rolsuper = false` | PASS |

**Finding D2.** A `LOGIN` role created without a password is a valid, login-capable role. Whether it can connect depends entirely on the `pg_hba.conf` method in force: under `trust` it connects with no credential at all; under `scram-sha-256` it cannot, because there is no credential to present. The role's inability to satisfy password authentication is a property of the authentication method, not a property of the role — `peer`, `cert`, `gss`, `sspi` or `trust` rules would still admit it. The proposal's §7 out-of-band credential provisioning remains the operative requirement for password-authenticated deployments, and this diagnostic does not claim the role is unusable everywhere.

## 25. Important security findings

1. **New functions are `PUBLIC`-executable under §6 as written** (D1). A future migration that creates `SECURITY DEFINER` functions in `public` would expose them to every role that can reach the schema, including the application role and any other login role. Measured, not inferred.
2. **The intuitive mitigation does not work.** `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` is a silent no-op on PostgreSQL 16.15 — it stores no row, raises no error, and changes nothing. A future fix that uses the schema-scoped form would appear correct in review and be ineffective in practice; only the global form removes the built-in grant.
3. **Privilege separation itself holds for everything the harness exercised.** In all three alternatives the application role's granted privileges are DML on tables plus `USAGE` on sequences; it cannot create in `public`, cannot alter or drop the migrator's objects (`42501: must be owner`), cannot create roles, cannot `SET ROLE`, cannot grant membership, and cannot change default privileges. Database-level `TEMPORARY` and `CONNECT` were **not** exercised and are not covered by this statement — see §34–§35.
4. **Neither role carries administrative attributes** — `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION` and `BYPASSRLS` are all false for migrator and app alike.
5. **B's defaults are creator-bound.** Objects created by the administrator in B do not inherit the migrator's default privileges, so the operator must create objects as the migrator for the proposal's model to take effect.
6. **`INHERIT` and `SET` are genuinely independent**, and each alternative depends on the option it names: B needs `INHERIT TRUE`, C needs `SET TRUE`. A membership carrying only one of them cannot perform the other alternative's action.

No finding here is a privilege escalation outside the disposable database, and no finding changes the published proposal.

## 26. Limitations

1. **L2 — image digest provenance.** The digest is taken from the runner's `docker pull` output (`Digest: sha256:5db6dac…`); the harness's own `docker inspect .RepoDigests` call returned `unavailable` inside the job, so `CONTAINER_FACTS.image_digest` records that gap.
2. **L3 — artifact binary not downloaded locally.** `archive_download_url` for artifact `13313880536` is an authenticated `api.github.com` endpoint; the unauthenticated attempt returned HTTP `403` (API rate limit). The artifact remains on GitHub until `2026-11-11T10:16:36Z` with the recorded digest, and the **run log archive was downloaded** and is the source of every figure above — the harness prints the same evidence table the artifact carries. A reviewer with repository access can download and compare against `sha256:b17f2667…`.
3. **L4 — harness not retained in the repository.** The two files lived only on the temporary branch, which this task required deleting. Their SHA-256 values are recorded in §17 so a rerun can be proven to use identical inputs, but the text itself is not in git. Reconstruction requires the two files.
4. **L5 — version coverage.** Everything here is PostgreSQL 16.15 (`160015`). Behaviour within 16.x is expected to match, but a Neon-managed minor version was not exercised.
5. **L6 — authentication coverage.** A/B/C ran against a `trust` service. Password-authenticated login behaviour is covered only by the D2 diagnostic; SCRAM-over-TLS and Neon's specific `pg_hba` were not tested.
6. **L7 — action pinning.** Actions are pinned to reviewed major tags (`@v4`, `@v5`), matching the repository's other workflows, not to immutable SHAs. SHA pinning was preferred but not achieved.
7. **L8 — scope.** This is a role-privilege rehearsal on a disposable database. It says nothing about Neon's connection pooling, Render's environment handling, migration ordering in production, or application behaviour.
8. **L9 — harness defects in runs 1–3** (§20). They are documented rather than hidden; the run of record is the fifth.

## 27. Cleanup evidence

| Item | Result | Evidence |
|---|---|---|
| Remote temporary branch deleted | yes | `git push origin --delete temp/pg16-privilege-rehearsal-20261104` → `- [deleted] temp/pg16-privilege-rehearsal-20261104`; `git ls-remote --heads origin 'temp/*'` now returns nothing |
| Local branch deleted | yes | `git branch -D temp/pg16-privilege-rehearsal-20261104` → `Deleted branch … (was 165f70e)` |
| Worktree removed | yes | `git worktree remove .pg16-wt`; `git worktree list` no longer lists it (only the pre-existing `.wt-*` worktrees) |
| PR created | none | `pull_requests: []` on run `41813028902`; PR list filtered to that head branch returns `[]` |
| `main` untouched | yes | `git ls-remote origin refs/heads/main` → `e62f196f7d3530943a8669c6ded41c74b55bf77d`; that commit is dated `2026-10-29T12:29:40Z`, i.e. it predates this task |
| Documentation branch untouched | yes | local and remote both `f56d9922d89a465ad94343f51fcc60f0f41d74bf` |
| Branch protection / Actions settings | unchanged | no setting was read-modify-written; the only GitHub writes were one branch creation, pushes to it, and its deletion |
| PostgreSQL service | ended with the job | GitHub-hosted ephemeral runner; the `postgres:16` service container and the second diagnostic container were destroyed when the job finished — no volume existed, no port was published, nothing survives |
| Run evidence | retained | run `41813028902` and artifact `13313880536` remain on GitHub until 2026-11-11; the run log archive was downloaded to this host |
| Report | local and uncommitted | `?? docs/audits/neon-staging-readiness/POSTGRESQL_16_PRIVILEGE_REHEARSAL.md` in `git status --short`; nothing committed, nothing pushed |

## 28. How to reproduce

1. Create a branch from the recorded base commit and add exactly the two files whose SHA-256 values are in §17.
2. Push it; the workflow's `push` filter starts a single job on `ubuntu-latest` with a disposable `postgres:16` service using `POSTGRES_HOST_AUTH_METHOD: trust` and `contents: read`.
3. The harness creates `rehearsal_a`/`b`/`c` plus the `rehearsal_f` diagnostic database, applies proposal Parts 1–5 then 6/7/8 in order with the §21 substitutions, and records identity, catalog state, `has_*_privilege` results, and SQLSTATE for each intentional negative.
4. Expect 43/61/62 assertions for A/B/C, 35 assertions plus 12 observations for D1, 5 for D2, and `Total failed assertions: 0`.
5. Compare against artifact digest `sha256:b17f2667…` and the recorded image digest.

## 29. Part II conclusion

| Alternative | Executed result |
|---|---|
| A — direct migrator connection | **VALIDATED** on PostgreSQL 16.15: 43/43 |
| B — `INHERIT TRUE` + `FOR ROLE` | **VALIDATED** on PostgreSQL 16.15: 61/61 |
| C — `SET ROLE` | **VALIDATED** on PostgreSQL 16.15: 62/62 |
| D1 — function `EXECUTE` posture | **MEASURED**: `PUBLIC` retains `EXECUTE` on new functions under §6 as written; schema-scoped `REVOKE` is ineffective, global `REVOKE` works |
| D2 — passwordless `LOGIN` role | **MEASURED**: login depends on the `pg_hba.conf` method, not on the presence of a password |

The privilege mechanics the proposal describes for A, B and C behave as described, at the level of role attributes, memberships, default ACLs, ownership and effective grants. The proposal's silence on function default privileges is a real gap with a concrete, measured consequence.

This report records evidence and draws no gate conclusion. Staging implementation, migration execution, Render integration, and any change to the proposal remain unauthorized and pending separate Founder approval.

**Part II status: `EXECUTED — A/B/C VALIDATED ON POSTGRESQL 16.15 — FUNCTION-EXECUTE GAP MEASURED — TEMPORARY BRANCH DELETED`**

---

# Part III — Corrective pass (2026-11-04)

**Prepared by:** independent PostgreSQL 16 security and documentation review, under Founder authority.
**Status:** documentation only. No SQL was executed in this pass. No database, role, privilege, migration, provider, or deployment was touched. Every number below is re-derived from GitHub's copy of run `41813028902` and from the harness at commit `165f70e`.

## 30. Scope and baseline

| Item | Value | How verified |
|---|---|---|
| Repository | `shahid1995/options-dashboard` | `git remote -v` |
| Branch | `docs/neon-staging-readiness-artifacts` | `git rev-parse --abbrev-ref HEAD` |
| Branch HEAD at the start of this pass | `f56d9922d89a465ad94343f51fcc60f0f41d74bf` (local = remote) | `git rev-parse HEAD`; `git ls-remote origin refs/heads/docs/neon-staging-readiness-artifacts` |
| `main` | `e62f196f7d3530943a8669c6ded41c74b55bf77d`, dated 2026-10-29 | `git ls-remote origin refs/heads/main` |
| Temporary rehearsal branch | still deleted; no `refs/heads/temp/*` on origin | `git ls-remote --heads origin 'refs/heads/temp/*'` → empty |
| Run-of-record commit | `165f70e92b71b4b1bf5f00aa0ae6694ee08542fe`, still fetchable from `origin` | `git fetch origin 165f70e…` succeeded |
| Harness at that commit | SHA-256 `6a9bd0535edf7344c06861a90a95174a79af69a7ec74608e1bcf4e684bdedab4` | re-hashed in this pass; matches §17 |
| Workflow at that commit | SHA-256 `5d6ae99d4f4f6b0e85fdfb130c83eb0c38606bcc83e3ad47e36d1501f530400e` | re-hashed in this pass; matches §17 |

Part I (§1–§16) is preserved unmodified. Part II's measurements stand where the evidence supports them; §41 lists every statement this pass corrected, including two object-name errors corrected in place in §21 and §23.

**What this pass is not.** It is not a new rehearsal, not a rerun, not a gate decision, and not an implementation. The disposable instances are gone; nothing here was re-executed against PostgreSQL.

## 31. Run-of-record re-verification

| Item | Value |
|---|---|
| Run | [`41813028902`](https://github.com/shahid1995/options-dashboard/actions/runs/41813028902) — "PostgreSQL 16 privilege rehearsal (temporary)", run number 5, attempt 1 |
| Conclusion | **`success`**, created `2026-11-04T10:16:04Z`, updated `2026-11-04T10:16:41Z` |
| Head commit | `165f70e92b71b4b1bf5f00aa0ae6694ee08542fe` ("test: label recorded observations distinctly from failures") |
| Head branch at run time | `temp/pg16-privilege-rehearsal-20261104` (deleted after the run; the run record retains it) |
| Pull requests associated with the run | `[]` |
| Job | `126516745164`, "PG16 role membership and default privilege rehearsal", `ubuntu-latest`, conclusion `success` |
| Job steps | 13 reported steps, all `success` — including "Confirm PostgreSQL 16 server identity", "Run PostgreSQL 16 privilege rehearsal", "Show sanitized evidence summary", "Upload sanitized evidence", "Stop containers" |
| PostgreSQL | `PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2)`, `server_version_num` `160015` |
| Image | `postgres:16`, digest `sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a`, taken from the runner's `docker pull` output |
| Runner | Ubuntu 26.04.1, runner 2.338.2, `GITHUB_TOKEN` permissions `contents: read` only |
| Action SHAs resolved by the runner | `actions/checkout@v4` → `11d5960a…`, `actions/setup-python@v5` → `a26af69b…`, `actions/upload-artifact@v4` → `ea165f8d…` |

### 31.1 Evidence artifact

| Field | Value |
|---|---|
| Artifact id | `13313880536` |
| Name | `pg16-privilege-rehearsal-evidence` |
| Size | 13 970 bytes |
| Created | `2026-11-04T10:16:37Z` |
| Expires | `2026-11-11T10:16:36Z` (`expired: false` as of this pass) |
| Digest (GitHub metadata) | `sha256:b17f26677b8a01dba1922bad260f71a5ba6b9715da0189b59ff9af1e879808e6` |
| Digest (runner, at upload) | `SHA256 digest of uploaded artifact zip is b17f26677b8a01dba1922bad260f71a5ba6b9715da0189b59ff9af1e879808e6` — identical to the metadata digest and to the digest supplied with this correction task |

**Retrieval limitation (new, L10).** The artifact binary could not be opened. `archive_download_url` is an authenticated `api.github.com` endpoint; an unauthenticated request in this pass returned **HTTP 401**, and no repository credential is available to this review. The evidence in Part III therefore comes from two sources that the job itself produced:

1. the **run log archive** (`shahid1995_options-dashboard_run_41813028902_logs.zip`, downloaded in this pass), which contains the harness's complete 219-record result table; and
2. the `head -40` of `evidence.md` printed by the "Show sanitized evidence summary" step, which shows the same summary table the artifact carries.

Consequence for precision: the harness prints `actual` truncated to **120 characters** on the console. Long values (for example the full `pg_default_acl` dump in D1-300b) are cut in the log and are **not** recoverable without the artifact binary. Where this affects a claim, Part III says so; it does not fill the gap from memory.

## 32. Evidence classes and totals

| Class | Count | Detail |
|---|---:|---|
| Assertions that passed | **206** | A 43, B 61, C 62, D1 35, D2 5 |
| Environment precheck that passed | 1 | `S-001`, server major version `16`; outside the per-alternative tables |
| Total `[ok]` records | 207 | 206 + 1 |
| Failed assertions | **0** | `Total failed assertions: 0`; no `[ERR]` record exists in the log |
| Recorded observations (deliberately not assertions) | **12** | D1-203, D1-204, D1-205, D1-206, D1-215, D1-216, D1-217, D1-220, D1-221, D1-303, D1-303b, D1-400 |
| Total result records | 219 | 207 + 12 |

The 12 observations are **not** added to the 206. They are measurements the harness chose not to assert, and they are what the function finding rests on. The artifact's own summary table reports the same split (`D1 | 47 records | 35 assertions | 35 passed | 12 observed | 0 failed`).

`A: 43/43`, `B: 61/61`, `C: 62/62`, `D1: 35/35 (+12 observed)`, `D2: 5/5` — matching §22.

## 33. Finding 1 — new functions are `PUBLIC`-executable; the schema-scoped `REVOKE` is a no-op

**Class: confirmed observation** (catalog output plus behavioural execution, on PostgreSQL 16.15).

The four D1 cases, as recorded:

| Case | Statement executed by `rehearsal_f_migrator` | Stored `pg_default_acl` | `proacl` of the next function | App role |
|---|---|---|---|---|
| 0 — baseline | *(none; proposal SQL as written)* | — | `NULL` (built-in default) | **executed** |
| 1 | `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` with no row present | `[]` — **nothing stored** | `NULL` | **executed** |
| 2 | `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO rehearsal_f_probe` | `{rehearsal_f_probe=X/rehearsal_f_migrator}` | `{=X/…,rehearsal_f_migrator=X/…,rehearsal_f_probe=X/…}` — `PUBLIC` re-added | **executed** |
| 3 | the same schema-scoped `REVOKE` again, now that a row exists | unchanged, `{rehearsal_f_probe=X/…}` | `{=X/…,…}` — `PUBLIC` still present | **executed** |
| 4 | `ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` (no `IN SCHEMA`) | global row `[rehearsal_f_migrator, "-", "f", "{rehearsal_f_migrator=X/rehearsal_f_migrator}"]` plus the `public` row | `{rehearsal_f_migrator=X/rehearsal_f_migrator}` — **no `PUBLIC` entry** | **refused, `42501`**, in both `public` and `jobs` |

Case 4's refusals are `D1-304` (`permission denied for function fn_global_jobs`) and `D1-305` (`permission denied for function fn_global_public`). In case 4 the `public` function's ACL still carried the case-2 probe grant, merged on top of the global default — per-schema defaults add to global ones, which is why the probe entry survived and `PUBLIC` did not.

**Why this is not a harness artefact.** PostgreSQL 16 documents the exact behaviour, including this statement as its own counter-example:

> "Note however that you cannot accomplish that effect with a command limited to a single schema. This command has no effect, unless it is undoing a matching GRANT: `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;` That's because per-schema default privileges can only add privileges to the global setting, not remove privileges granted by it."
> — [PostgreSQL 16, ALTER DEFAULT PRIVILEGES](https://www.postgresql.org/docs/16/sql-alterdefaultprivileges.html)

The built-in `EXECUTE` grant to `PUBLIC` on functions originates from the global default ([PostgreSQL 16 §5.7](https://www.postgresql.org/docs/16/ddl-priv.html): "EXECUTE privilege for functions and procedures"). Removing it therefore requires a global statement, exactly as measured.

**Scope of the risk, stated without inflation.** No application migration in the approved tree creates a function or a trigger — `grep -rniE "create (or replace )?function|CREATE TRIGGER"` over `alembic/` and `app/` in the target tree returns no match. The exposure is therefore **prospective**: it applies to any future revision, or any hand-run DDL, that creates a function under the migrator. The rehearsal measured PostgreSQL's behaviour; it did not find a live exposure in staging, and it did not look for one (the staging catalog was not read).

**Status of the fix.** The corrected SQL now in `NEON_STAGING_PRIVILEGE_PROPOSAL.md` §6 is **review-only and unexecuted**. The rehearsal executed the *global* statement in a disposable database; the proposal's corrected statements have never been run against Neon or against any of this repository's databases.

## 34. Finding 2 — database `TEMPORARY` is not addressed, and the "DML-only" claim was unqualified

**Class: confirmed gap in the proposal; confirmed absence of an application requirement; founder decision unresolved.**

**PostgreSQL 16 semantics (documented, authoritative).** `TEMPORARY` is a **database**-level privilege, not a schema privilege. The documented default is that `PUBLIC` holds `TEMPORARY` and `CONNECT` on a new database ([§5.7, Table 5.2](https://www.postgresql.org/docs/16/ddl-priv.html): DATABASE — all privileges `CTc`, default `PUBLIC` privileges `Tc`). The proposal's §5 already states the syntax point correctly; it never states the *effective* consequence.

**Does the application need it?** Searched the approved tree (excluding virtualenvs and `node_modules`) for `CREATE TEMP TABLE`, `CREATE TEMPORARY TABLE`, `TEMP TABLE`, `TEMPORARY`, `ON COMMIT … ROWS`:

| Location | Findings | Judgement |
|---|---|---|
| `options-dashboard-project/backend/app/**` | none | runtime does not create temporary tables |
| `options-dashboard-project/backend/alembic/**` | none; `batch_alter_table` used for `create_index`/`add_column`/`drop_column`/`create_foreign_key`/`drop_constraint`, with **no** `recreate=True` | PostgreSQL batch mode emits ordinary `ALTER TABLE`/`CREATE INDEX`; it does not build a temporary table |
| `options-dashboard-project/backend/tests/**` | "temp" only as *filesystem* temp dirs and temporary **SQLite** databases | test tooling, not the runtime path |
| `app/services/upstox_token_manager.py` | "temp → flush → rename" | filesystem atomic write, unrelated to the database |

**Conclusion.** No evidence establishes a requirement for `TEMPORARY` for either the runtime or the migrator. That is *not* the same as evidence that it is safe to revoke: the privilege is currently available to every role through `PUBLIC`, and revoking it from `PUBLIC` affects every non-superuser identity that connects to the database, including provider-managed roles whose needs cannot be established from this repository.

**What the proposal now says.** It no longer describes the runtime as "DML only" without qualification. It records `TEMPORARY` as an open decision, names the exact catalog check and the behavioural check, and states that the strict least-privilege posture is **not yet fully specified**.

**Founder decision required:** whether to leave `PUBLIC` `TEMPORARY` in place, or to `REVOKE TEMPORARY ON DATABASE … FROM PUBLIC` and grant it only to identities with a demonstrated need. Provider-role impact cannot be settled without the provider.

## 35. Finding 3 — `CONNECT` grants do not restrict who may connect

**Class: documented semantics; confirmed gap in the proposal's wording; founder decision unresolved.**

`CONNECT` is likewise a database-level privilege held by `PUBLIC` by default. `GRANT CONNECT … TO strikenova_staging_migrator` and `… TO strikenova_staging_app` are therefore *additive* statements: they guarantee those two roles can connect, and they restrict nothing. `CONNECT` is also checked at connection startup *in addition to* `pg_hba.conf` ([§5.7](https://www.postgresql.org/docs/16/ddl-priv.html)), and superusers bypass object privilege checks entirely.

The rehearsal did not test `CONNECT` either: A/B/C connected over a `trust` cluster where `CONNECT` was never the deciding check. The proposal's earlier wording could be read as connection control; it no longer can.

The proposal now states the two possible postures explicitly and does **not** adopt the strict one:

- **A (current text):** explicit grants to the migrator and app, `PUBLIC` `CONNECT` left intact. This is *not* exclusive connection control, and the document says so.
- **B (strict):** `REVOKE CONNECT ON DATABASE strikenova_staging FROM PUBLIC`, then grant `CONNECT` to the identities that need it. Superusers still connect; every non-superuser provider/service role that relies on `PUBLIC` would need its own grant, which cannot be enumerated from this repository.

Repository evidence does not settle the question: `DATA.md` §5 records the accepted production model as the app role being "DML only: … schema USAGE via PUBLIC", and says nothing about database `CONNECT` or `TEMPORARY`. No accepted decision, ADR, or invariant addresses either privilege.

**Founder decision required:** posture A or B, and if B, the authoritative list of identities that must retain `CONNECT` — which requires provider-side confirmation that this review cannot obtain without contacting Neon.

## 36. Finding 4 — credential-handling wording

**Class: confirmed by reading the harness source and the run log.**

The earlier Part II text said the trust service meant "no credential exists in the repository at all", which is true but incomplete, and Part I's blanket "no credential … was written" describes attempt 1 only. The accurate statement for attempt 2 is:

> A random disposable test password was generated in memory for the optional SCRAM authentication container and passed to it through a temporary environment file with `0600` permissions. The harness did not print or archive it. The harness attempted to unlink the temporary environment file and remove the SCRAM container in its `finally` block. The cleanup command's successful completion is not independently established by the run log. The hosted runner was ephemeral and was removed by the workflow environment after the job. No Neon, Render, CockroachDB, or production credential was accessed or used.

Supporting harness lines at `165f70e` (read, not assumed):

| Line | Code |
|---|---|
| 1010 | `password = secrets.token_urlsafe(24)` — generated in memory |
| 1012–1015 | `tempfile.NamedTemporaryFile("w", delete=False, prefix="pg16-env-")`, write `POSTGRES_PASSWORD=…`, `os.chmod(env_file.name, 0o600)` |
| 1020 | `docker run … --env-file env_file.name -p 5544:5432 postgres:16` — the value never appears in argv |
| 1094–1097 | `finally: os.unlink(env_file.name)` |
| 1098–1099 | `finally: subprocess.run(["docker", "rm", "-f", name])` |

The password does not appear in the run log; the log shows it neither in the step environment nor in any command echo.

**Cleanup verification, stated precisely.** The environment file's deletion and the second container's removal both run in `finally` blocks whose stdout/stderr are captured, so the log does **not** show them succeeding. What the log does show is the hosted runner's own teardown for the service container (`docker rm --force 39da39ab…`, network removed) and the fact that the job ran on an ephemeral runner that is destroyed afterwards. The honest claim is: **the `finally` block issued the removal commands; their success is not independently evidenced in the log; the container and file cannot outlive the ephemeral runner.** §27's earlier phrasing ("were destroyed when the job finished") is true of the runner but overstated what the log proves about the harness's own `docker rm`. Three states must stay distinct: a cleanup command being issued, that command returning success, and the hosting platform destroying the ephemeral runner. Only the first and third are evidenced here; the second is not upgraded by inference (§42, R6).

## 37. Finding 5 — assertion-ID collisions in the historical run

**Class: confirmed by mechanical audit of the run log.** No SQL result is affected; traceability is.

Method: the run log was stripped of timestamps and every result line was matched as `^\[(ok|obs|FAIL)\]\s+(<id>)\s+(<alt>)\s+(<role>)`. Result: **219 records, 204 distinct IDs, 15 IDs used twice, 30 records carrying an ambiguous ID, 0 IDs used more than twice.** No record was altered, merged, or removed.

Four mechanisms, all in the harness at `165f70e`:

| Mechanism | Harness construct | IDs affected |
|---|---|---|
| One ID for two roles in a loop | `check_role_attributes()` emits `{alt}-X10` once per role | A-X10, B-X10, C-X10 |
| Two privileges abbreviated to the same two letters | `f"{alt}-X4{priv[:2]}"` — `TRUNCATE` and `TRIGGER` both give `TR` | A-X4TR, B-X4TR, C-X4TR |
| Role name last character used as a suffix | `f"C-30{role[-1]}"` — `…_noset` and `…_noinherit` both end in `t` | C-30t |
| Shared namespace between setup and diagnostic | `setup_parts_1_to_4()` emits `D1-101/102/201..206`; the function diagnostic uses the same numbers for its own records | D1-101, D1-102, D1-201, D1-202, D1-203, D1-204, D1-205, D1-206 |

**The historical run did not use unique identifiers.** Report-local keys below disambiguate the records for citation; they do not change the harness, the run, or the artifact.

| Report key | Original id | SQL / object | Acting role | Expected | Recorded actual |
|---|---|---|---|---|---|
| A-X10.1 | `A-X10` | `SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = 'rehearsal_a_app'` | `rehearsal_a_app` | `(True, False, False, False, False, False)` | same |
| A-X10.2 | `A-X10` | same query, `… = 'rehearsal_a_migrator'` | `rehearsal_a_migrator` | `(True, False, False, False, False, False)` | same |
| B-X10.1 | `B-X10` | same query, `rehearsal_b_app` | `rehearsal_b_app` | as above | same |
| B-X10.2 | `B-X10` | same query, `rehearsal_b_migrator` | `rehearsal_b_migrator` | as above | same |
| C-X10.1 | `C-X10` | same query, `rehearsal_c_app` | `rehearsal_c_app` | as above | same |
| C-X10.2 | `C-X10` | same query, `rehearsal_c_migrator` | `rehearsal_c_migrator` | as above | same |
| A-X4TR.1 | `A-X4TR` | `SELECT has_table_privilege('rehearsal_a_app','public.rehearsal_orders','TRUNCATE')` | `rehearsal_a_app` | `False` | `False` |
| A-X4TR.2 | `A-X4TR` | same, `'TRIGGER'` | `rehearsal_a_app` | `False` | `False` |
| B-X4TR.1 | `B-X4TR` | `… public.rehearsal_orders … 'TRUNCATE'` as `rehearsal_b_app` | `rehearsal_b_app` | `False` | `False` |
| B-X4TR.2 | `B-X4TR` | same, `'TRIGGER'` | `rehearsal_b_app` | `False` | `False` |
| C-X4TR.1 | `C-X4TR` | same, `'TRUNCATE'` | `rehearsal_c_app` | `False` | `False` |
| C-X4TR.2 | `C-X4TR` | same, `'TRIGGER'` | `rehearsal_c_app` | `False` | `False` |
| C-30t.1 | `C-30t` | `CREATE ROLE rehearsal_c_admin_noset LOGIN` | `postgres` | role created | `OK` |
| C-30t.2 | `C-30t` | `CREATE ROLE rehearsal_c_admin_noinherit LOGIN` | `postgres` | role created | `OK` |
| D1-101.1 | `D1-101` | `CREATE ROLE rehearsal_f_migrator LOGIN` (Part 1 setup) | `postgres` | role created | `OK` |
| D1-101.2 | `D1-101` | `SELECT proacl FROM pg_proc WHERE proname = 'fn_default'` | `postgres` | `None` | `None` |
| D1-102.1 | `D1-102` | `CREATE ROLE rehearsal_f_app LOGIN` (Part 1 setup) | `postgres` | role created | `OK` |
| D1-102.2 | `D1-102` | `SELECT has_function_privilege('rehearsal_f_app','public.fn_default()','EXECUTE')` | `rehearsal_f_app` | `True` | `True` |
| D1-201.1 | `D1-201` | `GRANT CONNECT ON DATABASE rehearsal_func TO rehearsal_f_migrator` (Part 2 setup) | `postgres` | grant succeeds | `OK` |
| D1-201.2 | `D1-201` | `CREATE FUNCTION public.fn_scoped_public() …` | `rehearsal_f_migrator` | creates | `OK` |
| D1-202.1 | `D1-202` | `GRANT CONNECT ON DATABASE rehearsal_func TO rehearsal_f_app` (Part 2 setup) | `postgres` | grant succeeds | `OK` |
| D1-202.2 | `D1-202` | `CREATE FUNCTION jobs.fn_scoped_jobs() …` | `rehearsal_f_migrator` | creates | `OK` |
| D1-203.1 | `D1-203` | `GRANT USAGE, CREATE ON SCHEMA public TO rehearsal_f_migrator` (Part 3 setup) | `postgres` | grant succeeds | `OK` |
| D1-203.2 | `D1-203` | `SELECT proacl … proname = 'fn_scoped_public'` | `postgres` | observation | `<NULL: built-in default ACL>` |
| D1-204.1 | `D1-204` | `GRANT USAGE ON SCHEMA public TO rehearsal_f_app` (Part 3 setup) | `postgres` | grant succeeds | `OK` |
| D1-204.2 | `D1-204` | `SELECT public.fn_scoped_public()` | `rehearsal_f_app` | observation | `executed` |
| D1-205.1 | `D1-205` | `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rehearsal_f_app` (Part 4 setup) | `postgres` | no-op success | `OK` |
| D1-205.2 | `D1-205` | `SELECT proacl … proname = 'fn_scoped_jobs'` | `postgres` | observation | `<NULL: built-in default ACL>` |
| D1-206.1 | `D1-206` | `GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rehearsal_f_app` (Part 4 setup) | `postgres` | no-op success | `OK` |
| D1-206.2 | `D1-206` | `SELECT jobs.fn_scoped_jobs()` | `rehearsal_f_app` | observation | `executed` |

Nothing was renamed in the artifact, and no replacement workflow or branch was created to change identifiers.

**Other harness hygiene issues, recorded rather than fixed:** `actual` is truncated to 120 characters on the console (L10); the `obs` marker shares the failure column position in the log format, which is what earlier runs read as a red run (already corrected in run 5 by the `[obs]` marker); and `C-30{role[-1]}` will collide again for any future pair of role names with a shared last character.

## 38. What this rehearsal proves, and what it does not

**Proves (disposable PostgreSQL 16.15, schema `public` and one second schema):**

1. The proposal's role attributes, schema grants, bulk `ALL TABLES`/`ALL SEQUENCES` grants and membership prerequisites behave as §4 describes, in all three arrangements (206/206).
2. Default table and sequence privileges are bound to the creating role; `FOR ROLE` and `SET ROLE` paths both install them; `INHERIT` and `SET` are independent.
3. The runtime role cannot create objects, alter or drop the migrator's objects, create roles, grant membership, `SET ROLE`, or change default privileges — each refused with `42501`.
4. A schema-scoped `REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` is inert on 16.15, and the global form removes `PUBLIC` `EXECUTE` and denies the runtime role.
5. A passwordless `LOGIN` role is connectable under `trust` and not under `scram-sha-256`; the constraint belongs to the authentication method.

**Does not prove:**

1. Anything about the live `strikenova_staging` database — its current `pg_roles`, `pg_default_acl`, `datacl` or `pg_proc` state was not read.
2. Anything about Neon's `pg_hba.conf`, connection pooling, or provider-managed roles.
3. Anything about `TEMPORARY` or `CONNECT` **as configured** — neither was tested; both statements here are documentation-derived.
4. That the migration creates any function, trigger, or temporary table — it creates none today, which is a property of the pinned tree, not a guarantee about future revisions.
5. That the corrected SQL in the proposal works end-to-end in arrangement A, B or C as a whole: only its function statement was executed, in isolation, in case 4.
6. Anything about Render, Vercel, CockroachDB, deployment, or application behaviour.

## 39. Limitations

L2–L9 of §26 stand. Changes and additions:

| # | Limitation |
|---|---|
| L2 | Image digest provenance: taken from the runner's `docker pull`; the harness's `docker inspect` recorded `unavailable`, including for the second (SCRAM) container, whose digest is **not** substitutable with the first container's digest |
| L3 | Superseded by L10 — the artifact binary was not obtained in either pass |
| L4 | The harness lived only on the deleted temporary branch; it remains retrievable from commit `165f70e` on `origin` (verified in this pass) but is not on any branch |
| L5 | Version coverage: 16.15 only; a Neon-managed minor was not exercised |
| L6 | Authentication coverage: A/B/C ran on `trust`; `CONNECT`/`TEMPORARY` as configured were never exercised |
| L7 | Actions pinned to major tags, not SHAs (the runner resolved and logged the SHAs, §31) |
| L8 | Scope: disposable database only |
| L9 | Harness defects in runs 1–3, documented in §20 |
| **L10** | Artifact binary not retrievable without repository credentials (HTTP 401); console `actual` values are truncated at 120 characters |
| **L11** | 15 assertion IDs collide in the historical run (§37); report-local keys are the only disambiguation |
| **L12** | `TEMPORARY`, `CONNECT` and existing-function privilege state are **untested**; Part III's statements about them are documentation-derived |
| **L13** | Cleanup of the SCRAM container and its env file was executed in `finally` blocks whose output was captured; the log does not prove success (§36) |

## 40. Founder decisions still required

| # | Decision | Why it cannot be settled here |
|---|---|---|
| 1 | `TEMPORARY`: leave `PUBLIC` default, or revoke from `PUBLIC` and grant selectively | No application requirement found; provider-role impact unknowable without Neon |
| 2 | `CONNECT`: posture A (public default, non-exclusive) or B (revoke from `PUBLIC`, grant explicitly) | Superusers and provider/service roles bypass or depend on `PUBLIC`; identity list requires provider confirmation |
| 3 | Function default privileges: accept the global `REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` as a staging control | ADR-018's accepted text covers tables and sequences only; this adds a third default-ACL row and extends the accepted scope |
| 4 | Existing functions, if any exist at gate 2 | Defaults never apply retroactively; an inventory and explicit review are required at that time |
| 5 | Whether `strikenova_staging_owner`'s broad attributes stay (unchanged proposal §9.5) | Unchanged by this pass |

None of these decisions is made, implied, or recommended here.

## 41. Corrections applied in this pass

| # | Document | Was | Now |
|---|---|---|---|
| C1 | This report §21, §23 | diagnostic objects named `rehearsal_private` / database `rehearsal_f` | database `rehearsal_func`, schemas `public` and `jobs` — the names the harness at `165f70e` actually uses |
| C2 | This report §25 item 3, §29 | the app role's privileges described as "DML and sequence `USAGE` only", with no mention of `TEMPORARY` or `CONNECT` | the claim is scoped to what the harness exercised (§25), and §34–§35 state that neither database-level privilege was tested |
| C3 | This report §27 | containers "were destroyed when the job finished" | precise: service container removal is in the log; the SCRAM container's `docker rm` ran in a `finally` whose success is not logged (§36, L13) |
| C4 | This report | implicit "no credential was created" reading | accurate random-password/temp-env-file wording with harness line evidence (§36) |
| C5 | This report | unique-ID assumption in the result tables | 15 collisions declared, 30 records keyed, artifact untouched (§37) |
| C6 | Proposal §6, §4.8, §8, §9, §10 | table/sequence defaults only; "exactly two `pg_default_acl` rows"; function gap unstated | global function `EXECUTE` revoke added for A/B/C; three-row expectation with scope and object type; verification statements for function, `TEMP`, `CONNECT` |
| C7 | Proposal header, §9.2 | "No rehearsal … was used to validate §4 or §6" | corrected: a disposable PostgreSQL 16.15 rehearsal exists and validates A/B/C; the corrected SQL remains unexecuted |
| C8 | Execution plan step 5/7, exit criteria | two rows; no function/TEMP/CONNECT steps | three rows; global function revoke; conditional `TEMP`/`CONNECT` steps marked Founder-gated |
| C9 | Migration preflight §3 | privilege policy not mentioned | records that the chain creates no function, trigger or temporary table, so the open policy does not block the chain; defaults still future-only |
| C10 | README | report absent from the index | report listed with byte/line counts, run link, artifact metadata, and "not a gate approval"; all six counts re-measured |
| C11 | This report §3 (Part I) | one Part I table cell contained a literal pipe character inside a code span, so the row rendered with a spurious extra column | the pipe is escaped as a GFM table escape; the rendered text and every recorded value are unchanged — a rendering fix only, logged here because Part I is otherwise preserved verbatim |

Corrections raised by the post-publication review of 2026-11-05 are indexed separately in §42 (R1–R8); they change procedure and wording, not the measurements above.

**Part III status: `CORRECTED — 206 ASSERTIONS / 12 OBSERVATIONS / 0 FAILURES RE-VERIFIED — FUNCTION-EXECUTE FINDING CONFIRMED AGAINST PostgreSQL 16 DOCUMENTATION — TEMP AND CONNECT REPORTED AS OPEN FOUNDER DECISIONS — 15 ID COLLISIONS DECLARED — CORRECTED SQL REMAINS REVIEW-ONLY AND UNEXECUTED`**

---

# Part IV — Post-publication independent review corrections (2026-11-05)

A review of the published set (commit `5e084ce836ad1c2746de2eae2e82fcb3021ed1a9`) found sequence and wording defects in the **verification plan** and in the **publication metadata**. They are corrected here. No SQL was executed, no database or provider was contacted, no rehearsal result changed, and no Founder decision is made or implied. Where Part IV and an earlier part disagree about a *procedure* or a *count*, Part IV governs; Parts I–III remain the historical record of the 2026-11-04 pass and are otherwise untouched.

## 42. Corrections applied in this pass

| # | Where | Was | Now |
|---|---|---|---|
| R1 | Proposal §8, function `EXECUTE` rows | the first row created the function, read `proacl` and **dropped it**; the next row required calling "the same function … with the probe function still present" — an order that cannot execute | a single ordered lifecycle (§8.2, steps 1–10): create as the migrator, read `proacl` by `regprocedure` OID, verify no `PUBLIC` entry, then invoke as the app role in a **separate session**, and drop only afterwards |
| R2 | Proposal §8, second-schema row | "repeat the probe in a second schema" — with no statement of the app role's `USAGE` requirement and an implicit assumption that a second schema exists | preconditions checked first (`has_schema_privilege` for the app; schema must already exist; `jobs` is a rehearsal name, not a target assumption); no permanent grant, no schema created for the test; if no schema qualifies, the runtime probe is recorded **not executable** and the catalog check plus the disposable rehearsal stand in separately |
| R3 | Proposal §8, `TEMPORARY` probe row | "succeeds if `PUBLIC` `TEMPORARY` is retained, `42501` if it is revoked" — treated a `PUBLIC` revocation as an automatic denial | expectation follows the **effective** privilege for the app identity under the approved policy: `true` → creation must succeed (including when it arrives by direct grant or membership); `false` → `42501`; policy undecided → recorded `NOT EXECUTABLE — POLICY UNDECIDED` |
| R4 | Proposal §8, default-ACL rows | "Exactly **three** rows" plus "Nothing bound to another role" — readable as a database-wide prohibition on any other role's default privileges | **Check 1** asserts exactly three rows for `strikenova_staging_migrator` only (namespace, object type, app ACL entries, no `PUBLIC` entry, no fourth row); **Check 2** inventories the database and *assesses* other roles' rows instead of failing them |
| R5 | Proposal §8 heading and probe rows | "two disposable probes" while three rows were marked `probe` and a second-schema test was described separately | two **probe categories**: the function probe is one multi-step lifecycle, the `TEMPORARY` probe is conditional; a table names, for each, the object, the creating identity, the invoking identity and the cleaning identity |
| R6 | This report §36 | "deleted the file during cleanup" and "removal was executed by a `finally` block" | the harness **attempted** the unlink and the `docker rm` in `finally`; success is not independently established by the log; the three states — command issued, command returned success, runner destroyed — are kept distinct |
| R7 | `README.md` publication record | "this revision of these six files" and a status line ending `SIX FILES` | **five files in the commit** (four modified, one added), **six artifacts catalogued**; `AUDIT_REPORT_CORRECTED.md` was not modified |
| R8 | This report, header | "Where Part II and Part III disagree, Part III governs" | Part IV noted as governing on procedural conflicts; Parts I–III preserved unchanged |

## 43. What this pass did not change

| Item | State |
|---|---|
| Rehearsal results | **unchanged** — 206 passing assertions (A 43, B 61, C 62, D1 35, D2 5), 12 observations, **0 failures**, 219 result records |
| Assertion-ID audit | **unchanged** — 15 IDs used twice, 30 ambiguous records, 204 distinct IDs |
| Evidence classes and limitations | unchanged; L1–L13 stand, and R6 sharpens L13's wording rather than relaxing it |
| Corrected SQL status | **still review-only and unexecuted** as a complete A/B/C arrangement; only the global function statement was ever executed, in isolation, in a disposable database |
| `TEMPORARY` / `CONNECT` | **still untested and still open Founder decisions.** §8.4 now states that no acceptance expectation can be assigned while the `TEMPORARY` policy is undecided |
| Scope of this pass | documentation only: no database contacted, no SQL executed, no role or privilege changed, no migration, deployment or provider setting touched |

This is a review of wording and procedure. It is not a new rehearsal, not a re-run, and not evidence about the live `strikenova_staging` database.

**Part IV status: `PROCEDURE CORRECTED — FUNCTION PROBE SEQUENCED — SECOND-SCHEMA AND TEMPORARY PROBES MADE CONDITIONAL — DEFAULT-ACL ASSERTIONS SCOPED TO THE MIGRATOR — CLEANUP CLAIMS DOWNGRADED TO ATTEMPTS — README COUNTS CORRECTED TO FIVE FILES / SIX ARTIFACTS — REHEARSAL VALUES UNCHANGED`**
