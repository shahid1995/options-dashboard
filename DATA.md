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
| CI | PostgreSQL 16 (service container) | `postgresql+psycopg` | `PostgreSQL compatibility` workflow gate |
| Production | **CockroachDB Cloud** | `cockroachdb+psycopg` (+ `sqlalchemy-cockroachdb`) | Runtime-validated; Alembic migrations apply cleanly |

Portability is an invariant ([`INVARIANTS.md`](INVARIANTS.md) §6): engine
construction is `DATABASE_URL`-driven (`backend/app/db.py`), and no
production code path may depend on a single vendor dialect where portability
exists today. Railway-era PostgreSQL assumptions in historical documents are
superseded — production is CockroachDB.

## 3. Data domains

| Domain | Models | Notes |
|---|---|---|
| Identity | `User`, `UserSession` (`app/identity.py`) | Sessions hashed (`hash_session_id`), durable, revocable, TTL'd |
| Broker BYOB | `BrokerConnection`, `BrokerAuthorization`, `BrokerToken` | Encrypted credentials; connection-ownership resolution path |
| Paper trading | positions, executions, journal, templates (`app/models.py`) | Server-authoritative balances and P&L |
| Market data | option chains, candles, Greeks, GEX snapshots/history | Tier-1 backfill + live ingestion (Phases 7.x) |
| Broker sync | `app/broker_sync/` | Ingestion pipeline models |
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
role as part of the change (Invariant 6e, ADR-018). Do not revoke these rows
while the migrator-creator model is in force.

**Migration serialization.** The single-row lease table `_migration_lock`
(ADR-017) is owned by `strikenova_prod_migrator`; the lock must be free
(`locked_by IS NULL`) outside an active migration. The lease TTL must be
>= 2 seconds (`MIGRATION_LOCK_TTL_SECONDS`, default 120; renewal interval
`max(1.0, TTL/3)` — configuration rejects smaller values). The manual
operator CLI path (`alembic upgrade head`) remains outside the application
lock (ADR-017 Residual). It resolves its database with the same precedence
as startup migrations — explicit operator `sqlalchemy.url`, then
`STRIKENOVA_MIGRATION_DATABASE_URL` (the migration identity), then
`DATABASE_URL` — so a standalone CLI run must use the migration identity
(`strikenova_prod_migrator`) unless an explicit operator URL is
intentionally supplied; running it under the DML-only runtime identity
would create future tables that lack the runtime default privileges.

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
