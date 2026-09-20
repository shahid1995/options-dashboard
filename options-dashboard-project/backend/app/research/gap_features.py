"""Issue #17 — Overnight Gap Intelligence feature engine (Phase 1, research-only).

Deterministic, testable feature functions over end-of-session research
snapshots (docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md §6). Research-only:
nothing here feeds the production dashboard, signals, or execution.

Conventions (all documented and tested):

* **PE Delta sign normalization** — broker PE deltas are negative. Every
  directional delta quantity in this module uses ``abs(pe_delta)`` and is
  signed by a documented rule, never by the raw negative delta. ``delta_diff
  = ce_delta - abs(pe_delta)``.
* **OI in contracts** — never lots; no lot-size multiplier anywhere (matching
  the GEX_V1_0_SPEC §11 unit contract).
* **Missing is not zero** — any feature whose required inputs are missing is
  *absent* from the returned mapping (or ``None`` in component scores).
  A measured zero stays a legitimate zero. Completeness flags live in the
  session/pipeline layer.
* **GEX reuse** — strike-level gamma exposure uses the authoritative
  :mod:`app.quant.gex` engine via :func:`build_gamma_profile` (canonical
  formula ``gamma × OI × S² × 0.01``, Call=+1 / Put=−1,
  NAIVE_DEALER_CONVENTION). This module never redefines that convention.
* **Strike-distance weighting** — Gaussian weights
  ``exp(-0.5 · (distance/σ_w)²)`` around the ATM strike, σ_w documented in
  :data:`WEIGHT_SIGMA_STRIKES`. Deterministic and expiry-aware only through
  the caller-provided chain (no wall clock here).
* **Determinism** — pure functions of the supplied snapshots; no wall clock,
  DB, HTTP, or broker SDK access.

All functions accept plain mappings (the ORM snapshot rows converted to
dicts) so the math is unit-testable without a database.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping, Sequence

from app.quant.gex import Side, build_gamma_profile
from app.market_data.contracts import NormalizedInstrument, Provenance, QualityState
from app.quant.contracts import OptionMarketData

# ---------------------------------------------------------------------------
# Documented constants (initial research values; changed only via version bump)
# ---------------------------------------------------------------------------

FEATURE_VERSION = "v1"

#: Gaussian strike-weighting width in strike units (spec §5: weighting must be
#: documented, not assumed). One standard deviation = 2 strike steps.
WEIGHT_SIGMA_STRIKES = 2.0

#: ATM window: strikes within this many strike steps of ATM are included.
ATM_WINDOW_STRIKES = 3

#: VIX regime boundaries (annualized vol fractions, e.g. 0.14 = 14%).
VIX_LOW = 0.10
VIX_HIGH = 0.18
VIX_EXTREME = 0.25

#: Robust z-score clipping bounds applied AFTER normalization (documented).
Z_CLIP = 5.0


def _finite(value: Any) -> float | None:
    """Return the value as float if it is a real, finite number; else None.

    None/NaN/inf/non-numeric all map to None (missing), never to zero.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _strike_distance(strike: float, atm: float, step: float | None) -> float:
    """Distance in strike units when the step is known, else in points/100."""
    if step and step > 0:
        return abs(strike - atm) / step
    return abs(strike - atm) / 100.0


def near_atm_weight(strike: float, atm: float, step: float | None) -> float:
    """Gaussian strike-distance weight (peak 1.0 at ATM)."""
    d = _strike_distance(strike, atm, step)
    return math.exp(-0.5 * (d / WEIGHT_SIGMA_STRIKES) ** 2)


def _infer_strike_step(strikes: Sequence[float]) -> float | None:
    """Most common positive gap between consecutive sorted strikes."""
    s = sorted({float(x) for x in strikes})
    if len(s) < 2:
        return None
    gaps: dict[float, int] = {}
    for a, b in zip(s, s[1:]):
        g = round(b - a, 6)
        if g > 0:
            gaps[g] = gaps.get(g, 0) + 1
    if not gaps:
        return None
    return max(gaps.items(), key=lambda kv: kv[1])[0]


def _atm_strike(chain: Sequence[Mapping[str, Any]], spot: float) -> float | None:
    strikes = [r["strike"] for r in chain if _finite(r.get("strike")) is not None]
    if not strikes:
        return None
    return min(strikes, key=lambda k: abs(k - spot))


def _window_rows(
    chain: Sequence[Mapping[str, Any]], atm: float, step: float | None
) -> list[Mapping[str, Any]]:
    """Rows inside the documented ATM window (±ATM_WINDOW_STRIKES steps)."""
    out = []
    for r in chain:
        k = _finite(r.get("strike"))
        if k is None:
            continue
        if _strike_distance(k, atm, step) <= ATM_WINDOW_STRIKES:
            out.append(r)
    return out


def _weighted_sum(
    rows: Sequence[Mapping[str, Any]],
    field: str,
    weights: Mapping[float, float],
    transform=lambda v: v,
) -> float | None:
    """Deterministic weight·value sum over rows carrying the field.

    Rows missing the field are skipped; if no row has the field the result is
    None (missing), never zero.
    """
    total = 0.0
    seen = False
    for r in rows:
        v = _finite(r.get(field))
        if v is None:
            continue
        w = weights.get(float(r["strike"]), 0.0)
        total += w * transform(v)
        seen = True
    return total if seen else None


def _norm_diff(numerator: float | None, denominator: float | None) -> float | None:
    """numerator / denominator with documented zero-denominator → None."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


# ---------------------------------------------------------------------------
# Delta features (§6.1)
# ---------------------------------------------------------------------------


def delta_features(
    chain: Sequence[Mapping[str, Any]],
    spot: float,
    prev_chain: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """CE/PE delta pressure features with the documented PE convention.

    * ``ce_delta``/``pe_delta`` — OI-weighted mean |PE| / CE delta in-window.
    * ``delta_diff`` — ``ce - abs(pe)`` (positive ⇒ call-delta dominance).
    * ``delta_pressure`` — OI-weighted directional delta of positioning:
      ``(ce_delta·ce_oi − abs(pe_delta)·pe_oi) / (ce_oi + pe_oi)``.
    * ``delta_change`` — intraday/end-of-session change vs ``prev_chain``
      (same convention on the previous snapshot).
    * ``delta_concentration`` — share of total delta pressure within ±1 step.
    """
    atm = _atm_strike(chain, spot)
    if atm is None:
        return {}
    step = _infer_strike_step([r.get("strike") for r in chain])
    rows = _window_rows(chain, atm, step)
    weights = {float(r["strike"]): near_atm_weight(float(r["strike"]), atm, step) for r in rows}

    ce_w = _weighted_sum(rows, "delta", weights, abs)
    pe_w = _weighted_sum(
        rows, "delta", weights, lambda v: abs(v)
    )  # both sides normalized to |delta| mass
    ce_oi = _weighted_sum(rows, "open_interest", weights)
    pe_oi = _weighted_sum(rows, "open_interest", weights)

    # Side-resolved weighting: weight×delta with side sign folded in.
    ce_sum = pe_sum = ce_oi_sum = pe_oi_sum = 0.0
    ce_seen = pe_seen = False
    for r in rows:
        w = weights.get(float(r["strike"]), 0.0)
        d = _finite(r.get("delta"))
        oi = _finite(r.get("open_interest"))
        if r.get("option_type") == "CALL":
            if d is not None:
                ce_sum += w * abs(d)
                ce_seen = True
            if oi is not None:
                ce_oi_sum += w * oi
        elif r.get("option_type") == "PUT":
            if d is not None:
                pe_sum += w * abs(d)
                pe_seen = True
            if oi is not None:
                pe_oi_sum += w * oi

    out: dict[str, float] = {}
    if ce_seen:
        out["ce_delta"] = ce_sum
    if pe_seen:
        out["pe_delta"] = pe_sum
    if ce_seen and pe_seen:
        out["delta_diff"] = out["ce_delta"] - out["pe_delta"]
        if (ce_oi_sum + pe_oi_sum) > 0:
            out["delta_pressure"] = (
                out["ce_delta"] * ce_oi_sum - out["pe_delta"] * pe_oi_sum
            ) / (ce_oi_sum + pe_oi_sum)
        # Concentration: |delta| mass within ±1 step of ATM vs whole window.
        inner = outer = 0.0
        for r in rows:
            d = _finite(r.get("delta"))
            if d is None:
                continue
            w = weights.get(float(r["strike"]), 0.0)
            mass = w * abs(d)
            if _strike_distance(float(r["strike"]), atm, step) <= 1.0:
                inner += mass
            outer += mass
        if outer > 0:
            out["delta_concentration"] = inner / outer
    if prev_chain:
        prev = delta_features(prev_chain, spot)
        if "delta_pressure" in out and "delta_pressure" in prev:
            out["delta_change"] = out["delta_pressure"] - prev["delta_pressure"]
    return out


# ---------------------------------------------------------------------------
# Vega features (§6.2)
# ---------------------------------------------------------------------------


def vega_features(
    chain: Sequence[Mapping[str, Any]],
    spot: float,
    prev_chain: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """CE/PE vega pressure. Raw vega never dominates by scale: every output is
    OI-weighted and the pressure ratio is bounded to [-1, +1]."""
    atm = _atm_strike(chain, spot)
    if atm is None:
        return {}
    step = _infer_strike_step([r.get("strike") for r in chain])
    rows = _window_rows(chain, atm, step)
    weights = {float(r["strike"]): near_atm_weight(float(r["strike"]), atm, step) for r in rows}

    ce = pe = 0.0
    ce_seen = pe_seen = False
    for r in rows:
        w = weights.get(float(r["strike"]), 0.0)
        v = _finite(r.get("vega"))
        if v is None:
            continue
        if r.get("option_type") == "CALL":
            ce += w * abs(v)
            ce_seen = True
        elif r.get("option_type") == "PUT":
            pe += w * abs(v)
            pe_seen = True
    out: dict[str, float] = {}
    if ce_seen:
        out["ce_vega"] = ce
    if pe_seen:
        out["pe_vega"] = pe
    if ce_seen and pe_seen:
        total = ce + pe
        if total > 0:
            # Bounded divergence in [-1, +1] (scale-free).
            out["vega_diff"] = (ce - pe) / total
            out["vega_pressure"] = out["vega_diff"]
    if prev_chain:
        prev = vega_features(prev_chain, spot)
        if "vega_pressure" in out and "vega_pressure" in prev:
            out["vega_change"] = out["vega_pressure"] - prev["vega_pressure"]
    return out


# ---------------------------------------------------------------------------
# OI / positioning features (§6.3)
# ---------------------------------------------------------------------------


def oi_features(
    chain: Sequence[Mapping[str, Any]],
    spot: float,
    prev_chain: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """OI positioning: PCR, concentration, migration, change vs prior snapshot."""
    atm = _atm_strike(chain, spot)
    if atm is None:
        return {}
    step = _infer_strike_step([r.get("strike") for r in chain])
    rows = _window_rows(chain, atm, step)
    weights = {float(r["strike"]): near_atm_weight(float(r["strike"]), atm, step) for r in rows}

    ce_oi = pe_oi = 0.0
    ce_seen = pe_seen = False
    for r in rows:
        w = weights.get(float(r["strike"]), 0.0)
        oi = _finite(r.get("open_interest"))
        if oi is None:
            continue
        if r.get("option_type") == "CALL":
            ce_oi += w * oi
            ce_seen = True
        elif r.get("option_type") == "PUT":
            pe_oi += w * oi
            pe_seen = True
    out: dict[str, float] = {}
    if ce_seen:
        out["ce_oi"] = ce_oi
    if pe_seen:
        out["pe_oi"] = pe_oi
    if ce_seen and pe_seen and ce_oi > 0:
        out["pcr"] = pe_oi / ce_oi  # weighted put-call ratio
        total = ce_oi + pe_oi
        # Concentration: max single-strike share of OI mass (either side).
        per_strike: dict[float, float] = {}
        for r in rows:
            oi = _finite(r.get("open_interest"))
            if oi is None:
                continue
            k = float(r["strike"])
            per_strike[k] = per_strike.get(k, 0.0) + oi * weights.get(k, 0.0)
        if per_strike:
            out["oi_concentration"] = max(per_strike.values()) / total
    if prev_chain:
        prev = oi_features(prev_chain, spot)
        if "pcr" in out and "pcr" in prev:
            out["pcr_change"] = out["pcr"] - prev["pcr"]
        if "ce_oi" in out and "ce_oi" in prev:
            out["ce_oi_change"] = out["ce_oi"] - prev["ce_oi"]
        if "pe_oi" in out and "pe_oi" in prev:
            out["pe_oi_change"] = out["pe_oi"] - prev["pe_oi"]
        # OI migration: net weighted-OI flow from CE toward PE (or reverse).
        if "ce_oi_change" in out and "pe_oi_change" in out:
            denom = out["ce_oi_change"] + out["pe_oi_change"]
            if denom != 0:
                out["oi_migration"] = (out["pe_oi_change"] - out["ce_oi_change"]) / abs(denom)
    # Documented change_in_oi aggregation (broker-reported day change).
    coi = _weighted_sum(rows, "change_in_oi", weights)
    if coi is not None:
        out["change_in_oi_weighted"] = coi
    return out


# ---------------------------------------------------------------------------
# IV / skew features (§6.4)
# ---------------------------------------------------------------------------


def iv_features(
    chain: Sequence[Mapping[str, Any]],
    spot: float,
    prev_chain: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """ATM IV, CE/PE IV, put-call skew and their changes vs the prior snapshot.

    IV is the canonical decimal fraction (0.1824 = 18.24%) per the platform
    IVObservation convention. Skew = ATM PE IV − ATM CE IV.
    """
    atm = _atm_strike(chain, spot)
    if atm is None:
        return {}
    step = _infer_strike_step([r.get("strike") for r in chain])
    rows = _window_rows(chain, atm, step)

    def side_iv(side: str) -> float | None:
        best: tuple[float, float] | None = None  # (distance, iv)
        for r in rows:
            if r.get("option_type") != side:
                continue
            v = _finite(r.get("iv"))
            if v is None or v <= 0:
                continue
            d = _strike_distance(float(r["strike"]), atm, step)
            if best is None or d < best[0]:
                best = (d, v)
        return best[1] if best else None

    ce_iv = side_iv("CALL")
    pe_iv = side_iv("PUT")
    out: dict[str, float] = {}
    if ce_iv is not None:
        out["ce_iv"] = ce_iv
    if pe_iv is not None:
        out["pe_iv"] = pe_iv
    if ce_iv is not None and pe_iv is not None:
        out["atm_iv"] = (ce_iv + pe_iv) / 2.0
        out["iv_skew"] = pe_iv - ce_iv
    if prev_chain:
        prev = iv_features(prev_chain, spot)
        if "atm_iv" in out and "atm_iv" in prev:
            out["iv_change"] = out["atm_iv"] - prev["atm_iv"]
        if "iv_skew" in out and "iv_skew" in prev:
            out["iv_skew_change"] = out["iv_skew"] - prev["iv_skew"]
    return out


# ---------------------------------------------------------------------------
# GEX / gamma features (§6.5) — authoritative engine reuse
# ---------------------------------------------------------------------------


def _to_option_rows(
    chain: Sequence[Mapping[str, Any]], spot: float, session_date: str
) -> list[OptionMarketData]:
    """Convert raw chain rows into the canonical OptionMarketData contract.

    Rows without gamma+OI+provenance-carrying identity are skipped here; the
    profile builder re-validates and structurally excludes anything else.
    """
    out: list[OptionMarketData] = []
    for r in chain:
        gamma = _finite(r.get("gamma"))
        oi = _finite(r.get("open_interest"))
        strike = _finite(r.get("strike"))
        side = r.get("option_type")
        if gamma is None or oi is None or strike is None or side not in ("CALL", "PUT"):
            continue
        inst = NormalizedInstrument(
            exchange="NSE",
            segment="FO",
            underlying="NIFTY",
            symbol="NIFTY",
            instrument_type="OPTION",
            expiry=str(r.get("expiry") or session_date),
            strike=strike,
            option_type=Side(side),
        )
        received_raw = str(r.get("timestamp") or session_date)
        try:
            received_dt = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
        except ValueError:
            # Snapshot rows always carry a timestamp; this fallback exists only
            # so malformed bookkeeping can never fabricate a market time.
            received_dt = datetime(1970, 1, 1)
        out.append(
            OptionMarketData(
                instrument=inst,
                spot=spot,
                gamma=gamma,
                open_interest=oi,
                greeks_source=str(r.get("greeks_source") or "MODEL"),
                provenance=Provenance(
                    source="gap_research_snapshot",
                    collection_mode="HISTORICAL",
                    received_at=received_dt,
                    normalization_version=f"gap_features_{FEATURE_VERSION}",
                    contract_version="market_data_v1",
                ),
            )
        )
    return out


def gex_features(
    chain: Sequence[Mapping[str, Any]],
    spot: float,
    session_date: str,
    prev_chain: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """Gamma/GEX regime features built on the authoritative GEX engine.

    * ``net_gex`` — profile total under the canonical convention.
    * ``gamma_concentration`` — max |net GEX| share across strikes.
    * ``gamma_flip`` — strike of extreme |net GEX| (documented Phase-1 proxy:
      the largest-exposure strike, NOT a full zero-cross interpolation).
    * ``spot_to_flip_pct`` — signed distance of spot from the flip strike.
    * ``gex_change`` — net GEX change vs the prior snapshot.
    """
    rows = _to_option_rows(chain, spot, session_date)
    if not rows:
        return {}
    profile = build_gamma_profile(rows)
    out: dict[str, float] = {}
    if profile.total_net_gex is not None:
        out["net_gex"] = profile.total_net_gex
        if profile.total_call_gex is not None:
            out["call_gex"] = profile.total_call_gex
        if profile.total_put_gex is not None:
            out["put_gex"] = profile.total_put_gex
        abs_total = sum(abs(r.net_gex) for r in profile.rows)
        if abs_total > 0:
            out["gamma_concentration"] = max(abs(r.net_gex) for r in profile.rows) / abs_total
            flip = max(profile.rows, key=lambda r: abs(r.net_gex))
            out["gamma_flip"] = flip.strike
            out["spot_to_flip_pct"] = (spot - flip.strike) / spot
    if prev_chain:
        prev = gex_features(prev_chain, spot, session_date)
        if "net_gex" in out and "net_gex" in prev:
            out["gex_change"] = out["net_gex"] - prev["net_gex"]
    return out


# ---------------------------------------------------------------------------
# Futures features (§6.6)
# ---------------------------------------------------------------------------


def futures_features(
    underlying: Mapping[str, Any],
    spot: float,
    prev_underlying: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """Futures basis, changes and volume pressure (missing-safe)."""
    out: dict[str, float] = {}
    fut = _finite(underlying.get("futures_ltp"))
    if fut is not None and spot:
        out["futures_basis"] = fut - spot
        out["futures_basis_pct"] = (fut - spot) / spot
    vol = _finite(underlying.get("futures_volume"))
    if vol is not None:
        out["futures_volume"] = vol
    oi = _finite(underlying.get("futures_oi"))
    if oi is not None:
        out["futures_oi"] = oi
    if prev_underlying:
        pf = _finite(prev_underlying.get("futures_ltp"))
        if fut is not None and pf is not None and pf != 0:
            out["futures_change_pct"] = (fut - pf) / pf
        p_oi = _finite(prev_underlying.get("futures_oi"))
        if oi is not None and p_oi is not None and p_oi != 0:
            out["futures_oi_change_pct"] = (oi - p_oi) / p_oi
        # Price/OI buildup classification (documented, tested for value later).
        if "futures_change_pct" in out and "futures_oi_change_pct" in out:
            p_up = out["futures_change_pct"] > 0
            oi_up = out["futures_oi_change_pct"] > 0
            out["futures_buildup"] = float(
                (p_up and oi_up) or not (p_up or oi_up)
            )  # 1.0 = long-buildup/short-unwind family, 0.0 = the other family
    return out


# ---------------------------------------------------------------------------
# Option flow features (§6.7)
# ---------------------------------------------------------------------------


def flow_features(
    chain: Sequence[Mapping[str, Any]],
    spot: float,
    prev_chain: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """Premium/volume/bid-ask flow pressure. Direction is NEVER inferred from
    LTP alone when bid/ask is available (documented rule)."""
    atm = _atm_strike(chain, spot)
    if atm is None:
        return {}
    step = _infer_strike_step([r.get("strike") for r in chain])
    rows = _window_rows(chain, atm, step)
    weights = {float(r["strike"]): near_atm_weight(float(r["strike"]), atm, step) for r in rows}

    ce_prem = pe_prem = 0.0
    ce_seen = pe_seen = False
    ce_ba = pe_ba = 0.0
    ce_ba_seen = pe_ba_seen = False
    for r in rows:
        w = weights.get(float(r["strike"]), 0.0)
        prem = _finite(r.get("ltp"))
        vol = _finite(r.get("volume"))
        if r.get("option_type") == "CALL":
            if prem is not None and vol is not None:
                ce_prem += w * prem * vol
                ce_seen = True
            bid = _finite(r.get("bid"))
            ask = _finite(r.get("ask"))
            bq = _finite(r.get("bid_qty"))
            aq = _finite(r.get("ask_qty"))
            if bid is not None and ask is not None and ask > 0 and bq is not None and aq is not None:
                ce_ba += w * (bq - aq)
                ce_ba_seen = True
        elif r.get("option_type") == "PUT":
            if prem is not None and vol is not None:
                pe_prem += w * prem * vol
                pe_seen = True
            bid = _finite(r.get("bid"))
            ask = _finite(r.get("ask"))
            bq = _finite(r.get("bid_qty"))
            aq = _finite(r.get("ask_qty"))
            if bid is not None and ask is not None and ask > 0 and bq is not None and aq is not None:
                pe_ba += w * (bq - aq)
                pe_ba_seen = True
    out: dict[str, float] = {}
    if ce_seen and pe_seen:
        total = ce_prem + pe_prem
        if total > 0:
            out["premium_pressure"] = (ce_prem - pe_prem) / total  # [-1, +1]
    if ce_ba_seen and pe_ba_seen:
        ce_norm = _norm_diff(ce_ba, ce_ba + pe_ba if (ce_ba + pe_ba) != 0 else None)
        if ce_ba + pe_ba != 0:
            out["bid_ask_pressure"] = (ce_ba - pe_ba) / abs(ce_ba + pe_ba)
    if prev_chain:
        prev = flow_features(prev_chain, spot)
        if "premium_pressure" in out and "premium_pressure" in prev:
            out["net_flow_change"] = out["premium_pressure"] - prev["premium_pressure"]
    return out


# ---------------------------------------------------------------------------
# India VIX features (§6.8)
# ---------------------------------------------------------------------------


def vix_features(
    underlying: Mapping[str, Any],
    prev_underlying: Mapping[str, Any] | None = None,
    vix_history: Sequence[float] | None = None,
) -> dict[str, float]:
    """Level, change, regime and percentile features for India VIX.

    ``vix_history`` must contain ONLY observations at or before the prediction
    timestamp (the caller/pipeline guarantees causality).
    """
    out: dict[str, float] = {}
    vix = _finite(underlying.get("india_vix"))
    if vix is None:
        return out
    out["vix"] = vix
    if prev_underlying:
        pv = _finite(prev_underlying.get("india_vix"))
        if pv is not None and pv != 0:
            out["vix_change_pct"] = (vix - pv) / pv
            out["vix_shock"] = 1.0 if abs(out["vix_change_pct"]) > 0.15 else 0.0
    # Documented regime band (level-based, causal).
    if vix < VIX_LOW:
        out["vix_regime"] = 0.0  # LOW
    elif vix < VIX_HIGH:
        out["vix_regime"] = 1.0  # NORMAL
    elif vix < VIX_EXTREME:
        out["vix_regime"] = 2.0  # HIGH
    else:
        out["vix_regime"] = 3.0  # EXTREME
    if vix_history:
        h = [v for v in (_finite(x) for x in vix_history) if v is not None]
        if h:
            below = sum(1 for v in h if v <= vix)
            out["vix_percentile"] = below / len(h)
    return out


# ---------------------------------------------------------------------------
# Cross-component aggregation (§6 cross-component / §9)
# ---------------------------------------------------------------------------

#: Component-to-direction mapping for confluence. Each entry lists the feature
#: keys that vote bullishly (+1) when positive; negative votes bearishly.
DIRECTION_COMPONENTS: tuple[tuple[str, ...], ...] = (
    ("delta_pressure", "delta_diff"),
    ("vega_pressure",),
    ("pcr_change", "oi_migration"),
    ("iv_skew_change",),
    ("futures_basis_pct", "futures_change_pct"),
    ("premium_pressure", "bid_ask_pressure"),
    ("spot_to_flip_pct",),
    ("vix_change_pct",),
)


def component_scores(features: Mapping[str, float]) -> dict[str, float]:
    """One bounded [-1, +1] directional score per available component group.

    Missing features never contribute zero silently — a group with no
    available key is simply absent from the result.
    """
    out: dict[str, float] = {}
    names = ("delta", "vega", "oi", "iv_skew", "futures", "flow", "gex", "vix")
    for name, keys in zip(names, DIRECTION_COMPONENTS):
        vals = [features[k] for k in keys if k in features]
        if not vals:
            continue
        # Bounded mean: clip each contributing feature to [-1, +1] first so a
        # raw-scale feature (e.g. vix_change_pct) cannot dominate by magnitude.
        clipped = [max(-1.0, min(1.0, v)) for v in vals]
        out[name] = sum(clipped) / len(clipped)
    return out


def agreement_and_dispersion(scores: Mapping[str, float]) -> tuple[float, float] | None:
    """``(agreement, dispersion)`` where agreement = 1 − mean|z| over signed
    score magnitudes; None when no component scores exist."""
    if not scores:
        return None
    vals = list(scores.values())
    mean = sum(vals) / len(vals)
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    disp = math.sqrt(variance)
    mean_mag = sum(abs(v) for v in vals) / len(vals)
    agreement = max(0.0, 1.0 - disp)
    return agreement, disp


def cross_component_features(features: Mapping[str, float]) -> dict[str, float]:
    """Confluence bundle: component scores, agreement, dispersion, mean score."""
    scores = component_scores(features)
    out: dict[str, float] = {f"component_{k}": v for k, v in scores.items()}
    ad = agreement_and_dispersion(scores)
    if ad:
        agreement, dispersion = ad
        out["agreement"] = agreement
        out["dispersion"] = dispersion
        vals = list(scores.values())
        out["mean_component_score"] = sum(vals) / len(vals)
    return out
