# Point-in-Time Dataset Contract (Day 49)

## Purpose

Backtesting and historical research must evaluate a simulated decision at an explicit market-data timestamp without allowing observations from the future into the feature set.

The governing invariant is:

> For decision timestamp T, a point observation may be used when its observation timestamp is <= T. Interval-derived market bars may be used only after the interval has completed.

## Timestamp semantics

StrikeNova historical market-data candles are persisted as **naive IST (Asia/Kolkata)** timestamps. The public PIT interface therefore accepts either a naive datetime (interpreted as IST) or a timezone-aware timestamp, which is normalized through `app.utils.market_time.to_ist_naive()`. IV observations are normalized to the same canonical naive-IST representation when persisted.

### Feature time vs processing time

For raw market data, the source observation timestamp is the feature-availability boundary:

- `NiftyCandle.open_time` plus its interval-completion boundary
- `OptionCandle.open_time` plus its interval-completion boundary
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
