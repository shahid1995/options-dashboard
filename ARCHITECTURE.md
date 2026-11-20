# StrikeNova — Architecture

> **Purpose:** Living architectural map of the current StrikeNova system.
>
> **Status:** Active
>
> **Primary question answered here:** How do the major parts of StrikeNova relate to one another, where are the boundaries, and where does authoritative state live?
>
> This document describes the current architecture. It is not a backlog, implementation plan, or historical phase ledger.

---

## 1. System overview

StrikeNova is a full-stack options-intelligence and paper-trading application with four major technical boundaries:

```text
                         ┌───────────────────────────┐
                         │      Public Website       │
                         │       Next.js / React     │
                         └─────────────┬─────────────┘
                                       │
                                       │
┌───────────────────────────┐          │          ┌───────────────────────────┐
│    Authenticated Web App  │──────────┼─────────▶│       FastAPI Backend     │
│       Next.js / React     │          │          │ Domain / API / Services   │
└───────────────────────────┘          │          └─────────────┬─────────────┘
                                       │                        │
                                       │                        │
                                       │            ┌───────────┴───────────┐
                                       │            │                       │
                                       │      ┌─────▼─────┐         ┌──────▼──────┐
                                       │      │  Broker   │         │  Database   │
                                       │      │  Gateway  │         │ SQLAlchemy  │
                                       │      └─────┬─────┘         │ + Alembic   │
                                       │            │               └─────────────┘
                                       │            ▼
                                       │      Broker APIs
                                       │       (Upstox)
                                       │
                                       └──── Application session /
                                             authentication flow
```

The most important architectural rule is the separation of responsibilities:

- **Frontend:** interaction, presentation, local UI state, previews, client-side display calculations.
- **Backend:** authenticated API boundary, business rules, broker access, authoritative paper execution, persistence, server-side analytics and validation.
- **Broker layer:** provider-specific market/auth connectivity behind a common domain boundary.
- **Database:** durable application state and analytical/history state, with Alembic controlling schema evolution.

---

## 2. Repository boundary

The repository root is:

```text
/
├── PROJECT-CONTROL.md
├── AI.md
├── AGENTS.md
├── CONTEXT.md
├── INVARIANTS.md
├── DECISIONS.md
├── .github/
└── options-dashboard-project/
```

The application source lives under:

```text
options-dashboard-project/
end{verbatim}
```

Primary runtime areas:

```text
options-dashboard-project/
├── frontend/
├── backend/
├── docs/
└── .github/
```

The `docs/` tree contains historical engineering records, audits, specifications, plans, and acceptance evidence. Those records are useful architectural evidence but do not automatically override the active root control documents.

---

## 3. Frontend architecture

### 3.1 Stack

Current frontend stack:

- Next.js 14.2.35
- React 18.3.1
- React DOM 18.3.1
- JavaScript / JSX
- Axios 1.19.0
- Recharts 2.12.7
- Vitest 4.1.10
- V8 coverage through `@vitest/coverage-v8` 4.1.10
- Google OAuth client package where applicable

The frontend uses the Next.js App Router.

### 3.2 Route structure

The frontend is divided into intentional route groups:

```text
frontend/app/
├── (public)/
└── (app)/
```

The public route group contains the public StrikeNova website, including:

- `/`
- `/features`
- `/market-intelligence`
- `/strategy-lab`
- `/paper-trading`
- `/how-it-works`
- `/about`

The authenticated application group contains product areas such as:

- dashboard;
- activity;
- brokers;
- gex;
- market;
- orders;
- paper;
- portfolio;
- positions;
- settings;
- strategies.

The route-group separation is architectural: public presentation and authenticated product behavior are related, but not the same application boundary.

### 3.3 Frontend responsibilities

The frontend is responsible for:

- rendering product workflows;
- accepting user input;
- calling backend APIs;
- displaying backend state;
- presenting market/option-chain information;
- displaying GEX and portfolio analytics;
- managing user-facing previews and interaction state;
- providing public-site visual and navigation experiences.

The frontend is **not** authoritative for:

- authenticated ownership;
- broker authorization;
- fill price;
- paper order acceptance;
- positions;
- cash;
- realized P&L;
- execution success;
- database schema.

### 3.4 Frontend shared/domain layer

`frontend/lib/` contains reusable application/domain utilities and calculations, including:

- market status;
- option/pricing utilities;
- paper/portfolio utilities;
- strategy utilities;
- session/auth helpers;
- chain-feed hooks;
- GEX capture/persistence helpers;
- UI primitives/tokens.

`frontend/components/` contains reusable feature and domain components, including the GEX visualization/panel family.

The existence of a frontend calculation does not make that calculation the backend system of record. When the same concept exists on both sides, the authoritative role must remain explicit.

---

## 4. Backend architecture

### 4.1 Stack

Current backend stack:

- Python 3.13 runtime
- FastAPI 0.141.1
- Uvicorn
- httpx
- Pydantic Settings
- SQLAlchemy 2.0.43
- Alembic 1.15.2
- cryptography
- PyJWT

Primary entry point:

```text
backend/app/main.py
```

### 4.2 Backend layers

The backend can be understood as these layers:

```text
HTTP / FastAPI Routers
        │
        ▼
Authentication / Authorization Dependencies
        │
        ▼
Domain & Application Services
        │
        ├───────────────▶ Broker Gateway / Adapters
        │
        └───────────────▶ SQLAlchemy Persistence
                              │
                              ▼
                         PostgreSQL / SQLite
```

The router layer should coordinate HTTP concerns, validation, authentication dependencies, and service invocation.

Domain rules should live in reusable backend services rather than being duplicated across routers.

### 4.3 Router responsibilities

The repository contains router modules for major domains such as:

- authentication;
- option chains;
- paper trading;
- templates;
- GEX snapshots;
- live GEX;
- historical GEX;
- candles;
- resolution/selection;
- annotations.

Important mounting fact:

The current `main.py` registration must be treated as the source of truth for what is externally mounted. The existence of a router module in the repository does not by itself mean that every endpoint is currently exposed.

### 4.4 Authentication and identity

The identity architecture separates:

```text
StrikeNova User
      │
      ├── UserSession
      │
      └── BrokerConnection
              │
              └── BrokerToken / credential state
```

The canonical application identity is the StrikeNova `User`.

A broker connection is a user-owned relationship, not the user's platform identity.

Authentication dependencies resolve the current identity before protected resources are accessed.

---

## 5. Broker integration architecture

Broker integration is intentionally layered:

```text
Application service
       │
       ▼
Broker Gateway
       │
       ▼
Broker Registry
       │
       ▼
Broker Adapter
       │
       ▼
Provider API
```

Current provider implementation includes Upstox under:

```text
backend/app/brokers/adapters/upstox/
```

The broker domain contains:

- capabilities;
- enums;
- errors;
- models;
- protocols.

### 5.1 Why the boundary exists

Broker APIs differ in:

- authentication flows;
- token lifetimes;
- request formats;
- capabilities;
- error semantics;
- account/profile representations.

Provider-specific behavior therefore belongs behind the adapter boundary.

Business services should depend on broker capabilities and stable domain contracts rather than spreading provider-specific conditions throughout the application.

### 5.2 BYOB ownership

The broker layer follows the active BYOB architecture:

- customer broker credentials belong to the corresponding StrikeNova user;
- broker connections are user-scoped;
- tokens/credentials are treated as secrets;
- the system must not silently switch users or connections;
- explicit connection selection is required where ambiguity would matter.

---

## 6. Paper-trading architecture

Paper trading has a deliberate authoritative domain model.

### 6.1 Core state flow

```text
User intent
    │
    ▼
HTTP / Paper Router
    │
    ▼
Execution Intent / Validation
    │
    ▼
Market Gate + Market Data Resolution
    │
    ▼
Authoritative Paper Execution
    │
    ├── StrategyExecution
    ├── PaperOrder
    ├── Position
    ├── PaperTransaction
    ├── StrategyLegExposure
    └── Journal linkage
```

The backend is authoritative for:

- accepted/rejected execution;
- authoritative market price;
- filled quantity;
- order state;
- position state;
- cash movement;
- realized P&L;
- execution/journal reconciliation.

### 6.2 Entry and exit

Entry operations include manual/strategy/template-driven paper execution.

Exit operations include:

- position exits;
- trade-leg closing;
- bulk exits;
- strategy-aware exits.

All state-changing execution paths must preserve the same fundamental protections rather than introducing a second execution implementation.

### 6.3 Idempotency

`client_order_id` is used as an idempotency boundary.

The purpose is to make retries safe against:

- duplicate clicks;
- network retries;
- ambiguous responses;
- client reconnects.

Idempotency must be:

- server enforced;
- user scoped;
- backed by persistence/constraints;
- preserved across entry and exit flows.

### 6.4 Market gate

Paper execution must pass an execution-time server-side market-status check.

The frontend may display market status, but the backend determines whether the execution is permitted.

Unknown market state is not treated as open.

---

## 7. Database architecture

### 7.1 Database technology

The application uses SQLAlchemy ORM with:

- SQLite for local/default operation;
- PostgreSQL (Neon) as the production database target, established as a clean database using the approved schema/migration chain (ADR-019); CockroachDB is retired as a production target and is not required for new work.

SQLite is configured with WAL behavior for local resilience.

The current production endpoint remains CockroachDB (legacy, unreachable) until cutover evidence exists. PostgreSQL/Neon connections use the configured SQLAlchemy database URL. Do not treat the Neon staging database as production.

### 7.2 Schema authority

Alembic is the authoritative production schema mechanism.

Startup uses the versioned migration path:

```text
Application startup
      │
      ▼
Alembic upgrade head
      │
      ▼
Idempotent data backfill / startup maintenance
      │
      ▼
Application ready
```

The production architecture must not fall back to ORM `create_all()` or runtime column mutation as the schema authority.

### 7.3 Principal data domains

The database can be understood as several domains.

#### Identity

- `users`
- `user_sessions`
- `broker_connections`
- `broker_tokens`

#### Paper trading

- `paper_accounts`
- `strategy_executions`
- `paper_orders`
- `positions`
- `paper_transactions`
- `strategy_leg_exposures`
- `exit_exposure_allocations`
- `bulk_exit_records`

#### Journal / strategy compatibility

- `trades`
- `legs`
- `strategy_templates`
- `strategy_template_legs`

#### Market data

- `nifty_candles`
- `contract_specs`
- `option_candles`
- `option_greeks`

#### Analytics / research

- `gex_snapshots`
- `historical_gex`
- `iv_observations`

#### Ingestion infrastructure

- `ingestion_log`
- `data_completeness`
- `ingestion_checkpoint`

### 7.4 Ownership

User-owned records must remain user-scoped.

Shared market/reference data may be shared where intentionally designed, but ownership must never be inferred from the table name alone.

---

## 8. Market-data and quant architecture

The market-data side of the system distinguishes source data from derived analytics.

A simplified pipeline is:

```text
Broker / upstream market data
           │
           ▼
     Raw / reference data
           │
           ▼
      Derived models
       (e.g. Greeks)
           │
           ▼
       Analytics
   (GEX / exposures / research)
           │
           ▼
    Frontend visualizations
```

This separation is important because:

- raw data has different provenance from derived calculations;
- derived models can be recomputed;
- analytical contracts may have methodology/version requirements;
- historical research must be traceable to source assumptions.

---

## 9. GEX architecture

GEX exists as a cross-cutting analytics domain.

Relevant backend components include:

- GEX routers;
- `LiveGexService`;
- GEX history/persistence services;
- GEX capture;
- GEX data-quality analysis;
- historical GEX services.

The current raw GEX convention is:

```text
raw_gex = gamma × open_interest × spot² × 0.01
```

The repository convention does not add a lot-size multiplier to this raw calculation.

Signed GEX uses the established call/put sign convention.

### 9.1 GEX persistence ownership

Authenticated GEX snapshots are user-scoped.

Background capture also requires explicit user/connection authorization and must not silently select an arbitrary user's broker connection.

### 9.2 Historical GEX

Historical GEX is an operationally controlled feature.

The repository exposes explicit configuration controls for history/capture behavior. Historical collection is not assumed to be globally active merely because the service exists.

---

## 10. Historical-data architecture

Historical data has a separate operational lifecycle from request-time product behavior.

The broader flow is:

```text
Upstream historical API
        │
        ▼
Backfill / ingestion
        │
        ├── checkpoints
        ├── rate control
        ├── ingestion logs
        └── completeness checks
        │
        ▼
Persisted historical market data
        │
        ▼
Derived Greeks
        │
        ▼
Historical GEX / research analytics
```

Important architectural properties:

- ingestion is checkpointable;
- rate limiting and upstream failure are explicit concerns;
- source limitations must remain visible;
- derived analytics must not overwrite raw source records;
- historical collection must remain bounded by configuration and retention policy.

---

## 11. Background processing

The web application contains controlled background behavior, including the optional GEX capture loop.

Background work must:

- have explicit enablement;
- use user-owned authorization;
- close database sessions reliably;
- tolerate individual failures;
- apply bounded retry/backoff where implemented;
- stop cleanly during application shutdown;
- avoid creating hidden global mutable state.

Long-running ingestion/backfill workflows also exist as operational tooling rather than being treated as ordinary HTTP request handlers.

---

## 12. Configuration architecture

Backend configuration is centralized through the application settings layer.

Configuration controls include:

- database connection;
- broker/application credentials;
- historical-data feature flags;
- GEX capture/history behavior;
- candle interval/retention;
- frontend origin / API origin integration.

Feature flags that govern expensive or historical collection must default to the safe/non-collecting state unless explicitly enabled.

Secrets belong in environment/configuration infrastructure, never source-controlled documentation.

---

## 13. Runtime and deployment architecture

The current runtime topology is:

```text
GitHub
  │
  ├──────────────▶ Vercel
  │                 └── Next.js frontend
  │
  └──────────────▶ Render
                    └── FastAPI backend
                         │
                         └── CockroachDB (legacy; target Neon PostgreSQL per ADR-019)
```

### Current hosting responsibilities

| Layer | Current platform | Responsibility |
|---|---|---|
| Frontend | Vercel | Next.js public website and authenticated web application |
| Backend | Render | FastAPI application, authentication, broker integration, business logic, paper execution, analytics APIs |
| Database (current endpoint, legacy) | CockroachDB | Legacy durable application database used by the backend (unreachable; retirement in progress) |
| Database (target) | Neon PostgreSQL | Future production database, established as a clean database using the approved schema/migration chain (ADR-019; not yet provisioned or cutover) |

The backend connects to its configured database through the SQLAlchemy database URL. Neon staging is a separate database/environment from the future production database. Do not describe production as already running on Neon until cutover evidence exists.

The public website and authenticated application have separate frontend/deployment concerns within the product architecture.

Deployment is operationally controlled and must not be inferred from code changes alone.
## 14. Verification architecture

The project uses multiple verification layers:

```text
Unit / domain tests
        ↓
API / integration tests
        ↓
Security / ownership / migration tests
        ↓
Frontend test suite
        ↓
Next.js production build
        ↓
Browser/runtime verification where applicable
        ↓
CI
```

Backend verification should cover domain correctness, persistence, security boundaries, concurrency, and migration behavior.

Frontend verification should cover component behavior, application behavior, route/runtime behavior, accessibility where applicable, and production build integrity.

A green unit suite does not by itself prove browser/runtime correctness, deployment correctness, or security isolation.

---

## 15. Architectural boundaries that should not be crossed casually

### Frontend → Backend

Do not move authoritative business state into browser-only storage for convenience.

### Backend → Broker

Do not bypass the broker gateway/adapter boundary with direct provider calls from unrelated business modules.

### Router → Database

Do not duplicate domain state transitions directly in many routers. Use the established service/domain path.

### ORM → Schema

Do not treat model definitions as a substitute for Alembic migration history.

### Historical docs → Current architecture

Do not treat an old phase plan or audit as automatically current.

### User → Broker identity

Do not equate broker account identity with the StrikeNova platform user.

### Quant formula → UI convenience

Do not alter a persisted analytical methodology merely to simplify frontend presentation.

---

## 16. Common architectural change patterns

### Adding a feature

Prefer:

```text
Issue
  ↓
Boundary identification
  ↓
Service/domain change
  ↓
API contract
  ↓
Frontend integration
  ↓
Tests / verification
```

### Adding a broker

Prefer:

```text
Broker domain capability
        ↓
Provider adapter
        ↓
Registry / gateway
        ↓
Service integration
        ↓
User-owned connection
        ↓
Verification
```

Do not spread provider-specific conditionals through the application.

### Changing database structure

Prefer:

```text
Schema design
  ↓
Alembic migration
  ↓
Data compatibility analysis
  ↓
Application model/service updates
  ↓
Migration tests
  ↓
Runtime verification
```

### Changing a quant methodology

Prefer:

```text
Research / evidence
  ↓
Decision record
  ↓
Methodology version/change contract
  ↓
Implementation
  ↓
Historical compatibility analysis
  ↓
Tests + analytics verification
```

---

## 17. Source-of-truth map

| Question | Primary authority |
|---|---|
| What is the current implementation? | Repository code |
| What work is active? | GitHub issues / project workflow |
| How must an agent operate? | `AGENTS.md` |
| Where should an agent start reading? | `AI.md` |
| What is the current system map? | `CONTEXT.md` |
| What architecture is currently intended? | `ARCHITECTURE.md` + active `DECISIONS.md` |
| What must not be broken? | `INVARIANTS.md` |
| Why was a major decision made? | `DECISIONS.md` |
| What happened historically? | `options-dashboard-project/docs/` |
| What is the project workflow? | `PROJECT-CONTROL.md` |

When these sources appear inconsistent, inspect the current code and active control documents before acting.

---

## 18. Architecture-change rule

Architecture may evolve. The goal is not to freeze implementation details.

The requirement is to preserve intentional boundaries and make material changes explicit.

When a change affects an architectural invariant or active decision:

1. identify the affected boundary;
2. record the proposed change;
3. update the relevant decision/invariant documentation;
4. implement the change in a scoped issue;
5. verify the integrated behavior;
6. update this architecture map when the topology actually changes.

The architecture document should describe the system that exists, not an imagined future system.

---

## 19. Architectural principle

StrikeNova should remain understandable as the system grows:

**Clear boundaries, authoritative state, explicit ownership, controlled change, and evidence-backed verification.**

The architecture is successful when a future developer or AI agent can determine:

- where a behavior belongs;
- which layer owns the truth;
- which boundary must not be bypassed;
- which decision governs the change;
- and how to verify that the change preserved the system.
