# StrikeNova — Architecture

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-28

Ground truth is the code; this document maps it. Deep-dive phase documents
live under `options-dashboard-project/docs/` (historical evidence).

---

## 1. Deployment topology (current truth)

```text
Browser
   ↓  HttpOnly strikenova_session cookie (secure session transport)
Vercel — Next.js frontend (options-dashboard-project/frontend)
   ↓  HTTPS REST (axios, withCredentials) + cookie-authenticated WebSocket
Render — FastAPI backend, uvicorn (options-dashboard-project/backend)
   ↓  SQLAlchemy 2.x + psycopg / sqlalchemy-cockroachdb
CockroachDB Cloud — production database (Alembic-managed schema)
```

- Staging deployments mirror this shape (Vercel + Render free tiers,
  documented in `docs/architecture/VERCEL_STAGING_DEPLOYMENT.md` and
  `RENDER_STAGING_DEPLOYMENT.md`).
- **Railway is historical** (superseded staging experiment) — never present it
  as current production truth ([`DECISIONS.md`](DECISIONS.md) ADR-004).
- Portability: SQLite (local), PostgreSQL (CI service container), CockroachDB
  (production target) — [`DECISIONS.md`](DECISIONS.md) ADR-003.

## 2. Backend (FastAPI)

| Layer | Location | Responsibility |
|---|---|---|
| API | `backend/app/routers/` | auth, paper, gex, chains, candles, resolve, templates, annotations, broker_diagnostics, historical_gex, live_gex |
| Auth dependencies | `backend/app/routers/deps.py` | Single canonical session resolver (`_canonical_session_id`/`get_session_id`); `CurrentUser`/`get_current_user` resolve platform identity from the cookie (broker token optional) |
| Identity | `backend/app/identity.py` | `User`, `UserSession` (hashed, durable), identity linking, `create_session_record` |
| Token/BYOB | `backend/app/services/token_store.py`, `backend/app/brokers/` | Encrypted broker credentials, HMAC-signed OAuth state, adapter-per-broker (`upstox/`) |
| Paper trading | `backend/app/services/paper_execution.py` | Server-authoritative execution, positions, exits, P&L |
| Broker sync | `backend/app/broker_sync/` | Ingestion models/pipeline for broker data |
| Data/config | `backend/app/db.py`, `backend/app/config.py` | Engine/session construction from `DATABASE_URL`; pydantic settings |
| Durable background jobs | `backend/app/services/background_jobs.py`, `backend/run_jobs.py` | Day 47 database-backed job queue on the application database (see below) |
| Historical data governance | `backend/app/services/historical_data_governance.py`, `HistoricalDatasetGovernance`, `HistoricalIngestionRun` | Day 48 provenance/entitlement/usage/redistribution/retention control plane for historical datasets; keeps policy distinct from raw market observations and derived analytics. |
| Migrations | `backend/alembic/` | **Sole schema authority** (ADR-002) |
| Tests | `backend/tests/` | pytest suite (185 test files) |

Request authentication path (Issue #61 contract):

```text
Cookie strikenova_session → CurrentUser/get_current_user → AuthenticatedUser(user_id, access_token|None)
```

### 2.1 Durable background jobs (Day 47)

Operational work that must survive process restart runs as durable jobs
instead of direct CLI execution. The database IS the queue — no external
broker (Redis/Celery/RabbitMQ/Kafka) is introduced.

- **Persistence domain:** `background_jobs` table (migration
  `d47aa0000001`), owned by Alembic like every other table. Model:
  `BackgroundJob` in `app/models.py`.
- **Worker/queue boundary:** producer = `enqueue` (idempotent per
  `idempotency_key` — one row per key, ever; terminal rows are re-armed
  in place); consumer = `claim_next` (single conditional UPDATE lease
  claim, the ADR-017 `_migration_lock` pattern; expired leases make
  crashed jobs claimable again); transitions out of RUNNING are
  ownership-protected (`complete_job`/`fail_job` verify RUNNING + owner +
  unexpired lease atomically, so a stale worker can never overwrite a
  replacement attempt).
- **Lease heartbeat:** while a claimed job executes, a daemon heartbeat
  thread renews the lease every ~lease/3 (floored) via `renew_lease` —
  an atomic conditional UPDATE requiring RUNNING + owner + unexpired
  lease, on its own session (execution work never shares it). The
  effective lease is resolved ONCE by `claim_next` (explicit override >
  payload `policy.lease_seconds` > default) and threaded through
  execution, heartbeat, and completion unchanged. Renewal stops when
  execution ends (either path) or when ownership is lost; a database
  failure — session acquisition OR renewal — is logged, retried at the
  next interval, never fatal to the thread, and never fabricated into
  success. A crashed process stops renewing by construction, so lease
  expiry remains the crash-recovery path and no second ownership race
  is introduced.
- **Retry/dead-letter semantics:** bounded attempts with exponential
  backoff (payload-overridable policy: `max_attempts`,
  `backoff_base_seconds`, `lease_seconds`); transient failures
  (SQLSTATE 40001/40P01/55P03, connection markers) are retried;
  authentication failures and malformed payloads are non-retryable;
  exhausted jobs become inspectable `DEAD_LETTERED` rows, never deleted.
  BOTH transitions out of RUNNING persist through
  `retry_on_serialization` on a fresh session — success re-fetches the
  row and re-runs the ownership-protected completion (only the state
  transition is retried, never the ingestion work); failures re-run the
  ownership-protected `fail_job`.
- **Entry points:** `run_jobs.py` (`enqueue-backfill`, `work`, `status`)
  is the administrative CLI; it performs NO schema mutation (Alembic is
  the sole authority — ADR-002). Historical ingestion executes through
  the real `BackfillOrchestrator` service boundary (no subprocess).
- **Rate-limiter lifecycle:** the ``GlobalRateLimiter`` is a WORKER-
  LIFETIME in-process limiter (the same scope ``run_backfill.py`` gives
  its whole CLI process): ``run_worker`` creates one limiter and passes
  it through ``_execute_one`` → ``execute_job`` →
  ``execute_historical_ingestion``, so 429 cooldown, widened pacing, and
  adaptive-concurrency state survive job boundaries. Each job's
  requested concurrency is applied per-run by
  ``prepare_run_rate_limiter`` (recovery ceiling + semaphore) WITHOUT
  resetting the preserved adaptive state; separate worker processes keep
  independent limiter state (no global singleton), and direct callers
  omitting the limiter get the original per-run construction. The
  HTTP-session ``SessionRateLimiter`` is untouched.
- **Not deployed:** no production worker service or scheduler exists yet;
  enabling one requires separate authorization.

### 2.2 Historical data governance (Day 48)

Historical data acquisition is governed independently from queue mechanics:

- **Dataset catalog:** `HistoricalDatasetGovernance` records source, source
  reference/version, entitlement state, license state, usage scope,
  redistribution state, raw immutability, recomputability, dependencies and
  retention policy.
- **Ingestion manifest:** `HistoricalIngestionRun` snapshots the catalog
  decisions for each acquisition so later policy changes do not rewrite
  historical audit context. It can link a durable `BackgroundJob` ID.
- **Existing pipeline evidence remains authoritative:** the governance service
  reads `IngestionCheckpoint` and `IngestionLog` rather than duplicating their
  operational state. Manifest metrics are derived only from records carrying
  the run's own identity, so evidence from another acquisition can never be
  attributed to this run. `DataCompleteness` is cumulative and carries no run
  identity, so it is not a manifest evidence source. A run that produced no
  evidence of its own stays `UNKNOWN` rather than being reported complete.
- **Enforced rights boundary:** acquisition is gated before any data is
  fetched. Unresolved entitlement is not treated as permission, and
  redistribution rights are never implied: public redistribution is allowed
  only when the catalog explicitly says `ALLOWED`. A run refused by the gate
  fails permanently and leaves no manifest behind. `DECISIONS.md` ADR-020
  records the one approved exception — unresolved entitlement may be acquired
  for internal research or backtest only, and the exception is written to the
  manifest.
- **Terminal manifests:** once a manifest is committed as `RUNNING`, every
  exit path finalizes it, including orchestrator construction failure and a
  failure of the finalization itself.
- **Retention:** deletion is dry-run-first and uses a static allow-list of
  governed ORM targets. The current catalog keeps raw datasets and disables
  enforcement for derived datasets until a controlled policy enables it.
- **Recomputation:** derived model/analytics datasets must declare governed raw
  dependencies; the service checks the dependency graph before a dataset is
  considered recomputation-safe.

No Day 48 scheduler, production purge, production database mutation, or
deployment is enabled by this architecture record.
 
## 3. Frontend (Next.js)

| Layer | Location | Responsibility |
|---|---|---|
| App router | `frontend/app/` | `(public)` marketing pages, `(app)` dashboard/product pages |
| Session gate | `frontend/components/AuthGate.js` | `/auth/me` is the authority; 401/403 → redirect `/`; 5xx/network → retryable error, **never auto-logout** |
| Auth hook | `frontend/lib/useAuth.js` | Cookie-only auth state; transient failures preserve user; 401/403 clear it |
| API client | `frontend/lib/api.js` | axios with `withCredentials: true`; **no** `X-Session-Id` injection; WS uses cookies (`chainWsProtocols() → undefined`) |
| Session helpers | `frontend/lib/session.js` | Google id_token URL scrubber only — no session transport |
| Quant/calcs | `frontend/lib/calculations/` | Presentation-side analytics mirroring server math |
| Tests | `*.test.js` (vitest) | Unit + behavioral suites |

## 4. Cross-cutting contracts

- **Sessions:** HttpOnly `strikenova_session` cookie; server-side durable
  `UserSession`; legacy `session_id` cookie is not a transport.
  [`SECURITY.md`](SECURITY.md).
- **Brokers:** BYOB OAuth per connection; encrypted at rest; separate from
  platform identity ([`DECISIONS.md`](DECISIONS.md) ADR-005/006).
- **GEX conventions:** owned by `docs/GEX_V1_0_SPEC.md`.
- **Timezones:** standardized per `docs/PHASE_7_24_4_TIMEZONE_STANDARDIZATION.md`.

## 5. Historical architecture record

Phase-by-phase designs and audits (7.x data pipeline, 10.x identity/BYOB,
CockroachDB validations, deployment reports) live in
`options-dashboard-project/docs/` — evidence, not open work
([`DECISIONS.md`](DECISIONS.md) ADR-010).
