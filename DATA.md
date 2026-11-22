# StrikeNova — Data

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-28

---

## 1. Schema authority

**Alembic is the sole schema authority** ([`DECISIONS.md`](DECISIONS.md)
ADR-002). All DDL flows through `backend/alembic/versions/`. Application code
and tests never issue ad-hoc DDL; existing migrations are never edited to
satisfy tests. SQLAlchemy models (`backend/app/models.py`) mirror the migrated
schema.

## 2. Environments

| Environment | Database | Driver | Notes |
|---|---|---|---|
| Local development | SQLite (`sqlite://` / file) | built-in | Default when `DATABASE_URL` unset; zero external services |
| CI | PostgreSQL 16.16 (pinned service container) | `postgresql+psycopg` | `PostgreSQL compatibility` and Issue #145 application-integration gates |
| Production (accepted target) | **Neon PostgreSQL 16** (ADR-019) | `postgresql+psycopg` | Strategic target only: not provisioned by this work, no cutover performed |
| Production (historical) | CockroachDB Cloud (retired) | `cockroachdb+psycopg` | Retired by ADR-019; evidence retained as historical record, not active target |

Portability is an invariant ([`INVARIANTS.md`](INVARIANTS.md) §6): engine
construction is `DATABASE_URL`-driven (`backend/app/db.py`), and no
production code path may depend on a single vendor dialect where portability
exists today. **Neon PostgreSQL is the accepted strategic production database
(ADR-019); CockroachDB is retired.** Neon provisioning, live role/grant
configuration and cutover are separate founder-authorized work and have not
happened, so no document may present them as complete.

## 3. Data domains

| Domain | Models | Notes |
|---|---|---|
| Identity | `User`, `UserSession` (`app/identity.py`) | Sessions hashed (`hash_session_id`), durable, revocable, TTL'd |
| Broker BYOB | `BrokerConnection`, `BrokerAuthorization`, `BrokerToken` | Encrypted credentials; connection-ownership resolution path |
| Paper trading | positions, executions, journal, templates (`app/models.py`) | Server-authoritative balances and P&L |
| Market data | option chains, candles, Greeks, GEX snapshots/history | Tier-1 backfill + live ingestion (Phases 7.x) |
| Broker sync | `app/broker_sync/` | Ingestion pipeline models |
| Migration serialization | `migration_internal.migration_lease`, `migration_internal.lease_protocol`, four `SECURITY DEFINER` lease functions | ADR-017 PostgreSQL row-lock lease. Schema, tables and functions are owned by the migration identity; PUBLIC and the runtime role hold no schema USAGE/CREATE, table or function privileges, and no global default privilege grants them access. `migration_internal` contains no application data. |
| Durable jobs | `BackgroundJob` (`background_jobs`, `app/models.py`) | Day 47 queue domain: idempotency key (unique), status, attempt count, lease owner/expiry, run-after, dead-letter reason. One row per idempotency key; terminal rows re-armed in place; `DEAD_LETTERED` rows retained for inspection. While a job runs, the worker's heartbeat extends `lease_expires_at` by the effective lease every ~lease/3 via the ownership-checked `renew_lease` (separate session per renewal); both RUNNING-exit transitions (success/failure) persist through the serialization-retry boundary on fresh sessions. Schema owned by Alembic (`d47aa0000001`) |
| Historical data governance | `HistoricalDatasetGovernance`, `HistoricalIngestionRun` | Day 48 catalog/manifest for provenance, source entitlement, usage, redistribution, immutability, recomputation dependencies, completeness/checkpoint snapshots and retention policy. Policy snapshots are append-only audit context; current catalog state remains editable by controlled administration. |
| Templates | `StrategyTemplate` (+legs) | User-owned reusable strategy blueprints |

## 4. Conventions

- **Timestamps:** market-data timestamps are **naive IST (Asia/Kolkata)** —
  IST market context with IST storage — standardized per
  `docs/PHASE_7_24_4_TIMEZONE_STANDARDIZATION.md` and converted at ingestion
  boundaries by `app/utils/market_time.py::to_ist_naive()` (the single
  canonical conversion, so the database never stores UTC market data). This
  covers candle `open_time` (NIFTY, option, Greeks and GEX) and, since Day 49,
  `IVObservation.observed_at`; the point-in-time contract
  (`options-dashboard-project/docs/POINT_IN_TIME_DATASET_CONTRACT.md`)
  normalizes through the same function. No naive `datetime.now()` in
  production paths. Scoped exception: the Day 47 job-scheduling fields
  (`available_at`, `lease_expires_at`, `started_at`, `completed_at`,
  `created_at`, `updated_at` on `background_jobs`) deliberately store and
  compare naive UTC so claim/retry semantics are identical across SQLite,
  PostgreSQL and CockroachDB — see `_utcnow_naive` in `app/models.py`.
- **GEX conventions:** sign, flip/wall, and aggregation definitions are owned
  by `docs/GEX_V1_0_SPEC.md`.
- **Secrets at rest:** broker tokens Fernet-encrypted (`app/crypto.py`);
  session identifiers hashed; never logged in full.
- **Alembic contract tests** guard migration behavior in CI
  (`test_day5_alembic_authority.py`, postgres/migration suites — see
  [`TESTING.md`](TESTING.md)).

## 5. Production privilege model (database `strikenova`)

Verified against live production metadata 2026-09-26. This is the state that
actually exists; changes require a decision record.

**Identities.**

| Role | Login | Purpose | Privileges |
|---|---|---|---|
| `strikenova_production_app` | yes | Serves all application traffic | DML only: SELECT / INSERT / UPDATE / DELETE on all 53 tables; schema USAGE via PUBLIC; no CREATE, no DDL, no memberships, no admin |
| `strikenova_prod_migrator` | yes | Runs Alembic + migration lock | Schema CREATE+USAGE on `public`; DML on all tables; no cluster attributes |
| `strikenova_prod_runtime` | no | Ownership-only role (NOLOGIN) | Owns the 52 application tables; nobody logs in as it |
| `strikenova_prod_admin` | yes | Break-glass superuser (never deleted) | Cluster admin; retained for recovery only |

All 52 application tables are owned by the NOLOGIN `strikenova_prod_runtime`
role; migration-created objects (including `_migration_lock`) are owned by
`strikenova_prod_migrator`.

**Future-table privilege defaults (critical operational state).** `pg_default_acl`
in `strikenova` contains exactly two rows, both bound to the migration/creator
role:

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_prod_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_production_app;
ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_prod_migrator IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO strikenova_production_app;
```

Tables and sequences created by `strikenova_prod_migrator` (i.e. by future
Alembic revisions) therefore automatically carry runtime DML/USAGE. The
runtime identity gains no CREATE/ALTER/DROP/TRUNCATE/ownership/admin through
this mechanism. **The defaults do not follow a creator-role change:** if
migrations ever run under a different role, re-apply the defaults for that
role as part of the change (Invariant 6f, ADR-018). Do not revoke these rows
while the migrator-creator model is in force.

**Migration serialization.** The ADR-017/018 lease lives in its own schema
`migration_internal`, owned by `strikenova_prod_owner`, and consists of the
single-row `migration_lease` table plus the `lease_protocol` control row and
the `SECURITY DEFINER` function set (`acquire_lease`, `enter_lease`,
`release_lease`; `search_path=pg_catalog`; no `PUBLIC` EXECUTE). Outside an
active migration the lease row is unowned (`owner_token_hash IS NULL`) and the
barrier is `open`; `bootstrap_state` is `ready` only after a verified
bootstrap. Only the SHA-256 of the 256-bit owner token is stored. Nobody —
including the runtime role — gets `USAGE` on `migration_internal`, and the
schema's default ACLs grant nothing. The renewal thread is retired: ownership
is the lease row lock, `MIGRATION_LEASE_TTL_SECONDS` (default 300) bounds
recovery of an abandoned row, and
`MIGRATION_LEASE_WAIT_SECONDS`/`MIGRATION_LEASE_LOCK_TIMEOUT_MS` bound
admission. `python -m app.migrate` is the only supported migration path; a
bare `alembic upgrade head` fails closed. Operating detail:
`docs/ADR-017-018-IMPLEMENTATION-RUNBOOK.md`. A standalone CLI run must use
the migration identity (`strikenova_prod_migrator`) unless an explicit
operator URL is intentionally supplied; running it under the DML-only runtime
identity would create future tables that lack the runtime default privileges,
and on PostgreSQL the unguarded path is refused outright.

## 6. Historical data architecture

The Phase 7.x record (persistence foundation, backfill orchestrators, Greeks
reconstruction, coverage audits) lives in `options-dashboard-project/docs/` —
evidence of completed work, not open tasks.

Day 48 adds a governance catalog alongside the existing raw/model/analytics
tables. `HistoricalDatasetGovernance` records source and source-reference
metadata, entitlement/license/usage/redistribution status, tier, immutability,
recomputation dependencies and retention policy. `HistoricalIngestionRun`
snapshots those policy decisions for each acquisition and derives its
checkpoint and completeness metrics from the run-scoped `IngestionCheckpoint`
and `IngestionLog` rows that acquisition produced, without replacing those
records. `DataCompleteness` is cumulative and carries no run identity, so it
is not a source of per-run manifest metrics; a run with no run-scoped evidence
of its own reports `UNKNOWN` completeness rather than being reported complete.
A manifest's `expected_records` and `actual_records` cover the same population:
the work declared by that run's checkpoints and the rows fetched by the
operations that publish exactly those pipelines. Rows fetched by an operation
with no declared expectation (contract metadata, NIFTY candles) are not counted
as actual, though their operations still decide completeness.

Raw market observations remain the recomputation source. Current upstream
entitlement and redistribution status is intentionally `REVIEW_REQUIRED` unless
an explicit governance decision changes it; API availability is not treated as
proof of either. The durable `HISTORICAL_INGESTION` job path gates acquisition
on those recorded rights before anything is fetched, under `DECISIONS.md`
ADR-020. Other existing acquisition paths (the admin `POST
/api/v1/admin/acquisition/run` route and the `run_backfill.py` CLI) acquire
directly through `BackfillOrchestrator` and are outside that rights gate;
they are recorded as known limitations / open follow-ups, not as compliant
paths. Retention execution is dry-run-first, allow-listed and disabled in the
catalog by default; raw-tier deletion is not executable through the Day 48
service.
