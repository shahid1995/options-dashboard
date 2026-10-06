# Point-in-Time Dataset Contract (Day 49)

## Purpose

Backtesting and historical research must evaluate a simulated decision at an explicit market-data timestamp without allowing observations from the future into the feature set.

The governing invariant is:

> For decision timestamp T, a point observation may be used when its observation timestamp is <= T. Interval-derived market bars may be used only after the interval has completed.

## Timestamp semantics

StrikeNova historical market-data candles are persisted as **naive IST (Asia/Kolkata)** timestamps. The public PIT interface therefore accepts either a naive datetime (interpreted as IST) or a timezone-aware timestamp, which is normalized through `app.utils.market_time.to_ist_naive()`. IV observations are normalized to the same canonical naive-IST representation when persisted. A Day 49 Alembic migration converts pre-Day-49 naive-UTC IV rows to that canonical IST representation before the new contract is used.

### Feature time vs processing time

For raw market data, the source observation timestamp is the feature-availability boundary:

- `NiftyCandle.open_time` plus its interval-completion boundary; the latest
  fully completed bar at or before T is selected
- `OptionCandle.open_time` plus its interval-completion boundary; the latest
  fully completed bar at or before T is selected
- `IVObservation.observed_at`

For derived historical datasets, the source market timestamp remains the PIT boundary:

- `OptionGreeks.open_time`
- `HistoricalGexSnapshot.open_time`

The later `calculated_at` field is provenance for when StrikeNova reconstructed the derived row; it is **not** treated as market-information availability. Otherwise a backtest would incorrectly become dependent on when an offline reconstruction job happened to run.

## Boundary rule

The comparison is inclusive:

`observation_timestamp <= decision_timestamp`

Therefore:

- observation exactly at T → allowed;
- observation after T → forbidden;
- a missing/invalid decision timestamp → rejected.

## Labels vs features

Forward outcomes are labels, not features. A research/backtest pipeline may deliberately query future market observations to calculate a label after selecting the PIT feature set, but those rows must never enter the PIT feature interface.

The PIT dataset interface contains feature reads only; it deliberately does not expose a "future outcome" method.

## Initial supported datasets

Day 49 establishes one server-side interface for:

- NIFTY candles;
- option candles;
- IV observations;
- reconstructed option Greeks;
- reconstructed historical GEX.

Additional datasets must adopt the same contract before they become backtest features.

## Security / integrity requirements

- - The cutoff is mandatory at the PIT interface.
- Callers do not supply SQL predicates themselves.
- A future observation inserted into the database must remain invisible to an earlier PIT query.
- Exact-boundary observations remain visible.
- Timezone normalization happens at the PIT boundary rather than being reimplemented by individual consumers.

## Non-goals

This contract does not change live/paper execution, mutate production data, or create a second payoff/risk implementation. The existing shared strategy/risk calculation layer remains authoritative for strategy mathematics.


## Strategy/risk/scenario integration seam

The backend now exposes `build_point_in_time_strategy_inputs()` as the decision-bounded input seam for a historical strategy evaluation. It deliberately stops at market inputs; it does not reimplement payoff or scenario mathematics.

The repository's existing authoritative calculation modules remain the shared engines:

- `frontend/lib/calculations/strategyCalculator.js`
- `frontend/lib/calculations/scenario.js`
- `frontend/lib/calculations/greekAnalytics.js`
- `frontend/lib/calculations/gexAnalytics.js`

A future backtest runner must consume the PIT strategy-input bundle and pass only that decision-bounded market state into those existing engines. Creating a second Python payoff/risk/scenario implementation would violate the existing architecture.

The current repository does not yet have a server-side backtest runner or server-side strategy evaluator, so Day 49 establishes the safe input seam without inventing a duplicate calculation engine.
