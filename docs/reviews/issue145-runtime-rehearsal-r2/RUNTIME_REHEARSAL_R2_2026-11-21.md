# Issue #145 — T-01…T-27 Runtime Rehearsal R2 (PostgreSQL 16.16)

Second authorized disposable runtime rehearsal of the PostgreSQL lease-protocol
acceptance harness, after the F-1 and F-2 harness corrections. It is separate
from, and does not overwrite, the R1 report.

## Identity

| Item | Value |
| --- | --- |
| Harness-fix commit | `8f1e25ea98e64da6185dec9364ef65d42c813e78` |
| Parent (reviewed starting point) | `fa49b468de8b3c9dbd900dfd77ff7b0603d818a4` |
| Harness SHA-256 | `36c2fab3b4e1790bc7dc983cc6e0d773d1842d93388f822ad35c525e69e08b63` |
| Fix branch | `review/issue145-t01-t27-harness-runtime-fixes` |
| Test-only workflow branch | `test/issue145-runtime-rehearsal-r2` |
| Workflow | https://github.com/shahid1995/options-dashboard/actions/runs/44893528712 |
| Job result | success (gate `GATE: OK`) |
| Artifact | `issue145-runtime-rehearsal-r2` on that run (logs included above) |
| ADR pin | `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` @ `ae2226ef592dc48d5e9619bd2198de33285911df` |

The harness, its README and its results document are the only three files that
differ from `fa49b46` on the fix branch. The ADR and its SQL constants are
unchanged.

## Environment

| Item | Value |
| --- | --- |
| PostgreSQL | `16.16 (Debian 16.16-1.pgdg13+1)` |
| `server_version_num` | `160016` |
| Image | `postgres@sha256:3950ad7c3148ca2175ef1b6c36c9efbf78957ae8fbe8b3a5d7c35af50500382f` |
| Cluster `system_identifier` | `7710566614162509864` (ephemeral, destroyed with the job) |
| Python | `3.12.16` |
| psycopg | `3.3.9` |
| Host port | `127.0.0.1:55432` only |
| Persistence | none: no volume, no restart policy, container destroyed with the runner |
| Authentication | `trust` inside the single-use container only |

No Neon, CockroachDB, Render, staging, production, shared or persistent database
and no provider credential was used.

## Safety gate and guard

- `Safety gate: ACCEPTED` and `Target check: ACCEPTED` after the version probe
  and the read-only target validation.
- A fresh guard nonce was generated inside the job (`secrets.token_hex(24)`),
  masked, and written only into the pre-provisioned `issue145_guard` comment as
  `issue145-harness-guard:<system_identifier>:<nonce>`. The harness verified the
  guard's prefix, cluster identifier and nonce. **The nonce is not recorded in
  this report or in any published log.**
- The harness never created the guard, never wrote its comment and never
  generated the nonce.

## Static verification of the corrections

- `python3 -m py_compile` — passed locally and in the job.
- DB-free self-test: **Total: 20, PASS: 20, FAIL: 0** (exit 0), with the real
  connection counter at zero. The same assertion ran in the job before the
  harness (`Total: 20, PASS: 20, FAIL: 0`).
- SQL constants loaded from the pinned ADR (§6.1 both tables, §6.3–§6.6,
  §10.2) are byte-for-byte unchanged and still match the pinned ADR text; no
  literal backslash-n.
- `git diff --check` clean; exactly the three approved files changed.

## F-1 — function identity corrected

`pg_get_function_identity_arguments()` on PostgreSQL 16 renders argument names
(`p_protocol integer, p_token text, …`), which aborted bootstrap in R1 before any
lease function ran. The §9.3 verifier now reads `pg_proc.proargtypes` — the
catalog's ordered input-type vector — unnests it `WITH ORDINALITY` and renders
each type with `pg_catalog.format_type()`, then compares the ordered array with
the documented sequences. The name-bearing text is not used. Result signature,
owner, `SECURITY DEFINER` and `search_path=pg_catalog` checks are unchanged.

Runtime result: **T-01 PASS**, so the complete §9.3 verifier accepted the
installed schema, constraints, function input types, result signatures, ACLs
and privilege checks on a genuinely empty database. Every scenario that had
failed on this check in R1 (T-01, T-04, T-05, T-06, T-07, T-11, T-12, T-14,
T-15, T-16, T-22, T-27) now passes.

## F-2 — scenario naming aligned, live regression control

`scenario_db_name()` was unchanged and still generates `issue145_t_01`…
`issue145_t_27`; `SCENARIO_DATABASE_NAMES` now holds exactly those 27 values in
the same order.

Live control, before the harness started (disposable cluster only):

```
preseeded_marked_database: issue145_t_01
preseeded_marker_matches_expected: True
databases_before_run: ['issue145_guard', 'issue145_t_01', 'postgres', 'template0', 'template1']
```

The pre-seeded database carried the exact marker
`strikenova-issue145-harness-disposable:<system_identifier>:<nonce>`. The gate
accepted it, T-01 passed, and the ordinary `prepare_scenario_database()` path
dropped and recreated it; it is absent after the run
(`databases_after_run` holds only `issue145_guard` and the system databases).
No other scenario database was pre-created.

## Outcome matrix

| Scenario | Result |
| --- | --- |
| T-01 | PASS |
| T-02 | NOT RUN — needs two independent backends starting §9.2.1 simultaneously |
| T-03 | NOT RUN — needs two independent backends calling acquire concurrently |
| T-04 | PASS |
| T-05 | PASS |
| T-06 | PASS |
| T-07 | PASS |
| T-08 | NOT RUN — needs the real Alembic chain and two backends |
| T-09 | NOT RUN — needs the real backfill implementation |
| T-10 | NOT RUN — needs the real backfill implementation |
| T-11 | PASS |
| T-12 | PASS |
| T-13 | NOT RUN — needs the multi-user backfill, a killed worker and recovery run |
| T-14 | PASS |
| T-15 | PASS |
| T-16 | PASS |
| T-17 | NOT RUN — no Alembic binary/environment in this harness |
| T-18 | NOT RUN — no application migration runner in this harness |
| T-19 | NOT RUN — needs a second blocking session |
| T-20 | NOT RUN — the supported application command does not exist here |
| T-21 | NOT RUN — operator `bootstrap-lease --close-barrier` / `--upgrade-protocol` not implemented |
| T-22 | PASS |
| T-23 | NOT RUN — documented `--upgrade-protocol` flow not implemented; a direct `UPDATE` is not an upgrade |
| T-24 | NOT RUN — needs installed `*_v2` functions and two contending backends |
| T-25 | NOT RUN — `bootstrap-lease` verify command not implemented |
| T-26 | NOT RUN — `--upgrade-protocol` cancellation path not implemented |
| T-27 | PASS |

| Category | Count |
| --- | --- |
| PASS | 12 |
| FAIL | 0 |
| INVALID_SETUP | 0 |
| NOT RUN | 15 |
| INCONCLUSIVE | 0 |
| **Total** | **27** |

Process exit status was `0`; the workflow parsed the summary, confirmed five
counts summing to 27 with 27 matrix rows, and independently enforced that no
scenario was FAIL, INVALID_SETUP or INCONCLUSIVE. No FAIL was converted to PASS
and no expectation was changed to make a scenario pass.

## T-16 — reached, and passed

T-16 executed on a `strikenova_prod_migrator` session against `issue145_t_16`
and passed, which means all of the following held at runtime:

1. `SET ROLE strikenova_prod_owner` took effect (`current_user` = owner) before
   the application tables were created, and `RESET ROLE` returned to the
   migrator.
2. All four table/sequence grants were issued as the owner on a separate
   migrator connection to that scenario database after asserting
   `current_database()` and the owner context (never by the administrator and
   never on `postgres`), and `RESET ROLE` restored the migrator afterwards.
3. The exact privilege matrix held: `positions` SELECT; `paper_orders` SELECT;
   `strategy_leg_exposures` SELECT and INSERT; no other table privilege; sequence
   `USAGE` yes and `SELECT` no.
4. `INSERT … RETURNING id` succeeded as the migrator, while UPDATE/DELETE on
   those tables returned SQLSTATE **`42501`**.
5. Inside the owner window, `ALTER TABLE public.positions …` succeeded and
   `lease_protocol_info_v1()` returned **`42501`**.

## T-11 and T-12 — reached, and passed

Both got beyond the earlier preflight and produced real locking observations:

- **T-11:** while W1 held the lease row lock inside its protected transaction,
  W2's acquire was refused with SQLSTATE **`55P03`**; after the database clock
  passed the short TTL, the pre-commit `lease_enter_v1` raised **`45002`**, the
  protected `INSERT INTO public.positions` rolled back (count `0`), the failed
  transaction released the row lock (`FOR UPDATE NOWAIT` succeeded), and W2 then
  took over at generation `2` with the new owner fingerprint.
- **T-12:** the administrator terminated W1's backend inside the protected
  transaction and the session observed a termination SQLSTATE —
  **`57P01` or `08006`** (the harness accepts either; it does not log which);
  the killed transaction's write rolled back (count `0`), the row lock was
  released, W2's acquire before expiry was refused with **`45002`** with
  ownership unchanged, and after expiry W2 took over at generation `2`.

The harness reports a scenario outcome, not each intermediate value; the
SQLSTATEs above are the ones the passing assertions require.

## Other passing scenarios

- **T-04–T-07:** the acquire/enter/release/info behaviours and their documented
  SQLSTATE rejection paths held.
- **T-14, T-15:** expiry against the database clock and the runtime-role
  privilege matrix (including §10.3 "reaches no role at any depth") held in a
  real `strikenova_production_app` session.
- **T-22, T-27:** the application/operator-facing invariants implemented in this
  harness held.

## Cleanup proof

```
databases_after_run: issue145_guard, postgres, template0, template1
harness_roles_after_run: strikenova_prod_migrator, strikenova_prod_owner, strikenova_production_app
harness_memberships_after_run: ('strikenova_prod_owner','strikenova_prod_migrator',False,True,False)
issue145_databases_after_cleanup: 0
strikenova_roles_after_cleanup: 0
final_databases: ['postgres', 'template0', 'template1']
cleanup: OK (zero issue145_* databases, zero strikenova_* roles)
```

All 27 scenario databases were dropped by the harness during the run. The
cleanup step then terminated only harness backends, dropped the guard, revoked
the owner→migrator membership and dropped the three roles, and failed loudly if
anything had remained. No volume, credential or container survived; the
runner destroyed the service container.

## Workflow note

The push to `test/issue145-runtime-rehearsal-r2` also triggered the repository's
pre-existing **Pylint** workflow (it runs on all pushes and pull requests). That
run failed in `Set up Python 3.8/3.9`: `actions/setup-python@v3` cannot install
those versions on `ubuntu-24.04`. It is a read-only lint job that only checks
out code — it deployed nothing and touched no database. It is unrelated to this
rehearsal and was not modified.

## Remaining unimplemented prerequisites

The 15 NOT RUN scenarios need capabilities this harness deliberately does not
provide: two independent PostgreSQL backends (T-02, T-03, T-24), the real
Alembic chain and backfill implementation (T-08, T-09, T-10, T-13, T-19), the
application migration runner (T-18), and the operator `bootstrap-lease`
`--verify` / `--close-barrier` / `--upgrade-protocol` commands (T-20, T-21,
T-23, T-25, T-26). T-23 remains NOT RUN by design.

## Scope statement

This evidence covers one disposable PostgreSQL 16.16 cluster. It does not
establish production behaviour, does not include the NOT RUN scenarios, and is
not a statement that the whole ADR-017/018 protocol is accepted. Within the 12
executed scenarios, the harness reported PASS and no FAIL. No application code,
migration, ADR, PR, `main`, prior evidence branch or prior R2 report was
changed; nothing was merged, deployed or published as a PR.
