# StrikeNova — Data

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-26

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
| Templates | `StrategyTemplate` (+legs) | User-owned reusable strategy blueprints |

## 4. Conventions

- **Timestamps:** UTC storage, IST market context — standardized per
  `docs/PHASE_7_24_4_TIMEZONE_STANDARDIZATION.md`; no naive `datetime.now()`
  in production paths.
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
