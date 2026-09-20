# StrikeNova Overnight Gap Intelligence — Phase 2: Data-Enriched SOS Validation

**Issue:** #76 — Research Phase 2 (data enrichment + validation only)
**Phase 1 record:** `GAP_INTELLIGENCE_PHASE1.md` (unchanged; cross-referenced)
**Scope guard:** research/backtest only — nothing here feeds the dashboard,
paper execution, broker execution, auth, or any user-facing forecast.

---

## 1. Discovered historical sources (Phase A audit)

| Source | Location | Content | Status for Phase 2 |
|---|---|---|---|
| `nifty_candles` | authorized candle-store backup (`paper_journal_backup_greeks_20260826_013136.db`) | NIFTY index 3-min OHLCV | **USED** — underlying + targets |
| `option_candles` | same backup | 3-min per-instrument OHLCV + OI | **USED** — chain snapshots |
| `contract_specs` | same backup | token → strike / CE-PE / expiry | **USED** — chain mapping |
| `option_greeks` | same backup | IV + BS Greeks per candle (Phase 7.19B) | **GENERATED** by the canonical engine (`app/services/historical_greeks.py`), computed at research cutoffs only; the 625 stored pilot rows (5 instruments, 2024-10-03) are evidence the engine works, not a usable dataset |
| `historical_gex` / `iv_observations` | schema exists; **0 rows in every local DB** | — | NOT available historically; GEX instead derives from engine gamma via the canonical formula inside `gap_features` (no duplicate math) |
| `gex_snapshots` | `rehearsal_staging.db` (60 rows) | live-capture rehearsals | Not usable for the historical period |
| futures | — | no `FUT` instrument types anywhere | **UNAVAILABLE** (0%) |
| India VIX | — | no table, no symbol rows | **UNAVAILABLE** (0%) |
| bid/ask | — | candle store is OHLCV+OI only | **UNAVAILABLE** (0%) |

The source store is treated as strictly read-only: the CLI validates the three
required source tables via schema inspection and never creates or modifies
anything in it (tested).

## 2. Measured coverage (Phase B)

| Data family | Earliest | Latest | Sessions | Instrument/expiry coverage | Completeness |
|---|---|---|---:|---|---:|
| NIFTY spot | 2024-10-01 | 2026-08-24 | 463 (3-min) | NIFTY index | 57,675 rows |
| option chain (candles) | 2024-10-03 | 2026-08-18 | 96 (expiry days only) | 4,019 instruments; front expiry per session | 514,610 rows; OI 100% |
| contract specs | 2024-10-03 | 2026-08-18 | — | 20,584 (CE 10,289 / PE 10,295), 99 expiries | 100% for captured instruments |
| IV (engine-derived) | at research cutoffs | — | 93 enriched sessions | front-expiry chains | 70.6% of chain rows |
| Delta (engine-derived) | cutoffs | — | 93 | — | 73.6% of rows |
| Gamma (engine-derived) | cutoffs | — | 93 | — | 73.6% of rows |
| Vega (engine-derived) | cutoffs | — | 93 | — | 73.6% of rows |
| Theta (engine-derived) | cutoffs | — | 93 | — | 73.6% of rows |
| OI / change | with candles | — | 96 | — | OI 100%; `change_in_oi` derived causally from prior session |
| GEX | derived from gamma | — | 93 | — | feature-level activation (canonical formula) |
| Futures | — | — | — | — | **0%** |
| India VIX | — | — | — | — | **0%** |
| bid/ask | — | — | — | — | **0%** |

Note: option candles exist only on weekly-expiry days (Tue→Thu over the
years), so the DTE of every enriched session is 0 — expiry-day cutoffs. This
makes expiry-proximity segmentation degenerate (single bucket) and is a
structural property of the store, not a bug.

## 3. Provenance and timestamp rules (Phase C)

* Research cutoff = the session's last option candle (terminal candle,
  typically 15:27 IST). Index candles after the cutoff are excluded from the
  underlying snapshot (tested).
* **IV/Greeks valuation** = the cutoff candle only: S = index close aligned
  at the cutoff, K/market price from the cutoff candle. No EOD alignment.
* The candle store holds **IST wall-clock** timestamps; the canonical
  `HistoricalGreeksEngine` expects UTC valuation timestamps. The adapter
  converts IST→UTC (−5:30) before calling engine functions. Consequence: an
  expiry-day 15:27 IST cutoff is 3 minutes before the 15:30 IST settlement
  reference → T > 0 where the session is the expiry day; rows whose expiry
  has passed at the cutoff take the engine's expired branch (directional
  delta, no IV) and are counted (`expired_at_cutoff`), never patched.
* Rows where the IV solver fails (quote below intrinsic etc.) keep
  `iv`/Greeks = None — **missing ≠ zero** is preserved everywhere.
* Every enriched session stores provenance on its row:
  `completeness = "ENRICHED_GREEKS"`, `completeness_detail` = JSON with
  engine label, calc version, tz rule, per-family row coverage. Phase 1 and
  Phase 2 samples are therefore separately identifiable inside any research
  DB (and are never merged into one result).

## 4. Eligibility profiles (Phase F — derived from measured data)

* `CORE` — spot + chain OI/volume (Phase 1 candle-store baseline).
* `CORE+GREEKS` — CORE + IV/Delta/Gamma/Vega/Theta + canonical GEX features
  (achieved by the enriched sample).
* `FULL` — additionally futures + India VIX + bid/ask: **not achievable from
  any authorized historical source found in this repository**; documented
  for completeness, never claimed.

## 5. Phase 1 vs Phase 2 methodology and results

Phase 1 pipeline, ordering, targets, flat band (±0.1%), and model weights are
**unchanged**. The only Phase-2 difference is the `--enrich-greeks` flag,
which attaches engine-derived IV/Greeks to chain rows before ingest. A
control run (same store, enrichment off) reproduced the Phase 1 metrics
**exactly** — session identity and target identity are preserved, so any
metric difference below is attributable to the added fields alone.

Sample: **93 sessions, 93/93 targets** (2024-10-10 → 2026-08-18),
target distribution FLAT 28 / GAP_UP 32 / GAP_DOWN 33 — identical in both
samples. Determinism: two enriched runs produce byte-identical output
(SHA-256 match).

| Model | Sample | scored/n | NO_EDGE | accuracy | balanced acc. | Brier | ROC-AUC | MAE (pts) |
|---|---|---|---:|---:|---:|---:|---:|---:|
| baseline | control | 91/93 | 2 | 0.352 | 0.333 | — | — | — |
| baseline | enriched | 91/93 | 2 | 0.352 | 0.333 | 0.239 | 0.408 | — |
| pos_style | control | 93/93 | 0 | 0.419 | 0.403 | — | — | — |
| pos_style | enriched | 93/93 | 0 | 0.398 | 0.380 | 0.234 | 0.607 | — |
| sos | control | 42/93 | 51 | 0.357 | 0.357 | — | — | 74.4 |
| sos | enriched | 19/93 | 74 | 0.368 | 0.357 | 0.222 | 0.622 | 74.6 |

What enrichment actually enabled: probability outputs (Brier/AUC) for all
three models; Delta/Vega/IV/GEX feature activation; GEX/IV/prior-day regime
segmentation. What it changed in behavior: SOS **abstains more**
(NO_EDGE 51 → 74) once the fuller component set participates in the
confluence test, and POS-style's fixed-weight score loses accuracy
(0.419 → 0.398) — the added IV/Delta inputs did not help it.

### Regime segmentation (enriched, SOS; report-only)

| Dim | Bucket | n | scored | acc |
|---|---|---:|---:|---:|
| gex | NET_GEX_NEG | 53 | 12 | 0.417 |
| gex | NET_GEX_POS | 40 | 7 | 0.286 |
| iv | IV_HIGH | 45 | 10 | 0.300 |
| iv | IV_LOW | 44 | 8 | 0.500 |
| prevday | PREV_DOWN | 49 | 11 | 0.455 |
| prevday | PREV_UP | 42 | 8 | 0.250 |

Expiry-proximity segmentation is not possible in this dataset (all sessions
are expiry days — see §2). VIX regime remains impossible (no VIX data).

## 6. Out-of-sample discipline (Phase K)

The three models have **no learned or optimized parameters** (fixed explicit
weights), so there is nothing to fit in a training window; walk-forward fold
structure from Phase 1 is retained. As a descriptive check only, a
chronological 70/30 split was evaluated:

* pos_style: train AUC 0.663 (n=65) → test AUC 0.495 (n=28) — collapses to
  chance; Brier degrades 0.204 → 0.304.
* sos: train AUC 0.631 (11 scored) → test AUC 0.589 (8 scored) — far too
  few scored sessions to interpret.

## 7. Limitations

* 93 sessions, expiry days only, one instrument (NIFTY), one market regime
  mix; all sub-slices are tiny.
* IV/Greeks are model reconstructions (Black-Scholes from candle closes,
  6.5% flat risk-free rate) — not observed quotes; deep-ITM/short-T rows
  legitimately fail IV solving (26.4% of rows).
* The front expiry is always the expiry day here (store design), so
  time-to-expiry is ≤ 0.02 years and gamma/vega magnitudes are extreme.
* Futures, India VIX, and bid/ask remain unavailable → SOS still cannot use
  its futures/VIX/bid-ask components; `FULL` eligibility is unreachable with
  current authorized data.

## 8. Conclusion (edge assessment)

**No evidence of predictive value is demonstrated.** In-sample AUC signals
collapse toward chance in the chronological test slice; scored SOS counts
shrink when the fuller component set participates; POS-style accuracy
degrades with enrichment. Phase 2's contribution is a verified, enriched,
deterministic research dataset and the proof that the missing SOS components
are genuinely active — not a trading recommendation.
