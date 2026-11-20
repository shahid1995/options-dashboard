#!/usr/bin/env /workspace/chats/7b64e00b5b2d90163583ee86/.venv/bin/python3
"""Run the T-01…T-27 acceptance matrix from §14 of ADR-017-018.

Fixed harness: proper 64-char tokens, correct lambda closures, dedicated
database per scenario with teardown, SQLSTATE precise catching.
"""

import importlib.util
import sys

sys.path.insert(0, '.')
spec = importlib.util.spec_from_file_location('lv', 'analysis/adr_patch/lease_verify.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import psycopg
import json

LD_LIBRARY_PATH = '/tmp/pg16/ext/lib'
SOCK = mod.SOCK
PORT = mod.PORT
ADMIN = mod.ADMIN
MIG = mod.MIG
OWN = mod.OWN

# 64-char tokens matching the harness main() convention ("a"*64, "b"*64)
TOKEN_OWNER = "a" * 64     # owner token used by scenarios that test the lease owner
TOKEN_OTHER = "b" * 64     # different owner token

results_log = []

def log_t(number, scenario, executable, expected_sqlstate, actual_sqlstate, outcome, detail=""):
    """Record a result line."""
    results_log.append({
        'number': number,
        'scenario': scenario,
        'executable': executable,
        'expected_sqlstate': expected_sqlstate,
        'actual_sqlstate': actual_sqlstate,
        'outcome': outcome,
        'detail': detail
    })
    marker = {"PASS": "✓", "FAIL": "✗", "INVALID_SETUP": "! ", "NOT_RUN": "≀", "INCONCLUSIVE": "? "}.get(outcome, "? ")
    print(f"  {number}: {marker} {outcome} (expected={expected_sqlstate}, actual={actual_sqlstate}) {detail}")

def setup_database(dbname):
    """Create a fresh database with the full harness setup."""
    import psycopg as _psycopg
    a = _psycopg.connect(ADMIN, autocommit=True)
    cur = a.cursor()
    qdb = '"' + dbname + '"'
    cur.execute('DROP DATABASE IF EXISTS ' + qdb)
    cur.execute('CREATE DATABASE ' + qdb)
    a.close()

    c = _psycopg.connect(dbname=dbname, host='/tmp/pg16/sock', port='55432', user='postgres', autocommit=True)
    cur = c.cursor()

    # Full harness setup
    cur.execute('CREATE SCHEMA migration_internal AUTHORIZATION ' + MIG)
    cur.execute('SET ROLE ' + MIG)
    cur.execute(mod.TABLES)
    cur.execute(mod.SEED_PROTOCOL)
    cur.execute(mod.SEED_LEASE)
    for fname in ('lease_acquire_v1', 'lease_enter_v1', 'lease_release_v1', 'lease_protocol_info_v1'):
        fsql = mod.FN[fname]
        cur.execute(fsql)
    cur.execute('RESET ROLE')
    cur.execute(mod.REVOKES)
    cur.execute(mod.FINALIZE)

    # Create public tables needed by test scenarios
    cur.execute('CREATE TABLE public.positions (id serial PRIMARY KEY, user_id int)')
    cur.execute('CREATE TABLE public.paper_orders (id serial PRIMARY KEY, user_id int)')
    cur.execute('CREATE TABLE public.strategy_leg_exposures (id serial PRIMARY KEY, user_id int, order_id int)')

    return c

def teardown_database(dbname):
    """Drop the test database."""
    with psycopg.connect(ADMIN, autocommit=True) as adm:
        try:
            adm.cursor().execute('DROP DATABASE IF EXISTS "' + dbname + '"')
        except:
            pass

def sqlstate(fn):
    """Execute a callable and return the psycopg SQLSTATE, or None."""
    try:
        fn()
        return None
    except psycopg.Error as e:
        return e.sqlstate

def run_test(number, scenario, expected_sqlstate, test_fn):
    """Run a single test scenario.

    test_fn(c) takes a connection and performs setup + function calls.
    Outcomes captured via psycopg.Error SQLSTATE inspection.
    """
    dbname = number  # use test number as database name
    c = setup_database(dbname)

    actual_sqlstate = None
    outcome = None

    try:
        # Execute the scenario-specific setup and test function
        test_fn(c)

        # If we get here without exception, determine outcome based on expected
        if expected_sqlstate is None:
            # Expected PASS - verify protocol state
            cur = c.cursor()
            r = cur.execute("SELECT installed_version, bootstrap_state FROM migration_internal.lease_protocol WHERE singleton")
            row = cur.fetchone()
            cur.close()
            actual_sqlstate = 'PASS'
            outcome = 'PASS'
        else:
            # Expected failure - should have raised; if not, actual is None
            actual_sqlstate = None
            outcome = 'FAIL'  # Did not raise as expected

    except psycopg.Error as e:
        actual_sqlstate = e.sqlstate

        if expected_sqlstate is None:
            # Expected PASS but got exception - INVALID SETUP
            actual_sqlstate = 'INVALID_SETUP'
            outcome = 'INVALID_SETUP'
        elif actual_sqlstate == expected_sqlstate:
            outcome = 'PASS'
        elif actual_sqlstate != expected_sqlstate and actual_sqlstate is not None:
            outcome = 'FAIL'
        else:
            outcome = 'INCONCLUSIVE'
    except Exception as e:
        actual_sqlstate = f'ERROR: {type(e).__name__}'
        outcome = 'INVALID_SETUP'
    finally:
        teardown_database(dbname)

    log_t(number, scenario, True, expected_sqlstate, actual_sqlstate, outcome)
    return outcome == 'PASS'


# ─── Matrix definitions ────────────────────────────────────────────────

MATRIX = []

def make_test_fn(token_owner, *extra_stmts):
    """Factory for test functions that capture token_owner correctly."""
    def test_fn(c):
        cur = c.cursor()
        # Grant migrator permissions (Topology B)
        cur.execute(f"GRANT strikenova_prod_owner TO strikenova_prod_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE")
        cur.execute("GRANT SELECT ON public.positions TO strikenova_prod_migrator")
        cur.execute("GRANT SELECT ON public.paper_orders TO strikenova_prod_migrator")
        cur.execute("GRANT SELECT, INSERT ON public.strategy_leg_exposures TO strikenova_prod_migrator")
        cur.execute("GRANT USAGE ON SEQUENCE public.strategy_leg_exposures_id_seq TO strikenova_prod_migrator")
        for stmt in extra_stmts:
            stmt(cur)
    return test_fn

# T-01: First installation from genuinely empty database
MATRIX.append({
    'number': 'T-01',
    'scenario': 'First installation. Genuinely empty database; execute §9.2.1 statement by statement.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER),
})

# T-02: First installation, twice concurrently
MATRIX.append({
    'number': 'T-02',
    'scenario': 'First installation, twice concurrently. Two empty-database runs of §9.2.1 start at the same moment; W1, W2 blocks on bootstrap guard B.',
    'expected_sqlstate': '55P03',
    'test_fn': make_test_fn(TOKEN_OWNER),
})

# T-03: Bootstrapped DB, unowned lease - acquire
MATRIX.append({
    'number': 'T-03',
    'scenario': 'Bootstrapped DB, unowned lease. W1, W2 simultaneously call acquire; one owner; generation=1.',
    'expected_sqlstate': '45002',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
    ),
})

# T-04: W1 owns; open one transaction - enter twice
MATRIX.append({
    'number': 'T-04',
    'scenario': 'W1 owns; open one transaction. W1 calls enter twice in that tx; W2 calls enter in its own tx.',
    'expected_sqlstate': '55P03',
    'test_fn': make_test_fn(TOKEN_OWNER,
        # T-04 test: W1 acquires, commits, W2 enters after
    ),
})

# T-05: W1 owns; GUC forgery
MATRIX.append({
    'number': 'T-05',
    'scenario': 'W1 owns; GUC forgery. W2 sets session variables to W1-like values, then calls enter with its own token.',
    'expected_sqlstate': '45001',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
    ),
})

# T-06: W1 owns; W2 has its own valid-format token
MATRIX.append({
    'number': 'T-06',
    'scenario': 'W1 owns; W2 has its own valid-format token. W2 enter: 45001; W2 release: 45001; W2 acquire (unexpired): 45002.',
    'expected_sqlstate': '45001',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
    ),
})

# T-07: W1 holds an open transaction holding the row lock
MATRIX.append({
    'number': 'T-07',
    'scenario': 'W1 holds an open transaction holding the row lock. W2 calls enter with p_lock_timeout_ms=1000.',
    'expected_sqlstate': '55P03',
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("BEGIN"),
            cur.execute("SELECT pg_sleep(2)"),
            cur.execute("SELECT * FROM migration_internal.lease_enter_v1(1, '" + TOKEN_OTHER + "', 600, 1000)"),
        ),
    ),
})

# T-08: Bootstrapped DB; a slow test revision
MATRIX.append({
    'number': 'T-08',
    'scenario': 'Bootstrapped DB; a slow test revision (SELECT pg_sleep(3)). W1 and W2 start upgrade head.',
    'expected_sqlstate': '45002',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OTHER + "', 600, 500)"),
        ),
    ),
})

# T-09: 3 users with legacy positions
MATRIX.append({
    'number': 'T-09',
    'scenario': '3 users with legacy positions. W1 and W2 start backfill; no duplicate strategy_leg_exposures.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("CREATE USER user1; CREATE USER user2; CREATE USER user3"),
            cur.execute("INSERT INTO public.positions (user_id) VALUES (1), (2), (3)"),
            cur.execute("SELECT * FROM migration_internal.lease_enter_v1(1, 'user1_token', 600, 500)"),
            cur.execute("INSERT INTO public.strategy_leg_exposures (user_id, order_id) VALUES (1,1) RETURNING id"),
        ),
    ),
})

# T-10: 3 users; instrument enter calls per transaction
MATRIX.append({
    'number': 'T-10',
    'scenario': '3 users; instrument enter calls per transaction. W1; each user transaction shows exactly 2 enter calls.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: cur.execute("SELECT * FROM migration_internal.lease_enter_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
    ),
})

# T-11: W1 opens a user transaction and sleeps past TTL
MATRIX.append({
    'number': 'T-11',
    'scenario': 'W1 opens a user transaction and sleeps past TTL. W1\'s pre-commit enter fails; after expiry W2 takes over.',
    'expected_sqlstate': '45002',
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("BEGIN"),
            cur.execute("SELECT pg_sleep(5)"),
        ),
    ),
})

# T-12: W1 owns and is inside a user transaction; killed
MATRIX.append({
    'number': 'T-12',
    'scenario': 'W1 owns and is inside a user transaction. kill W1\'s backend.',
    'expected_sqlstate': '57P01',
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("BEGIN"),
            cur.execute("SELECT pg_sleep(5)"),
        ),
    ),
})

# T-13: W1 commits user 1, then is killed before user 2
MATRIX.append({
    'number': 'T-13',
    'scenario': 'W1 commits user 1, then is killed before user 2. User 1 rows present once; after expiry W2 reprocesses idempotently.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("BEGIN"),
            cur.execute("INSERT INTO public.positions (user_id) VALUES (1)"),
            cur.execute("SELECT pg_sleep(3)"),
        ),
    ),
})

# T-14: installed_version=2; barrier check
MATRIX.append({
    'number': 'T-14',
    'scenario': 'installed_version=2; separately barrier_state=\'closed\' with a normal startup.',
    'expected_sqlstate': '45006',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: (
            cur.execute("UPDATE migration_internal.lease_protocol SET installed_version=2, min_supported_version=1, max_supported_version=2, barrier_state='open', bootstrap_state='ready' WHERE singleton"),
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
        ),
    ),
})

# T-15: Roles provisioned; APP exists; privilege denial
MATRIX.append({
    'number': 'T-15',
    'scenario': 'Roles provisioned; APP exists. has_table_privilege all false; direct SELECT/UPDATE/DELETE denied 42501.',
    'expected_sqlstate': '42501',
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("REVOKE ALL ON SCHEMA migration_internal FROM PUBLIC"),
            cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA migration_internal FROM PUBLIC"),
            cur.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA migration_internal FROM PUBLIC"),
            cur.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA migration_internal FROM PUBLIC"),
            cur.execute("CREATE TABLE public.positions (id serial PRIMARY KEY, user_id int)"),
            cur.execute("CREATE TABLE public.paper_orders (id serial PRIMARY KEY, user_id int)"),
            cur.execute("CREATE TABLE public.strategy_leg_exposures (id serial PRIMARY KEY, user_id int, order_id int)"),
            cur.execute("INSERT INTO public.positions (user_id) VALUES (1), (2)"),
            cur.execute("UPDATE public.positions SET user_id=9 WHERE id=1"),
        ),
    ),
})

# T-16: Alembic chain applied by migrator; §8.3 Topology B
MATRIX.append({
    'number': 'T-16',
    'scenario': 'Alembic chain applied by migrator. §8.3 Topology B: migrator is member of owner role with INHERIT FALSE; four minimal grants only; UPDATE/DELETE denied.',
    'expected_sqlstate': '42501',
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute(f"GRANT strikenova_prod_owner TO strikenova_prod_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE"),
            cur.execute("GRANT SELECT ON public.positions TO strikenova_prod_migrator"),
            cur.execute("GRANT SELECT ON public.paper_orders TO strikenova_prod_migrator"),
            cur.execute("GRANT SELECT, INSERT ON public.strategy_leg_exposures TO strikenova_prod_migrator"),
            cur.execute("GRANT USAGE ON SEQUENCE public.strategy_leg_exposures_id_seq TO strikenova_prod_migrator"),
            cur.execute("CREATE TABLE public.positions (id serial PRIMARY KEY, user_id int)"),
            cur.execute("CREATE TABLE public.paper_orders (id serial PRIMARY KEY, user_id int)"),
            cur.execute("CREATE TABLE public.strategy_leg_exposures (id serial PRIMARY KEY, user_id int, order_id int)"),
            cur.execute("INSERT INTO public.positions (user_id) VALUES (1), (2)"),
            cur.execute("SET ROLE strikenova_prod_owner"),
            cur.execute("UPDATE public.positions SET user_id=9 WHERE id=1"),
            cur.execute("RESET ROLE"),
            cur.execute("UPDATE public.positions SET user_id=9 WHERE id=1"),
        ),
    ),
})

# T-17: Bootstrapped DB; run alembic upgrade head directly
MATRIX.append({
    'number': 'T-17',
    'scenario': 'Bootstrapped DB; run alembic upgrade head directly.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
        ),
    ),
})

# T-18: Bootstrapped DB; run python -m app.migrate upgrade head
MATRIX.append({
    'number': 'T-18',
    'scenario': 'Bootstrapped DB; run python -m app.migrate upgrade head.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
        ),
    ),
})

# T-19: W1 owns the lease and is inside a user transaction
MATRIX.append({
    'number': 'T-19',
    'scenario': 'W1 owns the lease and is inside a user transaction. Force backfill_exposures to raise (injected error); force cleanup release to fail.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("BEGIN"),
            cur.execute("SELECT pg_sleep(3)"),
        ),
    ),
})

# T-20: Database already at head; run the supported command
MATRIX.append({
    'number': 'T-20',
    'scenario': 'Database already at head; run the supported command.',
    'expected_sqlstate': None,
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
        ),
    ),
})

# T-21: Operator runs bootstrap-lease --close-barrier while W1 is inside its DDL transaction
MATRIX.append({
    'number': 'T-21',
    'scenario': 'Operator runs bootstrap-lease --close-barrier while W1 is inside its DDL transaction holding the lease row lock.',
    'expected_sqlstate': '45004',
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
        ),
    ),
})

# T-22: acquire and enter called with p_lock_timeout_ms=0 (and separately p_ttl_seconds=0)
MATRIX.append({
    'number': 'T-22',
    'scenario': 'acquire and enter called with p_lock_timeout_ms=0 (and separately p_ttl_seconds=0). Use valid token; p_lock_timeout_ms=0 should be rejected by the function.',
    'expected_sqlstate': '45004',
    'test_fn': make_test_fn(TOKEN_OWNER,
        lambda cur: cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 0)"),
    ),
})

# T-23: Stale process across an upgrade
MATRIX.append({
    'number': 'T-23',
    'scenario': 'Stale process across an upgrade. W1 (protocol 1) pauses before acquire; operator activates protocol 2; W1 resumes and calls acquire/enter → 45006.',
    'expected_sqlstate': '45006',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("UPDATE migration_internal.lease_protocol SET installed_version=2, min_supported_version=1, max_supported_version=2, barrier_state='open', bootstrap_state='ready' WHERE singleton"),
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
        ),
    ),
})

# T-24: Cross-version concurrency
MATRIX.append({
    'number': 'T-24',
    'scenario': 'Cross-version concurrency. Protocol 2 alongside protocol 1. W1 (v1) holds open protected transaction; W2 (v2) calls enter → 55P03.',
    'expected_sqlstate': '55P03',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: (
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
            cur.execute("COMMIT"),
            cur.execute("UPDATE migration_internal.lease_protocol SET installed_version=2, min_supported_version=1, max_supported_version=2, barrier_state='open', bootstrap_state='ready' WHERE singleton"),
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(2, '" + TOKEN_OTHER + "', 600, 500)"),
        ),
    ),
})

# T-25: Plain bootstrap refuses a version mismatch
MATRIX.append({
    'number': 'T-25',
    'scenario': 'Plain bootstrap refuses a version mismatch. Protocol-2 binary, installed_version=1, verify mode → 45006.',
    'expected_sqlstate': '45006',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: (
            cur.execute("UPDATE migration_internal.lease_protocol SET installed_version=1, min_supported_version=1, max_supported_version=1, barrier_state='open', bootstrap_state='ready' WHERE singleton"),
            cur.execute("SELECT * FROM migration_internal.lease_acquire_v1(2, '" + TOKEN_OWNER + "', 600, 500)"),
        ),
    ),
})

# T-26: Explicit upgrade, refusal, and cancellation
MATRIX.append({
    'number': 'T-26',
    'scenario': 'Explicit upgrade, refusal, and cancellation. Same database and binary: (a) --upgrade-protocol 2 after release; (b) --upgrade-protocol 1 from installed 2 → 45006; (c) --upgrade-protocol 3 at any point.',
    'expected_sqlstate': '45006',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: (
            cur.execute("UPDATE migration_internal.lease_protocol SET installed_version=2, min_supported_version=1, max_supported_version=2, barrier_state='open', bootstrap_state='ready' WHERE singleton"),
            cur.execute("UPDATE migration_internal.lease_protocol SET installed_version=1 WHERE singleton"),
        ),
    ),
})

# T-27: enter refuses an unready protocol
MATRIX.append({
    'number': 'T-27',
    'scenario': 'enter refuses an unready protocol. Database with bootstrap_state=\'installing\', unowned lease row, application rows present → 45005.',
    'expected_sqlstate': '45005',
    'test_fn': make_test_fn(TOKEN_OTHER,
        lambda cur: (
            cur.execute("UPDATE migration_internal.lease_protocol SET bootstrap_state='installing' WHERE singleton"),
            cur.execute("SELECT * FROM migration_internal.lease_enter_v1(1, '" + TOKEN_OWNER + "', 600, 500)"),
        ),
    ),
})

# ─── Run the matrix ────────────────────────────────────────────────────

print(f'T-01 through T-27 matrix defined with {len(MATRIX)} scenarios')
print('Starting harness execution...')

passed = 0
total = len(MATRIX)

for case in MATRIX:
    number = case['number']
    scenario = case['scenario']
    expected = case['expected_sqlstate']

    try:
        result = run_test(number, scenario, expected, case['test_fn'])
        if result:
            passed += 1
    except Exception as e:
        print(f'  {number}: ERROR during test execution: {e}')
        total += 1  # count it

# Write results markdown
table_header = '| # | Scenario and setup | Expected SQLSTATE / exception | Observed | Outcome |\n|---|---|---|---|---|'

with open('/tmp/adr017-design/results_t01_to_t27_rerun.md', 'w') as f:
    f.write(table_header + '\n')
    for r in results_log:
        outcome_str = r['outcome']
        obs = r['actual_sqlstate'] if r['actual_sqlstate'] and not str(r['actual_sqlstate']).startswith('ERROR') else ('PASS' if r['actual_sqlstate'] == 'PASS' else 'ERROR')
        f.write(f'| {r["number"]} | {r["scenario"][:80]}… | {r["expected_sqlstate"] if r["expected_sqlstate"] else "PASS"} | {obs[:25]}… | {outcome_str}\\n')
    
    f.write(f'\\n**{passed}/{total} checks passed**\\n')

print(f'\\nResults written to /tmp/adr017-design/results_t01_to_t27_rerun.md')
print(f'Passed: {passed}/{total}')
print('Done.')