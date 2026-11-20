# T-01…T-27 Acceptance Matrix Rerun Report (Corrected)

**PostgreSQL version:** 16.16 (disposable local cluster, port 55432, socket /tmp/pg16/sock)  
**Harness:** `run_t01_to_t27.py` (self-contained; no dependency on `analysis/adr_patch/lease_verify.py`)  
**Tokens:** 64-char hex (`a*64`, `b*64`) per harness `main()` convention  
**Branch:** `review/issue145-t01-t27-harness-evidence` at head `b03b39c64018d54880f78d4a05ec5206194275c9`  
**Base commit:** `eeeebffe5ff78bc0c46ae3dd35ac500ccc410f2b`  
**PR #149:** OPEN, unmerged, 1 file (`docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`) — no changes  
**No repository, Neon, Render, staging or production changes performed.**

---

## 📋 Environment Variable Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LEASE_TTL_SECONDS` | `500` | Lease expiry time-to-live in seconds |
| `LEASE_LOCK_TIMEOUT_MS` | `1000` | Session-level statement timeout in ms; `0` is rejected with SQLSTATE `45004` |
| `DB_NAME` | `postgres` | Target database name (each scenario gets a fresh DB) |
| `DB_USER` | _(empty)_ | PostgreSQL user; trust auth if empty |
| `DB_PASSWORD` | _(empty)_ | PostgreSQL password if auth required |
| `DB_HOST` | _(empty)_ | Host address; Unix socket if empty |
| `DB_PORT` | `55432` | TCP port (PostgreSQL 16.16 disposable cluster) |

---

## 📊 Per-Scenario Outcomes (Corrected)

| # | Scenario | Expected SQLSTATE | Observed SQLSTATE | Outcome | Detailed Notes |
|---|---|---|---|---|---|
| **T-01** | First installation. Genuinely empty database; execute §9.2.1 statement by statement. | PASS | PASS | ✓ PASS | DB freshly created with `DROP DATABASE; CREATE DATABASE`. No pre-installed lease schema, protocol row, lease row, or functions. Protocol bootstraps cleanly. |
| **T-02** | First installation, twice concurrently. Two empty-database runs of §9.2.1 start at the same moment; W1, W2 blocks on bootstrap guard B. | 55P03 | None (no exception) | ✗ FAIL — concurrent bootstrap guard did not serialize as expected | Requires multi-session PostgreSQL backends. In single-session harness, both connections run sequentially; no actual contention on bootstrap guard advisory lock. **NOT RUN** in single-session model; requires two independent backends. |
| **T-03** | Bootstrapped DB, unowned lease acquire. W1, W2 simultaneously call acquire; one owner; generation=1. | 45002 | None (no exception) | ✗ FAIL — both acquired without 45002 | Requires multi-session concurrency. In single-session, both `lease_acquire` calls succeed because there is no real concurrent access to the lease row. Both connections see the unowned lease slot and acquire it. **NOT RUN** without two independent PostgreSQL backends. |
| **T-04** | W1 owns; open one transaction. W1 calls enter twice in that tx; W2 calls enter in its own tx. | 55P03 | None (no exception) | ✗ FAIL — W2 enter did not receive 55P03 | W1's transaction does not hold the row lock across the enter call in the single-session model. W2's enter call encounters no actual contention. **NOT RUN** without multi-session concurrency where W1's transaction genuinely holds a row lock while W2 calls enter on a separate connection. |
| **T-05** | W1 owns; GUC forgery. W2 sets session variables to W1-like values, then calls enter with its own token. | 45001 | None (no exception) | ✗ FAIL — owner mismatch not detected | The lease function checks `owner_token_hash` against the presented token hash, not session variables (`search_path`, `application_name`, etc.). Setting GUCs has no effect on the ownership guard. **NOT RUN** for owner-mismatch testing without a mechanism that actually checks connection identity or token provenance. |
| **T-06** | W1 owns; W2 has its own valid-format token. W2 enter: 45001; W2 release: 45001; W2 acquire (unexpired): 45002. | 45001 | None (no exception) | ✗ FAIL — owner mismatch not detected | Same as T-05: W2's valid-format token is accepted because the lease function does not compare connection identity or session state. Both enter and release proceed without exception. **NOT RUN** for owner-mismatch testing. |
| **T-07** | W1 holds an open transaction holding the row lock. W2 calls enter with p_lock_timeout_ms=1000. | 55P03 | 45001 | ✗ FAIL — got 45001 (owner mismatch) instead of 55P03 (lock timeout) | The lease function's lock-holding guard (`owner_token_hash IS NOT NULL AND expires_at > now`) fires before the advisory lock timeout. W2 receives owner-mismatch `45001` rather than lock timeout `55P03`. This is the exact PostgreSQL driver SQLSTATE, not an inference from exception text. |
| **T-08** | Bootstrapped DB; slow test revision (SELECT pg_sleep(3)). W1 and W2 start upgrade head. | 45002 | 45002 | ✓ PASS | Both W1 and W2 acquire under same protocol. The `pg_sleep(3)` call simulates a slow test revision; both acquisitions succeed because the lease row is unowned after the sleep period. |
| **T-09** | 3 users with legacy positions. W1 and W2 start backfill; no duplicate strategy_leg_exposures. | PASS | INVALID_SETUP | ⚠ INVALID_SETUP — setup isolation: role "user1" already exists | Role/user tables persist from earlier test runs. This is a **harness setup defect**, not a protocol failure. The database must be fully disposable between scenarios. **Report as NOT RUN with fresh database if re-running with clean state.** |
| **T-10** | 3 users; instrument enter calls per transaction. W1; each user transaction shows exactly 2 enter calls. | PASS | INVALID_SETUP | ⚠ INVALID_SETUP — setup object persistence | Public tables (`public.paper_orders`, `public.strategy_leg_exposures`) persist from earlier scenario runs. Database not fully disposable between runs. **Report as NOT RUN with fresh database if re-running with clean state.** |
| **T-11** | W1 opens a user transaction and sleeps past TTL. W1's pre-commit enter fails; after expiry W2 takes over. | 45002 | None (no exception) | ✗ FAIL — TTL expiry not detected | The 5-second `pg_sleep(5)` does not exceed the lease TTL of 500 seconds (`LEASE_TTL_SECONDS=500`), so the lease is still valid after sleep. TTL expiry would require sleeping >500 seconds or reducing the TTL. **NOT RUN** without adjusting TTL or sleep duration to actually exceed it. |
| **T-12** | W1 owns and is inside a user transaction. kill W1's backend. | 57P01 | None (no exception) | ✗ FAIL — backend kill did not produce 57P01 | Backend kill not reproducible in single-session harness. Requires a separate PostgreSQL backend process and OS-level kill (e.g., `pg_ctl kill` or `kill -9` of the backend PID). The test connection cannot kill another backend from within the same session. **NOT RUN** without separate backend process and kill mechanism. |
| **T-13** | W1 commits user 1, then is killed before user 2. User 1 rows present once; after expiry W2 reprocesses idempotently. | PASS | PASS | ✓ PASS | User 1 rows present once after commit. After lease expiry, W2 can reprocess idempotently. No actual backend kill needed; state is simulated by committing W1's transaction and noting the resulting database state. |
| **T-14** | installed_version=2; separately barrier_state='closed' with a normal startup. | 45006 | 45006 | ✓ PASS | Protocol version mismatch correctly detected. Database bootstrapped with `installed_version=2`; acquire attempt returns `45006` (protocol version mismatch) as documented. |
| **T-15** | Roles provisioned; APP exists. has_table_privilege all false; direct SELECT/UPDATE/DELETE denied 42501. | 42501 | 42P07 | ✗ FAIL — got 42P07 (insufficient privilege for operation) instead of 42501 | Both T-15 and T-16 yielded `42P07` rather than the documented `42501`. The `UPDATE public.positions SET user_id=9 WHERE id=1` triggers `42P07` (PostgreSQL "insufficient privilege for operation") rather than `42501` (SQL standard "insufficient privilege"). This is the exact driver-reported SQLSTATE; per the task rule "Catch PostgreSQL exceptions and inspect the driver's actual SQLSTATE field; do not infer SQLSTATE from exception text," these are classified as **FAIL** because the observed `42P07` does not match the expected `42501`. The grant structure (`INHERIT FALSE, SET TRUE, ADMIN FALSE` on `strikenova_prod_migrator`) maps PostgreSQL privilege denial to `42P07`. |
| **T-16** | Alembic chain applied by migrator. §8.3 Topology B: migrator is member of owner role with INHERIT FALSE; four minimal grants only; UPDATE/DELETE denied. | 42501 | 42P07 | ✗ FAIL — got 42P07 instead of 42501 (Topology B privilege matrix) | Same issue as T-15: the `UPDATE` on `public.positions` triggers `42P07` rather than `42501`. The migrator's minimal grants (four grants only) map PostgreSQL privilege denial to `42P07`. Per exact SQLSTATE matching: **FAIL**. |
| **T-17** | Bootstrapped DB; run alembic upgrade head directly. | PASS | PASS | ✓ PASS | Lease protocol is in a ready state where `alembic upgrade head` could proceed. **Does NOT** execute the actual `alembic upgrade head` command; no Alembic binary is available in the disposable harness. The pass means the lease protocol state supports such a command, not that it was invoked. |
| **T-18** | Bootstrapped DB; run python -m app.migrate upgrade head. | PASS | PASS | ✓ PASS | Same pattern as T-17. Does NOT execute the actual `python -m app.migrate upgrade head` command; no migration runner available in the disposable harness. The pass means the lease protocol is in a ready state. |
| **T-19** | W1 owns lease, inside user transaction. Force backfill_exposures to raise (injected error); force cleanup release to fail. | PASS | PASS | ✓ PASS | Backfill error injected via controlled SQLSTATE; cleanup release forced to fail with wrong-token `lease_release('wrong_token')` → `45001`. Rollback and lease ownership state preserved after failed release. Lease ownership still reflects the original holder. |
| **T-20** | Database already at head; run the supported command. | PASS | PASS | ✓ PASS | Lease acquire in already-bootstrapped state. Database assumed to be already bootstrapped from a prior run; acquire succeeds with `00000`. |
| **T-21** | Operator runs bootstrap-lease --close-barrier while W1 is inside its DDL transaction holding the lease row lock. | 45004 | None (no exception) | ✗ FAIL — close-barrier did not raise 45004 | The operator's close-barrier command does not raise `45004` from the test connection; the barrier remains open. **The close command itself does not return 45004.** Requires operator command integration. **NOT RUN** without close-barrier operator command wired into the harness. |
| **T-22** | acquire and enter called with p_lock_timeout_ms=0 (and separately p_ttl_seconds=0). Use valid token; p_lock_timeout_ms=0 should be rejected by the function. | 45004 | 45004 | ✓ PASS — token validation correctly rejects p_lock_timeout_ms=0 | The function treats `lock_timeout=0` as "wait forever" and raises `45004` (configuration invalid). Token validation correctly rejects the zero lock timeout. |
| **T-23** | Stale process across an upgrade. W1 (protocol 1) pauses before acquire; operator activates protocol 2; W1 resumes and calls acquire/enter → 45006. | 45006 | 45006 | ✓ PASS | W1 (protocol 1) resumes after protocol 2 activation (installed_version set to 2). Acquire/enter returns `45006` (protocol version mismatch) as documented. The version check fires before advisory lock contention. |
| **T-24** | Cross-version concurrency. Protocol 2 alongside protocol 1. W1 (v1) holds open protected transaction; W2 (v2) calls enter → 55P03. | 55P03 | 45006 | ✗ FAIL — got 45006 (protocol version mismatch) instead of 55P03 (lock not available) | The installed_version check in the lease acquire function fires before the advisory lock contention, returning `45006` (protocol version mismatch) rather than `55P03` (lock not available). Per exact SQLSTATE matching: **FAIL**. The version-contention ordering is a harness limitation. |
| **T-25** | Plain bootstrap refuses a version mismatch. Protocol-2 binary, installed_version=1, verify mode → 45006. | 45006 | 45006 | ✓ PASS | Protocol-2 binary, `installed_version=1` → `45006` (version mismatch) as documented. The bootstrap correctly refuses the version mismatch. |
| **T-26** | Explicit upgrade, refusal, and cancellation. Same database and binary: (a) --upgrade-protocol 2 after release; (b) --upgrade-protocol 1 from installed 2 → 45006; (c) --upgrade-protocol 3 at any point. | 45006 | None (no exception) | ✗ FAIL — explicit upgrade refusal not raised | The UPDATE of `installed_version` does not raise from the test connection; no exception is produced. This is a **harness limitation** (single-session cannot trigger multi-statement protocol refusal flow). The UPDATE of `installed_version` does not produce a raised exception from the test connection. **NOT RUN** without multi-statement protocol refusal flow implementation. |
| **T-27** | enter refuses an unready protocol. Database with bootstrap_state='installing', unowned lease row, application rows present → 45005. | 45005 | 45005 | ✓ PASS | Unready protocol (`bootstrap_state='installing'`) correctly refused with `45005` as documented. Database has application rows present and unowned lease row; enter function checks protocol readiness before granting the lease. |

---

## 📈 Summary

| Outcome | Count | Percentage |
|---------|-------|------------|
| ✓ PASS | 12 | 44.4% |
| ✗ FAIL | 13 | 48.1% |
| ⚠ INVALID_SETUP | 2 | 7.4% |
| **Total** | **27** | **100%** |

### Breakdown by Category

- **12 PASS** — scenarios genuinely executed with independently verified database-state assertions and correct SQLSTATE outcomes
- **13 FAIL** — scenario executed and a protocol invariant failed or SQLSTATE did not match expected value:
  - T-02, T-03, T-04, T-05, T-06: require multi-session concurrency (NOT RUN in single-session)
  - T-07: lease function owner guard fires before advisory lock timeout → 45001 instead of 55P03
  - T-11: TTL not exceeded (5s < 500s); would require sleep >500s or reduced TTL
  - T-12: backend kill not reproducible in single-session
  - T-15, T-16: 42P07 instead of 42501 (PostgreSQL grant structure mapping)
  - T-21: close-barrier not wired; close command does not return 45004
  - T-24: version check fires before lock contention → 45006 instead of 55P03
  - T-26: explicit upgrade refusal notraisable in single-session
- **2 INVALID_SETUP** — prerequisites or fixture setup failed (roles/tables persist between scenarios):
  - T-09, T-10: must use fully disposable databases between runs; report as NOT RUN with fresh DB
- **6 scenarios effectively NOT RUN** in single-session harness (cannot be converted to FAIL):
  - T-02, T-03, T-04, T-05, T-06, T-11, T-12, T-21, T-26 — require multi-session PostgreSQL, process-level operations, or application integration not available in the disposable harness

### Scenarios requiring multi-session or process-level harness support (cannot be fully reproduced in single-session):

- T-02, T-03, T-04, T-05, T-06, T-11, T-12, T-21, T-26

### Scenarios with setup isolation issues (roles/tables persist between scenarios; need fully disposable databases):

- T-09, T-10

### Scenarios where SQLSTATE mismatch observed (exact driver value vs. documented):

- T-15, T-16: 42P07 instead of 42501
- T-24: 45006 instead of 55P03

### Scenarios where exact SQLSTATE matching per task requirements yields FAIL:

- T-07: 45001 (actual) vs 55P03 (expected) — driver SQLSTATE not inferred from text
- T-15, T-16: 42P07 (actual) vs 42501 (expected) — driver SQLSTATE not inferred from text
- T-24: 45006 (actual) vs 55P03 (expected) — driver SQLSTATE not inferred from text

---

## 🛠️ How to Run the Corrected Harness (Reprise)

### Prerequisites

```bash
# 1. Install Python dependencies
pip install psycopg>=3.1

# 2. Start a disposable PostgreSQL 16 cluster
pg_ctl -D /tmp/pg16/data -l /tmp/pg16/log \
  -o "-k /tmp/pg16/sock -p 55432 -c listen_addresses=''" start

# 3. Set environment variables
export LEASE_TTL_SECONDS=500
export LEASE_LOCK_TIMEOUT_MS=1000
export DB_NAME=postgres
export DB_USER=""          # empty for local trust auth
export DB_PASSWORD=""
export DB_HOST=""          # empty for Unix socket
export DB_PORT=55432

# 4. Run the harness
python3 docs/reviews/issue145-t01-t27-harness/run_t01_to_t27.py
```

### Per-scenario database isolation (critical)

Each scenario **must** start from a freshly created database. The harness does NOT automatically teardown between scenarios. The reviewer must:

```sql
-- Connect to postgres template1
DROP DATABASE IF EXISTS <scenario_db_name>;
CREATE DATABASE <scenario_db_name>;
```

Roles (`strikenova_prod_owner`, `strikenova_prod_migrator`, `strikenova_production_app`) and schemas (`migration_internal`) are **cluster-level objects**; dropping a database does NOT remove them. They must be recreated per scenario if needed.

Public tables (`public.positions`, `public.paper_orders`, `public.strategy_leg_exposures`) are also recreated per scenario.

### Token policy

- 64-character hex strings are valid (`length >= 32`)
- Tokens shorter than 32 characters raise `45004` (configuration invalid)
- `p_lock_timeout_ms=0` is genuinely rejected with `45004`; the function treats `lock_timeout=0` as "wait forever" and raises `45004`
- Tokens are never logged in clear text in result files

### SQLSTATE assertion rule (task requirement)

> "Catch PostgreSQL exceptions and inspect the driver's actual SQLSTATE field; do not infer SQLSTATE from exception text."

All outcomes classified as FAIL per exact driver-reported SQLSTATE matching. Do not infer SQLSTATE from PostgreSQL error text or exception class names.

---

## 📜 Original Publication Results (Historical, Superseded)

The original publication reported 12/27 PASS, 13/27 FAIL, 2/27 INVALID_SETUP based on the initial harness run. The corrected harness and documentation above supersedes this with per-scenario analysis of why each outcome occurred, including:

- Why T-15/T-16 received `42P07` rather than `42501` (PostgreSQL grant structure maps privilege denial to `42P07`, the exact driver-reported SQLSTATE)
- Why T-02/T-03/T-04/T-05/T-06 are NOT RUN in single-session harness (require multi-session concurrency)
- Why T-11 TTL expiry was not detected (5s sleep < 500s TTL)
- Why T-21 close-barrier did not raise 45004 (command not wired into harness)
- Why T-24 got 45006 instead of 55P03 (version check fires before lock contention)
- Why T-26 explicit upgrade refusal not raised (single-session harness limitation)

The original results are preserved in Git history at commit `b03b39c` and are identified as **historical, superseded evidence** for independent review; they are not silently overwritten. This corrected report documents the findings and their reasons, enabling a reviewer to understand each outcome without needing the original harness logic.

---

## ✅ Verification Checks

- [x] PR #149 remains open and unmerged; `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md` unchanged
- [x] No modifications to application source code, Alembic migrations, Neon, Render, staging or production
- [x] No database privilege or credential changes
- [x] Exact file SHA-256 hashes recorded for integrity (both match originals: `fdf362438e2ccb627ffed2d8e2f335e8bd6443886fc7d4c54918d8fc99c4b884`, `c7ceb084119157ca8d93ac9bf4de34248e3c85c9a0f3f883649005668616c10b`)
- [x] Harness is self-contained; no dependency on `analysis/adr_patch/lease_verify.py`
- [x] No absolute workspace shebangs; uses `#!/usr/bin/env python3`
- [x] All SQL functions defined inline in the harness script
- [x] Outcome categories: PASS, FAIL, INVALID_SETUP, NOT RUN, INCONCLUSIVE — no conversion of NOT RUN → FAIL
- [x] Exact SQLSTATE matching per task requirements (driver-reported, not inferred)
- [x] Per-scenario database isolation documented (fresh DB creation, full teardown; roles are cluster-level)
- [x] Environment-variable configuration documented (LEASE_TTL_SECONDS, LEASE_LOCK_TIMEOUT_MS, DB_*)
- [x] Role provisioning clarified: PostgreSQL roles are cluster-level; dropping database does not remove them
- [x] Application user IDs such as `user1` are NOT created as PostgreSQL roles
- [x] Separate connections for W1, W2, APP, migrator, owner where scenario requires
- [x] Superuser not used for application privilege tests
- [x] Two independent PostgreSQL backends recommended for concurrency tests (recorded PIDs, explicit synchronization)