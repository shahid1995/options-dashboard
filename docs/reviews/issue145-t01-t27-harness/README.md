# Issue #145 — T-01…T-27 Harness Evidence for Independent Review

**Branch:** `review/issue145-t01-t27-harness-evidence`
**Base:** `main` at `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`
**ADR source:** `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` at pinned commit `ae2226ef592dc48d5e9619bd2198de33285911df`
**PR #149:** open, unmerged, one file (`docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`) — **untouched**

---

## Verification status: no runtime SQL verification was performed

PostgreSQL 16.16 is **not available** in this cloud environment (`pg_ctl` is not
present and no server listens on the configured port). The harness therefore
**did not execute any scenario against a server** in this environment.

The harness was run once with no server reachable. It exited cleanly
(`exit status 0`) and produced **27 NOT RUN** outcomes, one per scenario. That
run verifies the unavailability path, not the lease protocol.

No PASS is claimed for any scenario. Every T-01…T-27 outcome in
`results_t01_to_t27_rerun.md` is **NOT RUN**.

---

## What the harness now does

All SQL is copied verbatim from the pinned ADR — §6.1 tables, §6.3–§6.6
functions, §10.2 revoke set and the §9.2.1 first-installation procedure. A
static check in this review extracts the same fenced blocks from the ADR and
asserts the loaded Python string values equal them line for line.

Structural rules the current file enforces:

1. **Real SQL line breaks.** No SQL string contains a literal backslash-n; all
   bodies use real newlines. This is asserted against the *loaded* values, not
   the source comments.
2. **Function bodies match the ADR.** `lease_enter_v1` declares `v_prev` and
   `v_exp`, saves `lock_timeout` into `v_prev`, applies
   `p_lock_timeout_ms`, and restores `v_prev` after the lease row lock — exactly
   as §6.4. `acquire` and `release` also save/restore `lock_timeout`.
3. **Caller-owned transactions.** `run_sql()` never commits or rolls back.
   Scenarios use explicit `conn.transaction()` blocks (nested blocks become
   savepoints). Every call passes the connection first.
4. **Correct SQL module.** The file imports `from psycopg import sql`; it uses
   `sql.SQL` / `sql.Identifier` and no stale alias.
5. **Genuine per-scenario database isolation.** The runner calls
   `drop_create_scenario_db()` for every scenario, connects to that unique
   database, runs the scenario, then calls `drop_scenario_db()`. The helper is
   not merely defined — it is on the execution path.
6. **Unavailability handling.** The first connection and the server-version
   probe are wrapped together. If the server is unreachable, or
   `server_version_num` is below `160016`, the runner reports all 27 NOT RUN and
   exits 0. `SELECT 1` alone is never treated as proof of PostgreSQL 16.16.
7. **Result contracts and error handling.** Successful `acquire`/`enter`
   return the documented 3-column row (fingerprint, expiry, generation);
   `release` returns a boolean; `protocol_info` returns five columns. Errors are
   PostgreSQL exceptions, and SQLSTATE is read from the driver's `sqlstate`
   attribute. Exceptions are never modelled as query rows.
8. **Real topologies.** T-15 and T-16 build actual roles, object ownership,
   `GRANT`/`REVOKE`, membership (`INHERIT FALSE, SET TRUE, ADMIN FALSE`),
   `SET ROLE`/`RESET ROLE` windows and effective-privilege queries.
9. **No unsupported PASS.** T-13, T-17, T-18, T-19, T-20 and T-25 return NOT RUN
   with the missing prerequisite. T-23 is implemented only through the real
   control-row upgrade and the two function calls it asserts.
10. **No stale publication metadata.** This README does not embed a commit SHA
    or file hash for its own publication; those are reported with the commit
    that publishes it.

### Database-local versus cluster-level objects

A database is dropped and recreated per scenario. Roles
(`strikenova_prod_migrator`, `strikenova_prod_owner`,
`strikenova_production_app`) are **cluster-level**: dropping a database does not
remove them. The harness therefore creates them idempotently
(`pg_roles` check) inside the disposable cluster and scopes every object it
creates to the scenario database. The lease schema, lease tables, functions and
application tables are all database-local and vanish with the database.

### SQLSTATE note

`42P07` is `duplicate_table` and `42501` is `insufficient_privilege`; they are
unrelated. The harness never treats `42P07` as a privilege-denial code and never
infers SQLSTATE from an exception message. Where a privilege denial is expected
the assertion is `42501`, read from the driver.

---

## Per-scenario classification (all public outcomes are static; none executed here)

| # | Scenario | Classification | Basis |
|---|---|---|---|
| T-01 | First installation from an empty database | NOT RUN | Implemented; requires PostgreSQL 16.16 |
| T-02 | Concurrent first installation | NOT RUN | Two backends required |
| T-03 | Concurrent acquire on an unowned lease | NOT RUN | Two backends required |
| T-04 | W1 enters twice; second worker refused | NOT RUN | Implemented (two connections); not executed |
| T-05 | GUC forgery cannot substitute for the token | NOT RUN | Implemented; not executed |
| T-06 | Foreign valid-format token is refused | NOT RUN | Implemented; not executed |
| T-07 | Row-lock contention with the correct token | NOT RUN | Implemented (two connections); not executed |
| T-08 | Two workers run `alembic upgrade head` | NOT RUN | Requires the real Alembic chain |
| T-09 | Concurrent backfill of three users | NOT RUN | Requires the real backfill |
| T-10 | Two `enter` calls per backfill transaction | NOT RUN | Requires the real backfill |
| T-11 | TTL genuinely exceeded by the database clock | NOT RUN | Implemented; not executed |
| T-12 | Terminate W1's backend mid-transaction | NOT RUN | Implemented (admin + W1); not executed |
| T-13 | Interrupted backfill and recovery | NOT RUN | Requires the real backfill |
| T-14 | Version mismatch and closed barrier | NOT RUN | Implemented; not executed |
| T-15 | Topology A — runtime APP has no lease privilege | NOT RUN | Implemented; not executed |
| T-16 | Topology B — non-inherited membership and grants | NOT RUN | Implemented; not executed |
| T-17 | `alembic upgrade head` directly | NOT RUN | No Alembic binary |
| T-18 | `python -m app.migrate upgrade head` | NOT RUN | No application runner |
| T-19 | Injected backfill and release failures | NOT RUN | Requires a blocker session |
| T-20 | Supported command on a database already at head | NOT RUN | No application command |
| T-21 | Operator close-barrier sequence | NOT RUN | Requires operator commands |
| T-22 | `p_lock_timeout_ms = 0` / `p_ttl_seconds = 0` | NOT RUN | Implemented; not executed |
| T-23 | Stale protocol-1 worker after upgrade to 2 | NOT RUN | Implemented (two connections); not executed |
| T-24 | Cross-version concurrency | NOT RUN | Requires `*_v2` function set and concurrency |
| T-25 | Plain bootstrap verify refuses a mismatch | NOT RUN | Requires the `bootstrap-lease` command |
| T-26 | Explicit upgrade, refusal and cancellation | NOT RUN | Requires operator commands |
| T-27 | `enter`/`acquire` refuse an unready protocol | NOT RUN | Implemented; not executed |

Totals: **27 NOT RUN, 0 PASS, 0 FAIL, 0 INVALID_SETUP, 0 INCONCLUSIVE.**

A NOT RUN outcome is never also counted as FAIL.

---

## How to run it (only where PostgreSQL 16.16 exists)

```bash
pip install 'psycopg[binary]>=3.1'
export DB_PORT=55432          # disposable PostgreSQL 16.16
export DB_HOST=""             # Unix socket
export DB_USER=""
export DB_PASSWORD=""
export LEASE_TTL_SECONDS=500
export LEASE_LOCK_TIMEOUT_MS=1000
python3 docs/reviews/issue145-t01-t27-harness/run_t01_to_t27.py
```

The runner creates `issue145_t01`, `issue145_t02`, … per scenario from
`template1`, runs the scenario, and drops each database afterwards.

---

## Historical, superseded evidence

The original publication on this branch reported 12 PASS / 13 FAIL /
2 INVALID_SETUP from an earlier harness. That harness did not implement this
protocol faithfully (literal-backslash SQL, caller-committing helper, no real
isolation, SQLSTATE inferred from text, and simulated results modelled as rows).
Those numbers are preserved in Git history as **historical and superseded**;
they are not current results and are not reused anywhere in this document.

---

## Static checks performed for this revision

- `python3 -m py_compile docs/reviews/issue145-t01-t27-harness/run_t01_to_t27.py` — passed.
- Loaded SQL constants compared line-for-line with the pinned ADR blocks for
  §6.1, §6.3, §6.4, §6.5, §6.6 and §10.2 — exact match.
- Loaded SQL constants contain no literal backslash-n — asserted.
- All `run_sql()` calls pass a connection first and use only `params` /
  `fetch_mode`; `run_sql()` contains no commit or rollback.
- `drop_create_scenario_db()` is invoked on the runner path.
- The runner executed with no server: exit status 0, 27 NOT RUN.

No protocol behaviour was exercised against a database in this environment.
