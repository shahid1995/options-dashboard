# StrikeNova Overnight Gap Intelligence — Phase 4: Prospective End-of-Session Chain Capture

**Issue:** #80 — Research Phase 4 (new authorized data capture; no model changes)
**Phase 1–3 records:** `GAP_INTELLIGENCE_PHASE1.md` / `..._PHASE2.md` / `..._PHASE3.md`
**Scope guard:** research-only. No dashboard UI, no user-facing forecast, no live
gap signal, no paper/broker execution changes, no ML, no paid vendors, no
deployment. Nothing here schedules itself: capture is operator-invoked.

---

## 1. Authorized capture-path audit (before any code)

| Question | Finding (measured in the canonical branch) |
|---|---|
| Canonical chain path | Day-11 gateway → broker adapters (`get_option_chain(symbol, expiry)`) → Day-9 canonical `OptionChainObservation`; used today by the public chains router and the Phase 8B GEX capture loop |
| Chain fields mapped before Phase 4 | LTP, volume, OI only — **both adapters silently dropped** the bid/ask/quantities and broker analytics their payloads carry |
| Observed IV availability | FYERS `options-chain-v3` carries `callIV/putIV` (percentage); FYERS's own community confirms IV was historically absent and was added later — payloads are **variable**, so mapping must be payload-tolerant. Upstox legs carry `option_greeks.iv` (percentage) |
| Observed Greeks | FYERS `callDelta/putDelta`, `callGamma/putGamma` (and vega/theta keys exist in payloads); Upstox `option_greeks` (delta/gamma/vega/theta). Unit conventions for vega/theta are **not verified** |
| Bid/ask + quantities | Present in both payloads (FYERS `callBidPrice/callAskPrice/callBidQty/callAskQty`; Upstox `market_data.bid_price/ask_price/bid_qty/ask_qty`) |
| Futures / India VIX | No adapter endpoint, no mapper, no model, no ingestion anywhere in the repository — genuinely unavailable on this path |
| Instrument/expiry metadata | `get_option_contracts(symbol)` returns expiry list; front expiry = `expiries[0]` (the same selection the GEX loop uses) |
| Timestamps / timezone | Canonical observation carries `market_timestamp` (event time when payload provides it) and `received_timestamp`; brokers report IST wall-clock context; capture compares **instants** (tz-aware), never wall-clock strings |
| End-of-session capture capability | Precedent exists: `GexCaptureService.capture_once()` + flag-gated `asyncio` loop in `main.py` (analytics-token-first, OAuth fallback, explicit default connection only) |
| Scheduler / runtime | No new scheduler added in Phase 4. Capture is a CLI command following the `run_*.py` research-CLI pattern; a future flag-gated loop can mirror the GEX loop without new architecture |

**Key audit defect found:** the chain mappers discarded most of what the
brokers send. Phase 4's first change is therefore to *stop dropping observed
data* at the adapter boundary.

## 2. What Phase 4 changed

| File | Change | Why |
|---|---|---|
| `app/market_data/contracts.py` | `PriceQuote` gains optional `iv`, `delta`, `gamma` (observed broker analytics; conventions documented; **vega/theta deliberately not mapped** — unverified broker units, converting would fabricate semantics) | the canonical contract had nowhere to carry observed IV/Greeks |
| `app/brokers/adapters/fyers/mapper.py` | chain rows now map bid/ask/qty + IV (percentage → decimal fraction) + delta/gamma when present; absent stays `None` | stop dropping observed data; payload-tolerant (FYERS shipped chains with and without analytics) |
| `app/brokers/adapters/upstox/mapper.py` | same for legs: `market_data` bid/ask/qty, `option_greeks` iv/delta/gamma (percentage → decimal) | same |
| `app/research/gap_capture.py` (**new**) | `capture_session()`, `observation_to_research_rows()`, `classify_session()`, `resolve_capture_token()`, `observed_iv_coverage()` — converts a canonical observation into the Phase-1 research schema with `observed`-value provenance | the research snapshot contract, reusing existing persistence |
| `run_gap_research.py` | `capture` subcommand (operator-invoked; fetches one front-expiry chain via the existing gateway/adapter, persists it) | execution interface without touching production code paths |
| `app/research/gap_pipeline.py` | two latent Phase-1 defects fixed (see §6) | exposed by the new tests |
| `tests/test_gap_research.py` | +10 focused tests (§7) | contract coverage |
| `docs/research/GAP_INTELLIGENCE_PHASE4.md` | this record | documentation |

**No migration was required**: the Phase-1 research tables already carry every
needed column (bid/ask/qty/iv/delta/gamma/vega/theta + `value_provenance` from
Phase 3). Schema additions would have violated the "only if genuinely
required" rule.

## 3. Prospective snapshot contract

Persisted through the existing `ingest_session_snapshots` (immutability,
natural-key uniqueness, JSON raw-chain copy unchanged):

* **Underlying snapshot** — spot LTP (observed); spot OHLC, futures
  (ltp/oi/volume), India VIX: `unavailable` on this authorized path — stored
  as NULL, never zero.
* **Chain rows** (per expiry/strike/type) — LTP, volume, OI, bid, ask, bid
  qty, ask qty, IV, delta, gamma: `observed` when the broker payload carried
  them, absent (NULL) when it did not. `change_in_oi` is `derived` — the
  difference against the previously captured session's OI for the same
  (expiry, strike, type), only when that prior exists; otherwise missing.
  Vega/theta: `unavailable` at the canonical contract (unit safety).
* **Provenance** — per-row `value_provenance` JSON records
  observed/derived/unavailable per value class; `capture` summary records
  cutoff, source timestamps, classification, token source
  (`analytics_token` / `broker_oauth`). Capture NEVER writes
  `reconstructed` — no Black-Scholes value can ever be labelled observed.

### Cutoff integrity

The caller declares the end-of-session cutoff. `capture_session` rejects an
observation whose **market/event timestamp** is after the cutoff (instant
comparison: a 10:00 UTC observation equals a 15:30 IST cutoff — tested).
Receive-time is recorded as-is (never back-dated to the cutoff).

### Classification / expiry / DTE

Front expiry comes from broker contract metadata (`expiries[0]`, same rule
as the GEX loop); DTE = calendar days between session date and expiry;
`expiry` vs `non_expiry` classification derives from that metadata — never
from data absence.

### Idempotency / duplicates

Re-capturing an existing session raises `SessionExistsError` (research
immutability preserved); `--replace` replays deterministically to identical
rows (tested).

## 4. Storage

* **Captured:** one row per (session, expiry, strike, CE/PE) with observed
  quote/book/analytics fields, plus one underlying snapshot per session.
* **Natural dedup key:** existing `uq_gap_chain_symbol_session_expiry_strike_type`
  (unique per symbol/session/expiry/strike/type) and
  `uq_gap_underlying_symbol_session`.
* **Frequency:** one capture per trading session at the research cutoff
  (operator-invoked CLI today); a future flag-gated loop would mirror the GEX
  capture config pattern (`GEX_HISTORY_SAMPLE_SECONDS` precedent).
* **Expected growth:** NIFTY chains run ~250–400 instruments/day
  (historical: 285–362 in the Oct–Nov 2024 daily backfill; 40–60 on captured
  expiry days) → **~500–800 chain rows + 1 underlying row per session**;
  at 250 sessions/year ≈ 150–200k rows/year (SQLite-compatible; identical to
  existing research-table sizing).
* **Retention / cleanup:** none automatic — research snapshots are immutable
  and small; the Phase-3 merged-store provenance pattern (`_store_provenance`)
  documents source composition, and any future pruning must go through an
  explicit, documented research decision, not silent deletion.
* **Infrastructure:** none added (no paid vendor, no new store, no second
  market-data architecture).

## 5. Validation

Per the issue: **no model performance claims.** The first prospective
validation reports per-capture: sessions captured, chain rows, observed IV /
Greeks / bid-ask coverage (IV coverage computed by `observed_iv_coverage`),
futures/VIX coverage (0% until a new authorized source exists), DTE
distribution, provenance, replay/duplicate behavior, and cutoff integrity —
all measured fields the CLI summary prints. Model re-evaluation is deferred
until enough prospective sessions accumulate for chronological
out-of-sample analysis.

## 6. Latent Phase-1 defects found and fixed (behavior-neutral for Phases 1–3)

1. **`ingest_session_snapshots(replace=True)` failed on re-ingest** — the
   unit of work executes INSERTs before DELETEs, so replacing an existing
   session hit the natural-key UNIQUE constraint. Fixed with a
   `db.flush()` after the deletes. (The historical loader never used
   `replace=True`; the capture replay path did.)
2. **`prior_close` argument was ignored** — the function always derived
   prior_close from the underlying snapshot's `spot_close`/`spot_ltp`. The
   explicit argument is now authoritative (a live capture's spot LTP is not
   the session close); the historical loader passes T's own cutoff close,
   so values were already identical there — behavior-neutral for Phases 1–3.

## 7. Tests

10 new focused tests (all in `tests/test_gap_research.py`), covering:
observed-provenance + missing≠zero; post-cutoff rejection (nothing
persisted); cutoff timezone normalization (UTC vs IST instants); expiry/DTE
classification from metadata; immutability + deterministic replace-replay;
derived change_in_oi only with prior; deterministic row conversion;
compatibility with the existing causal pipeline (features → predictions →
target attachment, gap = 115.0 from the explicit prior close); and both
brokers' chain mappers (analytics mapped when present — IV % → decimal —
missing stays missing when absent). Adapters' mapper/gateway/quality suites
re-run green (291 passed; the one upstox failure is the pre-existing
capabilities-matrix failure verified on the pristine Phase-1 base).

## 8. Limitations

* Vega/theta are not captured at the canonical contract until broker unit
  conventions are verified (raw values remain in adapter payloads for an
  audited future mapping).
* Futures/VIX remain unavailable — the capture improves IV/book/Greeks
  coverage, not the missing macro families.
* Capture is operator-invoked; accumulating a meaningful sample requires
  actually running the command at session ends (or a Founder-approved
  flag-gated loop later).
