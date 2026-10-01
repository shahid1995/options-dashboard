"""Historical GEX Analytics Engine — Phase 7.8D.

Builds research-grade analytics on top of the persisted historical_gex data.
This module does NOT recalculate GEX — it reads from the historical_gex table
as the authoritative source.

Architecture:
    historical_gex (observed, per-option)
        ↓
    GexAnalyticsEngine
        ↓
    Time-series aggregation (per-timestamp, per-strike, per-expiry)
        ↓
    Derived analytics (change, acceleration, regime, flip, walls)
        ↓
    Price relationship (forward returns)
        ↓
    Signal dataset (combined analytics view)

Sign convention (inherited from Phase 7.1):
    Call GEX = +raw_gex
    Put GEX  = -raw_gex
    Net GEX  = call_gex + put_gex

Calculation versioning:
    All queries filter by calc_version to ensure version isolation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_, text
from sqlalchemy.orm import Session

from app.models import HistoricalGexSnapshot, NiftyCandle
from app.services.point_in_time import PointInTimeDataset

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CALC_VERSION = "h_gex_v1"
DEFAULT_INTERVAL = "3min"

# Gamma regime thresholds (configurable)
NEUTRAL_THRESHOLD = 0.0  # net_gex exactly zero → NEUTRAL

# Forward return intervals (in 3-minute candles)
FORWARD_RETURN_INTERVALS = [1, 2, 3, 5, 10, 20]  # 3min, 6min, 9min, 15min, 30min, 60min


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TimestampGex:
    """Aggregated GEX at one timestamp across all instruments."""
    timestamp: datetime
    spot: float
    call_gex: float = 0.0
    put_gex: float = 0.0
    net_gex: float = 0.0
    absolute_gex: float = 0.0
    positive_gex: float = 0.0
    negative_gex: float = 0.0
    instrument_count: int = 0
    strike_count: int = 0


@dataclass
class StrikeGex:
    """Aggregated GEX at one strike for one timestamp."""
    strike: float
    call_gex: float = 0.0
    put_gex: float = 0.0
    net_gex: float = 0.0
    absolute_gex: float = 0.0
    rank: int = 0


@dataclass
class ExpiryGex:
    """Aggregated GEX at one expiry for one timestamp."""
    expiry: str
    call_gex: float = 0.0
    put_gex: float = 0.0
    net_gex: float = 0.0
    absolute_gex: float = 0.0
    concentration: float = 0.0  # share of total absolute GEX


@dataclass
class GexChange:
    """GEX change between two consecutive timestamps."""
    timestamp: datetime
    previous_timestamp: Optional[datetime]
    current_net_gex: float
    previous_net_gex: Optional[float]
    gex_change: Optional[float] = None
    gex_change_pct: Optional[float] = None


@dataclass
class GexAcceleration:
    """GEX acceleration (second derivative of net GEX)."""
    timestamp: datetime
    current_change: Optional[float]
    previous_change: Optional[float]
    acceleration: Optional[float] = None


@dataclass
class GammaRegime:
    """Gamma regime classification at one timestamp."""
    timestamp: datetime
    spot: float
    net_gex: float
    regime: str  # POSITIVE_GAMMA, NEGATIVE_GAMMA, NEUTRAL
    previous_regime: Optional[str] = None
    regime_transition: Optional[str] = None  # e.g. "NEGATIVE→POSITIVE"
    regime_duration: int = 0  # consecutive timestamps in current regime


@dataclass
class GammaFlip:
    """Estimated gamma-flip level at one timestamp."""
    timestamp: datetime
    spot: float
    flip_strike: Optional[float] = None
    flip_confidence: Optional[float] = None  # 0-1, based on interpolation quality
    strike_below: Optional[float] = None  # strike where GEX changes sign
    strike_above: Optional[float] = None
    gex_below: Optional[float] = None
    gex_above: Optional[float] = None
    num_sign_changes: int = 0
    status: str = "ESTIMATED"  # ESTIMATED, EXACT_ZERO, NO_CROSSING, INSUFFICIENT_DATA


@dataclass
class GammaWall:
    """Significant GEX concentration strike."""
    strike: float
    gex: float
    absolute_gex: float
    distance_from_spot: float
    distance_pct: float
    wall_type: str  # "POSITIVE" or "NEGATIVE"
    rank: int = 0


@dataclass
class GammaWallsResult:
    """Gamma wall analysis at one timestamp."""
    timestamp: datetime
    spot: float
    strongest_positive: Optional[GammaWall] = None
    strongest_negative: Optional[GammaWall] = None
    positive_walls: list = field(default_factory=list)
    negative_walls: list = field(default_factory=list)
    wall_movement: Optional[dict] = None  # previous vs current walls


@dataclass
class PriceGexRelationship:
    """Joined price + GEX data at one timestamp."""
    timestamp: datetime
    spot: float
    net_gex: float
    gex_change: Optional[float] = None
    gex_acceleration: Optional[float] = None
    gamma_regime: Optional[str] = None
    gamma_flip: Optional[float] = None
    strongest_positive_wall: Optional[float] = None
    strongest_negative_wall: Optional[float] = None
    wall_distance: Optional[float] = None
    # Forward returns
    spot_return_3m: Optional[float] = None
    spot_return_6m: Optional[float] = None
    spot_return_9m: Optional[float] = None
    spot_return_15m: Optional[float] = None
    spot_return_30m: Optional[float] = None
    spot_return_60m: Optional[float] = None
    # Excursion
    max_favorable: Optional[float] = None
    max_adverse: Optional[float] = None


@dataclass
class ForwardReturns:
    """Forward price returns from a given timestamp."""
    timestamp: datetime
    spot: float
    returns: dict = field(default_factory=dict)  # {interval_candles: return_pct}
    max_favorable: Optional[float] = None
    max_adverse: Optional[float] = None


@dataclass
class AnalyticsStats:
    """Statistical summary for a group of observations."""
    count: int = 0
    mean: Optional[float] = None
    median: Optional[float] = None
    std: Optional[float] = None
    win_pct: Optional[float] = None
    p25: Optional[float] = None
    p75: Optional[float] = None
    max_favorable: Optional[float] = None
    max_adverse: Optional[float] = None


# ---------------------------------------------------------------------------
# GexAnalyticsEngine
# ---------------------------------------------------------------------------

class GexAnalyticsEngine:
    """Historical GEX analytics engine.

    Reads from historical_gex and nifty_candles.
    Does NOT modify the production database.
    """

    def __init__(self, db: Session, calc_version: str = DEFAULT_CALC_VERSION):
        self.db = db
        self.calc_version = calc_version
        self.pit = PointInTimeDataset(db)

    def _decision_timestamp_for_observation(self, timestamp: datetime) -> datetime:
        """Return the decision time at the end of the three-minute source bar."""
        return timestamp + timedelta(minutes=3)

    def _exact_rows(self, ts: datetime) -> list[HistoricalGexSnapshot]:
        """Snapshots whose source bar is exactly ``ts`` (no fallback).

        PIT seam for the exact-observation reads: the decision time is
        the end of ``ts``'s three-minute source bar, and the accessor
        returns rows only for instruments that reported ``ts`` itself.
        """
        return self.pit.historical_gex_observed_at(
            self._decision_timestamp_for_observation(ts),
            ts,
            interval=DEFAULT_INTERVAL,
            calc_version=self.calc_version,
        )

    # ------------------------------------------------------------------
    # Phase 2: Time-series aggregation
    # ------------------------------------------------------------------

    def aggregate_timestamp(self, ts: datetime) -> Optional[TimestampGex]:
        """Aggregate GEX at a single timestamp across all instruments."""
        rows = self._exact_rows(ts)

        if not rows:
            return None

        call_gex = sum(r.signed_gex for r in rows if r.option_type == "CE")
        put_gex = sum(r.signed_gex for r in rows if r.option_type == "PE")
        net_gex = call_gex + put_gex

        spot = rows[0].spot
        strikes = set(r.strike for r in rows)

        return TimestampGex(
            timestamp=ts,
            spot=spot,
            call_gex=call_gex,
            put_gex=put_gex,
            net_gex=net_gex,
            absolute_gex=abs(net_gex),
            positive_gex=max(net_gex, 0.0),
            negative_gex=min(net_gex, 0.0),
            instrument_count=len(rows),
            strike_count=len(strikes),
        )

    def aggregate_timestamps_bulk(self, timestamps: list[datetime]) -> list[TimestampGex]:
        """Aggregate GEX for multiple timestamps efficiently."""
        if not timestamps:
            return []

        results = []
        for ts in timestamps:
            agg = self.aggregate_timestamp(ts)
            if agg:
                results.append(agg)
        return results

    def get_timestamps(self, start: Optional[datetime] = None, end: Optional[datetime] = None) -> list[datetime]:
        """Get all unique timestamps in the historical GEX data."""
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

    def aggregate_strike(self, ts: datetime) -> list[StrikeGex]:
        """Aggregate GEX by strike at a single timestamp."""
        rows = self._exact_rows(ts)

        strike_map: dict[float, StrikeGex] = {}
        for r in rows:
            if r.strike not in strike_map:
                strike_map[r.strike] = StrikeGex(strike=r.strike)
            sg = strike_map[r.strike]
            if r.option_type == "CE":
                sg.call_gex += r.signed_gex
            else:
                sg.put_gex += r.signed_gex
            sg.net_gex = sg.call_gex + sg.put_gex
            sg.absolute_gex = abs(sg.net_gex)

        # Rank by absolute GEX
        strikes = sorted(strike_map.values(), key=lambda s: s.absolute_gex, reverse=True)
        for i, s in enumerate(strikes):
            s.rank = i + 1

        return strikes

    def aggregate_expiry(self, ts: datetime) -> list[ExpiryGex]:
        """Aggregate GEX by expiry at a single timestamp."""
        rows = self._exact_rows(ts)

        expiry_map: dict[str, ExpiryGex] = {}
        total_abs = 0.0
        for r in rows:
            exp = r.expiry
            if exp not in expiry_map:
                expiry_map[exp] = ExpiryGex(expiry=exp)
            eg = expiry_map[exp]
            if r.option_type == "CE":
                eg.call_gex += r.signed_gex
            else:
                eg.put_gex += r.signed_gex
            eg.net_gex = eg.call_gex + eg.put_gex
            eg.absolute_gex = abs(eg.net_gex)
            total_abs += eg.absolute_gex

        # Calculate concentration
        for eg in expiry_map.values():
            eg.concentration = eg.absolute_gex / total_abs if total_abs > 0 else 0.0

        return sorted(expiry_map.values(), key=lambda e: e.absolute_gex, reverse=True)

    # ------------------------------------------------------------------
    # Phase 3: GEX change and acceleration
    # ------------------------------------------------------------------

    def compute_gex_change(self, current: TimestampGex, previous: Optional[TimestampGex]) -> GexChange:
        """Compute GEX change between two timestamps."""
        gex_change = None
        gex_change_pct = None
        if previous and previous.net_gex != 0:
            gex_change = current.net_gex - previous.net_gex
            if previous.net_gex != 0:
                gex_change_pct = gex_change / abs(previous.net_gex) * 100

        return GexChange(
            timestamp=current.timestamp,
            previous_timestamp=previous.timestamp if previous else None,
            current_net_gex=current.net_gex,
            previous_net_gex=previous.net_gex if previous else None,
            gex_change=gex_change,
            gex_change_pct=gex_change_pct,
        )

    def compute_gex_acceleration(
        self, current_change: Optional[GexChange], previous_change: Optional[GexChange]
    ) -> GexAcceleration:
        """Compute GEX acceleration (second derivative)."""
        current_val = current_change.gex_change if current_change else None
        prev_val = previous_change.gex_change if previous_change else None
        acceleration = None
        if current_val is not None and prev_val is not None:
            acceleration = current_val - prev_val

        return GexAcceleration(
            timestamp=current_change.timestamp if current_change else datetime.min,
            current_change=current_val,
            previous_change=prev_val,
            acceleration=acceleration,
        )

    # ------------------------------------------------------------------
    # Phase 4: Gamma regime
    # ------------------------------------------------------------------

    def classify_regime(self, net_gex: float) -> str:
        """Classify gamma regime based on net GEX."""
        if net_gex > NEUTRAL_THRESHOLD:
            return "POSITIVE_GAMMA"
        elif net_gex < NEUTRAL_THRESHOLD:
            return "NEGATIVE_GAMMA"
        else:
            return "NEUTRAL"

    def compute_regime_series(self, timestamps: list[TimestampGex]) -> list[GammaRegime]:
        """Compute gamma regime for a series of timestamps."""
        regimes = []
        previous_regime = None
        regime_start_idx = 0

        for i, ts in enumerate(timestamps):
            regime = self.classify_regime(ts.net_gex)

            # Detect transition
            transition = None
            if previous_regime and previous_regime != regime:
                transition = f"{previous_regime}→{regime}"
                regime_start_idx = i

            duration = i - regime_start_idx + 1

            regimes.append(GammaRegime(
                timestamp=ts.timestamp,
                spot=ts.spot,
                net_gex=ts.net_gex,
                regime=regime,
                previous_regime=previous_regime,
                regime_transition=transition,
                regime_duration=duration,
            ))

            previous_regime = regime

        return regimes

    # ------------------------------------------------------------------
    # Phase 5: Gamma flip detection
    # ------------------------------------------------------------------

    def detect_gamma_flip(self, ts: datetime) -> GammaFlip:
        """Detect gamma-flip level at a single timestamp.

        The gamma flip is the strike/price level where aggregate GEX
        changes sign. We interpolate between adjacent strikes.
        """
        strike_data = self.aggregate_strike(ts)

        if len(strike_data) < 2:
            return GammaFlip(
                timestamp=ts,
                spot=0.0,
                status="INSUFFICIENT_DATA",
            )

        # Get spot from first row
        spot = 0.0
        rows = self._exact_rows(ts)[:1]
        if rows:
            spot = rows[0].spot

        # Sort by strike
        sorted_strikes = sorted(strike_data, key=lambda s: s.strike)

        # Find sign changes in net GEX
        sign_changes = []
        for i in range(len(sorted_strikes) - 1):
            s1 = sorted_strikes[i]
            s2 = sorted_strikes[i + 1]
            if s1.net_gex * s2.net_gex < 0:  # Different signs
                # Linear interpolation to find zero crossing
                if s2.net_gex - s1.net_gex != 0:
                    flip_strike = s1.strike - s1.net_gex * (s2.strike - s1.strike) / (s2.net_gex - s1.net_gex)
                else:
                    flip_strike = (s1.strike + s2.strike) / 2.0

                # Confidence based on how close the signs are to zero
                min_abs = min(abs(s1.net_gex), abs(s2.net_gex))
                max_abs = max(abs(s1.net_gex), abs(s2.net_gex))
                confidence = 1.0 - (min_abs / max_abs) if max_abs > 0 else 0.5

                sign_changes.append({
                    "strike": flip_strike,
                    "confidence": confidence,
                    "strike_below": s1.strike,
                    "strike_above": s2.strike,
                    "gex_below": s1.net_gex,
                    "gex_above": s2.net_gex,
                })
            elif s1.net_gex == 0:
                sign_changes.append({
                    "strike": s1.strike,
                    "confidence": 1.0,
                    "strike_below": s1.strike,
                    "strike_above": s1.strike,
                    "gex_below": 0.0,
                    "gex_above": 0.0,
                })

        if not sign_changes:
            return GammaFlip(
                timestamp=ts,
                spot=spot,
                status="NO_CROSSING",
                num_sign_changes=0,
            )

        if len(sign_changes) == 1 and sign_changes[0]["gex_below"] == 0 and sign_changes[0]["gex_above"] == 0:
            return GammaFlip(
                timestamp=ts,
                spot=spot,
                flip_strike=sign_changes[0]["strike"],
                flip_confidence=1.0,
                status="EXACT_ZERO",
                num_sign_changes=1,
            )

        # Select the flip closest to spot
        best = min(sign_changes, key=lambda sc: abs(sc["strike"] - spot))

        return GammaFlip(
            timestamp=ts,
            spot=spot,
            flip_strike=best["strike"],
            flip_confidence=best["confidence"],
            strike_below=best["strike_below"],
            strike_above=best["strike_above"],
            gex_below=best["gex_below"],
            gex_above=best["gex_above"],
            num_sign_changes=len(sign_changes),
            status="ESTIMATED",
        )

    # ------------------------------------------------------------------
    # Phase 6: Gamma walls
    # ------------------------------------------------------------------

    def detect_walls(
        self, ts: datetime, top_n: int = 3, previous_walls: Optional[GammaWallsResult] = None
    ) -> GammaWallsResult:
        """Detect gamma walls at a single timestamp."""
        strike_data = self.aggregate_strike(ts)

        if not strike_data:
            return GammaWallsResult(timestamp=ts, spot=0.0)

        spot = 0.0
        # Timestamp-level signal: the spot must come from an exact
        # ``open_time == ts`` row. The exact-observation PIT seam
        # guarantees no stale fallback row ever reaches the filter
        # (and never drives wall distances).
        rows = self._exact_rows(ts)[:1]
        if rows:
            spot = rows[0].spot

        # Separate positive and negative GEX strikes
        positive = [s for s in strike_data if s.net_gex > 0]
        negative = [s for s in strike_data if s.net_gex < 0]

        positive.sort(key=lambda s: s.net_gex, reverse=True)
        negative.sort(key=lambda s: s.net_gex)  # Most negative first

        def to_wall(s: StrikeGex, wall_type: str, rank: int) -> GammaWall:
            dist = s.strike - spot
            dist_pct = dist / spot * 100 if spot > 0 else 0.0
            return GammaWall(
                strike=s.strike,
                gex=s.net_gex,
                absolute_gex=s.absolute_gex,
                distance_from_spot=dist,
                distance_pct=dist_pct,
                wall_type=wall_type,
                rank=rank,
            )

        pos_walls = [to_wall(s, "POSITIVE", i + 1) for i, s in enumerate(positive[:top_n])]
        neg_walls = [to_wall(s, "NEGATIVE", i + 1) for i, s in enumerate(negative[:top_n])]

        # Wall movement
        wall_movement = None
        if previous_walls and previous_walls.strongest_positive and pos_walls:
            wall_movement = {
                "positive_strike_change": pos_walls[0].strike - previous_walls.strongest_positive.strike,
                "positive_gex_change": pos_walls[0].gex - previous_walls.strongest_positive.gex,
            }
            if previous_walls.strongest_negative and neg_walls:
                wall_movement["negative_strike_change"] = neg_walls[0].strike - previous_walls.strongest_negative.strike
                wall_movement["negative_gex_change"] = neg_walls[0].gex - previous_walls.strongest_negative.gex

        return GammaWallsResult(
            timestamp=ts,
            spot=spot,
            strongest_positive=pos_walls[0] if pos_walls else None,
            strongest_negative=neg_walls[0] if neg_walls else None,
            positive_walls=pos_walls,
            negative_walls=neg_walls,
            wall_movement=wall_movement,
        )

    # ------------------------------------------------------------------
    # Phase 7: Price + GEX relationship
    # ------------------------------------------------------------------

    def compute_forward_returns(
        self, ts: datetime, spot: float, nifty_candles: list
    ) -> ForwardReturns:
        """Compute forward returns from a given timestamp.

        Uses pre-fetched NIFTY candles for efficiency.
        No future data leakage: only candles with open_time > ts are used.
        """
        # Find candles after this timestamp
        future_candles = [c for c in nifty_candles if c.open_time > ts]
        future_candles.sort(key=lambda c: c.open_time)

        returns = {}
        max_favorable = None
        max_adverse = None

        for interval in FORWARD_RETURN_INTERVALS:
            if interval <= len(future_candles):
                future_spot = future_candles[interval - 1].close
                ret = (future_spot - spot) / spot * 100
                returns[interval] = round(ret, 4)

                # Track excursion
                prices = [c.close for c in future_candles[:interval]]
                prices.insert(0, spot)
                max_up = max(prices)
                max_down = min(prices)
                mf = (max_up - spot) / spot * 100
                ma = (min(prices) - spot) / spot * 100

                if max_favorable is None or mf > max_favorable:
                    max_favorable = round(mf, 4)
                if max_adverse is None or ma < max_adverse:
                    max_adverse = round(ma, 4)

        return ForwardReturns(
            timestamp=ts,
            spot=spot,
            returns=returns,
            max_favorable=max_favorable,
            max_adverse=max_adverse,
        )

    def build_price_gex_series(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[PriceGexRelationship]:
        """Build the complete price-GEX relationship series."""
        timestamps = self.get_timestamps(start, end)
        if not timestamps:
            return []

        # Aggregate all timestamps
        agg_series = self.aggregate_timestamps_bulk(timestamps)

        # Compute GEX changes
        changes = []
        for i, ts in enumerate(agg_series):
            prev = agg_series[i - 1] if i > 0 else None
            changes.append(self.compute_gex_change(ts, prev))

        # Compute acceleration
        accelerations = []
        for i, ch in enumerate(changes):
            prev_ch = changes[i - 1] if i > 0 else None
            accelerations.append(self.compute_gex_acceleration(ch, prev_ch))

        # Compute regime
        regimes = self.compute_regime_series(agg_series)

        # Compute gamma flips
        flips = [self.detect_gamma_flip(ts) for ts in timestamps]

        # Compute walls
        walls_list = []
        prev_walls = None
        for ts in timestamps:
            w = self.detect_walls(ts, previous_walls=prev_walls)
            walls_list.append(w)
            prev_walls = w

        # Fetch NIFTY candles for forward returns
        if timestamps:
            nifty_start = timestamps[0]
            nifty_end = timestamps[-1] + timedelta(hours=8)  # 8 hours ahead for 60min returns
            nifty_candles = self.db.execute(
                select(NiftyCandle)
                .where(
                    NiftyCandle.open_time >= nifty_start,
                    NiftyCandle.open_time <= nifty_end,
                    NiftyCandle.interval == DEFAULT_INTERVAL,
                )
                .order_by(NiftyCandle.open_time)
            ).scalars().all()
        else:
            nifty_candles = []

        # Build combined series
        results = []
        for i, ts_agg in enumerate(agg_series):
            fr = self.compute_forward_returns(ts_agg.timestamp, ts_agg.spot, nifty_candles)

            results.append(PriceGexRelationship(
                timestamp=ts_agg.timestamp,
                spot=ts_agg.spot,
                net_gex=ts_agg.net_gex,
                gex_change=changes[i].gex_change,
                gex_acceleration=accelerations[i].acceleration,
                gamma_regime=regimes[i].regime if i < len(regimes) else None,
                gamma_flip=flips[i].flip_strike if i < len(flips) else None,
                strongest_positive_wall=walls_list[i].strongest_positive.strike if i < len(walls_list) and walls_list[i].strongest_positive else None,
                strongest_negative_wall=walls_list[i].strongest_negative.strike if i < len(walls_list) and walls_list[i].strongest_negative else None,
                wall_distance=(walls_list[i].strongest_positive.distance_from_spot if i < len(walls_list) and walls_list[i].strongest_positive else None),
                spot_return_3m=fr.returns.get(1),
                spot_return_6m=fr.returns.get(2),
                spot_return_9m=fr.returns.get(3),
                spot_return_15m=fr.returns.get(5),
                spot_return_30m=fr.returns.get(10),
                spot_return_60m=fr.returns.get(20),
                max_favorable=fr.max_favorable,
                max_adverse=fr.max_adverse,
            ))

        return results

    # ------------------------------------------------------------------
    # Phase 9: Statistical analysis
    # ------------------------------------------------------------------

    @staticmethod
    def compute_stats(values: list[float]) -> AnalyticsStats:
        """Compute basic statistics for a list of values."""
        if not values:
            return AnalyticsStats(count=0)

        n = len(values)
        sorted_vals = sorted(values)
        mean = sum(values) / n
        median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        variance = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(variance)

        p25_idx = n // 4
        p75_idx = (3 * n) // 4
        p25 = sorted_vals[p25_idx]
        p75 = sorted_vals[min(p75_idx, n - 1)]

        wins = sum(1 for v in values if v > 0)
        win_pct = wins / n * 100

        return AnalyticsStats(
            count=n,
            mean=round(mean, 4),
            median=round(median, 4),
            std=round(std, 4),
            win_pct=round(win_pct, 2),
            p25=round(p25, 4),
            p75=round(p75, 4),
            max_favorable=round(max(values), 4),
            max_adverse=round(min(values), 4),
        )

    def analyze_by_regime(
        self, series: list[PriceGexRelationship], field_name: str = "spot_return_15m"
    ) -> dict:
        """Analyze forward returns grouped by gamma regime."""
        groups = {}
        for point in series:
            regime = point.gamma_regime or "UNKNOWN"
            ret = getattr(point, field_name, None)
            if ret is not None:
                groups.setdefault(regime, []).append(ret)

        return {regime: self.compute_stats(vals) for regime, vals in groups.items()}

    def analyze_by_gex_change(
        self, series: list[PriceGexRelationship], field_name: str = "spot_return_15m"
    ) -> dict:
        """Analyze forward returns grouped by GEX change direction."""
        groups = {"INCREASING": [], "DECREASING": [], "FLAT": []}
        for point in series:
            ret = getattr(point, field_name, None)
            if ret is None or point.gex_change is None:
                continue
            if point.gex_change > 0:
                groups["INCREASING"].append(ret)
            elif point.gex_change < 0:
                groups["DECREASING"].append(ret)
            else:
                groups["FLAT"].append(ret)

        return {k: self.compute_stats(v) for k, v in groups.items() if v}
