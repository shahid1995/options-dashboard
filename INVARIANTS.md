# StrikeNova — Engineering Invariants

> **Purpose:** This document defines the behaviors, boundaries, and architectural properties that must not be changed casually.
>
> **Status:** Active
>
> **Authority:** These invariants are implementation guardrails. A change to an invariant requires explicit architectural/product approval and an accompanying decision record where the change is material.

## 1. How to use this document

An invariant is a property that the system must continue to preserve across implementation changes.

AI agents and developers must:

1. Read the relevant invariants before changing affected code.
2. Treat an invariant as a constraint, not a suggestion.
3. Stop scope expansion when a requested change conflicts with an invariant.
4. Propose an explicit decision/change record instead of silently weakening the invariant.
5. Verify the affected invariant after implementation.

This document is not a substitute for the issue, architecture documentation, security documentation, or tests. It defines the properties those artifacts must preserve.

---

## 2. Authority and change-control invariants

### INV-001 — Human decision authority remains final

Founder-approved product and architecture decisions remain authoritative.

Agents may inspect, reason, implement, test, and report. They must not silently redefine accepted product behavior, architecture, security boundaries, or compliance-sensitive decisions.

A technically cleaner implementation is not automatically an approved architectural change.

### INV-002 — GitHub remains the implementation authority

Executable project state belongs in GitHub:

- source code;
- migrations;
- tests;
- issues;
- pull requests;
- commits;
- CI evidence;
- implementation documentation.

Historical documents are evidence, not automatically the current truth.

### INV-003 — No autonomous deployment

Implementation agents must not deploy StrikeNova to Vercel, Railway, production databases, or other live environments unless deployment is explicitly authorized for that task.

A successful local build or test does not authorize release.

---

## 3. Identity, tenancy, and session invariants

### INV-004 — User-owned data must remain user-scoped

User-owned records must not become globally shared merely because doing so is simpler.

At minimum, the following domains are user-scoped:

- StrikeNova identity/session state;
- broker connections;
- broker credentials/tokens;
- paper accounts;
- trades and paper execution records;
- positions and orders;
- user GEX snapshots;
- user strategy/template state where ownership applies.

Queries, writes, updates, deletes, caches, and background processing must preserve the authenticated user's ownership boundary.

### INV-005 — No cross-user data leakage through fallback behavior

A missing user-specific record must not cause the application to return another user's record, token, position, execution, or analytics data.

Fallback behavior must be explicit and must never cross a user boundary.

### INV-006 — OAuth state must remain bound to the initiating session when a session-bound flow is used

The broker OAuth callback must be able to identify the session/user that initiated the flow deterministically.

OAuth state must remain protected against:

- session mix-up;
- callback races;
- replay of consumed state;
- accidental association with another user.

Changes to the OAuth state/identity binding model require security review.

### INV-007 — Broker credentials are customer-owned secrets

Broker API credentials and authorization tokens must be treated as secrets.

They must not be:

- exposed to frontend application code when server-side use is sufficient;
- written to logs;
- committed to the repository;
- embedded in source code;
- copied into unrelated user-visible records;
- returned through APIs without an explicit security requirement.

Any new credential path must preserve encryption/protection, ownership, expiry, revocation, and auditability requirements already established by the authentication architecture.

### INV-008 — One authenticated StrikeNova identity owns its connected broker relationships

Broker connection records must remain associated with the correct StrikeNova user.

Broker account identification must not be used as a substitute for StrikeNova identity without an explicit identity-design decision.

---

## 4. Paper-trading execution invariants

### INV-009 — The backend is authoritative for paper execution

The frontend must never be treated as the source of truth for:

- order acceptance;
- fill price;
- filled quantity;
- position state;
- cash;
- realized P&L;
- execution status;
- journal/portfolio reconciliation.

Client-submitted prices, positions, calculations, or status values may be hints or display inputs, but the backend execution domain is authoritative.

### INV-010 — No paper execution when the market is closed or unverifiable

Every paper execution path must pass the backend market-status gate at execution time.

Closed, halted, suspended, or unknown/unverifiable market state must not be treated as open for order execution.

This applies to:

- manual orders;
- strategy-generated orders;
- template execution;
- position exits;
- bulk exits;
- future automated paper-order paths.

### INV-011 — Paper execution must not partially succeed silently

A grouped multi-leg paper execution must preserve the established atomic execution contract.

The current execution engine pre-validates required conditions before writing the authoritative execution state. A failure must not leave a misleading fully-executed trade behind.

Any future introduction of asynchronous or partial fills must define and verify a new explicit lifecycle instead of weakening the current atomic contract accidentally.

### INV-012 — Client order IDs provide idempotency

A retry of the same logical execution must not create:

- duplicate executions;
- duplicate orders;
- duplicate fills;
- duplicate journal entries;
- double-counted cash;
- duplicated position changes.

The existing per-user `client_order_id` contract is part of the execution boundary and must remain intact.

### INV-013 — Quantities and exposure conventions remain internally consistent

Paper-trading quantity semantics currently use lots at the execution layer, with rupee exposure derived using the applicable lot size.

A change between lots, contracts, units, or rupee exposure must update the complete execution, position, P&L, validation, and test chain together.

### INV-014 — Exit operations remain authoritative and idempotent

Position exit, trade-leg closing, and bulk exit operations must:

- identify the correct user-owned open exposure;
- obtain authoritative market pricing through the backend path;
- apply the same execution safeguards as entry;
- remain retry-safe;
- reconcile positions, orders, transactions, and journal state.

---

## 5. Market-data and quant invariants

### INV-015 — GEX methodology is a controlled contract

The current repository convention for raw GEX is:

`raw_gex = gamma × open_interest × spot² × 0.01`

The established convention uses option open interest as stored by the market-data model and does not add a lot-size multiplier to this formula.

Signed GEX currently follows the established call/put sign convention in the repository.

This formula and sign convention must not be altered as an implementation detail.

Any change requires:

1. explicit quant/architecture approval;
2. a decision record;
3. migration or compatibility analysis where persisted data is affected;
4. updated tests;
5. clear documentation of the new methodology.

### INV-016 — Market-data provenance and ownership must remain explicit

The system must distinguish between:

- broker/upstream market data;
- user-owned snapshots;
- derived analytics;
- historical research data;
- operational metadata.

Derived values must not be represented as raw source data without preserving that distinction.

### INV-017 — Data-quality failures must not be silently converted into valid analytics

Missing, invalid, non-finite, contradictory, or stale market inputs must remain distinguishable from valid calculated values.

The application must not silently manufacture valid-looking GEX, Greeks, prices, or signals from invalid source data.

### INV-018 — Historical collection is opt-in and bounded

Historical IV, historical GEX, GEX capture, and candle backfill are controlled data-collection features.

Their configuration defaults are currently disabled.

Enabling or expanding historical collection must consider:

- storage growth;
- retention;
- collection frequency;
- ownership;
- data provenance;
- operational cost;
- cleanup/retention behavior;
- user/privacy boundaries where applicable.

A new collector must not be enabled globally merely because the code exists.

### INV-019 — Time and expiry semantics must remain explicit

Option-chain, expiry, candle, GEX, and execution calculations must not silently mix:

- different expiries;
- stale chain data;
- market timestamps;
- local time and UTC;
- one instrument's metadata with another instrument's data.

When an expiry-specific dataset is required, the corresponding expiry-specific source must be used.

---

## 6. Persistence and schema invariants

### INV-020 — Alembic is the authoritative production schema mechanism

Database schema changes must be represented by versioned Alembic migrations.

Production code must not reintroduce ad-hoc runtime schema creation or column mutation as a substitute for migrations.

The established startup sequence may execute Alembic migrations, but the ORM model definitions alone are not the production migration mechanism.

### INV-021 — Data migrations must be compatible with existing state

Schema changes must consider:

- existing production databases;
- existing rows;
- nullability;
- indexes and constraints;
- rollback/forward-compatibility;
- SQLite development/test behavior;
- CockroachDB production behavior.

A migration is incomplete until the affected existing-data path has been verified.

### INV-022 — Transaction boundaries must preserve domain consistency

Changes involving orders, positions, transactions, execution records, or journal state must preserve the consistency of the aggregate they update.

A developer must not introduce a second independent write path that can leave authoritative paper-trading state partially updated.

### INV-023 — Concurrency controls are part of correctness

Where the current code uses row locking, idempotency constraints, transactional checks, or serialized state transitions to prevent races, those mechanisms are correctness requirements.

They must not be removed or weakened as an optimization or refactor without proving the concurrent behavior remains safe.

---

## 7. API and application-boundary invariants

### INV-024 — Authentication/authorization belongs at the server boundary

Frontend route protection is not sufficient authorization.

Backend endpoints that expose or mutate authenticated resources must validate the current authenticated identity and enforce ownership at the data-access boundary.

### INV-025 — New API behavior must preserve domain ownership

Adding an endpoint must not create a new path that bypasses established:

- authentication;
- user scoping;
- broker authorization;
- market gates;
- validation;
- idempotency;
- transaction rules.

A shortcut endpoint is still part of the security and domain boundary.

### INV-026 — Public and authenticated application boundaries remain intentional

The public marketing website and authenticated application are separate application/deployment concerns.

A change that merges, bypasses, or weakens that boundary must be explicitly designed and reviewed rather than introduced accidentally through routing or authentication changes.

---

## 8. Frontend invariants

### INV-027 — The frontend is a consumer of authoritative backend state

Frontend calculations may support presentation, previews, analytics, and user interaction.

They must not become an alternate source of truth for authoritative execution state.

Where backend and frontend values intentionally differ in role, naming and documentation must make that distinction clear.

### INV-028 — UI changes must not bypass domain safeguards

A UI control that initiates paper execution must call the established server-authoritative execution path.

A new direct browser-side persistence or execution shortcut is not acceptable merely because it reduces latency or code complexity.

### INV-029 — Public-site design changes must preserve accepted design-system contracts

Accepted public-site design-system and accessibility behavior must not be removed or bypassed to implement an unrelated feature.

Intentional visual-system changes require their own scoped change record and verification.

---

## 9. AI-agent behavior invariants

### INV-030 — Agents must inspect current repository state before changing code

Agents must not rely only on historical prompts, old phase documents, cached assumptions, or prior chat context.

The current repository is the implementation reality.

### INV-031 — Agents must not expand scope silently

When implementation reveals a neighboring defect or architectural issue:

- do not silently fix unrelated work;
- record the finding;
- create or update the appropriate issue/decision when required.

### INV-032 — Agents must not weaken an invariant to make tests pass

Tests are evidence of intended behavior, not permission to rewrite the architecture.

If a test conflicts with an invariant, first determine whether the test or implementation is wrong. Do not weaken a system invariant merely to obtain green CI.

### INV-033 — Verification must be fresh

A claim that an invariant is preserved must be supported by verification appropriate to the change.

Previously reported green tests are not fresh evidence for a materially changed code path.

### INV-034 — No secret, credential, or private runtime artifact belongs in repository documentation

Repository documentation may describe the security model and configuration contract.

It must never contain:

- real API keys;
- access tokens;
- passwords;
- private certificates;
- session secrets;
- copied production credentials;
- other live secret material.


### INV-035 — ChatGPT is Markdown-only for repository file changes

ChatGPT may plan, research, brainstorm, review code/diffs, interpret test and CI
evidence, supervise authorised coding agents, write prompts/tasks, prepare
issues and pull requests, and author Markdown documentation. ChatGPT must not
create, edit, delete, generate, or apply patches to non-Markdown repository
files. Non-Markdown implementation is delegated to an authorised coding agent.
See the ChatGPT engineering-supervisor boundary in [AGENTS.md](AGENTS.md).

---

## 10. What requires a decision record

A change should be treated as architectural and require explicit decision tracking when it changes or weakens an invariant involving:

- identity or user ownership;
- broker credential ownership;
- OAuth/session binding;
- real/live order execution;
- paper execution authority;
- idempotency;
- market-open execution gates;
- transaction/concurrency guarantees;
- database migration authority;
- GEX methodology/sign convention;
- historical data collection defaults or retention;
- public/authenticated application boundaries;
- deployment authority.

Implementation details may evolve freely inside these boundaries. The boundary itself must not drift silently.

---

## 11. Verification expectation

When a change touches an invariant, the implementation report must identify:

1. which invariant(s) were affected;
2. what code path changed;
3. how the invariant was preserved;
4. what tests/checks were run;
5. any remaining unverified risk.

The minimum acceptable standard is:

**Inspect → Understand → Change Minimally → Verify → Report Evidence.**

---

## 12. Conflict rule

When repository code, an old document, an agent instruction, or a proposed implementation conflicts with an explicit active invariant:

1. stop treating the conflicting behavior as automatically authoritative;
2. inspect the current implementation and relevant decision records;
3. identify whether the invariant itself has been intentionally superseded;
4. do not silently change the invariant;
5. escalate through the project decision/change-control process when required.

The goal is to prevent architecture drift caused by incremental AI-generated changes.
