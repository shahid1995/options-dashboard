# StrikeNova Overnight Gap Intelligence — Phase 1 Implementation

**Issue:** #17 — Research Phase 1 (POS Benchmark + SOS)
**Status:** Implemented (research/backtest only) — NOT a production signal
**Specification:** `docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md` (2026-08-24)
**Scope guard:** nothing in this module feeds the dashboard, paper execution,
broker execution, auth, or any user-facing forecast.

---

## 1. What was implemented

| Layer | Module | Purpose |
|---|---|---|
| Persistence | `app/models.py` (research models) + `alembic/versions/e9f8a7b6c5d4_issue17_gap_research_schema.py` | 6 research tables (§19) |
| Targets | `app/research/gap_targets.py` | Exact gap formulas + configurable class band (§2) |
| Features | `app/research/gap_features.py` | Delta/Vega/OI/IV/GEX/Futures/Flow/VIX/Cross (§6) |
| Normalization | `app/research/gap_normalization.py` | Causal rolling z-score + robust MAD (§7) |
| Models | `app/research/gap_models.py` | Baselines, POS-style, SOS, probabilities (§3/§8/§9/§10/§11) |
| Backtest | `app/research/gap_backtest.py` | Chronological evaluation + walk-forward folds (§13/§14/§15) |
| Pipeline | `app/research/gap_pipeline.py` | Snapshot → features → predictions → target → backtest |
| CLI | `run_gap_research.py` | Repository-standard execution entry point |
| Tests | `tests/test_gap_research.py` | 42 tests incl. explicit leak-proofing |

## 2. Research schema (§19)

| Table | Content | Immutability |
|---|---|---|
| `gap_prediction_sessions` | One row per session: cutoff, prior close, later-attached `next_open`/`gap_points`/`gap_pct`/`gap_class`, completeness flags | Predictor columns written once; target columns only via the attachment step |
| `gap_underlying_snapshots` | Spot OHLC, futures LTP/OI/volume, India VIX, raw chain JSON | Append-only; re-ingest raises `SessionExistsError` |
| `gap_option_chain_snapshots` | Strike-level CE/PE rows (quote, OI, ΔOI, IV, greeks) | Append-only, unique per (session, expiry, strike, type) |
| `gap_features` | Versioned feature JSON (`feature_version = "v1"`) | Feature code may evolve; raw rows never change |
| `gap_predictions` | Per-model outputs incl. component scores, state, probabilities | Upsert keyed (session, model, version) |
| `gap_backtest_results` | Aggregate metrics per (model, period, regime) | Upsert keyed (model, period, regime) |

Missing data is stored as NULL and flagged via `completeness` /
`completeness_detail` — missing is never silently zero (GEX_V1_0_SPEC §10
convention extended to the research layer).

## 3. Conventions

### PE Delta sign (§6.1)

Broker PE deltas are negative. Every directional delta quantity uses
`abs(pe_delta)`; `delta_diff = ce_delta − abs(pe_delta)` (positive ⇒ call
dominance). Raw negative deltas are never mixed with absolute values.

### OI units

Contracts everywhere, never lots; no lot-size multiplier anywhere (GEX_V1_0_SPEC §11).

### GEX reuse (§6.5)

`app/research/gap_features.gex_features` converts snapshots to the canonical
`OptionMarketData` contract and calls the authoritative
`app.quant.gex.build_gamma_profile`:

    Raw GEX = gamma × OI × S² × 0.01;  Call = +Raw, Put = −Raw  (NAIVE_DEALER_CONVENTION)

Tests verify the engine output against a manual canonical sum. Phase-1
`gamma_flip` is the documented proxy = the largest-|net-GEX| strike (not a
zero-cross interpolation); `spot_to_flip_pct` is the signed spot distance.

### Strike weighting (§5)

Gaussian `exp(−0.5·(d/σ)²)`, σ = 2 strike steps (`WEIGHT_SIGMA_STRIKES`), ATM
window ±3 steps (`ATM_WINDOW_STRIKES`), strike step inferred from the chain.
Windows are research parameters, not assumptions — varying them is future work.

## 4. Normalization (§7)

`z = (x − rolling_mean) / rolling_std` over strictly-prior observations
(`min_history = 20`, else `None`). Robust alternative: median/MAD with the
1.4826 consistency constant. Normalized values winsorized to ±5
(`DEFAULT_Z_CLIP`) — documented post-normalization transform. Zero-variance
windows return 0.0 at the mean, else the clip bound. **Causality:** pipeline
normalization history is queried with `session_date < T` (strictly prior,
tested); missing stays `None` through every layer.

## 5. Models

### Model A — Baselines (§3-A)

Previous-day direction, previous-gap direction, futures basis/price direction,
ATM straddle implied move (`0.8·S·σ·√(1/252)` documented approximation),
unconditional class distribution and expected gap from prior sessions only.
Backtest reference direction = futures-direction (fallback previous-day).

### Model B — POS-style (§3-B, §8)

**This is an approximation for research comparison and does not reproduce or
claim to reproduce Vibhore Gupta's proprietary POS implementation** (the
disclaimer is carried on every prediction and asserted in tests).

Fixed initial weights: delta 0.4 · OI 0.3 · vega 0.2 · price 0.1
(`POS_WEIGHTS`); components bounded to [−1, +1], weights renormalized over
available components; output in [−100, +100] with per-component explainability.

### Model C — StrikeNova SOS (§3-C, §9, §12)

Nine components (delta, vega, OI, IV-skew, futures, flow, GEX, VIX,
price-structure) with documented initial weights (`SOS_WEIGHTS`); normalized
z-inputs squashed via `x/√(1+x²)`, raw fallbacks clipped. Outputs preserved
separately: `direction_score` [−1, +1], `agreement_score` = 1 − dispersion,
`dispersion`, `confidence = |direction|·agreement`. **NO_EDGE** is returned
(not a forced call) when |direction| < 0.12, dispersion > 0.45, or agreement
< 0.55 (`SOS_MIN_DIRECTION` / `SOS_MAX_DISPERSION` / `SOS_MIN_AGREEMENT`).

### Probabilities (§10, §11)

Phase-1 transparent mechanisms (explicitly NOT calibration claims):
class probabilities tilt the FLAT band by the direction score;
expected gap = direction × causal mean |gap|; tail probabilities via Laplace
tails with scale = causal mean |gap|; `model_vs_implied` = expected gap ÷
straddle-implied move.

## 6. Backtest methodology (§13–§15)

Strictly chronological — no shuffling exists anywhere; expanding-window
walk-forward folds (`walk_forward_folds`) are provided for Phase-2 fitting.
NO_EDGE predictions are excluded from classification metrics but counted
(`n_no_edge`) — the model may abstain. Metrics: accuracy, balanced accuracy,
macro precision/recall/F1, confusion matrix, rank ROC-AUC, Brier score,
reliability buckets, MAE/RMSE/signed error, threshold hit rates, and the
**majority-class base rate reported beside accuracy** so >50% raw accuracy can
never be mistaken for edge. Segmentation by regime label and confidence
tercile. Same inputs ⇒ byte-identical results (determinism tested).

## 7. Look-ahead protections (tested)

1. Targets are computed and attached only in `attach_realized_target`, after
   features and predictions are stored; the session row carries NULL targets
   before that step.
2. Feature keys cannot contain target information (asserted).
3. Predictions are byte-identical before and after target attachment — the
   attachment is a pure downstream join.
4. Normalization and distribution histories query strictly-prior sessions
   only (`session_date < T`).
5. `rolling_zscore` refuses to normalize below `min_history` causal samples.

## 8. Data sources and legal constraints (§18)

No paid vendor and no restricted-exchange scraping is implemented. The CLI
ingests JSON snapshots produced from the project's authorized free/broker
paths (the existing Upstox-authorized market-data line). The research layer
consumes files, not live credentials.

## 9. Limitations

* Phase-1 gamma-flip is a proxy (largest-exposure strike).
* Probability outputs are structural, not calibrated; calibration is Phase-2
  work (spec §21) and must not be presented to users before then.
* Weights are fixed initial values pending walk-forward evidence; no weight
  optimization was run.
* Baseline "implied move" uses the 0.8·straddle approximation and a 1/252
  session-year convention.
* SOS-OPEN (§17 overnight information) is intentionally NOT implemented here.

## 10. Sample execution

```bash
cd options-dashboard-project/backend
python run_gap_research.py ingest   --session 2026-09-18 --cutoff 2026-09-18T15:30:00 \
    --underlying u.json --chain c.json
python run_gap_research.py features --session 2026-09-18
python run_gap_research.py predict  --session 2026-09-18
python run_gap_research.py target   --session 2026-09-18 --next-session 2026-09-22 \
    --next-open 25120.5 --next-open-ts 2026-09-22T09:15:00
python run_gap_research.py backtest --model sos
python run_gap_research.py status
```

### 10.1 Historical sample from the local candle store

`historical-sample` reconstructs sessions from the repository's own
authorized candle store (`nifty_candles` / `option_candles` /
`contract_specs` — the Phase 7.7/7.8/7.13 Upstox expired-instruments
backfill already present in this project; no new vendor):

```bash
cd options-dashboard-project/backend
DATABASE_URL="sqlite:////abs/path/research.db" \
python run_gap_research.py historical-sample \
    --store-url "sqlite:////abs/path/candle_store.db"
```

Extraction rules (enforced in `app/research/gap_historical.py`):

* a session needs index candles AND option candles on the same date;
* the previous **captured** session must exist within 12 calendar days
  (this store captures weekly expiry days — Tue→Thu over the years — so the
  natural change-feature cadence is ~7 days); longer gaps are skipped;
* cutoff = that date's last option candle (typically 15:27 IST);
* chain = NIFTY CE/PE of the front expiry, last candle at-or-before cutoff;
* `prior_close` = the session's OWN close (base of the predicted T+1 gap);
* passes run strictly in order: snapshots → features → predictions →
  realized targets → backtests (targets can never enter features).

### 10.2 Actual sample result (2024-10-10 → 2026-08-18, 93 sessions)

Reproducible, deterministic (byte-identical output across two runs;
SHA-256 `af5a526f43a946d5…` era of output). Data completeness of the
candle store: spot OHLC 100%, chain 100%, OI 100%, volume 100% —
**IV 0%, greeks 0%, bid/ask 0%, futures 0%, India VIX 0%** (the store has
no IV/greeks/bid-ask columns and no futures/VIX tables).

Target distribution (±0.1% flat band): FLAT 28 · GAP_UP 32 · GAP_DOWN 33.

| Model | scored / n | NO_EDGE | accuracy | balanced acc. | base rate |
|---|---|---|---|---|---|
| baseline | 91 / 93 | 2 | 0.352 | 0.333 | 0.352 |
| pos_style | 93 / 93 | 0 | 0.419 | 0.403 | 0.352 |
| sos | 42 / 93 | 51 | 0.357 | 0.357 | 0.352 |

SOS confidence buckets show NO monotone calibration
(bucket hit rates 0.286 / 0.429 / 0.357 at rising mean confidence).
AUC/Brier/MAE are null for this sample (no usable probability/magnitude
outputs without IV history).

**Honest reading:** no predictive value is demonstrated on this dataset.
POS-style's 0.419 accuracy is within noise of the 0.352 base rate at n=93,
and balanced accuracy is below 0.5. SOS correctly abstains (NO_EDGE) on
55% of sessions because Delta/Vega/IV/GEX/futures/VIX inputs are entirely
absent from the candle store — this is the designed missing-data behavior,
not a model failure. **This run validates the implementation pipeline, not
any trading edge.** A meaningful validation of POS-style/SOS requires a
snapshot source with IV/greeks (Phase 2 data requirement).

Tests: `python -m pytest tests/test_gap_research.py -q`
