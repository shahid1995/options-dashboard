# StrikeNova — Context

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-18

---

## 1. What StrikeNova is

StrikeNova is an options trading intelligence platform for Indian index options
(NIFTY/BANKNIFTY) built around three pillars:

1. **GEX (Gamma Exposure) intelligence** — market-maker positioning analytics
   computed from option chains, with regime/wall/flip tracking and history.
2. **Server-authoritative paper trading** — strategy execution, positions,
   journal, and P&L are computed and stored server-side; the client renders.
3. **BYOB broker connectivity** — users bring their own broker account
   (Upstox first) via OAuth; broker credentials authorize broker API calls only
   and never authenticate the StrikeNova platform.

## 2. Runtime topology (current truth)

```text
Browser
   ↓  HttpOnly strikenova_session cookie
Vercel (frontend — Next.js, options-dashboard-project/frontend)
   ↓  HTTPS REST + cookie-authenticated WebSocket
Render (backend — FastAPI/uvicorn, options-dashboard-project/backend)
   ↓
CockroachDB Cloud (production database)
```

- Frontend deploys on **Vercel**; backend deploys on **Render**; production
  data lives in **CockroachDB Cloud**.
- Local development uses SQLite with no external services.
- The backend is deliberately portable: SQLite (local), PostgreSQL-compatible
  CI, CockroachDB (validated runtime, production target).
- **Railway** references in historical documents describe a superseded
  staging experiment — not current production topology.

## 3. Monorepo layout

```text
options-dashboard-project/
├── backend/
│   ├── app/
│   │   ├── routers/        FastAPI routers (auth, paper, gex, chains, …)
│   │   ├── services/       Domain services (paper_execution, token_store, …)
│   │   ├── brokers/        BYOB broker adapters (upstox/, …)
│   │   ├── broker_sync/    Broker data ingestion models/pipeline
│   │   ├── identity.py     Users, UserSession, identity linking
│   │   ├── models.py       SQLAlchemy models
│   │   ├── schemas.py      Pydantic schemas
│   │   ├── db.py           Engine/session construction (DATABASE_URL-driven)
│   │   └── config.py       Settings (pydantic-settings)
│   ├── alembic/            Migrations — sole schema authority
│   └── tests/              pytest suite (~190 files)
├── frontend/
│   ├── app/                Next.js app router pages
│   ├── components/         React components (AuthGate, public site, …)
│   └── lib/                api.js, useAuth.js, calculations/, session.js
└── docs/                   Historical engineering record + superpowers/ tracker
```

Control documents live at the repository root (see [`AI.md`](AI.md)).

## 4. Glossary (selected)

- **GEX** — Gamma Exposure; aggregate dealer gamma from option chains.
- **Flip point / wall** — GEX regime-transition levels; see
  `options-dashboard-project/docs/GEX_V1_0_SPEC.md` for conventions.
- **BYOB** — Bring Your Own Broker; user-authorized broker connections.
- **Platform session** — a StrikeNova identity session (email/Google),
  carried by the HttpOnly `strikenova_session` cookie; independent of broker
  authorization.
- **Tier-1 / backfill** — historical candle/Greek reconstruction pipeline
  (Phases 7.x); see the phase documents under `docs/`.
- **Superpowers tracker** — `docs/superpowers/STRIKENOVA_IMPLEMENTATION_STATUS.md`,
  the canonical status snapshot.

## 5. Related documents

Architecture detail: [`ARCHITECTURE.md`](ARCHITECTURE.md) · Security:
[`SECURITY.md`](SECURITY.md) · Data: [`DATA.md`](DATA.md) · Testing:
[`TESTING.md`](TESTING.md) · Decisions: [`DECISIONS.md`](DECISIONS.md) ·
Invariants: [`INVARIANTS.md`](INVARIANTS.md)

## 6. Known capability gaps (open prerequisites)

- **Live option-OI history for unexpired contracts.** The repository has no
  production persistence path that stores prior OI for an unexpired/live
  option instrument key: `OptionCandle` is populated exclusively from the
  Upstox *expired*-instruments API (see
  `options-dashboard-project/docs/PHASE_7_13_OPTION_CANDLE_PERSISTENCE.md`
  and `docs/PHASE_7_15_LIVE_BACKFILL_PILOT.md`). **Upstream Upstox capability
  remains UNVERIFIED** — no safe authenticated live-option probe could be
  performed from the development environment (no live backend session, no
  current local broker authorization, and the existing live-verification
  tool targets the NIFTY index only). Repository absence and upstream API
  incapability are different claims; only the former is established here.
  The Day-50 paper-entry candidate producer (Issue #118) needs that
  observation to compute ΔOI, so the production paper-entry path fails
  closed at that gate until live option-OI persistence lands as separate
  architecture work. Invariant 17a forbids substituting anything else.
