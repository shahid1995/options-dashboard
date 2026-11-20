# StrikeNova — Data Architecture and Lifecycle

> **Purpose:** Living data map for StrikeNova: what data exists, where it comes from, who owns it, how it is transformed, where it is persisted, and what controls apply to its collection and retention.
>
> **Status:** Active
>
> **Primary rule:** Data ownership, provenance, lifecycle, and sensitivity must remain explicit. A table or API response must not become a different class of data merely because that is convenient for implementation.

---

## 1. Data architecture at a glance

StrikeNova follows a layered data model:

```text
External / user input
        │
        ├───────────────┐
        │               │
        ▼               ▼
Broker market data   User/auth data
        │               │
        ▼               ▼
RAW / source state   IDENTITY / ownership
        │
        ▼
Derived models
(Greeks, normalized state)
        │
        ▼
Analytics
(GEX, exposure, research)
        │
        ▼
Frontend presentation
```

The important distinction is:

- **Raw/source data** represents upstream observations or source facts.
- **Identity data** represents StrikeNova users, sessions, and broker relationships.
- **Transactional data** represents paper-trading state and financial simulation effects.
- **Derived/model data** is calculated from source data.
- **Analytics data** is a reusable interpretation of source/model data.
- **Operational data** records ingestion, checkpoints, completeness, and system behavior.

These classes have different ownership, retention, and change rules.

---

## 2. Source-of-truth rules

### 2.1 Raw data is not the same as derived data

Raw market data must remain distinguishable from calculated Greeks, GEX, signals, or other analytics.

Derived calculations must not silently overwrite the source observations from which they were calculated.

### 2.2 Transactional state is authoritative for paper trading

For paper trading:

- executions;
- orders;
- positions;
- cash transactions;
- exposure attribution

form the authoritative transactional state.

Legacy journal records are compatibility/presentation records linked to the authoritative execution domain.

### 2.3 Identity data is authoritative for ownership

The canonical ownership chain is:

```text
User
  ↓
UserSession / BrokerConnection
  ↓
User-owned resources
```

A broker account identifier, session transport value, or frontend object ID must not become an alternate ownership authority.

---

## 3. Data ownership classes

| Data class | Typical examples | Owner | Shared? |
|---|---|---|---|
| Platform identity | users, sessions | StrikeNova account | No |
| Broker connection | broker connections, credentials, tokens | Specific user | No |
| Paper trading | accounts, executions, orders, positions, transactions | Specific user | No |
| User analytics | user GEX snapshots, annotations, strategy state | Specific user | No |
| Raw market data | candles, contract metadata | Product/system | Yes, where intentionally shared |
| Derived market models | option Greeks | Product/system | Yes, where intentionally shared |
| Historical analytics | historical GEX, IV observations | Product/system or explicitly scoped research owner | By design |
| Operational ingestion | logs, checkpoints, completeness | System operations | Internal only |

The word “shared” means reusable application data, not public data exposure. Access controls still apply.

---

## 4. Identity and security-sensitive data

The identity domain contains:

- `users`;
- `user_sessions`;
- `broker_connections`;
- `broker_tokens`.

### 4.1 User records

The `users` table represents the durable StrikeNova platform identity.

Examples of identity data include:

- user ID;
- email;
- display name;
- status;
- authentication/provider identifiers;
- timestamps.

Ownership remains tied to the StrikeNova user, not to a transient broker session.

### 4.2 Sessions

Session records contain security-sensitive authentication metadata.

Current lifecycle properties include:

- session hash;
- user ownership;
- creation time;
- expiry;
- revocation;
- broker-connection association where applicable.

The raw session identifier must not be treated as durable database identity.

### 4.3 Broker connections

Broker connections contain relationship and capability state for a specific user.

They may include:

- broker;
- broker account identifier;
- connection status;
- default-connection state;
- data/trading capability state;
- provider metadata;
- encrypted broker credentials;
- encrypted Analytics Token;
- configured redirect/static-IP metadata.

### 4.4 Broker tokens

Broker tokens are session/connection-scoped secret material.

They are encrypted at rest and must remain inaccessible across user boundaries.

---

## 5. Paper-trading data domain

Paper trading is a transactional data domain.

### 5.1 Paper account

`paper_accounts` stores per-user simulated account configuration such as starting capital.

### 5.2 Authoritative execution records

The primary execution tables are:

- `strategy_executions`;
- `paper_orders`;
- `positions`;
- `paper_transactions`.

Supporting attribution/state tables include:

- `strategy_leg_exposures`;
- `exit_exposure_allocations`;
- `bulk_exit_records`.

### 5.3 Journal records

The legacy-facing:

- `trades`;
- `legs`

remain part of the product data model.

They are not an independent accounting source of truth when linked to the authoritative execution engine.

### 5.4 Paper cash

Available cash is derived from the starting capital plus the signed transaction ledger.

The transaction ledger must therefore remain auditable and internally consistent.

### 5.5 Position history

A closed position record remains queryable.

Closing a position must not mean deleting the historical record merely to make the active-position view simpler.

---

## 6. Strategy and template data

Strategy-related data includes:

- strategy identifiers and metadata;
- strategy templates;
- template legs;
- execution linkage;
- annotations/tags/notes where supported.

Strategy data that is user-created or user-owned must remain user-scoped.

A shared template or system-provided strategy must be explicitly identified as such rather than inferred from absence of a user ID.

---

## 7. Raw market-data domain

Historical/raw market data is distinct from transactional user data.

Current raw/reference models include:

- `nifty_candles`;
- `option_candles`;
- `contract_specs`.

### 7.1 NIFTY candles

`nifty_candles` stores index market observations used by historical analytics and Greek reconstruction.

### 7.2 Option candles

`option_candles` stores historical option market observations.

The option-candle pipeline should remain focused on source market observations rather than mixing in higher-level trading analytics.

### 7.3 Contract specifications

`contract_specs` stores instrument metadata such as:

- instrument identity;
- strike;
- option type;
- expiry;
- lot size;
- provider metadata as applicable.

Historical contract metadata has special immutability requirements.

A known valid historical lot size should not be silently overwritten by a conflicting later observation.

---

## 8. Derived market-model domain

The primary derived model currently includes:

- `option_greeks`.

The conceptual pipeline is:

```text
Raw option candles
       +
Contract specifications
       +
NIFTY spot candles
       ↓
Greek reconstruction
       ↓
option_greeks
```

Derived Greeks may be recalculated when methodology changes.

Raw source tables should not be changed merely because a new calculation method is introduced.

---

## 9. GEX and analytical data domain

Relevant analytical data includes:

- `gex_snapshots`;
- `historical_gex`;
- `iv_observations`.

### 9.1 GEX methodology

The current repository convention is:

```text
raw_gex = gamma × open_interest × spot² × 0.01
```

The current convention does not add a lot-size multiplier to this calculation.

GEX methodology is a controlled analytical contract. Historical records must remain interpretable according to the methodology that produced them.

### 9.2 User GEX snapshots

Authenticated snapshot records are user-scoped where the product captures them from a customer's authorized broker connection.

A user-facing snapshot must not become globally visible merely because its underlying market data is public.

### 9.3 Historical GEX

Historical GEX is a derived/research dataset.

It must preserve:

- source provenance;
- calculation status;
- exclusion reasons;
- timestamp identity;
- instrument identity;
- methodology assumptions.

The presence of historical analytical tables does not mean historical collection must remain continuously enabled.

### 9.4 Data quality

The GEX quality layer explicitly distinguishes valid observations from:

- missing OI;
- zero OI;
- missing/invalid spot;
- missing/invalid gamma;
- invalid strike/type;
- incomplete chains;
- failed calculations;
- provider-specific data limitations.

Missing or invalid data must not be silently converted into valid-looking analytics.

---

## 10. Provenance

Data provenance answers:

**Where did this value come from, and what happened to it before it was presented?**

For important analytical data, provenance should preserve enough information to distinguish:

1. upstream/provider source;
2. normalization;
3. validation;
4. persistence;
5. derivation;
6. analytical interpretation.

Examples:

```text
Upstox option chain
    ↓
normalized chain
    ↓
GEX calculation
    ↓
user snapshot
    ↓
GEX visualization
```

and:

```text
Upstox historical candle
    ↓
raw option_candles
    ↓
Greek reconstruction
    ↓
option_greeks
    ↓
historical GEX
```

Do not describe a derived number as though it were a direct broker observation.

---

## 11. Data lifecycle

Each major data class should have an explicit lifecycle:

```text
Collect
  ↓
Validate
  ↓
Persist
  ↓
Use
  ↓
Derive / aggregate
  ↓
Retain for defined purpose
  ↓
Archive / prune / delete when policy permits
```

### 11.1 Collection

Collection must be intentional.

Expensive or historical collectors are configuration-controlled.

### 11.2 Validation

Invalid source values should be rejected, excluded, or explicitly marked rather than silently repaired.

### 11.3 Persistence

Persistence should preserve the role of the data:

- raw remains raw;
- transactional state remains auditable;
- derived data remains reproducible where practical;
- sensitive records remain protected.

### 11.4 Retention

Retention must be based on product purpose and operational cost.

Do not introduce indefinite retention simply because storage is currently available.

### 11.5 Deletion/pruning

When deletion or pruning exists:

- it must be scoped to the intended owner/domain;
- it must not break referential or audit expectations;
- retention cleanup must be deterministic and safe to retry.

---

## 12. Historical data collection controls

The current configuration exposes separate controls for historical/capture behavior, including:

- `IV_HISTORY_ENABLED`;
- `GEX_HISTORY_ENABLED`;
- `GEX_CAPTURE_ENABLED`;
- `CANDLE_BACKFILL_ENABLED`.

The current defaults are disabled for these historical/capture features.

This is deliberate.

A collector must not be globally activated merely because:

- its code exists;
- a UI can display historical data;
- an agent believes more data would be useful;
- storage is available.

Enabling collection requires explicit consideration of:

- upstream limits;
- request volume;
- storage growth;
- collection frequency;
- retention;
- provenance;
- operational failure/retry behavior;
- user ownership where applicable.

---

## 13. Ingestion and operational data

The ingestion infrastructure includes:

- `ingestion_log`;
- `data_completeness`;
- `ingestion_checkpoint`.

These records are operational metadata.

They should answer questions such as:

- what pipeline ran;
- what was attempted;
- what succeeded/failed;
- what remains incomplete;
- where processing can resume.

Operational data is not automatically product-facing data.

Do not expose internal ingestion details through public product APIs without an explicit requirement and security review.

---

## 14. Timestamps and temporal data

Time is part of the data model.

The system should distinguish:

- broker/provider timestamps;
- UTC persistence timestamps;
- market-local time;
- expiry dates;
- execution timestamps;
- ingestion timestamps.

For historical market data, stored UTC timestamps are the canonical persistence/deduplication representation where the relevant pipeline specifies UTC.

Do not compare or deduplicate records by mixing local-time strings and UTC timestamps.

Expiry is a domain identifier, not merely a display date.

---

## 15. Instrument identity

Instrument identity must remain explicit.

For option data, relevant dimensions can include:

- underlying symbol;
- expiry;
- strike;
- option type;
- provider instrument key.

A market-data record from one expiry must not be silently substituted for another expiry merely because the symbol and strike match.

For paper positions, the repository's position identity includes:

```text
user + symbol + expiry + strike + option_type
```

This identity is part of correctness.

---

## 16. Shared versus user-scoped analytics

A recurring architectural distinction is:

### Shared source data

Examples:

- historical NIFTY candles;
- option candles;
- contract specifications;
- recalculated Greeks;
- system-level research datasets.

These can be reused by many users where intentionally designed.

### User-scoped derived state

Examples:

- user's GEX snapshots;
- user's annotations;
- user's strategy state;
- user's paper portfolio;
- user's execution history.

These must remain isolated to the owning user.

### Rule

Public market origin does not imply shared application ownership.

A public market fact can become a user-owned record when the product captures it as part of that user's workflow or configuration.

---

## 17. Data sensitivity classification

Use this baseline classification for implementation decisions:

| Class | Examples | Handling |
|---|---|---|
| **Critical secret** | encryption key, broker secret, access/refresh token | Server-only, encrypted/protected, never logged |
| **Authentication-sensitive** | session identifiers/hashes, OAuth state, password hashes | Strict access, short/controlled lifetime where applicable |
| **User confidential** | paper portfolio, orders, positions, journal, strategy notes | User-scoped authorization |
| **Operational internal** | ingestion checkpoints, internal diagnostics | Backend/admin use only unless explicitly exposed |
| **Product analytical** | derived GEX, Greeks, research metrics | Preserve provenance and methodology |
| **Shared market/reference** | candles, contract specifications | Shared only where explicitly designed |

A lower sensitivity classification must never be inferred from “the source data is publicly observable.”

---

## 18. Data consistency rules

### Rule 1 — Owner consistency

A user-owned record must always resolve to the correct owner.

### Rule 2 — Source consistency

Derived values must remain traceable to the source/model inputs needed to interpret them.

### Rule 3 — Temporal consistency

Records must not mix incompatible timestamps, expiries, or market sessions.

### Rule 4 — Transaction consistency

Paper-trading state changes must preserve the relationship between:

- execution;
- order;
- position;
- transaction;
- exposure;
- journal.

### Rule 5 — Idempotency consistency

Retry-safe operations must not create duplicate financial or ownership effects.

### Rule 6 — Methodology consistency

Persisted analytical results must remain interpretable according to their calculation methodology.

---

## 19. Data migration rules

A data/schema change must consider both structure and meaning.

Before changing a data model:

1. identify producers;
2. identify consumers;
3. identify current ownership;
4. identify current nullability/constraints;
5. identify historical rows affected;
6. identify derived data that must be regenerated;
7. create the appropriate Alembic migration;
8. verify existing data compatibility.

Do not alter a column merely because its current representation feels inconvenient.

---

## 20. Data deletion rules

Deletion of user-owned data is a product/security operation, not merely a SQL operation.

Before deleting user data, determine:

- whether the data is user-owned;
- whether it has dependent records;
- whether audit/history must remain;
- whether deletion should cascade;
- whether derived/shared data can actually be deleted;
- whether the action is reversible.

Never delete another user's data as a side effect of a user-level cleanup.

Never treat "reset" as permission to bypass ownership or transactional safeguards.

---

## 21. Analytics reproducibility

Where a calculation materially affects product decisions or research, strive for reproducibility.

At minimum, preserve enough information to answer:

- what source data was used;
- what timestamp/expiry/instrument was used;
- what methodology produced the result;
- whether source data was complete/valid;
- whether the result was user-captured or system-generated.

For important methodology changes, update the decision/architecture records and tests together with the implementation.

---

## 22. Backup and recovery expectations

Durable production data must have an operational recovery strategy appropriate to its criticality.

This document does not prescribe a specific vendor-level backup schedule.

It does require that:

- production data is not considered safe merely because it is persisted;
- restore procedures are known and testable;
- schema migrations are compatible with the recovery process;
- user transactional data and sensitive credential data are included in recovery planning;
- recovery does not silently violate user ownership.

The target production database is Neon PostgreSQL. CockroachDB is historical and is no longer a supported target. Backup/restore controls belong to the production database operations environment as well as the application.

---

## 23. Data export and observability

Exports, debugging dumps, screenshots, fixtures, and diagnostics can turn safe backend data into a security incident.

Do not export:

- passwords;
- broker secrets;
- access tokens;
- refresh tokens;
- encryption keys;
- full session identifiers.

When exporting user-owned product data, preserve ownership context and minimize the dataset to what is required.

Operational diagnostics should prefer summaries and identifiers over full sensitive payloads.

---

## 24. AI-agent rules for data changes

AI agents must not:

- invent ownership where the schema is ambiguous;
- turn user-scoped data into shared data without a decision;
- add a collector without considering storage/retention;
- silently modify a quant methodology;
- overwrite raw data with derived values;
- delete production/history data as cleanup;
- bypass migrations for schema changes;
- copy live secrets into fixtures or documentation;
- use historical data counts from old audits as though they were current production counts.

When data behavior changes, update the relevant source-of-truth documentation as part of the same controlled work item.

---

## 25. Data verification checklist

A data-affecting change should verify, where applicable:

### Ownership

- correct user can access the record;
- another user cannot access the record;
- no unsafe fallback occurs.

### Provenance

- source versus derived status remains clear;
- metadata needed for interpretation is preserved.

### Integrity

- constraints remain valid;
- transactions remain consistent;
- idempotency remains intact.

### Time/instrument identity

- timestamps remain correct;
- expiry/instrument identity is preserved;
- no cross-expiry substitution occurs.

### Retention

- new collectors have a defined retention behavior;
- cleanup is bounded and safe;
- storage growth is understood.

### Migration

- Alembic migration applies cleanly;
- existing rows remain compatible;
- rollback/recovery implications are known.

---

## 26. Target storage topology

The target production architecture is:

```text
Next.js frontend
      |
      ▼
FastAPI backend on Render
      |
      ▼
Neon PostgreSQL
      |
      ├── identity / session data
      ├── broker connection metadata and encrypted secrets
      ├── paper-trading state
      ├── market/reference data
      └── analytics / ingestion data
```

Local development may use SQLite through the backend configuration when DATABASE_URL is not set.

Target production persistence must be treated as the configured Neon PostgreSQL environment, not as the local SQLite file. CockroachDB is historical and is not a supported target, so no CockroachDB-specific data, schema or recovery behaviour may be added.

---

## 27. What this document should not contain

Do not add to DATA.md:

- real user records;
- production row counts copied from private environments;
- passwords or tokens;
- database credentials;
- connection strings containing secrets;
- temporary debugging output;
- one-off migration notes that are no longer current.

DATA.md is a durable architectural contract, not an operational dump.

---

## 28. Data principle

StrikeNova data architecture should remain:

**Owned correctly → sourced explicitly → transformed transparently → persisted deliberately → retained purposefully → deleted safely → verified independently.**

The goal is not merely to store data. The goal is to ensure that future developers and AI agents can answer:

- who owns this data;
- where did it come from;
- what does it mean;
- what can safely change;
- how long should it exist;
- and how do we prove it remains correct.
