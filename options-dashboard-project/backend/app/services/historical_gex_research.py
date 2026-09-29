"""Historical GEX Research Engine — Phase 7.8E.

Builds a leakage-safe research dataset and performs statistical analysis
on Historical GEX to determine whether GEX characteristics have predictive
relationships with subsequent NIFTY price movement.

Architecture:
    historical_gex (observed, per-option)
        +
    option_greeks (gamma, delta, vega, IV)
        +
    option_candles (OI, volume)
        +
    nifty_candles (spot, forward returns)
        +
    contract_specs (strike, type, expiry, lot_size)
            |
            v
    GexResearchEngine
        |
        v
    ResearchDataset (per-timestamp, leakage-safe)
        |
        v
    Analysis modules:
        - Regime classification
        - Gamma flip analysis
        - Gamma wall behaviour
        - GEX change/acceleration
        - GEX + OI confirmation
        - Expiry-day research
        - Signal candidate discovery
        - Walk-forward validation
        - Statistical robustness

Sign convention (inherited from Phase 7.1):
    Call GEX = +raw_gex
    Put GEX  = -raw_gex
    Net GEX  = call_gex + put_gex

ANTI-LEAKAGE RULES:
    1. At timestamp T, only data available at or before T may be used.
    2. Forward returns are LABELS only, never used as features.
    3. NIFTY spot comes from option_greeks.spot (already aligned by Greeks engine).
    4. OI comes from option_candles.open_time (exact match).
    5. No future GEX, future OI, future volume, future spot is used as input.
"""

from __future__ import annotations

import logging
from bisect import insort
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_, distinct
from sqlalchemy.orm import Session

from app.services.point_in_time import PointInTimeDataset

from app.models import (
    HistoricalGexSnapshot,
    OptionGreeks,
    OptionCandle,
    NiftyCandle,
    ContractSpec,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CALC_VERSION = "h_gex_v1"
DEFAULT_INTERVAL = "3min"
GEX_FACTOR = 0.01

# Forward return intervals in 3-minute candles
FORWARD_RETURN_INTERVALS = {3: "3min", 6: "6min", 9: "9min", 15: "15min", 30: "30min", 60: "60min"}

# Regime classification thresholds (configurable, versioned)
REGIME_VERSION = "regime_v1"
STRONG_POSITIVE_PCTILE = 75  # Net GEX above 75th percentile = STRONG_POSITIVE
WEAK_POSITIVE_PCTILE = 25    # Net GEX between 25th-75th = WEAK_POSITIVE
STRONG_NEGATIVE_PCTILE = 25  # Net GEX below 25th percentile = STRONG_NEGATIVE
WEAK_NEGATIVE_PCTILE = 75    # Net GEX between 25th-75th = WEAK_NEGATIVE
FLIP_ZONE_PCTILE = 10        # Within 10% of zero = GAMMA_FLIP_ZONE

# Wall interaction thresholds
WALL_TOUCH_PCT = 0.5         # Spot within 0.5% of wall = TOUCH
WALL_BREAK_PCT = 0.3         # Spot moves 0.3% beyond wall = BREAK


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TimestampResearch:
    """Complete research data at one timestamp — leakage-safe."""
    timestamp: datetime
    spot: float

    # NIFTY returns (labels only)
    nifty_return_3m: Optional[float] = None
    nifty_return_6m: Optional[float] = None
    nifty_return_9m: Optional[float] = None
    nifty_return_15m: Optional[float] = None
    nifty_return_30m: Optional[float] = None
    nifty_return_60m: Optional[float] = None
    max_favorable_excursion: Optional[float] = None
    max_adverse_excursion: Optional[float] = None

    # GEX state
    total_net_gex: float = 0.0
    total_call_gex: float = 0.0
    total_put_gex: float = 0.0
    absolute_gex: float = 0.0
    positive_gex_concentration: float = 0.0
    negative_gex_concentration: float = 0.0
    pos_neg_ratio: Optional[float] = None

    # GEX derivatives
    gex_change: Optional[float] = None
    gex_change_pct: Optional[float] = None
    gex_acceleration: Optional[float] = None

    # Regime
    gamma_regime: str = "UNKNOWN"
    previous_regime: Optional[str] = None
    regime_transition: Optional[str] = None
    regime_duration: int = 0

    # Gamma flip
    gamma_flip: Optional[float] = None
    distance_to_flip: Optional[float] = None
    flip_confidence: Optional[float] = None
    flip_status: str = "UNKNOWN"

    # Walls
    strongest_positive_wall: Optional[float] = None
    strongest_negative_wall: Optional[float] = None
    pos_wall_distance: Optional[float] = None
    neg_wall_distance: Optional[float] = None
    pos_wall_gex: Optional[float] = None
    neg_wall_gex: Optional[float] = None

    # OI state
    total_oi: float = 0.0
    call_oi: float = 0.0
    put_oi: float = 0.0
    oi_change: Optional[float] = None
    call_oi_change: Optional[float] = None
    put_oi_change: Optional[float] = None
    oi_call_put_ratio: Optional[float] = None

    # Volume state
    total_volume: float = 0.0
    call_volume: float = 0.0
    put_volume: float = 0.0

    # Metadata
    instrument_count: int = 0
    strike_count: int = 0
    expiry_count: int = 0
    is_expiry_day: bool = False


@dataclass
class SignalCandidate:
    """A candidate trading signal with statistical evidence."""
    signal_name: str
    signal_version: str = "v1"
    entry_condition: str = ""
    confirmation_condition: str = ""
    invalidation_condition: str = ""
    expected_direction: str = ""  # "LONG", "SHORT", "NEUTRAL"
    preferred_regime: str = ""
    preferred_time_window: str = ""

    # Statistical evidence
    sample_size: int = 0
    win_rate: float = 0.0
    mean_return: float = 0.0
    median_return: float = 0.0
    std_return: float = 0.0
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    expected_value: float = 0.0
    confidence_level: str = ""  # "HIGH", "MEDIUM", "LOW", "INSUFFICIENT_SAMPLE"
    known_failure_conditions: str = ""

    # Walk-forward
    in_sample_win_rate: Optional[float] = None
    out_of_sample_win_rate: Optional[float] = None
    walk_forward_stable: Optional[bool] = None


@dataclass
class WalkForwardResult:
    """Walk-forward validation result for a signal."""
    period_name: str
    start_date: datetime
    end_date: datetime
    sample_size: int
    win_rate: float
    mean_return: float
    median_return: float
    expected_value: float


@dataclass
class ExpiryDayAnalysis:
    """Expiry-day vs non-expiry-day comparison."""
    metric: str
    expiry_day_value: Optional[float] = None
    non_expiry_day_value: Optional[float] = None
    expiry_day_count: int = 0
    non_expiry_day_count: int = 0
    difference_pct: Optional[float] = None


# ---------------------------------------------------------------------------
# GexResearchEngine
# ---------------------------------------------------------------------------

class GexResearchEngine:
    """Historical GEX Research Engine.

    Performs leakage-safe research on the historical GEX dataset.
    Does NOT modify the production database.
    """

    def __init__(self, db: Session, calc_version: str = DEFAULT_CALC_VERSION):
        self.db = db
        self.calc_version = calc_version
        self.pit = PointInTimeDataset(db)

    def _decision_timestamp_for_observation(self, timestamp: datetime) -> datetime:
        """Return the decision time at the end of a three-minute observation bar."""
        return timestamp + timedelta(minutes=3)


    # ==================================================================
    # Phase 2: Research Dataset Builder
    # ==================================================================

    def build_research_dataset(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        max_timestamps: Optional[int] = None,
    ) -> list[TimestampResearch]:
        """Build a complete leakage-safe research dataset.

        Research timestamps identify the opening time of a three-minute
        observation bar. Feature availability is enforced at that bar's close
        (timestamp + 3 minutes); forward outcomes remain labels only.

        For every timestamp, assembles:
        - Market state (spot, returns as labels)
        - GEX state (aggregated from historical_gex)
        - GEX derivatives (change, acceleration)
        - Regime classification
        - Gamma flip level
        - Gamma walls
        - OI state (from option_candles)
        - Volume state

        ANTI-LEAKAGE: Forward returns are computed ONLY from candles
        with open_time > current timestamp. They are never used as features.
        """
        # Get all unique timestamps
        timestamps = self._get_timestamps(start, end)
        if max_timestamps:
            timestamps = timestamps[:max_timestamps]

        if not timestamps:
            return []

        logger.info(f"Building research dataset for {len(timestamps)} timestamps")

        # Pre-fetch NIFTY candles for forward returns (only future candles used)
        nifty_end = timestamps[-1] + timedelta(hours=12)
        nifty_candles = self._fetch_nifty_candles(timestamps[0], nifty_end)

        # Pre-fetch OI data for all timestamps
        oi_data = self._fetch_oi_data(timestamps)

        # Build timestamp-level GEX aggregations
        gex_series = self._build_gex_series(timestamps)

        # Compute derivatives
        changes = self._compute_changes(gex_series)
        regimes = self._compute_regimes(gex_series)
        flips = self._compute_flips(timestamps)
        walls = self._compute_walls(timestamps)

        # Assemble research dataset
        dataset = []
        for i, ts in enumerate(timestamps):
            gex = gex_series.get(ts)
            if not gex:
                continue

            # Forward returns (LABELS only — not features)
            fr = self._compute_forward_returns(ts, gex["spot"], nifty_candles)

            # OI state (from option_candles at this timestamp)
            oi = oi_data.get(ts, {})

            # Previous GEX for derivatives
            prev_ts = timestamps[i - 1] if i > 0 else None
            prev_gex = gex_series.get(prev_ts) if prev_ts else None
            prev_change = changes.get(prev_ts) if prev_ts else None

            # GEX change
            gex_change = None
            gex_change_pct = None
            if prev_gex:
                gex_change = gex["net_gex"] - prev_gex["net_gex"]
                if abs(prev_gex["net_gex"]) > 1e-10:
                    gex_change_pct = gex_change / abs(prev_gex["net_gex"]) * 100

            # GEX acceleration
            gex_acceleration = None
            if gex_change is not None and prev_change is not None:
                gex_acceleration = gex_change - prev_change

            # Regime
            regime_data = regimes.get(ts, {})

            # Flip
            flip_data = flips.get(ts, {})

            # Walls
            wall_data = walls.get(ts, {})

            # Expiry day check
            is_expiry = self._is_expiry_day(ts)

            # Assemble
            research = TimestampResearch(
                timestamp=ts,
                spot=gex["spot"],
                # Labels
                nifty_return_3m=fr.get(3),
                nifty_return_6m=fr.get(6),
                nifty_return_9m=fr.get(9),
                nifty_return_15m=fr.get(15),
                nifty_return_30m=fr.get(30),
                nifty_return_60m=fr.get(60),
                max_favorable_excursion=fr.get("mfe"),
                max_adverse_excursion=fr.get("mae"),
                # GEX
                total_net_gex=gex["net_gex"],
                total_call_gex=gex["call_gex"],
                total_put_gex=gex["put_gex"],
                absolute_gex=gex["absolute_gex"],
                positive_gex_concentration=gex["pos_concentration"],
                negative_gex_concentration=gex["neg_concentration"],
                pos_neg_ratio=gex["pos_neg_ratio"],
                # Derivatives
                gex_change=gex_change,
                gex_change_pct=gex_change_pct,
                gex_acceleration=gex_acceleration,
                # Regime
                gamma_regime=regime_data.get("regime", "UNKNOWN"),
                previous_regime=regime_data.get("previous_regime"),
                regime_transition=regime_data.get("transition"),
                regime_duration=regime_data.get("duration", 0),
                # Flip
                gamma_flip=flip_data.get("flip_strike"),
                distance_to_flip=flip_data.get("distance_to_flip"),
                flip_confidence=flip_data.get("confidence"),
                flip_status=flip_data.get("status", "UNKNOWN"),
                # Walls
                strongest_positive_wall=wall_data.get("pos_wall_strike"),
                strongest_negative_wall=wall_data.get("neg_wall_strike"),
                pos_wall_distance=wall_data.get("pos_wall_distance"),
                neg_wall_distance=wall_data.get("neg_wall_distance"),
                pos_wall_gex=wall_data.get("pos_wall_gex"),
                neg_wall_gex=wall_data.get("neg_wall_gex"),
                # OI
                total_oi=oi.get("total_oi", 0.0),
                call_oi=oi.get("call_oi", 0.0),
                put_oi=oi.get("put_oi", 0.0),
                oi_change=oi.get("oi_change"),
                call_oi_change=oi.get("call_oi_change"),
                put_oi_change=oi.get("put_oi_change"),
                oi_call_put_ratio=oi.get("call_put_ratio"),
                # Volume
                total_volume=oi.get("total_volume", 0.0),
                call_volume=oi.get("call_volume", 0.0),
                put_volume=oi.get("put_volume", 0.0),
                # Metadata
                instrument_count=gex["instrument_count"],
                strike_count=gex["strike_count"],
                expiry_count=gex.get("expiry_count", 0),
                is_expiry_day=is_expiry,
            )
            dataset.append(research)

        logger.info(f"Research dataset built: {len(dataset)} timestamps")
        return dataset

    # ==================================================================
    # Internal: Data fetching
    # ==================================================================

    def _get_timestamps(
        self, start: Optional[datetime], end: Optional[datetime]
    ) -> list[datetime]:
        """Get sorted unique timestamps from historical_gex."""
        stmt = (
            select(HistoricalGexSnapshot.open_time)
            .where(HistoricalGexSnapshot.calc_version == self.calc_version)
            .where(HistoricalGexSnapshot.status == "SUCCESS")
            .distinct()
            .order_by(HistoricalGexSnapshot.open_time)
        )
        if start:
            stmt = stmt.where(HistoricalGexSnapshot.open_time >= start)
        if end:
            stmt = stmt.where(HistoricalGexSnapshot.open_time <= end)
        return [r[0] for r in self.db.execute(stmt).all()]

    def _fetch_nifty_candles(
        self, start: datetime, end: datetime
    ) -> list:
        """Fetch NIFTY candles for forward return computation."""
        return self.db.execute(
            select(NiftyCandle)
            .where(
                NiftyCandle.open_time >= start,
                NiftyCandle.open_time <= end,
                NiftyCandle.interval == DEFAULT_INTERVAL,
            )
            .order_by(NiftyCandle.open_time)
        ).scalars().all()

    def _fetch_oi_data(self, timestamps: list[datetime]) -> dict:
        """Fetch OI and volume feature state through the PIT dataset boundary.

        Bulk PIT reads return decision-major selections: element i belongs to
        timestamps[i]. Each selected row keeps the SOURCE open_time it
        resolved to, so a fallback bar is attributed to its original
        observation time — it is never relabeled as a fresh observation at the
        requesting decision time, never double-counted across decisions, and
        never dropped merely because it is stale. Forward NIFTY candles remain
        separate because they are labels, not features.
        """
        if not timestamps:
            return {}

        decision_timestamps = [
            self._decision_timestamp_for_observation(ts) for ts in timestamps
        ]

        greek_selections = self.pit.option_greeks_selections_at_many(
            decision_timestamps,
            interval=DEFAULT_INTERVAL,
            calc_version="greeks_v3",
        )
        if not greek_selections:
            return {}

        # Decision-major pairing, keyed back by the research observation
        # timestamp (the API contract of this method): greeks_by_decision[ts]
        # holds exactly the instruments PIT selected FOR that observation's
        # decision time, never rows belonging to another decision.
        greeks_by_decision: dict[datetime, dict] = {}
        for ts, (_, selection) in zip(timestamps, greek_selections):
            greeks_by_decision[ts] = dict(selection)

        instruments = sorted({
            instrument_key
            for selection in greeks_by_decision.values()
            for instrument_key in selection
        })
        # Decision-major selection: each decision's own {instrument: candle}
        # selection is summed exactly once for that decision. A fallback row
        # keeps its source open_time (never relabeled to the decision time) and
        # its value carries forward as the latest available observation — so a
        # stale bar can neither be duplicated into a later timestamp nor
        # disappear from one.
        selection_by_decision: dict[datetime, dict] = defaultdict(dict)
        for ts, (_, selection) in zip(
            timestamps,
            self.pit.option_candles_selections_at_many(
                decision_timestamps,
                instrument_keys=instruments,
                interval=DEFAULT_INTERVAL,
            ),
        ):
            selection_by_decision[ts] = dict(selection)

        ik_to_type = {
            instrument_key: row.option_type
            for selection in greeks_by_decision.values()
            for instrument_key, row in selection.items()
        }

        result: dict[datetime, dict] = {}
        for ts in sorted(greeks_by_decision):
            allowed_keys = set(greeks_by_decision[ts])
            total_oi = 0.0
            call_oi = 0.0
            put_oi = 0.0
            total_vol = 0.0
            call_vol = 0.0
            put_vol = 0.0

            decision_selection = {
                instrument_key: row
                for instrument_key, row in selection_by_decision.get(ts, {}).items()
                if instrument_key in allowed_keys
            }
            if not decision_selection:
                # No OI observation was visible to this decision: omit the
                # timestamp rather than invent a zero observation (matches the
                # pre-PIT contract for timestamps without option candles).
                continue
            for row in decision_selection.values():
                oi_val = float(row.open_interest or 0)
                vol_val = float(row.volume or 0)
                opt_type = ik_to_type.get(row.instrument_key, "")
                total_oi += oi_val
                total_vol += vol_val
                if opt_type == "CE":
                    call_oi += oi_val
                    call_vol += vol_val
                elif opt_type == "PE":
                    put_oi += oi_val
                    put_vol += vol_val

            result[ts] = {
                "total_oi": total_oi,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "call_put_ratio": call_oi / put_oi if put_oi > 0 else None,
                "total_volume": total_vol,
                "call_volume": call_vol,
                "put_volume": put_vol,
                # Explicit source-time association: the latest observation time
                # this decision's OI selection actually resolved to. A fallback
                # bar keeps its original source time here, proving the value
                # was carried forward rather than freshly observed.
                "oi_source_open_time": (
                    max(row.open_time for row in decision_selection.values())
                    if decision_selection else None
                ),
                "oi_change": None,
                "call_oi_change": None,
                "put_oi_change": None,
            }

        sorted_ts = sorted(result)
        for i in range(1, len(sorted_ts)):
            curr = result[sorted_ts[i]]
            prev = result[sorted_ts[i - 1]]
            curr["oi_change"] = curr["total_oi"] - prev["total_oi"]
            curr["call_oi_change"] = curr["call_oi"] - prev["call_oi"]
            curr["put_oi_change"] = curr["put_oi"] - prev["put_oi"]

        return result

    # ==================================================================
    # Internal: GEX aggregation
    # ==================================================================

    def _build_gex_series(self, timestamps: list[datetime]) -> dict:
        """Build per-timestamp GEX aggregation from historical_gex."""
        result = {}

        for ts in timestamps:
            rows = self.pit.historical_gex_at(
                self._decision_timestamp_for_observation(ts),
                interval=DEFAULT_INTERVAL,
                calc_version=self.calc_version,
            )

            rows = [row for row in rows if row.open_time == ts]
            if not rows:
                continue

            call_gex = sum(r.signed_gex for r in rows if r.option_type == "CE")
            put_gex = sum(r.signed_gex for r in rows if r.option_type == "PE")
            net_gex = call_gex + put_gex
            abs_gex = sum(abs(r.signed_gex) for r in rows)
            spot = rows[0].spot

            pos_gex = sum(r.signed_gex for r in rows if r.signed_gex > 0)
            neg_gex = sum(r.signed_gex for r in rows if r.signed_gex < 0)

            strikes = set(r.strike for r in rows)
            expiries = set(r.expiry for r in rows)

            result[ts] = {
                "spot": spot,
                "call_gex": call_gex,
                "put_gex": put_gex,
                "net_gex": net_gex,
                "absolute_gex": abs_gex,
                "pos_concentration": pos_gex / abs_gex if abs_gex > 0 else 0.0,
                "neg_concentration": abs(neg_gex) / abs_gex if abs_gex > 0 else 0.0,
                "pos_neg_ratio": abs(pos_gex / neg_gex) if abs(neg_gex) > 1e-10 else None,
                "instrument_count": len(rows),
                "strike_count": len(strikes),
                "expiry_count": len(expiries),
            }

        return result

    def _compute_changes(self, gex_series: dict) -> dict:
        """Compute GEX changes (first derivative)."""
        sorted_ts = sorted(gex_series.keys())
        changes = {}
        for i, ts in enumerate(sorted_ts):
            if i == 0:
                changes[ts] = None
            else:
                curr = gex_series[ts]["net_gex"]
                prev = gex_series[sorted_ts[i - 1]]["net_gex"]
                changes[ts] = curr - prev
        return changes

    # ==================================================================
    # Internal: Regime classification
    # ==================================================================

    def _compute_regimes(self, gex_series: dict) -> dict:
        """Classify gamma regime with enhanced granularity.

        Regimes:
            STRONG_POSITIVE  — net GEX above 75th percentile
            WEAK_POSITIVE    — net GEX between 25th-75th percentile (positive)
            NEUTRAL          — net GEX ≈ 0
            WEAK_NEGATIVE    — net GEX between 25th-75th percentile (negative)
            STRONG_NEGATIVE  — net GEX below 25th percentile
            GAMMA_FLIP_ZONE  — net GEX very close to zero
        """
        sorted_ts = sorted(gex_series.keys())
        if not sorted_ts:
            return {}

        result = {}
        previous_regime = None
        regime_start_idx = 0
        historical_values: list[float] = []

        for i, ts in enumerate(sorted_ts):
            net_gex = gex_series[ts]["net_gex"]

            # PIT invariant: regime thresholds may use only observations known
            # at this timestamp. Never compute thresholds from future rows.
            insort(historical_values, net_gex)
            ordered_history = historical_values
            n = len(ordered_history)

            def percentile(pct):
                idx = int(pct / 100 * (n - 1))
                return ordered_history[max(0, min(idx, n - 1))]

            p25 = percentile(STRONG_NEGATIVE_PCTILE)
            p75 = percentile(STRONG_POSITIVE_PCTILE)
            median_abs = percentile(50)
            flip_zone_threshold = abs(median_abs) * FLIP_ZONE_PCTILE / 100

            # Classify
            if abs(net_gex) <= flip_zone_threshold:
                regime = "GAMMA_FLIP_ZONE"
            elif net_gex > p75:
                regime = "STRONG_POSITIVE"
            elif net_gex > 0:
                regime = "WEAK_POSITIVE"
            elif net_gex < p25:
                regime = "STRONG_NEGATIVE"
            elif net_gex < 0:
                regime = "WEAK_NEGATIVE"
            else:
                regime = "NEUTRAL"

            # Simplified for compatibility with existing analytics
            if net_gex > 0:
                simple_regime = "POSITIVE_GAMMA"
            elif net_gex < 0:
                simple_regime = "NEGATIVE_GAMMA"
            else:
                simple_regime = "NEUTRAL"

            transition = None
            if previous_regime and previous_regime != simple_regime:
                transition = f"{previous_regime}→{simple_regime}"
                regime_start_idx = i

            result[ts] = {
                "regime": simple_regime,
                "detailed_regime": regime,
                "previous_regime": previous_regime,
                "transition": transition,
                "duration": i - regime_start_idx + 1,
            }

            previous_regime = simple_regime

        return result

    # ==================================================================
    # Internal: Gamma flip detection
    # ==================================================================

    def _compute_flips(self, timestamps: list[datetime]) -> dict:
        """Compute gamma flip for each timestamp."""
        result = {}

        for ts in timestamps:
            flip = self._detect_gamma_flip_at_timestamp(ts)
            result[ts] = flip

        return result

    def _detect_gamma_flip_at_timestamp(self, ts: datetime) -> dict:
        """Detect gamma flip at a single timestamp using strike-level GEX."""
        # Get strike-level GEX
        rows = self.pit.historical_gex_at(
            self._decision_timestamp_for_observation(ts),
            interval=DEFAULT_INTERVAL,
            calc_version=self.calc_version,
        )

        if len(rows) < 2:
            return {"status": "INSUFFICIENT_DATA"}

        spot = rows[0].spot

        # Aggregate by strike
        strike_gex: dict[float, float] = defaultdict(float)
        for r in rows:
            strike_gex[r.strike] += r.signed_gex

        # Sort by strike
        sorted_strikes = sorted(strike_gex.items())

        # Find sign changes
        sign_changes = []
        for i in range(len(sorted_strikes) - 1):
            s1_strike, s1_gex = sorted_strikes[i]
            s2_strike, s2_gex = sorted_strikes[i + 1]

            if s1_gex * s2_gex < 0:  # Different signs
                # Linear interpolation
                if s2_gex - s1_gex != 0:
                    flip_strike = s1_strike - s1_gex * (s2_strike - s1_strike) / (s2_gex - s1_gex)
                else:
                    flip_strike = (s1_strike + s2_strike) / 2.0

                min_abs = min(abs(s1_gex), abs(s2_gex))
                max_abs = max(abs(s1_gex), abs(s2_gex))
                confidence = 1.0 - (min_abs / max_abs) if max_abs > 0 else 0.5

                sign_changes.append({
                    "flip_strike": flip_strike,
                    "confidence": confidence,
                    "distance_to_flip": flip_strike - spot,
                })

        if not sign_changes:
            return {"status": "NO_CROSSING", "spot": spot}

        # Select flip closest to spot
        best = min(sign_changes, key=lambda sc: abs(sc["flip_strike"] - spot))

        return {
            "status": "ESTIMATED",
            "flip_strike": best["flip_strike"],
            "confidence": best["confidence"],
            "distance_to_flip": best["distance_to_flip"],
            "spot": spot,
            "num_sign_changes": len(sign_changes),
        }

    # ==================================================================
    # Internal: Gamma walls
    # ==================================================================

    def _compute_walls(self, timestamps: list[datetime]) -> dict:
        """Compute gamma walls for each timestamp."""
        result = {}

        for ts in timestamps:
            wall = self._detect_walls_at_timestamp(ts)
            result[ts] = wall

        return result

    def _detect_walls_at_timestamp(self, ts: datetime) -> dict:
        """Detect gamma walls at a single timestamp."""
        rows = self.pit.historical_gex_at(
            self._decision_timestamp_for_observation(ts),
            interval=DEFAULT_INTERVAL,
            calc_version=self.calc_version,
        )

        if not rows:
            return {}

        spot = rows[0].spot

        # Aggregate by strike
        strike_gex: dict[float, float] = defaultdict(float)
        for r in rows:
            strike_gex[r.strike] += r.signed_gex

        # Separate positive and negative
        positive = [(s, g) for s, g in strike_gex.items() if g > 0]
        negative = [(s, g) for s, g in strike_gex.items() if g < 0]

        positive.sort(key=lambda x: x[1], reverse=True)
        negative.sort(key=lambda x: x[1])  # Most negative first

        result = {}

        if positive:
            pos_strike, pos_gex = positive[0]
            result["pos_wall_strike"] = pos_strike
            result["pos_wall_distance"] = pos_strike - spot
            result["pos_wall_gex"] = pos_gex

        if negative:
            neg_strike, neg_gex = negative[0]
            result["neg_wall_strike"] = neg_strike
            result["neg_wall_distance"] = neg_strike - spot
            result["neg_wall_gex"] = neg_gex

        return result

    # ==================================================================
    # Internal: Forward returns
    # ==================================================================

    def _compute_forward_returns(
        self, ts: datetime, spot: float, nifty_candles: list
    ) -> dict:
        """Compute forward returns from a timestamp.

        ANTI-LEAKAGE: Only candles with open_time > ts are used.
        """
        future = [c for c in nifty_candles if c.open_time > ts]
        future.sort(key=lambda c: c.open_time)

        returns = {}
        mfe = None
        mae = None

        for interval in FORWARD_RETURN_INTERVALS:
            if interval <= len(future):
                future_spot = future[interval - 1].close
                ret = (future_spot - spot) / spot * 100
                returns[interval] = round(ret, 4)

                # Max favorable/adverse excursion
                prices = [spot] + [c.close for c in future[:interval]]
                max_up = max(prices)
                max_down = min(prices)
                mf = (max_up - spot) / spot * 100
                ma = (min(prices) - spot) / spot * 100

                if mfe is None or mf > mfe:
                    mfe = round(mf, 4)
                if mae is None or ma < mae:
                    mae = round(ma, 4)

        if mfe is not None:
            returns["mfe"] = mfe
        if mae is not None:
            returns["mae"] = mae

        return returns

    def _is_expiry_day(self, ts: datetime) -> bool:
        """Check if a timestamp falls on an expiry day."""
        # NIFTY options expire on Thursdays
        return ts.weekday() == 3  # Thursday

    # ==================================================================
    # Phase 4: Price Reaction Around Gamma Flip
    # ==================================================================

    def analyze_flip_reactions(
        self, dataset: list[TimestampResearch]
    ) -> dict:
        """Analyze NIFTY behavior around gamma flip.

        Measures:
        - Probability of continuation vs reversal
        - Average forward return when above/below flip
        - Behavior when spot crosses flip
        """
        above_flip = []
        below_flip = []
        near_flip = []
        crossing_up = []
        crossing_down = []

        for i, point in enumerate(dataset):
            if point.gamma_flip is None or point.distance_to_flip is None:
                continue

            dist = point.distance_to_flip
            ret_15m = point.nifty_return_15m
            if ret_15m is None:
                continue

            # Above/below/near flip
            if abs(dist) < 20:  # Within 20 points
                near_flip.append(ret_15m)
            elif dist > 0:
                above_flip.append(ret_15m)
            else:
                below_flip.append(ret_15m)

            # Crossing detection
            if i > 0:
                prev = dataset[i - 1]
                if prev.distance_to_flip is not None:
                    if prev.distance_to_flip <= 0 and dist > 0:
                        crossing_up.append(ret_15m)
                    elif prev.distance_to_flip >= 0 and dist < 0:
                        crossing_down.append(ret_15m)

        stats = self._compute_stats_grouped({
            "above_flip_15m": above_flip,
            "below_flip_15m": below_flip,
            "near_flip_15m": near_flip,
            "crossing_up_15m": crossing_up,
            "crossing_down_15m": crossing_down,
        })

        return {
            "above_flip": stats.get("above_flip_15m"),
            "below_flip": stats.get("below_flip_15m"),
            "near_flip": stats.get("near_flip_15m"),
            "crossing_up": stats.get("crossing_up_15m"),
            "crossing_down": stats.get("crossing_down_15m"),
            "total_above": len(above_flip),
            "total_below": len(below_flip),
            "total_near": len(near_flip),
            "total_crossing_up": len(crossing_up),
            "total_crossing_down": len(crossing_down),
        }

    # ==================================================================
    # Phase 5: Gamma Wall Behaviour
    # ==================================================================

    def analyze_wall_behaviour(
        self, dataset: list[TimestampResearch]
    ) -> dict:
        """Analyze whether gamma walls act as support/resistance/rejection."""
        pos_wall_interactions = []
        neg_wall_interactions = []

        for i, point in enumerate(dataset):
            if point.strongest_positive_wall is None:
                continue

            wall_strike = point.strongest_positive_wall
            spot = point.spot
            wall_dist_pct = abs(wall_strike - spot) / spot * 100 if spot > 0 else 999

            # Check for interaction
            if wall_dist_pct < WALL_TOUCH_PCT:
                # Touch detected — classify interaction
                interaction = self._classify_wall_interaction(
                    dataset, i, wall_strike, "POSITIVE"
                )
                if interaction:
                    pos_wall_interactions.append(interaction)

            if point.strongest_negative_wall:
                neg_wall_strike = point.strongest_negative_wall
                neg_dist_pct = abs(neg_wall_strike - spot) / spot * 100 if spot > 0 else 999

                if neg_dist_pct < WALL_TOUCH_PCT:
                    interaction = self._classify_wall_interaction(
                        dataset, i, neg_wall_strike, "NEGATIVE"
                    )
                    if interaction:
                        neg_wall_interactions.append(interaction)

        return {
            "positive_wall": self._summarize_wall_interactions(pos_wall_interactions),
            "negative_wall": self._summarize_wall_interactions(neg_wall_interactions),
            "pos_interactions_count": len(pos_wall_interactions),
            "neg_interactions_count": len(neg_wall_interactions),
        }

    def _classify_wall_interaction(
        self, dataset: list, idx: int, wall_strike: float, wall_type: str
    ) -> Optional[dict]:
        """Classify a wall interaction as TOUCH/REJECTION/BREAK."""
        if idx >= len(dataset) - 3:
            return None

        point = dataset[idx]
        spot = point.spot

        # Look forward to determine outcome
        forward_spots = []
        for j in range(idx + 1, min(idx + 11, len(dataset))):
            forward_spots.append(dataset[j].spot)

        if not forward_spots:
            return None

        # Classification
        direction_from_wall = 1 if wall_type == "POSITIVE" else -1
        moved_away = False
        moved_toward = False

        for fs in forward_spots:
            if direction_from_wall > 0:  # Positive wall
                if fs > wall_strike * (1 + WALL_BREAK_PCT / 100):
                    moved_away = True
                    break
                elif fs < wall_strike * (1 - WALL_TOUCH_PCT / 100):
                    moved_toward = True
                    break
            else:  # Negative wall
                if fs < wall_strike * (1 - WALL_BREAK_PCT / 100):
                    moved_away = True
                    break
                elif fs > wall_strike * (1 + WALL_TOUCH_PCT / 100):
                    moved_toward = True
                    break

        if moved_away:
            interaction_type = "BREAK"
        elif moved_toward:
            interaction_type = "REJECTION"
        else:
            interaction_type = "TOUCH"

        # Forward return after interaction
        ret_15m = point.nifty_return_15m

        return {
            "type": interaction_type,
            "wall_type": wall_type,
            "wall_strike": wall_strike,
            "spot": spot,
            "distance_pct": abs(wall_strike - spot) / spot * 100 if spot > 0 else 0,
            "forward_return_15m": ret_15m,
        }

    def _summarize_wall_interactions(self, interactions: list) -> dict:
        """Summarize wall interaction statistics."""
        if not interactions:
            return {"count": 0}

        by_type = defaultdict(list)
        for ix in interactions:
            by_type[ix["type"]].append(ix.get("forward_return_15m"))

        summary = {}
        for ix_type, returns in by_type.items():
            valid_returns = [r for r in returns if r is not None]
            summary[ix_type] = self._compute_stats(valid_returns) if valid_returns else {"count": 0}

        return summary

    # ==================================================================
    # Phase 6: GEX Change/Acceleration Research
    # ==================================================================

    def analyze_gex_change_patterns(
        self, dataset: list[TimestampResearch]
    ) -> dict:
        """Analyze whether GEX change/acceleration has predictive info."""
        patterns = {
            "neg_gex_becoming_more_neg": [],  # Setup A
            "neg_gex_becoming_less_neg": [],   # Setup B
            "pos_gex_becoming_more_pos": [],   # Setup C
            "pos_gex_becoming_less_pos": [],   # Setup D
            "gex_accelerating_pos": [],
            "gex_accelerating_neg": [],
        }

        for point in dataset:
            if point.gex_change is None or point.gex_acceleration is None:
                continue
            if point.nifty_return_15m is None:
                continue

            net_gex = point.total_net_gex
            gex_change = point.gex_change
            accel = point.gex_acceleration
            ret = point.nifty_return_15m

            # Setup A: Negative GEX + becoming more negative
            if net_gex < 0 and gex_change < 0:
                patterns["neg_gex_becoming_more_neg"].append(ret)

            # Setup B: Negative GEX + becoming less negative
            if net_gex < 0 and gex_change > 0:
                patterns["neg_gex_becoming_less_neg"].append(ret)

            # Setup C: Positive GEX + becoming more positive
            if net_gex > 0 and gex_change > 0:
                patterns["pos_gex_becoming_more_pos"].append(ret)

            # Setup D: Positive GEX + becoming less positive
            if net_gex > 0 and gex_change < 0:
                patterns["pos_gex_becoming_less_pos"].append(ret)

            # Acceleration
            if accel > 0:
                patterns["gex_accelerating_pos"].append(ret)
            elif accel < 0:
                patterns["gex_accelerating_neg"].append(ret)

        return {k: self._compute_stats(v) for k, v in patterns.items()}

    # ==================================================================
    # Phase 7: GEX + Price Momentum
    # ==================================================================

    def analyze_gex_momentum_combinations(
        self, dataset: list[TimestampResearch]
    ) -> dict:
        """Compare predictive value: price-only vs GEX-only vs combined."""
        results = {
            "price_momentum_up": [],      # Spot going up
            "price_momentum_down": [],     # Spot going down
            "gex_positive": [],            # Net GEX > 0
            "gex_negative": [],            # Net GEX < 0
            "price_up_gex_positive": [],   # Combined
            "price_up_gex_negative": [],
            "price_down_gex_positive": [],
            "price_down_gex_negative": [],
        }

        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]

            if point.nifty_return_15m is None:
                continue

            ret = point.nifty_return_15m
            price_momentum = point.spot - prev.spot

            # Price momentum
            if price_momentum > 0:
                results["price_momentum_up"].append(ret)
            elif price_momentum < 0:
                results["price_momentum_down"].append(ret)

            # GEX direction
            if point.total_net_gex > 0:
                results["gex_positive"].append(ret)
            elif point.total_net_gex < 0:
                results["gex_negative"].append(ret)

            # Combined
            if price_momentum > 0 and point.total_net_gex > 0:
                results["price_up_gex_positive"].append(ret)
            elif price_momentum > 0 and point.total_net_gex < 0:
                results["price_up_gex_negative"].append(ret)
            elif price_momentum < 0 and point.total_net_gex > 0:
                results["price_down_gex_positive"].append(ret)
            elif price_momentum < 0 and point.total_net_gex < 0:
                results["price_down_gex_negative"].append(ret)

        return {k: self._compute_stats(v) for k, v in results.items()}

    # ==================================================================
    # Phase 8: GEX + OI Confirmation
    # ==================================================================

    def analyze_gex_oi_combinations(
        self, dataset: list[TimestampResearch]
    ) -> dict:
        """Research GEX + OI + price combinations."""
        patterns = {
            "long_buildup": [],     # OI up + price up
            "short_buildup": [],    # OI up + price down
            "short_covering": [],   # OI down + price up
            "long_unwinding": [],   # OI down + price down
        }

        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]

            if point.nifty_return_15m is None or point.oi_change is None:
                continue

            ret = point.nifty_return_15m
            price_change = point.spot - prev.spot
            oi_change = point.oi_change

            if oi_change > 0 and price_change > 0:
                patterns["long_buildup"].append(ret)
            elif oi_change > 0 and price_change < 0:
                patterns["short_buildup"].append(ret)
            elif oi_change < 0 and price_change > 0:
                patterns["short_covering"].append(ret)
            elif oi_change < 0 and price_change < 0:
                patterns["long_unwinding"].append(ret)

        # Add GEX-confirmed versions
        gex_confirmed = {
            "long_buildup_gex_pos": [],
            "long_buildup_gex_neg": [],
            "short_buildup_gex_pos": [],
            "short_buildup_gex_neg": [],
        }

        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]

            if point.nifty_return_15m is None or point.oi_change is None:
                continue

            ret = point.nifty_return_15m
            price_change = point.spot - prev.spot
            oi_change = point.oi_change

            if oi_change > 0 and price_change > 0:
                if point.total_net_gex > 0:
                    gex_confirmed["long_buildup_gex_pos"].append(ret)
                else:
                    gex_confirmed["long_buildup_gex_neg"].append(ret)
            elif oi_change > 0 and price_change < 0:
                if point.total_net_gex > 0:
                    gex_confirmed["short_buildup_gex_pos"].append(ret)
                else:
                    gex_confirmed["short_buildup_gex_neg"].append(ret)

        all_patterns = {**patterns, **gex_confirmed}
        return {k: self._compute_stats(v) for k, v in all_patterns.items()}

    # ==================================================================
    # Phase 9: Regime-Specific Performance
    # ==================================================================

    def analyze_regime_performance(
        self, dataset: list[TimestampResearch]
    ) -> dict:
        """Calculate statistics separately for each gamma regime."""
        regime_groups = defaultdict(lambda: defaultdict(list))
        period_groups = {"morning": defaultdict(list), "afternoon": defaultdict(list)}

        for point in dataset:
            regime = point.gamma_regime
            ret = point.nifty_return_15m
            if ret is None:
                continue

            regime_groups[regime]["15m_return"].append(ret)

            # Also group by other return intervals
            for attr in ["nifty_return_3m", "nifty_return_6m", "nifty_return_30m", "nifty_return_60m"]:
                val = getattr(point, attr)
                if val is not None:
                    regime_groups[regime][attr].append(val)

            # Morning vs afternoon
            if point.timestamp.hour < 12:
                period_groups["morning"][regime].append(ret)
            else:
                period_groups["afternoon"][regime].append(ret)

        result = {}
        for regime, metrics in regime_groups.items():
            result[regime] = {
                metric: self._compute_stats(values)
                for metric, values in metrics.items()
            }

        result["period_comparison"] = {}
        for period, regimes in period_groups.items():
            result["period_comparison"][period] = {
                regime: self._compute_stats(values)
                for regime, values in regimes.items()
            }

        return result

    # ==================================================================
    # Phase 10: Expiry-Day Research
    # ==================================================================

    def analyze_expiry_day(
        self, dataset: list[TimestampResearch]
    ) -> dict:
        """Compare expiry-day vs non-expiry-day behavior."""
        expiry_day = {
            "net_gex": [],
            "absolute_gex": [],
            "spot_return_15m": [],
            "spot_return_30m": [],
            "gex_change": [],
        }
        non_expiry_day = {
            "net_gex": [],
            "absolute_gex": [],
            "spot_return_15m": [],
            "spot_return_30m": [],
            "gex_change": [],
        }

        for point in dataset:
            target = expiry_day if point.is_expiry_day else non_expiry_day
            target["net_gex"].append(point.total_net_gex)
            target["absolute_gex"].append(point.absolute_gex)

            if point.nifty_return_15m is not None:
                target["spot_return_15m"].append(point.nifty_return_15m)
            if point.nifty_return_30m is not None:
                target["spot_return_30m"].append(point.nifty_return_30m)
            if point.gex_change is not None:
                target["gex_change"].append(point.gex_change)

        return {
            "expiry_day": {k: self._compute_stats(v) for k, v in expiry_day.items()},
            "non_expiry_day": {k: self._compute_stats(v) for k, v in non_expiry_day.items()},
            "expiry_day_count": len(expiry_day["net_gex"]),
            "non_expiry_day_count": len(non_expiry_day["net_gex"]),
        }

    # ==================================================================
    # Phase 11: Signal Candidate Discovery
    # ==================================================================

    def discover_signals(
        self, dataset: list[TimestampResearch]
    ) -> list[SignalCandidate]:
        """Discover candidate signals with statistical evidence."""
        candidates = []

        # Signal 1: Gamma Regime Shift
        candidates.append(self._test_regime_shift_signal(dataset))

        # Signal 2: Gamma Flip Break
        candidates.append(self._test_flip_break_signal(dataset))

        # Signal 3: Gamma Flip Rejection
        candidates.append(self._test_flip_rejection_signal(dataset))

        # Signal 4: Positive Gamma Pin
        candidates.append(self._test_positive_gamma_pin(dataset))

        # Signal 5: Negative Gamma Expansion
        candidates.append(self._test_negative_gamma_expansion(dataset))

        # Signal 6: GEX Acceleration
        candidates.append(self._test_gex_acceleration_signal(dataset))

        # Signal 7: GEX + OI Confirmation
        candidates.append(self._test_gex_oi_signal(dataset))

        # Signal 8: Gamma Wall Rejection
        candidates.append(self._test_wall_rejection_signal(dataset))

        # Signal 9: Gamma Wall Breakout
        candidates.append(self._test_wall_breakout_signal(dataset))

        # Signal 10: GEX Divergence (price up, GEX down or vice versa)
        candidates.append(self._test_gex_divergence_signal(dataset))

        return [c for c in candidates if c is not None]

    def _test_regime_shift_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """NEGATIVE_GAMMA → POSITIVE_GAMMA shift suggests mean reversion."""
        entries = []
        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]
            if (prev.gamma_regime == "NEGATIVE_GAMMA" and
                point.gamma_regime == "POSITIVE_GAMMA" and
                point.nifty_return_15m is not None):
                entries.append(point.nifty_return_15m)

        return self._make_signal(
            "RegimeShift_NegToPos", entries,
            entry="NEGATIVE_GAMMA → POSITIVE_GAMMA",
            direction="LONG",
            regime="NEGATIVE_GAMMA",
        )

    def _test_flip_break_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """Spot breaks above gamma flip after being below."""
        entries = []
        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]
            if (prev.distance_to_flip is not None and point.distance_to_flip is not None and
                prev.distance_to_flip < 0 and point.distance_to_flip > 0 and
                point.nifty_return_15m is not None):
                entries.append(point.nifty_return_15m)

        return self._make_signal(
            "FlipBreak_Above", entries,
            entry="Spot crosses above gamma flip",
            direction="LONG",
            regime="NEGATIVE_GAMMA",
        )

    def _test_flip_rejection_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """Spot approaches gamma flip but gets rejected."""
        entries = []
        for i in range(2, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]
            prev2 = dataset[i - 2]
            if (prev.distance_to_flip is not None and point.distance_to_flip is not None and
                prev2.distance_to_flip is not None and
                abs(prev.distance_to_flip) < 30 and
                abs(point.distance_to_flip) > abs(prev.distance_to_flip) * 1.5 and
                point.nifty_return_15m is not None):
                entries.append(-point.nifty_return_15m)  # Reversal expected

        return self._make_signal(
            "FlipRejection", entries,
            entry="Spot near flip, then moves away",
            direction="SHORT",
            regime="NEAR_GAMMA_FLIP",
        )

    def _test_positive_gamma_pin(self, dataset: list) -> Optional[SignalCandidate]:
        """Strong positive GEX with spot near wall — pinning behavior."""
        entries = []
        for point in dataset:
            if (point.gamma_regime == "POSITIVE_GAMMA" and
                point.strongest_positive_wall is not None and
                abs(point.spot - point.strongest_positive_wall) / point.spot * 100 < 0.3 and
                point.nifty_return_30m is not None):
                entries.append(abs(point.nifty_return_30m))  # Low volatility expected

        return self._make_signal(
            "PositiveGammaPin", entries,
            entry="Strong positive GEX + spot near wall",
            direction="NEUTRAL",
            regime="POSITIVE_GAMMA",
        )

    def _test_negative_gamma_expansion(self, dataset: list) -> Optional[SignalCandidate]:
        """Negative GEX + GEX becoming more negative → expansion expected."""
        entries = []
        for point in dataset:
            if (point.gamma_regime == "NEGATIVE_GAMMA" and
                point.gex_change is not None and point.gex_change < 0 and
                point.nifty_return_15m is not None):
                entries.append(abs(point.nifty_return_15m))  # Volatility expected

        return self._make_signal(
            "NegativeGammaExpansion", entries,
            entry="Negative GEX + GEX becoming more negative",
            direction="VOLATILITY",
            regime="NEGATIVE_GAMMA",
        )

    def _test_gex_acceleration_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """GEX acceleration > 0 suggests strengthening gamma."""
        entries = []
        for point in dataset:
            if (point.gex_acceleration is not None and point.gex_acceleration > 0 and
                point.nifty_return_15m is not None):
                entries.append(point.nifty_return_15m)

        return self._make_signal(
            "GexAcceleration_Positive", entries,
            entry="GEX acceleration > 0",
            direction="LONG",
            regime="POSITIVE_GAMMA",
        )

    def _test_gex_oi_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """GEX + OI confirmation: negative GEX + OI increase = bearish."""
        entries = []
        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]
            if (point.total_net_gex < 0 and
                point.oi_change is not None and point.oi_change > 0 and
                point.nifty_return_15m is not None):
                entries.append(-point.nifty_return_15m)  # Bearish expected

        return self._make_signal(
            "GexOi_NegGexOiUp", entries,
            entry="Negative GEX + OI increase",
            direction="SHORT",
            regime="NEGATIVE_GAMMA",
        )

    def _test_wall_rejection_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """Spot near positive wall then reverses."""
        entries = []
        for i in range(2, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]
            if (point.strongest_positive_wall is not None and
                prev.strongest_positive_wall is not None and
                abs(prev.spot - prev.strongest_positive_wall) / prev.spot * 100 < 0.5 and
                point.spot < prev.spot and
                point.nifty_return_15m is not None):
                entries.append(point.nifty_return_15m)

        return self._make_signal(
            "WallRejection_Positive", entries,
            entry="Spot touches positive wall, then reverses down",
            direction="SHORT",
            regime="POSITIVE_GAMMA",
        )

    def _test_wall_breakout_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """Spot breaks above positive wall."""
        entries = []
        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]
            if (point.strongest_positive_wall is not None and
                prev.strongest_positive_wall is not None and
                prev.spot < prev.strongest_positive_wall and
                point.spot > point.strongest_positive_wall and
                point.nifty_return_15m is not None):
                entries.append(point.nifty_return_15m)

        return self._make_signal(
            "WallBreakout_Positive", entries,
            entry="Spot breaks above positive wall",
            direction="LONG",
            regime="POSITIVE_GAMMA",
        )

    def _test_gex_divergence_signal(self, dataset: list) -> Optional[SignalCandidate]:
        """Price up but GEX down (or vice versa) — divergence."""
        entries = []
        for i in range(1, len(dataset)):
            point = dataset[i]
            prev = dataset[i - 1]
            if point.nifty_return_15m is None or point.gex_change is None:
                continue

            price_up = point.spot > prev.spot
            gex_up = point.gex_change > 0

            if price_up and not gex_up:
                # Bearish divergence
                entries.append(-point.nifty_return_15m)
            elif not price_up and gex_up:
                # Bullish divergence
                entries.append(point.nifty_return_15m)

        return self._make_signal(
            "GexDivergence", entries,
            entry="Price vs GEX divergence",
            direction="MEAN_REVERSION",
            regime="ALL",
        )

    def _make_signal(
        self,
        name: str,
        returns: list[float],
        entry: str = "",
        direction: str = "",
        regime: str = "",
    ) -> Optional[SignalCandidate]:
        """Create a SignalCandidate from a list of forward returns."""
        if not returns:
            return None

        stats = self._compute_stats(returns)
        if stats["count"] < 10:
            confidence = "INSUFFICIENT_SAMPLE"
        elif stats["count"] < 50:
            confidence = "LOW"
        elif stats["win_pct"] > 55 or stats["win_pct"] < 45:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        # Expected value = (win_rate * avg_win) - (loss_rate * avg_loss)
        win_returns = [r for r in returns if r > 0]
        loss_returns = [r for r in returns if r <= 0]
        avg_win = sum(win_returns) / len(win_returns) if win_returns else 0
        avg_loss = sum(loss_returns) / len(loss_returns) if loss_returns else 0
        ev = (stats["win_pct"] / 100 * avg_win) + ((100 - stats["win_pct"]) / 100 * avg_loss)

        return SignalCandidate(
            signal_name=name,
            entry_condition=entry,
            expected_direction=direction,
            preferred_regime=regime,
            sample_size=stats["count"],
            win_rate=stats["win_pct"],
            mean_return=stats["mean"],
            median_return=stats["median"],
            std_return=stats["std"],
            max_favorable=stats["max_favorable"],
            max_adverse=stats["max_adverse"],
            expected_value=round(ev, 4),
            confidence_level=confidence,
        )

    # ==================================================================
    # Phase 13: Walk-Forward Validation
    # ==================================================================

    def walk_forward_validate(
        self,
        dataset: list[TimestampResearch],
        signal_func,
        train_pct: float = 0.7,
    ) -> dict:
        """Walk-forward validation for a signal function.

        Splits dataset chronologically into training and validation.
        """
        n = len(dataset)
        split_idx = int(n * train_pct)

        train_data = dataset[:split_idx]
        test_data = dataset[split_idx:]

        train_returns = signal_func(train_data)
        test_returns = signal_func(test_data)

        return {
            "in_sample": self._compute_stats(train_returns) if train_returns else {"count": 0},
            "out_of_sample": self._compute_stats(test_returns) if test_returns else {"count": 0},
            "train_size": len(train_data),
            "test_size": len(test_data),
            "split_date": test_data[0].timestamp.isoformat() if test_data else None,
        }

    # ==================================================================
    # Phase 14: Statistical Robustness
    # ==================================================================

    def robustness_check(
        self, returns: list[float], name: str = ""
    ) -> dict:
        """Check robustness of a signal's returns."""
        if not returns:
            return {"name": name, "robust": False, "reason": "NO_DATA"}

        stats = self._compute_stats(returns)

        # Remove top 5% and check
        sorted_returns = sorted(returns)
        trim_count = max(1, len(returns) // 20)
        trimmed = sorted_returns[trim_count:-trim_count]
        trimmed_stats = self._compute_stats(trimmed) if trimmed else {"count": 0}

        # Check stability
        robust = (
            stats["count"] >= 30 and
            abs(stats["mean"]) > 0.001 and  # Non-trivial mean
            trimmed_stats.get("mean", 0) * stats["mean"] > 0  # Same direction after trimming
        )

        return {
            "name": name,
            "full_sample": stats,
            "trimmed_sample": trimmed_stats,
            "robust": robust,
            "sample_size": stats["count"],
            "significance": "HIGH" if stats["count"] >= 100 and robust else
                           "MEDIUM" if stats["count"] >= 50 and robust else
                           "LOW" if robust else "INSUFFICIENT",
        }

    # ==================================================================
    # Phase 15: Multiple-Testing Protection
    # ==================================================================

    def multiple_testing_adjustment(
        self, signals: list[SignalCandidate]
    ) -> dict:
        """Report multiple-testing context."""
        total_tested = len(signals)
        significant = [s for s in signals if s.confidence_level in ("MEDIUM", "HIGH")]
        # Bonferroni-adjusted alpha
        alpha = 0.05
        bonferroni_alpha = alpha / total_tested if total_tested > 0 else alpha

        return {
            "total_hypotheses_tested": total_tested,
            "apparently_successful": len(significant),
            "bonferroni_alpha": round(bonferroni_alpha, 6),
            "signals": [
                {
                    "name": s.signal_name,
                    "sample_size": s.sample_size,
                    "win_rate": s.win_rate,
                    "expected_value": s.expected_value,
                    "confidence": s.confidence_level,
                }
                for s in significant
            ],
        }

    # ==================================================================
    # Utility: Statistics
    # ==================================================================

    def _compute_stats(self, values: list[float]) -> dict:
        """Compute basic statistics for a list of values."""
        if not values:
            return {"count": 0}

        n = len(values)
        sorted_vals = sorted(values)
        mean = sum(values) / n
        median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        variance = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(variance)

        p25_idx = n // 4
        p75_idx = (3 * n) // 4

        wins = sum(1 for v in values if v > 0)
        win_pct = wins / n * 100

        return {
            "count": n,
            "mean": round(mean, 4),
            "median": round(median, 4),
            "std": round(std, 4),
            "win_pct": round(win_pct, 2),
            "p25": round(sorted_vals[p25_idx], 4),
            "p75": round(sorted_vals[min(p75_idx, n - 1)], 4),
            "max_favorable": round(max(values), 4),
            "max_adverse": round(min(values), 4),
        }

    def _compute_stats_grouped(self, groups: dict[str, list[float]]) -> dict:
        """Compute stats for multiple groups."""
        return {k: self._compute_stats(v) for k, v in groups.items() if v}

    # ==================================================================
    # Master: Run Complete Research
    # ==================================================================

    def run_complete_research(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        max_timestamps: Optional[int] = None,
    ) -> dict:
        """Run the complete Phase 7.8E research pipeline.

        Returns a comprehensive research results dictionary.
        """
        logger.info("Starting Phase 7.8E complete research pipeline")

        # Build dataset
        dataset = self.build_research_dataset(start, end, max_timestamps)
        if not dataset:
            return {"error": "No data available"}

        logger.info(f"Dataset: {len(dataset)} timestamps, "
                     f"{dataset[0].timestamp.date()} → {dataset[-1].timestamp.date()}")

        # Phase 4: Flip reactions
        flip_analysis = self.analyze_flip_reactions(dataset)

        # Phase 5: Wall behaviour
        wall_analysis = self.analyze_wall_behaviour(dataset)

        # Phase 6: GEX change patterns
        gex_change_analysis = self.analyze_gex_change_patterns(dataset)

        # Phase 7: GEX + momentum
        momentum_analysis = self.analyze_gex_momentum_combinations(dataset)

        # Phase 8: GEX + OI
        oi_analysis = self.analyze_gex_oi_combinations(dataset)

        # Phase 9: Regime performance
        regime_analysis = self.analyze_regime_performance(dataset)

        # Phase 10: Expiry day
        expiry_analysis = self.analyze_expiry_day(dataset)

        # Phase 11: Signal discovery
        signals = self.discover_signals(dataset)

        # Phase 13: Walk-forward for top signals
        walk_forward_results = {}
        for signal in signals[:5]:  # Top 5 signals
            def make_signal_func(sig_name):
                def signal_func(data):
                    # Re-run signal detection on subset
                    return self._extract_signal_returns(data, sig_name)
                return signal_func

            wf = self.walk_forward_validate(dataset, make_signal_func(signal.signal_name))
            walk_forward_results[signal.signal_name] = wf

        # Phase 14: Robustness
        robustness = {}
        for signal in signals:
            returns = self._extract_signal_returns(dataset, signal.signal_name)
            robustness[signal.signal_name] = self.robustness_check(returns, signal.signal_name)

        # Phase 15: Multiple testing
        multiple_testing = self.multiple_testing_adjustment(signals)

        return {
            "dataset_summary": {
                "timestamps": len(dataset),
                "date_range": f"{dataset[0].timestamp.date()} → {dataset[-1].timestamp.date()}",
                "total_gex_rows": sum(p.instrument_count for p in dataset),
                "unique_strikes": len(set(p.timestamp for p in dataset)),
            },
            "flip_analysis": flip_analysis,
            "wall_analysis": wall_analysis,
            "gex_change_analysis": gex_change_analysis,
            "momentum_analysis": momentum_analysis,
            "oi_analysis": oi_analysis,
            "regime_analysis": regime_analysis,
            "expiry_analysis": expiry_analysis,
            "signals": [
                {
                    "name": s.signal_name,
                    "entry": s.entry_condition,
                    "direction": s.expected_direction,
                    "regime": s.preferred_regime,
                    "sample_size": s.sample_size,
                    "win_rate": s.win_rate,
                    "mean_return": s.mean_return,
                    "median_return": s.median_return,
                    "expected_value": s.expected_value,
                    "confidence": s.confidence_level,
                }
                for s in signals
            ],
            "walk_forward": walk_forward_results,
            "robustness": robustness,
            "multiple_testing": multiple_testing,
        }

    def _extract_signal_returns(
        self, dataset: list[TimestampResearch], signal_name: str
    ) -> list[float]:
        """Extract forward returns for a specific signal from dataset."""
        # Map signal name to extraction logic
        extractors = {
            "RegimeShift_NegToPos": self._extract_regime_shift,
            "FlipBreak_Above": self._extract_flip_break,
            "FlipRejection": self._extract_flip_rejection,
            "PositiveGammaPin": self._extract_gamma_pin,
            "NegativeGammaExpansion": self._extract_gamma_expansion,
            "GexAcceleration_Positive": self._extract_gex_acceleration,
            "GexOi_NegGexOiUp": self._extract_gex_oi,
            "WallRejection_Positive": self._extract_wall_rejection,
            "WallBreakout_Positive": self._extract_wall_breakout,
            "GexDivergence": self._extract_gex_divergence,
        }

        extractor = extractors.get(signal_name)
        if extractor:
            return extractor(dataset)
        return []

    def _extract_regime_shift(self, dataset):
        returns = []
        for i in range(1, len(dataset)):
            p, prev = dataset[i], dataset[i-1]
            if prev.gamma_regime == "NEGATIVE_GAMMA" and p.gamma_regime == "POSITIVE_GAMMA" and p.nifty_return_15m is not None:
                returns.append(p.nifty_return_15m)
        return returns

    def _extract_flip_break(self, dataset):
        returns = []
        for i in range(1, len(dataset)):
            p, prev = dataset[i], dataset[i-1]
            if (prev.distance_to_flip is not None and p.distance_to_flip is not None and
                prev.distance_to_flip < 0 and p.distance_to_flip > 0 and p.nifty_return_15m is not None):
                returns.append(p.nifty_return_15m)
        return returns

    def _extract_flip_rejection(self, dataset):
        returns = []
        for i in range(2, len(dataset)):
            p, prev, prev2 = dataset[i], dataset[i-1], dataset[i-2]
            if (prev.distance_to_flip is not None and p.distance_to_flip is not None and
                prev2.distance_to_flip is not None and
                abs(prev.distance_to_flip) < 30 and
                abs(p.distance_to_flip) > abs(prev.distance_to_flip) * 1.5 and
                p.nifty_return_15m is not None):
                returns.append(-p.nifty_return_15m)
        return returns

    def _extract_gamma_pin(self, dataset):
        returns = []
        for p in dataset:
            if (p.gamma_regime == "POSITIVE_GAMMA" and
                p.strongest_positive_wall is not None and
                abs(p.spot - p.strongest_positive_wall) / p.spot * 100 < 0.3 and
                p.nifty_return_30m is not None):
                returns.append(abs(p.nifty_return_30m))
        return returns

    def _extract_gamma_expansion(self, dataset):
        returns = []
        for p in dataset:
            if (p.gamma_regime == "NEGATIVE_GAMMA" and
                p.gex_change is not None and p.gex_change < 0 and
                p.nifty_return_15m is not None):
                returns.append(abs(p.nifty_return_15m))
        return returns

    def _extract_gex_acceleration(self, dataset):
        returns = []
        for p in dataset:
            if (p.gex_acceleration is not None and p.gex_acceleration > 0 and
                p.nifty_return_15m is not None):
                returns.append(p.nifty_return_15m)
        return returns

    def _extract_gex_oi(self, dataset):
        returns = []
        for i in range(1, len(dataset)):
            p = dataset[i]
            if (p.total_net_gex < 0 and
                p.oi_change is not None and p.oi_change > 0 and
                p.nifty_return_15m is not None):
                returns.append(-p.nifty_return_15m)
        return returns

    def _extract_wall_rejection(self, dataset):
        returns = []
        for i in range(2, len(dataset)):
            p, prev = dataset[i], dataset[i-1]
            if (p.strongest_positive_wall is not None and
                prev.strongest_positive_wall is not None and
                abs(prev.spot - prev.strongest_positive_wall) / prev.spot * 100 < 0.5 and
                p.spot < prev.spot and p.nifty_return_15m is not None):
                returns.append(p.nifty_return_15m)
        return returns

    def _extract_wall_breakout(self, dataset):
        returns = []
        for i in range(1, len(dataset)):
            p, prev = dataset[i], dataset[i-1]
            if (p.strongest_positive_wall is not None and
                prev.strongest_positive_wall is not None and
                prev.spot < prev.strongest_positive_wall and
                p.spot > p.strongest_positive_wall and
                p.nifty_return_15m is not None):
                returns.append(p.nifty_return_15m)
        return returns

    def _extract_gex_divergence(self, dataset):
        returns = []
        for i in range(1, len(dataset)):
            p, prev = dataset[i], dataset[i-1]
            if p.nifty_return_15m is None or p.gex_change is None:
                continue
            price_up = p.spot > prev.spot
            gex_up = p.gex_change > 0
            if price_up and not gex_up:
                returns.append(-p.nifty_return_15m)
            elif not price_up and gex_up:
                returns.append(p.nifty_return_15m)
        return returns
