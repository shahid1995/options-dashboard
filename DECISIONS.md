# StrikeNova — Architectural Decisions

> **Purpose:** This is the living registry of durable architectural decisions that materially constrain implementation.
>
> **Status:** Active
>
> **Authority:** A decision recorded here represents an intentional architectural direction. Historical phase documents are supporting evidence; they do not automatically override the current decision registry.
>
> **Relationship to other control documents:**
> - `AI.md` — how agents find and use the project knowledge.
> - `AGENTS.md` — how agents must operate.
> - `CONTEXT.md` — current repository and system map.
> - `INVARIANTS.md` — properties that must remain true.
> - `DECISIONS.md` — why important architectural choices were made.
> - `PROJECT-CONTROL.md` — how implementation work is planned, reviewed, verified, and closed.

---

## 1. Decision lifecycle

A decision moves through these states:

| State | Meaning |
|---|---|
| **Proposed** | Under consideration; not an implementation authority. |
| **Approved** | Intentionally selected by the project's decision authority. |
| **Active** | Approved and currently governing implementation. |
| **Superseded** | Replaced by a newer decision. |
| **Rejected** | Considered and explicitly not selected. |
| **Historical** | Useful evidence from an earlier phase, but not current authority by itself. |

A new decision should include:

- decision ID;
- title;
- status;
- date;
- context/problem;
- decision;
- consequences/trade-offs;
- affected boundaries;
- evidence/source references;
- superseded/superseding decision when applicable.

Do not rewrite historical decisions to make the history look cleaner. When direction changes, add a new decision and explicitly supersede the old one.

---

## 2. Current decision registry

| ID | Decision | Status |
|---|---|---|
| ADR-001 | Separate StrikeNova platform identity from broker identity | Active |
| ADR-002 | Use BYOB broker connectivity with per-user broker credentials | Active |
| ADR-003 | Keep broker integrations behind provider-specific adapters/gateway boundaries | Active |
| ADR-004 | Treat the backend as the authoritative paper-execution engine | Active |
| ADR-005 | Require execution-time market-status gating | Active |
| ADR-006 | Use idempotency keys for user-facing paper execution operations | Active |
| ADR-007 | Treat Alembic as the authoritative production schema mechanism | Active |
| ADR-008 | Preserve the established GEX methodology and sign convention as a controlled quant contract | Active |
| ADR-009 | Keep historical market-data collection disabled by default and operationally bounded | Active |
| ADR-010 | Keep the public website and authenticated application as separate application/deployment concerns | Active |
| ADR-011 | Preserve GitHub as implementation/project authority and keep durable knowledge outside implementation state | Active |
| ADR-012 | Define the email delivery worker/reaper recovery contract | Proposed — NOT READY |
| ADR-013 | Target PostgreSQL on Neon as the production database engine; retire CockroachDB as historical | Active (target engine only) |

The entries below capture the decision intent and implementation consequences.

---

## ADR-001 — Separate StrikeNova Identity from Broker Identity

**Status:** Active  
**Date:** 2026-08-28  
**Category:** Identity / Authentication / Architecture

### Context

Broker authentication and StrikeNova account identity solve different problems.

The application needs a durable StrikeNova user identity that can exist independently of any particular broker connection. Broker accounts may be connected, disconnected, replaced, or expanded without redefining the platform user.

The repository's identity layer contains distinct models for:

- `User`;
- `UserSession`;
- `BrokerConnection`;
- `BrokerToken`.

### Decision

StrikeNova platform identity is canonical at the `User` level.

Broker authentication creates or manages a relationship to that identity. Broker authentication must not be used as the long-term definition of the StrikeNova account.

A user may have zero, one, or multiple broker connections without changing the identity itself.

### Consequences

**Positive:**

- broker disconnection does not destroy platform identity;
- future multi-broker support can attach to the same user;
- platform authentication can evolve independently from broker OAuth;
- user-owned data can consistently key to StrikeNova identity.

**Constraints:**

- APIs must resolve the authenticated StrikeNova user before accessing owned resources;
- broker connection records must belong to a user;
- migration code must not fall back to broker account identity as the platform identity without a new decision.

### Evidence

- `options-dashboard-project/backend/app/identity.py`
- `options-dashboard-project/docs/PHASE_10_2B_CONNECTION_ARCHITECTURE.md`
- Current project identity hardening work and session ownership implementation.

---

## ADR-002 — BYOB Broker Connectivity with Per-User Credentials

**Status:** Active  
**Date:** 2026-08-28  
**Category:** Broker / Security / Compliance

### Context

The product is designed around user-owned broker connectivity rather than a single platform broker credential shared across customers.

The connection architecture explicitly defines per-user broker credentials and rejects shared platform credentials as the multi-user model.

### Decision

StrikeNova uses **Bring Your Own Broker (BYOB)**.

Each customer connects using their own broker developer application/credentials. Broker credentials are associated with the corresponding StrikeNova user and are not shared as a universal platform credential.

### Consequences

**Positive:**

- broker authorization remains attributable to the customer;
- credentials are isolated by user;
- broker-specific limits and token lifecycles remain manageable;
- the architecture is compatible with multiple broker providers.

**Constraints:**

- credential handling must preserve encryption/protection and ownership boundaries;
- new broker integrations must preserve the gateway/adapter architecture;
- shared platform broker keys must not be reintroduced for convenience;
- operational flows that require customer-specific broker authorization must remain explicitly user-bound.

### Evidence

- `options-dashboard-project/docs/PHASE_10_2B_CONNECTION_ARCHITECTURE.md`
- `options-dashboard-project/backend/app/identity.py`
- `options-dashboard-project/backend/app/routers/auth.py`
- `options-dashboard-project/backend/app/brokers/`

---

## ADR-003 — Provider-Specific Broker Adapters Behind a Common Boundary

**Status:** Active  
**Date:** 2026-08-28  
**Category:** Broker / Architecture

### Context

Broker APIs differ in:

- authorization flows;
- credential formats;
- token lifecycles;
- request headers;
- available capabilities;
- error models.

A shared domain boundary is still valuable, but provider-specific behavior should not leak throughout the application.

### Decision

Broker integrations use the established abstraction layers:

```text
Application
    ↓
Broker Gateway
    ↓
Broker Registry
    ↓
Provider Adapter
    ↓
Broker API
```

Provider-specific authentication and transport behavior belongs inside the provider adapter/domain boundary.

The application must not spread provider-specific conditionals through unrelated business logic.

### Consequences

**Positive:**

- new brokers can be added without rewriting unrelated application logic;
- broker-specific protocol details remain localized;
- tests can target domain contracts and provider behavior separately.

**Constraints:**

- new provider features must enter through the broker boundary;
- business services should consume broker capabilities rather than provider implementation details;
- provider-specific exceptions must not become generic application assumptions.

### Evidence

- `options-dashboard-project/backend/app/brokers/gateway.py`
- `options-dashboard-project/backend/app/brokers/registry.py`
- `options-dashboard-project/backend/app/brokers/domain/`
- `options-dashboard-project/backend/app/brokers/adapters/upstox/`

---

## ADR-004 — Backend-Authoritative Paper Execution

**Status:** Active  
**Date:** 2026-08-28  
**Category:** Trading / Domain Architecture / Correctness

### Context

Paper trading contains state that must remain internally consistent:

- orders;
- fills;
- positions;
- cash;
- realized P&L;
- executions;
- journal records.

Allowing the browser to decide authoritative fills or state creates stale-data, retry, concurrency, and reconciliation risks.

### Decision

The backend is the single authoritative execution engine for paper trading.

The frontend may request execution, provide user intent, render previews, and perform presentation-level calculations, but authoritative execution state is determined and persisted by the backend.

The current domain is implemented around:

- `StrategyExecution`;
- `PaperOrder`;
- `Position`;
- `PaperTransaction`;
- related exposure and journal linkage.

### Consequences

- server-side validation is mandatory;
- authoritative fill prices come from the backend execution path;
- all entry/exit paths must preserve transaction consistency;
- UI shortcuts must not create parallel execution sources of truth.

### Evidence

- `options-dashboard-project/backend/app/models.py`
- `options-dashboard-project/backend/app/services/paper_execution.py`
- `options-dashboard-project/backend/app/services/execution_intent.py`
- `options-dashboard-project/docs/COMPREHENSIVE_ARCHITECTURE_AUDIT.md`

---

## ADR-005 — Execution-Time Market Gate

**Status:** Active  
**Date:** 2026-08-28  
**Category:** Trading / Safety / Domain Rules

### Context

The UI may display a market as open while the market state changes or cannot be verified by the time an execution request reaches the backend.

A check performed only in the browser is therefore insufficient.

### Decision

Every paper execution path must perform the backend market-status check at the execution boundary.

A market that is:

- closed;
- halted;
- suspended; or
- otherwise unverifiable

must not be treated as open for execution.

### Consequences

- the gate belongs server-side;
- entry and exit operations must use the same principle;
- future automation must not bypass the centralized execution gate;
- the frontend's market-status display remains informational rather than authoritative.

### Evidence

- `options-dashboard-project/backend/app/routers/paper.py`
- `options-dashboard-project/backend/app/services/market_status.py`
- `options-dashboard-project/frontend/lib/marketStatus.js`

---

## ADR-006 — Idempotent User-Facing Execution Operations

**Status:** Active  
**Date:** 2026-08-28  
**Category:** Trading / Reliability / Concurrency

### Context

Browsers and networks can retry requests because of double clicks, timeouts, reconnects, or ambiguous responses.

A retry of one logical execution must not create duplicate trades or duplicate financial state.

### Decision

`client_order_id` is the idempotency key at the paper-execution boundary.

The implementation must ensure that a repeated logical request returns/reuses the existing execution outcome rather than creating a second financial effect.

The same discipline applies to exits and other state-changing execution operations where the existing contract requires it.

### Consequences

- idempotency must be enforced server-side;
- uniqueness constraints are part of correctness;
- client-generated identifiers must be treated as request identity, not as proof of authorization;
- retries must remain user-scoped.

### Evidence

- `options-dashboard-project/backend/app/models.py`
- `options-dashboard-project/backend/app/services/paper_execution.py`
- `options-dashboard-project/backend/app/routers/paper.py`
- `options-dashboard-project/backend/app/schemas.py`
- `options-dashboard-project/frontend/lib/portfolio.js`

---

## ADR-007 — Alembic as the Authoritative Schema Mechanism

**Status:** Active  
**Date:** 2026-08-27 and subsequent hardening phases  
**Category:** Database / Persistence

### Context

Earlier iterations used runtime schema creation and additive column checks. That approach does not provide reliable versioned schema evolution.

The project introduced Alembic and subsequently removed transitional runtime DDL from production startup.

### Decision

Alembic is the sole authoritative mechanism for production schema evolution.

Database schema changes must be represented as versioned migrations.

ORM model changes alone are insufficient.

Production code must not reintroduce:

- `Base.metadata.create_all()` as schema authority;
- request-path DDL;
- ad-hoc runtime column creation;
- replacement migration logic hidden inside business services.

### Consequences

- every schema change requires a migration;
- migration tests are part of the database change contract;
- existing production data must be considered when changing schema;
- startup may run versioned migrations, but the migration history remains the authority.

### Evidence

- `options-dashboard-project/backend/app/db.py`
- `options-dashboard-project/backend/alembic/`
- `options-dashboard-project/backend/tests/test_alembic_migrations.py`
- `options-dashboard-project/docs/PHASE_10_1A_DATABASE_MIGRATIONS.md`
- `options-dashboard-project/docs/PROJECT_STATUS.md`

---

## ADR-008 — GEX Methodology is a Controlled Quant Contract

**Status:** Active  
**Date:** 2026-08 / Phase 7 series  
**Category:** Quant / Analytics

### Context

GEX is not merely a UI calculation. It is a methodology with persisted values, downstream analytics, tests, quality checks, and research dependencies.

Changing a formula silently would make historical and current analytics incomparable.

### Decision

The current raw GEX convention is:

```
raw_gex = gamma × open_interest × spot² × 0.01
```

The repository convention does not apply a lot-size multiplier to this raw-GEX calculation.

Signed GEX follows the repository's established call/put sign convention.

### Consequences

A methodology change requires coordinated treatment of:

- production calculations;
- historical calculations;
- persisted GEX data;
- quality checks;
- tests;
- downstream analytics;
- documentation;
- research comparisons.

The formula must not be casually altered during an unrelated refactor.

### Evidence

- `options-dashboard-project/backend/app/models.py`
- `options-dashboard-project/backend/app/services/historical_gex.py`
- `options-dashboard-project/backend/tests/test_historical_gex.py`
- `options-dashboard-project/docs/GEX_PHASE_7_4_DESIGN.md`
- `options-dashboard-project/docs/GEX_DATA_QUALITY_CONTRACT.md`

---

## ADR-009 — Historical Data Collection is Controlled and Disabled by Default

**Status:** Active  
**Date:** 2026-08 / Phase 7 history work  
**Category:** Data / Operations / Cost

### Context

Historical IV, GEX, candles, backfills, and periodic capture can materially increase:

- storage;
- upstream API usage;
- processing time;
- operational complexity;
- maintenance burden.

The repository already exposes explicit feature flags for historical collection behavior.

### Decision

Historical collection features remain opt-in and operationally bounded.

Current defaults include:

- `IV_HISTORY_ENABLED=false`;
- `GEX_HISTORY_ENABLED=false`;
- `GEX_CAPTURE_ENABLED=false`;
- `CANDLE_BACKFILL_ENABLED=false`.

A collector must not become globally active merely because its implementation exists.

### Consequences

Enabling collection requires deliberate consideration of:

- retention;
- volume;
- source limits;
- data ownership;
- provenance;
- operational scheduling;
- cleanup;
- cost.

### Evidence

- `options-dashboard-project/backend/app/config.py`
- historical-data services and operational documentation under `options-dashboard-project/docs/`.

---

## ADR-010 — Public and Authenticated Applications Remain Separate Concerns

**Status:** Active  
**Date:** 2026-09  
**Category:** Frontend / Deployment / Security Boundary

### Context

StrikeNova has a public marketing/site experience and a separate authenticated application experience.

The current repository and deployment history preserve that boundary rather than treating the public website as the authenticated product shell.

### Decision

The public site and authenticated application remain intentionally separate application/deployment concerns.

Changes may share design-system primitives where appropriate, but a public-site change must not implicitly rewrite authenticated routing, authentication boundaries, or backend business logic.

### Consequences

- public-site work should preserve the authenticated application boundary;
- cross-origin authentication handoff must be treated as a deliberate contract;
- deployment verification must distinguish the public Vercel project from the authenticated application;
- public UX work must not silently modify authenticated product behavior.

### Evidence

- `options-dashboard-project/frontend/app/(public)/`
- `options-dashboard-project/frontend/app/(app)/`
- `options-dashboard-project/docs/PROJECT_STATUS_CURRENT.md`
- public-site and authentication handoff acceptance records under `options-dashboard-project/docs/superpowers/`.

---

## ADR-011 — GitHub is Implementation Authority; Durable Knowledge is Separate

**Status:** Active  
**Date:** 2026-09  
**Category:** Governance / AI Workflow

### Context

StrikeNova uses multiple knowledge surfaces:

- GitHub for implementation/project state;
- long-lived knowledge and reasoning outside implementation state;
- historical engineering records under `docs/`.

Without an explicit boundary, AI agents can confuse a historical plan, a copied prompt, an old audit, or a current implementation state.

### Decision

GitHub is the implementation and project authority.

Durable reasoning, research context, and long-lived decision knowledge remain outside the executable implementation state.

Within GitHub:

- `PROJECT-CONTROL.md` governs execution workflow;
- `AI.md` is the entry map for agent-accessible project knowledge;
- `AGENTS.md` governs agent operating rules;
- `CONTEXT.md` describes current structure;
- `INVARIANTS.md` protects non-negotiable properties;
- `DECISIONS.md` records architectural intent.

Historical phase documents remain evidence unless they are explicitly promoted as current authority.

### Consequences

AI agents must inspect the current repository and current control documents before implementation.

Old documents must not be treated as automatically current.

A change to an accepted architectural decision requires an explicit decision transition rather than silent drift.

### Evidence

- `PROJECT-CONTROL.md`
- `options-dashboard-project/docs/PROJECT_STATUS_CURRENT.md`
- current AI workspace control documents.

---

## ADR-012 — Email Delivery Worker/Reaper Recovery Contract

**Status:** Proposed — NOT READY for implementation. No worker, reaper, finalizer, abandon transition, reconciler or provider integration exists. This ADR records the contract shape and the evidence behind it; it does not authorize implementation.
**Target engine:** Neon PostgreSQL (`postgresql` SQLAlchemy dialect, PostgreSQL 16/17). CockroachDB is retired as a target; its results are historical only (see "Historical engine evidence").
**Date:** 2026-11  
**Category:** Data / Reliability / Email Delivery  
**Related:** ADR-013 — the target engine is Neon PostgreSQL; CockroachDB results in this ADR are historical only.

### Context

`backend/app/services/email_delivery_retry.py` implements exactly one state transition: retry a delivery after a conclusive retryable provider outcome, inside a four-attempt budget, and finalize it as `retry_budget_exhausted` on the next request. Nothing in the application calls it.

The delivery contract is defined against Neon PostgreSQL. Every rule, measurement and test result below must be read in that light: the application is a PostgreSQL application, and the one migration revision that blocked CockroachDB (`125e1807df8d`) takes its PostgreSQL branch on Neon by construction. Nothing in this ADR is a Neon measurement; Neon certification is outstanding.

The worker is expected to commit an attempt outcome in one transaction and then call the retry writer in a second. A crash between the two commits strands a committed outcome. A reaper that selects only expired `sending` claims with no outcome does not recover that case, and recovers neither an accepted nor a permanently rejected outcome.

### Proposed contract (awaiting approval)

Nothing below is approved for implementation. Every rule is a **proposal** subject to the open Founder decisions at the end of this ADR; "proposed" is the default reading of every sentence in this section.

1. **Transaction ownership.** The reaper never holds a lock across a writer call. It performs a read-only candidate scan for ids, then invokes a transition that opens its own transaction, re-reads the delivery under lock and **reclassifies from durable state**. Correctness comes from reclassification under the lock, not from the scan; no lock is claimed to be held across transactions.
2. **Lease closure on a conclusive outcome.** The transaction that records `accepted`, `rejected_retryable` or `rejected_permanent` also sets `lease_expires_at` to the observation time in the same commit. Without it the writer's `abort_live_lease` guard makes inline retry impossible and every retry waits a full lease.
3. **Reaper selection.** Two disjoint queries: expired claims (`status IN ('sending','dispatched') AND lease_expires_at <= now`), and blocked deliveries whose payload expired (`status IN ('awaiting_reconciliation','review_required') AND payload_expires_at <= now`). The second has no lease predicate: `ck_delivery_blocked_no_lease` guarantees no lease exists. One delivery per transaction, ordered by the expiry column then `id`.
4. **Evidence identity (proposed, unapproved).** One provider observation is *proposed* to be identified by `(source, evidence_source)`, enforced by unique index `uq_evidence_provider_event` (revision `c1d2e3f4a5b6`). The index exists; **the identity choice is not approved** (F-6). Uniqueness, not a read-then-write check, is what would make duplicate webhook delivery idempotent across independent writers.
5. **Evidence authority.** Only the attempt on `delivery.active_generation_id` may drive finalization. A **decisive** outcome (`accepted`, `rejected_permanent`) recorded on a superseded generation is a contradiction and parks the delivery in `review_required`; it never proves delivery. Contradictory outcomes on one attempt likewise park. Late webhooks attach to their own attempt and never mutate a newer one.
   An ordinary historical `rejected_retryable` recorded on a superseded generation is **not** a contradiction. It is the expected predecessor of the next generation: it is what the retry writer consumed to create that generation, and it must be preserved as history rather than treated as a conflict. Only an outcome that claims a *final* result can contradict the active generation.
6. **Uncertainty is not an outcome.** A dispatch marker written without a recorded outcome (`dispatch_marker_at IS NOT NULL`, no `execution_completed`) parks the delivery as `awaiting_reconciliation` with `execution_disposition='execution_unknown'`, no `provider_outcome` and no decisive evidence row. The schema cannot store an "unknown" outcome (`ck_evidence_outcome_enum`); uncertainty is carried by the disposition and the absence of evidence. Never resend automatically.
7. **Ambiguous commit.** A commit error is resolved by re-reading whether the caller's `new_generation_id`/`new_attempt_id` landed and became active, never by treating a retry refusal as success. If the result cannot be attributed to this operation, return an explicit ambiguous result and park for reconciliation.
8. **Clock authority.** Lease validity is judged only inside the transaction holding the delivery lock, using a timestamp read after the lock. A caller-supplied timestamp is provisional: a measured PostgreSQL lock wait of 0.603 s left `now()` frozen at transaction start.
9. **Budget (unresolved — F-3).** Current behaviour: the writer counts reservations (`len(attempts)` / `attempt_index`), so pre-dispatch crashes consume budget. Alternatives (dispatch-authorized count, conclusive rejections) are derivable without a schema change but are a policy change requiring approval. Which count is correct is **not decided** here.

### Clock contract (measured, not assumed)

| Source | Representation | Granularity | Evidence |
|---|---|---|---|
| SQLite `CURRENT_TIMESTAMP` | `YYYY-MM-DD HH:MM:SS`, naive UTC | 1 s | measured 2026-11-13: 7 844 samples / 2.2 s, 3 distinct, min step 1.000 s |
| SQLite `strftime('%Y-%m-%dT%H:%M:%fZ','now')` | ISO UTC | 1 ms | measured: 4 179 samples / 1.2 s, 1 200 distinct, min step 0.001 s |
| `_bind_dt(aware datetime)` | ISO with `+00:00` | supplied precision, µs preserved | measured: instants 1 µs apart survive the round trip |
| PostgreSQL `now()` | transaction start | µs | measured: 0.000 s drift across a 250 ms transaction |
| PostgreSQL `clock_timestamp()` | wall clock | µs | measured: 0.251 s drift in 250 ms |
| CockroachDB | — | — | out of scope: retired as a target. The 24.1.0 experiment (end of support 2026-10-21) produced no clock measurement |

Mixed storage formats are **unsafe**, not merely untidy: with `'2026-01-01 00:00:30'` and `'2026-01-01T00:00:20.5+00:00'` in one column, both `ORDER BY ts` and `WHERE ts <= …` return the wrong rows because byte 10 (`' '` = 0x20 vs `'T'` = 0x54) dominates the comparison. One canonical representation (ISO `T` plus offset everywhere) restores correct ordering. Timestamps read through a typed column are **aware for writer-bound values and naive for SQL-written values**; every typed read must be normalized to aware UTC.

The current writer compares timestamps in Python after `_as_dt` and contains no SQL timestamp predicate, so it is not exposed. The hazard is the unwritten reaper and any future SQL filter.

### Certification status

- Certified on SQLite and PostgreSQL 16: the retry transition, rollback after each of its five stages, two-connection single-successor behaviour, argument validation, the four-attempt progression, and the three indexes of revision `c1d2e3f4a5b6`.
- Scope of that certification: the retry transition only. It certifies no lifecycle, no worker or reaper, and no engine other than the two named above.
- **Neon PostgreSQL: not certified.** Local PostgreSQL 16 results do not transfer automatically to a managed Neon branch. No statement in this ADR is a Neon measurement; the target engine's certification is outstanding.
- Not certified: every other transition, and the worker and reaper lifecycle.
- CockroachDB certification is neither claimed nor required. It is not part of the forward acceptance criteria.

### NOT READY

- No claim path, outcome writer, finalizer, abandon/redispatch transition, reconciler, reaper loop, provider adapter, ambiguous-commit verification read, or Neon certification run exists.
- The lifecycle questions below are open and are not closed by choosing the engine: expected historical retryable predecessors (rule 5), conclusive-outcome versus payload-expiry precedence (rule 2 against the reaper's second query), provider-event identity and deduplication (F-6), dispatch ownership and stale-worker fencing, ambiguous-commit resolution (rule 7), and unknown/unmatched evidence reconciliation (F-8).
- Advisory only: nothing in this ADR is approved for implementation.

### Historical engine evidence (CockroachDB — historical, not a target)

CockroachDB was evaluated and rejected as a target. The record is kept so the same ground is not re-covered, and it is **not** a release gate.

- Run on CockroachDB 24.1.0 (CCL, end of support 2026-10-21), dialect `cockroachdb`, driver `cockroachdb+psycopg2`.
- The full migration chain cannot complete there: revision `125e1807df8d` takes its non-PostgreSQL branch and emits `CREATE UNIQUE INDEX … WHERE is_default = 1` against a boolean column, which fails with `unsupported comparison operator: <bool> = <int>`. `DROP DATABASE … WITH (FORCE)` is also a syntax error there.
- The delivery revisions were reached only by stamping `b2c3d4e5f6a7` onto an empty database, so eight earlier revisions never ran. On that stamped schema one retry transition succeeded and a two-thread race produced one successor and one `abort_live_lease` refusal. No `40001` was observed and none was instrumented for.
- Therefore: two of ten revisions, one process, one stamped schema, one run. That is not certification of CockroachDB, and it is not evidence about Neon.
- The Neon target requires none of this: `125e1807df8d` selects its PostgreSQL branch (`WHERE is_default = true`) on any `postgresql` dialect, and no serialization-retry wrapper is planned for a READ COMMITTED target.

### Open Founder decisions

| # | Decision | Recommendation |
|---|---|---|
| F-1 | Close the lease in the conclusive-outcome transaction | approve |
| F-2 | Abandon is a distinct transition that consumes an attempt | approve |
| F-3 | What the four-attempt budget counts | reservations now, dispatch-authorized later |
| F-4 | Accept the dispatch-marker false-positive park rate | accept |
| F-5 | Reconciliation ownership: manual queue or provider status API | manual first |
| F-6 | Unique `(source, evidence_source)` index, and the identity it implies | index exists in `c1d2e3f4a5b6`; both the identity and its **retention** are unapproved |
| F-7 | Reaper indexes | exist in `c1d2e3f4a5b6`; **retention** is unapproved |
| F-8 | Quarantine for unmatched provider events, or drop plus counter | counter first |
| F-9 | Late `accepted` for a delivery already `failed`: correct or park | park for review |
| F-10 | Metrics backend (none exists) | structured logs first |
| F-11 | Lease TTL, reaper interval, whether SQLite runs a reaper | 60 s / 15 s / yes |
| F-12 | Move the clock read inside the writer's transaction | approve before multi-instance |

### Evidence

- `options-dashboard-project/backend/app/services/email_delivery_retry.py`
- `options-dashboard-project/backend/alembic/versions/f2a7c9d1e4b8_delivery_pipeline_tables.py`
- `options-dashboard-project/backend/alembic/versions/a8b3d5e7c902_delivery_referential_integrity.py`
- `options-dashboard-project/backend/alembic/versions/c1d2e3f4a5b6_delivery_reaper_indexes.py`
- `options-dashboard-project/backend/tests/test_email_delivery_retry.py`
- `options-dashboard-project/backend/tests/test_email_delivery_retry_certification.py`
- `options-dashboard-project/backend/tests/test_delivery_indexes.py`
- CockroachDB 24.1.0 experiment (historical): migrations reached only by stamping `b2c3d4e5f6a7` on an empty database; `f2a7c9d1e4b8`, `a8b3d5e7c902`, `c1d2e3f4a5b6` applied; one retry transition and one two-thread race observed.
- Neon PostgreSQL audit (source and documentation only): dialect inventory, complete migration graph, pooled versus direct connection architecture, branch-based test strategy. No Neon execution occurred.

### Revisit when

A provider integration is chosen, the worker/reaper host is agreed, F-1 to F-12 are decided, or a Neon staging branch is provisioned for certification.

---

## ADR-013 — PostgreSQL on Neon as the Target Database Engine

**Status:** Active — Founder-directed, and limited to the target engine. This ADR selects an engine; it does not approve any delivery-lifecycle rule, any ADR-012 Founder decision, or any schema-execution change.
**Date:** 2026-11  
**Category:** Data / Infrastructure / Deployment  
**Supersedes:** none — no prior ADR selected a database engine, and this record corrects a deployment fact that was documented inconsistently across the control documents.  
**Related:** ADR-007 (Alembic remains the authoritative schema mechanism; unaffected), ADR-012 (email delivery contract — remains **NOT READY**). The proposed move of migration execution out of application startup is described in this ADR's consequences only; it has no ADR of its own and is unapproved.

### Context

Every control document named CockroachDB as the production database, and none of them could show the schema being reached there. Two independent findings made that untenable:

- The full Alembic chain cannot complete on CockroachDB. Revision `125e1807df8d` branches on `dialect == "postgresql"` and falls through to a SQLite-shaped predicate for every other dialect, emitting `CREATE UNIQUE INDEX … WHERE is_default = 1` against a `BOOLEAN` column. CockroachDB rejects it (`unsupported comparison operator: <bool> = <int>`), so revision 2 of 10 blocks revisions 3 to 10. `DROP DATABASE … WITH (FORCE)` is also a syntax error there.
- The evaluation itself was run on CockroachDB 24.1.0, whose support ended 2026-10-21. Even a clean run would have certified an out-of-support engine.

The application, meanwhile, is already a PostgreSQL application: the `postgresql` SQLAlchemy dialect, `postgresql.insert(...).on_conflict_do_update()` through `dialect_insert()`, `FOR UPDATE` row locking, `timestamptz` columns, `bytea` payloads, `Text`-encoded JSON (no `JSONB`), UUID/string identifiers with no sequences and no autoincrement, and no extension, trigger or search-path dependence. Nothing in the schema requires CockroachDB, and nothing in it is rewritten by moving to PostgreSQL.

### Decision

1. **Target engine.** Neon PostgreSQL, reached through the ordinary PostgreSQL dialect (`postgresql+psycopg2`). A Neon project on PostgreSQL 16 or 17 — 16 keeps the engine line already certified on a local server.
2. **CockroachDB is historical.** It was evaluated and rejected as a target and is no longer a compatibility requirement. No CockroachDB-specific schema, dialect branch, test fixture, acceptance criterion or release gate may be added. Evidence gathered there is retained in ADR-012 as historical evidence only.
3. **Connection split (intent, not yet exercised).** Application traffic uses the pooled endpoint; schema migrations use the direct (unpooled) endpoint.
4. **Schema authority is unchanged.** Alembic remains authoritative (ADR-007). Selecting an engine does not change who owns the schema.
5. **`125e1807df8d` is not modified.** On any `postgresql` dialect it takes its existing PostgreSQL branch. Editing it for CockroachDB's sake is now explicitly out of scope.
6. **Existing CockroachDB remnants stay as remnants.** The `"cockroachdb"` member of `_lock_clause()`'s tuple is a historical leftover; it is not a supported target and must not become one by accretion.
7. **Control documents follow this decision.** `AI.md`, `CONTEXT.md`, `DATA.md`, `ARCHITECTURE.md`, `INVARIANTS.md` and `TESTING.md` name Neon PostgreSQL as the target and mark CockroachDB historical.

### Consequences

**Positive.** The blocked migration path disappears without touching a migration; no application rewrite is required; the dependency set (`SQLAlchemy 2.0.57`, `alembic 1.15.2`, `psycopg2-binary 2.9.13`) needs no change or upgrade; the PostgreSQL behaviour already tested locally becomes the behaviour that ships.

**Costs and risks.**

- **Zero Neon certification exists.** No statement about Neon in this repository is a measurement. Local PostgreSQL 16 results do not transfer automatically to a managed branch.
- **Migrations still execute at application startup** (`init_db()` → `alembic upgrade head`) on the application engine, which under this decision is the pooled endpoint, on every process boot. That is the one architectural change this pivot forces; it is proposed and unapproved, not implemented.
- **Neon suspends idle compute and drops idle connections, pooled ones included.** `pool_pre_ping=True` already covers the dead-socket case; recycling intervals and cold-start latency are tuning decisions, not code rewrites.
- **Transaction pooling forbids session-level state** (`SET`, `LISTEN/NOTIFY`, advisory locks, held cursors). The application uses none of them today; startup migrations would, if they ever took an advisory lock, and that is one more reason they belong on a direct connection.
- **The source of any data migration is unknown.** No document establishes what the current production database holds. This ADR does not assume it is empty, and it does not authorise a move.

### Not decided here

- Any Neon result, certification or performance claim. None exists.
- ADR-012 remains **NOT READY**; its rules remain **proposed**; the evidence identity `(source, evidence_source)` remains unapproved; F-1 to F-12 all remain open.
- Moving Alembic out of `init_db()` onto a direct-URL release step — design exists, implementation does not.
- Whether existing records must be preserved, and how they would move.
- Promoting the four SQLite-only `init_db()` indexes into Alembic.
- Neon plan, region, compute size, and the staging/production project separation.

### Affected boundaries

Schema and migrations; database connection configuration; deployment topology and hosting responsibilities; verification environments; backup and recovery planning.

### Evidence

- `DECISIONS.md` ADR-012 — "Historical engine evidence (CockroachDB — historical, not a target)" and the delivery contract that remains NOT READY.
- `options-dashboard-project/backend/alembic/versions/125e1807df8d_add_broker_connection_foundation.py` — the dialect branch.
- `options-dashboard-project/backend/app/db.py` — engine construction, pooling, `init_db()`.
- `options-dashboard-project/backend/app/utils/db_dialect.py` — the `postgresql`/`sqlite` upsert split.
- `options-dashboard-project/backend/app/services/email_delivery_retry.py` — `_lock_clause()`.
- Neon documentation: connection pooling (pooled versus direct), compute lifecycle and scale to zero, supported PostgreSQL versions.

### Revisit when

A Neon staging branch is provisioned and certified; the source-data question is answered by the Founder; migration execution moves out of application startup; or Neon's pooled/direct guidance changes.

---

## 3. Historical decision material

The repository contains many phase-specific specifications, audits, and plans.

These documents are valuable evidence but have a different role from this registry.

Examples include:

- `docs/PHASE_10_1A_DATABASE_MIGRATIONS.md`;
- `docs/PHASE_10_2B_CONNECTION_ARCHITECTURE.md`;
- `docs/GEX_PHASE_7_4_DESIGN.md`;
- `docs/GEX_DATA_QUALITY_CONTRACT.md`;
- `docs/COMPREHENSIVE_ARCHITECTURE_AUDIT.md`;
- `docs/superpowers/specs/*`;
- `docs/superpowers/plans/*`;
- `docs/superpowers/audits/*`.

When an older document conflicts with this registry, do not assume the older document is current.

Instead:

1. inspect the current implementation;
2. inspect the relevant active decision;
3. determine whether a newer decision already superseded the historical material;
4. create a new decision if the architecture has intentionally changed.

---

## 4. Decision-change protocol

An architectural decision should be changed only when the change is explicit enough to preserve the project's reasoning history.

### Required sequence

```text
Problem / new evidence
        ↓
Impact analysis
        ↓
Proposed decision
        ↓
Founder / authorized architecture approval
        ↓
New ADR entry
        ↓
Supersede affected ADR(s)
        ↓
Implementation issue
        ↓
Implementation + verification
```

### Do not do this

- silently change a frozen architectural boundary;
- edit a historical ADR to hide the old decision;
- implement a conflicting design and document it afterwards;
- treat passing tests as approval for an architectural change;
- broaden scope because the new architecture seems cleaner.

---

## 5. When an ADR is required

Create or update a decision record when a change materially affects:

- platform identity;
- user/tenant ownership;
- broker credential architecture;
- authentication/session binding;
- execution authority;
- order/exposure state semantics;
- idempotency;
- transaction/concurrency boundaries;
- database migration authority;
- GEX methodology;
- historical data collection policy;
- public/authenticated application boundaries;
- deployment authority;
- another invariant protected by `INVARIANTS.md`.

Routine bug fixes, styling changes, test additions, and implementation refactors do not automatically require a new ADR unless they alter one of these durable architectural choices.

---

## 6. Decision quality standard

A good decision record answers:

**Why is this the architecture?**

It should make it possible for a future engineer or AI agent to understand:

- what problem existed;
- what was chosen;
- what alternatives were intentionally not chosen when material;
- which constraints drove the decision;
- what consequences must be preserved;
- what evidence supports the decision;
- what would justify revisiting it.

The goal is not to record every technical choice. The goal is to prevent architectural drift by preserving the decisions that future implementations must continue to respect.
