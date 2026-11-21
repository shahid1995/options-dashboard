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
| ADR-017 | PostgreSQL migration-lease protocol, versioned bootstrap/barrier and guarded operator migration path (see `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`) | Approved |
| ADR-018 | Dedicated migration identity and zero runtime privilege on migration infrastructure (see `docs/ADR-017-018-POSTGRES-LEASE-DESIGN.md`) | Approved |
| ADR-019 | Clean-slate Neon PostgreSQL target; CockroachDB retired (`docs/ADR-019-NEON-CLEAN-SLATE.md`) | Active |

**Founder approval note (2026-11-21):** ADR-017/018 decisions D-2 through D-7 are approved for implementation. This does not authorize Neon production provisioning, live role/grant changes, production migrations, deployment or cutover. The application implementation and integration verification remain incomplete.

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
