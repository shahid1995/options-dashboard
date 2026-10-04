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
- **Active option instrument identity** — the broker's two-segment
  `NSE_FO|<id>` key for a live option contract. The three-segment
  `NSE_FO|<id>|<dd-mm-yyyy>` form names the same instrument with an expiry
  rendering appended; expiry itself is broker-authoritative (see
  [`INVARIANTS.md`](INVARIANTS.md) 17b).

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
  is now VERIFIED** — an authenticated live probe against the broker (the
  PR #125 probe, exposed read-only through the Day-50 admin verification
  seam) returned 129 historical 3-minute candles for the active NIFTY 22400
  CE, instrument `NSE_FO|40687` with authoritative expiry `2026-10-06`, all
  129 open-interest values non-null, and all four capability claims true.
  Repository absence and upstream API incapability are different claims:
  the first still holds, the second no longer does. **What remains missing
  is production persistence**, which is separate architecture work and is
  unaffected by that verification. The Day-50 paper-entry candidate producer
  (Issue #118) needs a *stored* prior-OI observation on the stored candle
  clock to compute ΔOI, so the production paper-entry path still fails
  closed at that gate. Invariant 17a forbids substituting anything else,
  and a probe result — read-only, persisting nothing — may never stand in for
  that stored observation.


## 7. Day-50 live option verification seam

`POST /api/v1/admin/live-verification/option-candle`
(`options-dashboard-project/backend/app/api/v1/admin.py`) is an admin-scoped,
read-only in-process invocation seam around the merged Day-50 probe
(`app/tools/live_verification.py`).

- **Credential:** the authenticated caller's own user-scoped market-data
  credential, resolved through the canonical `resolve_market_data_token`
  path — never a session token, browser cookie, `TokenBridge`, or the
  platform cache. It is never returned, persisted, or logged.
- **Instrument key:** validated through the probe's own allowlist grammar
  before any authenticated Upstox URL is constructed, and the normalized
  (stripped) key is the value passed downstream.
- **Authoritative expiry:** resolved server-side from Upstox contract
  metadata for that exact broker instrument identity (Invariant 17b). The
  request body carries no expiry field; an unmatched instrument, an
  unparseable broker date, or a key whose embedded expiry disagrees with the
  broker fails closed before any candle request is made.
- **Supported universe — NIFTY options only, declared, not inferred:**
  contract metadata is fetched for the NSE `Nifty 50` index underlying
  (`get_option_contracts` takes an *underlying* key, not an option key), so
  authoritative expiry exists here only for `NSE_FO` NIFTY option contracts.
  A key in any other segment — another exchange, an index, an equity — is out
  of scope and fails closed before any credential use or broker request. No
  general underlying-resolution architecture is implied, and the
  `EXPIRY_UNRESOLVED` response states the scope rather than implying coverage
  of arbitrary option instruments.
- **Identity matching:** both sides of every match are reduced to the
  canonical first-two-segment broker identity (`_broker_identity`), because
  Upstox may name one contract either `NSE_FO|<id>` (what `/option/contract`
  actually returns) or `NSE_FO|<id>|<dd-mm-yyyy>` (what the candle endpoint
  accepts). A metadata row that itself carries an expiry rendering must agree
  with that row's own `expiry` field, a row declaring a non-NIFTY underlying is
  not authority, and two rows disagreeing about one contract is an ambiguity —
  all fail closed rather than picking a winner.
- **Output:** a sanitized projection of probe facts and freshness evidence,
  including `authoritative_expiry_source`, which truthfully reports
  caller-supplied provenance. Probe semantics — the four capability claims
  and `live_option_oi_established` — are passed through without
  reinterpretation.
- **Side effects:** the sanitized admin audit record only. The probe is
  read-only against the broker and stores no probe data.