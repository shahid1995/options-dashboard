# Issue #145 — PostgreSQL 16.16 disposable runtime rehearsal (2026-11-21)

**Verdict: a real runtime rehearsal did occur, and it did not pass.** All 27
scenarios were executed to their terminal state on a genuine PostgreSQL 16.16
server: **0 PASS, 12 FAIL, 0 INVALID_SETUP, 15 NOT RUN, 0 INCONCLUSIVE**.
All 12 FAILs share one cause — a harness expectation that does not match what
PostgreSQL 16.16 returns from `pg_get_function_identity_arguments()` — and the
failure occurs during bootstrap verification, before any lease behaviour runs.

The harness was not modified, no SQL constant was changed, no scenario result
was reinterpreted, and no failure was repaired. This report separates observed
server evidence from static expectations.

## Provenance

| Item | Value |
|---|---|
| Source commit | `fa49b468de8b3c9dbd900dfd77ff7b0603d818a4` |
| Source parent | `0319225ce3db580e0ede2ea5cf57e84e78b36fbc` |
| Harness SHA-256 (as checked out and verified in CI) | `b4b3d15ab7798afc6164f879d977b987c7f77b6511794ddb46ef66320f63a108` |
| Report branch | `review/issue145-runtime-rehearsal-results` (new, based on `fa49b46`) |
| CI branch (test-only) | `test/issue145-runtime-rehearsal` |
| Workflow run | https://github.com/shahid1995/options-dashboard/actions/runs/44890107543 |
| Artifact | `issue145-runtime-rehearsal` (3.4 KB, 14-day retention) |
| ADR | `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` @ `ae2226ef592dc48d5e9619bd2198de33285911df` (unmodified) |

The workflow checked out `fa49b46` itself (not its own branch), then asserted
`git rev-parse HEAD == fa49b46…` and the harness SHA-256 above before running
anything. The working tree was clean at that point.

## Environment (observed)

| Item | Observed value |
|---|---|
| PostgreSQL | `PostgreSQL 16.16 (Debian 16.16-1.pgdg13+1) on x86_64-pc-linux-gnu` |
| `SHOW server_version` | `16.16 (Debian 16.16-1.pgdg13+1)` |
| `SHOW server_version_num` | `160016` |
| `pg_control_system().system_identifier` | `7710555473963491367` |
| Image | `postgres:16.16`, digest `postgres@sha256:3950ad7c3148ca2175ef1b6c36c9efbf78957ae8fbe8b3a5d7c35af50500382f` |
| Container | `eb1d8466fcf94dc5af7f8494a71aaf20_postgres1616_df5b60` (ephemeral service container) |
| Python | `3.12.16` |
| psycopg | `3.3.9` (`psycopg[binary]>=3.2,<4`) |
| Host / port | `127.0.0.1:55432`, loopback-only port mapping |

The version requirement was enforced, not assumed: the workflow refused to
provision the guard unless `server_version_num == 160016` and
`server_version` began with `16.16`. It matched exactly.

Isolation: GitHub-hosted ephemeral runner, `postgres:16.16` service container,
no volume, no restart policy, no external database, no provider credential and
no application secret. Trust authentication was enabled **only** inside this
single-run container because the harness creates password-less test roles.

## Pre-run guard (operator-provisioned, never by the harness)

1. A 48-character cryptographically random nonce was generated in the workflow.
2. The workflow read the real `system_identifier` from `pg_control_system()`.
3. It created the separate database `issue145_guard` as `postgres`.
4. It wrote `COMMENT ON DATABASE issue145_guard IS 'issue145-harness-guard:<system_identifier>:<nonce>'`
   using a SQL literal (a bind parameter is rejected by PostgreSQL for `COMMENT`).
5. It passed `HARNESS_TARGET_SYSTEM_IDENTIFIER` and `HARNESS_CLUSTER_GUARD`
   through the job environment and masked the nonce in the log.

The nonce is **not** published anywhere: not in this report, not in the logs,
not in the artifact. The harness does not create its own guard.

Evidence that the guard and target checks passed on the real server:

```
Safety gate: ACCEPTED (label 'issue145-disposable'; cluster identity is established independently after the version probe)
PostgreSQL server_version: 16.16 (Debian 16.16-1.pgdg13+1) (server_version_num=160016)
PostgreSQL availability: AVAILABLE (16.16+ within major 16)
Target check: ACCEPTED (verified cluster identity, harness-owned databases, exact role attributes and membership options)
```

`Target check: ACCEPTED` is the point at which the harness has already verified
the guard comment prefix, the guard's `system_identifier` binding, the nonce,
the guard-name allow-list, the exact database inventory and the role/membership
attributes. All of that succeeded against the real catalog.

## Pre-run static checks (same CI job, before the runtime run)

```
python3 -m py_compile  -> exit 0
--self-test            -> Total: 18, PASS: 18, FAIL: 0
```

## Outcome matrix — all 27 scenarios

Exact outcome strings as printed by the harness on the disposable cluster.
"FAIL — §9.3 …" rows are the same assertion (Finding F-1).

| # | Outcome | Detail |
|---|---|---|
| T-01 | FAIL | §9.3 verification failed: `lease_acquire_v1` identity arguments `'p_protocol integer, p_token text, p_ttl_seconds double precision, p_lock_timeout_ms integer' != 'integer, text, double precision, integer'` |
| T-02 | NOT RUN | requires two independent PostgreSQL backends starting §9.2.1 simultaneously; the bootstrap guard serialization cannot be observed from one session |
| T-03 | NOT RUN | requires two independent PostgreSQL backends calling acquire concurrently to observe 45002/55P03 on the loser |
| T-04 | FAIL | same §9.3 signature assertion as T-01 |
| T-05 | FAIL | same §9.3 signature assertion as T-01 |
| T-06 | FAIL | same §9.3 signature assertion as T-01 |
| T-07 | FAIL | same §9.3 signature assertion as T-01 |
| T-08 | NOT RUN | requires the real Alembic chain and two independent backends; no Alembic binary or migration runner exists in this harness |
| T-09 | NOT RUN | requires the real backfill implementation and two independent backends; no backfill code exists in this harness |
| T-10 | NOT RUN | requires the real backfill implementation; the per-user transaction pattern cannot be exercised without it |
| T-11 | FAIL | same §9.3 signature assertion as T-01 |
| T-12 | FAIL | same §9.3 signature assertion as T-01 |
| T-13 | NOT RUN | requires the real multi-user backfill implementation, a killed worker and a recovery run against the same ledger |
| T-14 | FAIL | same §9.3 signature assertion as T-01 |
| T-15 | FAIL | same §9.3 signature assertion as T-01 |
| T-16 | FAIL | same §9.3 signature assertion as T-01 |
| T-17 | NOT RUN | no Alembic binary or environment exists in this harness; the real command cannot be executed |
| T-18 | NOT RUN | no application migration runner exists in this harness; the real command cannot be executed |
| T-19 | NOT RUN | requires one session inside a user transaction plus a blocker session (lease row lock or mid-run EXECUTE revoke); no blocker exists here |
| T-20 | NOT RUN | the supported application command does not exist in this harness; lease state alone is not evidence for T-20 |
| T-21 | NOT RUN | requires the operator `bootstrap-lease --close-barrier` and `--upgrade-protocol` commands, which are not implemented here |
| T-22 | FAIL | same §9.3 signature assertion as T-01 |
| T-23 | NOT RUN | requires the documented `bootstrap-lease --upgrade-protocol` flow; a direct `UPDATE` of `installed_version` is not an upgrade, so it is not exercised |
| T-24 | NOT RUN | requires installed `*_v2` function names and two independent backends contending on the same advisory guard |
| T-25 | NOT RUN | requires the `bootstrap-lease` verify command; it is not implemented here |
| T-26 | NOT RUN | requires the operator `bootstrap-lease --upgrade-protocol` command and its cancellation path |
| T-27 | FAIL | same §9.3 signature assertion as T-01 |

Totals printed by the harness:

```
Total: 27
PASS: 0
FAIL: 12
INVALID_SETUP: 0
NOT RUN: 15
INCONCLUSIVE: 0
```

`harness_exit_code=0`. As the commission states, process exit 0 is not acceptance:
12 scenarios failed and none passed.

## Findings (genuine runtime behaviour, not repaired)

### F-1 — `pg_get_function_identity_arguments()` includes argument names on PG 16.16

Observed, on the first scenario that installs the protocol (T-01):

```
lease_acquire_v1 identity arguments
'p_protocol integer, p_token text, p_ttl_seconds double precision, p_lock_timeout_ms integer'
!= 'integer, text, double precision, integer'
```

The harness reads the signature with
`pg_get_function_identity_arguments(p.oid)` (harness source, `assert_function_signatures`)
and compares it to `EXPECTED_FUNCTIONS["lease_acquire_v1"]`, whose recorded
identity arguments omit the parameter names
(`"integer, text, double precision, integer"`).

On this server the function was created from the pinned ADR §6.3 definition,
which declares named parameters, and PostgreSQL 16.16 renders those names in
the identity-argument string. The comparison therefore fails for every
function whose declaration names its parameters, which is why every
tenant-bootstrap scenario fails identically.

Consequences observed:

- `bootstrap_first_installation()` aborts inside §9.3 verification, so the
  protocol row is never activated and no lease function is called by any
  scenario. This is the harness failing closed, exactly as designed, but it
  means no lease SQL was executed in this rehearsal.
- 12 scenarios (every scenario that installs the protocol) stop at this point.
  The remaining 15 are NOT RUN for their own documented prerequisites.

This is a harness expectation defect, not an ADR defect: the ADR SQL is
unchanged and produced the function. Two candidate corrections (not applied
here):
(a) normalize both sides by stripping parameter names before comparison, or
(b) record the expectation including the declared parameter names.

Which is correct depends on how §9.3's "exactly the documented identity
arguments" is meant to read; that is a review decision, not a rehearsal edit.

### F-2 — scenario database naming is inconsistent with the gate allow-list

Latent, not triggered by this run (the cluster was fresh and every drop
succeeded), but visible in the observed names:

- `scenario_db_name()` builds `"issue145_" + key.lower().replace("-", "_")`,
  which yields `issue145_t_01` … `issue145_t_27`.
- `SCENARIO_DATABASE_NAMES` (the gate's allow-list, also used for the
  guard-name reserved check and in the documentation) is
  `issue145_t01` … `issue145_t27`.

Consequence on a re-run or after an interrupted run: a leftover
`issue145_t_01` database that *does* carry a valid harness ownership marker
would be rejected by `validate_destructive_target()` as "not a harness
scenario database name", and the run would refuse before provisioning. The
guard-name check also would not reject `issue145_t_01` as a guard name.
Recommendation: make the generated name and the allow-list agree (and update
the documentation accordingly), so re-runs are idempotent.

### Environment notes from the workflow (not harness defects)

Two earlier attempts on the same test branch never reached the harness:

1. The workflow initially checked out its own branch, so the pinned-commit
   assertion failed by construction; the checkout was pinned to `fa49b46`.
2. `COMMENT ON DATABASE … IS %s` was rejected by PostgreSQL
   (`syntax error at or near "$1"`) and `pg_auth_members.inherit` does not
   exist on PG 16 (it is `inherit_option`); both were workflow-only fixes.

The first attempt left an `issue145_guard` database in a container that was
destroyed with its runner; no harness run occurred in either attempt and the
repository's reviewed files were never touched.

## T-16 owner-role grant: not observed

T-16 did not reach its grant path. It failed at the same §9.3 bootstrap
verification as T-01, before `grant_topology_b_application_privileges()` was
called. Therefore this rehearsal provides **no** runtime evidence that:

- the separate migrator connection to `issue145_t16` succeeds,
- `SET ROLE strikenova_prod_owner` is permitted by the `SET TRUE, INHERIT FALSE`
  membership in practice,
- the four grants succeed as the owner,
- `RESET ROLE` restores the migrator, or
- the effective privilege matrix and the `42501` DML denials hold.

The static self-test for that path (18/18) still passes, and remains static.

## T-11/T-12 and other lease behaviour: not observed

T-11 and T-12 failed at the same preflight, so no transaction boundary, lock
wait, `statement_timeout`, `pg_terminate_backend`, takeover or SQLSTATE
observation was made. The same applies to T-05/T-06/T-07/T-14/T-15/T-22/T-27
and to the constraint-definition comparison: none of those code paths produced
server evidence in this run.

No unexpected SQLSTATE was observed: no scenario raised a PostgreSQL error at
all, because none executed past the signature comparison. The signature strings
above are catalog renderings, not errors.

## Setup and cleanup evidence

Cluster lifecycle, all observed on the real server:

- `prepare_scenario_database()` created each scenario database, wrote the
  ownership marker and granted connect/create to the migrator and connect to
  the APP role — repeated 12 times without error.
- Cluster roles were provisioned once:
  `strikenova_prod_migrator`, `strikenova_prod_owner`,
  `strikenova_production_app`, with membership
  `('strikenova_prod_owner', 'strikenova_prod_migrator', inherit=False, set=True, admin=False)`
  — i.e. exactly the designed options.
- No cleanup failure was printed by the harness (its cleanup section is absent
  because the mapping is empty), and after the run the cluster held only:

```
databases_after_run:
  issue145_guard
  postgres
  template0
  template1
harness_roles_after_run:
  strikenova_prod_migrator
  strikenova_prod_owner
  strikenova_production_app
harness_memberships_after_run:
  ('strikenova_prod_owner', 'strikenova_prod_migrator', False, True, False)
backends_after_run:
  postgres: 1
  None: 5        # background workers/autovacuum, not scenario sessions
```

All 27 scenario databases were removed by the harness; the guard database and
the three cluster roles remained, as designed (roles are cluster-level).

Final cleanup inside the container, before the runner was destroyed:

```
issue145_databases_after_cleanup: 0
strikenova_roles_after_cleanup: 0
```

The `issue145_guard` database and all three harness roles were dropped. The
job's `Stop containers` step then removed the service container, and the
GitHub-hosted runner is destroyed at job end; the container had no volume and
no restart policy. No persistent server, data volume, credential or provider
resource was created. The test cluster no longer exists.

## Reproduction

The exact workflow is on `test/issue145-runtime-rehearsal`
(`.github/workflows/issue145-runtime-rehearsal.yml`); it pins the checkout to
`fa49b46`, verifies the harness SHA-256, refuses any server other than 16.16,
provisions the guard itself, runs the self-test, runs the harness, records the
catalog state, and uploads `runtime.log`, `selftest.log`, `versions.txt` and
`cleanup.txt`. It has no deploy, publish or release step and triggers only on
pushes to that test branch.

Sanitized harness output for this run:
[harness_output_sanitized.txt](harness_output_sanitized.txt).

## Remaining blockers and recommended next action

1. Resolve F-1 (signature comparison) in the reviewed harness by a deliberate
   review decision, then re-run this same disposable rehearsal. Without that
   decision no lease behaviour can be observed, so the T-01–T-27 protocol
   acceptance remains unproven at runtime.
2. Resolve F-2 (scenario database naming vs. allow-list) so an interrupted run
   can be re-run rather than refused.
3. Re-run, and only then record runtime evidence for the guard, T-16 owner
   grants, T-11/T-12 lock and timeout behaviour, the privilege matrix and the
   `42501` SQLSTATEs.
4. Present the runtime prerequisites for T-02/T-03/T-08/T-09/T-10/T-13/T-17–T-21
   and T-23–T-26 remain genuinely unimplemented; those are NOT RUN by design,
   not by environment limitation.

Every claim above is either an observed server output (quoted) or explicitly
labelled static/expectation. Nothing in this rehearsal asserts that the lease
protocol works.
