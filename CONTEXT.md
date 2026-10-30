# CONTEXT.md

> **Purpose:** Current repository and system context for AI-assisted engineering.
>
> This is a living map of the system. It is intentionally shorter than the detailed architecture, security, data, and testing documents.

## 1. Project

**StrikeNova** is a full-stack options-market analytics and paper-trading application.

The application provides market intelligence, options-chain analysis, quantitative analytics, strategy tooling, and server-authoritative paper trading.

## 2. Repository Layout

Primary application:

`options-dashboard-project/`

Main areas:

- `frontend/` — Next.js/React application
- `backend/` — FastAPI/Python application
- `docs/` — historical and supporting engineering documentation
- `.github/workflows/` — CI automation

Root control documents:

- `AI.md`
- `AGENTS.md`
- `PROJECT-CONTROL.md`
- `CONTEXT.md`
- `INVARIANTS.md`
- `DECISIONS.md`
- `ARCHITECTURE.md`
- `SECURITY.md`
- `DATA.md`
- `TESTING.md`
- `CHANGELOG.md`

## 3. Current Runtime Topology

Production:

```
Browser
   |
   v
Vercel
   |
   v
Render
   |
   v
CockroachDB
```

- Frontend: Vercel
- Backend: Render
- Production database: CockroachDB
- Local/default persistence may use SQLite when the configured database URL is absent.

Do not use historical Railway/PostgreSQL deployment descriptions as the current production topology.

## 4. Application Stack

Frontend:

- Next.js 14.2.x
- React 18.3.x
- JavaScript/JSX
- Vitest
- Recharts
- Axios

Backend:

- Python 3.13
- FastAPI
- Uvicorn
- SQLAlchemy
- Alembic
- Pydantic Settings
- PyJWT
- cryptography

## 5. Application Boundaries

### Identity

Identity/session and broker connectivity are separate concerns.

Core identity models include:

- `User`
- `UserSession`
- `BrokerConnection`
- `BrokerToken`

### Broker

StrikeNova follows the BYOB model. Broker connections are user-scoped and provider-specific behavior belongs behind the broker abstraction/adapter boundary.

### Paper Trading

Paper execution is server-authoritative. The backend owns execution state, fills, positions, orders, portfolio state, and related persistence.

### Quantitative Analytics

GEX and other quantitative calculations are governed by explicit conventions and tests.

Current GEX formula:

`raw_gex = gamma × open_interest × spot² × 0.01`

### Historical Data

Historical/capture pipelines are intentionally controlled and currently disabled by default where the relevant configuration flags are false.

## 6. Persistence

SQLAlchemy is the application ORM.

Alembic is the authoritative production schema migration mechanism.

Production persistence is CockroachDB. SQLite is permitted for local/default operation where repository configuration selects it.

Do not use ad-hoc startup table creation as a substitute for production migrations.

## 7. Verification

Frontend:

- Vitest
- Next.js production build

Backend:

- pytest
- import/startup verification
- targeted domain/security/migration tests

CI is defined under `options-dashboard-project/.github/workflows/`.

Browser/runtime verification is required when the change affects rendered behavior, routing, interaction, authentication handoff, or runtime integration.

See `TESTING.md` for the authoritative verification strategy.

## 8. Current Control Model

- Founder: final product/architecture authority and final acceptance/release authority
- Obsidian Second Brain: durable knowledge/context authority
- GitHub: implementation/project authority and canonical implementation evidence
- Primary Independent Engineering Reviewer (currently ChatGPT): independent technical review and verification for substantive engineering work; advisory, not implementation or merge authority
- Hermes / FreeBuff: execution agents

The current task contract comes from the GitHub Issue. Accepted architecture and invariants must be preserved unless explicitly changed.

## 9. Context Freshness

This document describes the current map, not historical evolution.

When it conflicts with current code or an accepted decision:

1. inspect the implementation;
2. inspect `INVARIANTS.md`;
3. inspect `DECISIONS.md`;
4. inspect `ARCHITECTURE.md);
5. update this document if the current map has materially changed.

Historical documents under `options-dashboard-project/docs/` remain evidence and should not be treated as current truth without confirmation.

## 10. Navigation Rule

Use this file to orient yourself. Do not turn it into a duplicate of the detailed documents.

For detail:

- architecture → `ARCHITECTURE.md`
- security → `SECURITY.md`
- data → `DATA.md`
- testing → `TESTING.md`
- decisions → `DECISIONS.md`
- invariants → `INVARIANTS.md`
- workflow → `PROJECT-CONTROL.md`
