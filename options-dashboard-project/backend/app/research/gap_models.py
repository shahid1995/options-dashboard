"""Issue #17 — transparent research models (Phase 1): baselines, POS-style, SOS.

Implements docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md §3, §8, §9, §10, §12.
Research-only; no production UI or signal consumes this module.

All three models are deterministic and explainable: every prediction carries
its per-component scores, so any historical observation can be audited.

* **Model A (baselines)** — previous-day direction, previous-day gap
  direction, futures basis, ATM straddle implied move, and the historical
  unconditional distribution (P(UP)/P(FLAT)/P(DOWN) from prior sessions).
* **Model B (POS-style)** — transparent public-information approximation of
  the benchmark described in §8. It does NOT reproduce Vibhore Gupta's
  proprietary POS implementation and claims no fidelity to it.
* **Model C (SOS)** — nine-component directional model with first-class
  direction/agreement/dispersion/confidence and a NO_EDGE state (§9, §12).

Normalization contract (§7): models may consume only causal normalized
features (see :mod:`app.research.gap_normalization`); they never see targets.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Model A — baselines
# ---------------------------------------------------------------------------


def baseline_previous_day_direction(features: Mapping[str, Any]) -> float | None:
    """Previous-day direction: sign of the last session's close-to-close move."""
    v = features.get("prev_day_change_pct")
    if v is None:
        return None
    return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)


def baseline_previous_gap_direction(features: Mapping[str, Any]) -> float | None:
    """Gap direction persistence: sign of the previous session's opening gap."""
    v = features.get("prev_gap_pct")
    if v is None:
        return None
    return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)


def baseline_futures_direction(features: Mapping[str, Any]) -> float | None:
    """Futures basis / price direction at the cutoff."""
    for key in ("futures_change_pct", "futures_basis_pct"):
        v = features.get(key)
        if v is not None:
            return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)
    return None


def straddle_implied_move(
    atm_iv: float | None,
    spot: float,
    sessions_to_expiry: float = 1.0,
) -> float | None:
    """ATM straddle-implied next-session movement (points).

    Uses the standard approximations: straddle ≈ 0.8·S·σ·√T (implied move
    over horizon T in years); ``sessions_to_expiry=1`` converts the annual
    figure to one session with √(1/252) convention. Documented approximation.
    """
    if atm_iv is None or not spot or spot <= 0:
        return None
    implied_annual = 0.8 * spot * atm_iv
    return implied_annual * math.sqrt(sessions_to_expiry / 252.0)


def baseline_unconditional_distribution(
    gap_classes: Sequence[str | None],
) -> dict[str, float] | None:
    """Historical unconditional P(GAP_UP)/P(FLAT)/P(GAP_DOWN) from prior sessions.

    ``gap_classes`` must contain only prior sessions (causality contract).
    """
    known = [g for g in gap_classes if g in ("GAP_UP", "FLAT", "GAP_DOWN")]
    if not known:
        return None
    n = len(known)
    return {
        "p_up": known.count("GAP_UP") / n,
        "p_flat": known.count("FLAT") / n,
        "p_down": known.count("GAP_DOWN") / n,
        "n": float(n),
    }


def baseline_expected_gap(history_gaps: Sequence[float | None]) -> float | None:
    """Expected signed gap from the historical distribution (causal history)."""
    clean = [g for g in (float(x) for x in history_gaps if x is not None)]
    if not clean:
        return None
    return sum(clean) / len(clean)


# ---------------------------------------------------------------------------
# Model B — POS-style benchmark (public-information approximation)
# ---------------------------------------------------------------------------

#: Explicit, fixed initial weights (spec §8: documented and fixed for the
#: first benchmark; optimization only inside training periods later).
POS_WEIGHTS: dict[str, float] = {
    "delta": 0.4,
    "vega": 0.2,
    "oi": 0.3,
    "price": 0.1,
}

POS_DISCLAIMER = (
    "This is an approximation for research comparison and does not reproduce "
    "or claim to reproduce Vibhore Gupta's proprietary POS implementation."
)


def pos_style_features(features: Mapping[str, Any]) -> dict[str, float] | None:
    """The four POS-style component inputs (missing-safe).

    * ``delta`` — delta pressure (directional, OI-weighted).
    * ``vega`` — CE/PE vega divergence.
    * ``oi`` — positioning pressure from PCR change / OI migration.
    * ``price`` — option-price (premium-flow) pressure.
    """
    out: dict[str, float] = {}
    if features.get("delta_pressure") is not None:
        out["delta"] = float(features["delta_pressure"])
    elif features.get("delta_diff") is not None:
        out["delta"] = float(features["delta_diff"])
    if features.get("vega_pressure") is not None:
        out["vega"] = float(features["vega_pressure"])
    for key in ("pcr_change", "oi_migration"):
        if features.get(key) is not None:
            out["oi"] = float(features[key])
            break
    for key in ("premium_pressure", "bid_ask_pressure"):
        if features.get(key) is not None:
            out["price"] = float(features[key])
            break
    return out or None


def pos_style_score(features: Mapping[str, Any]) -> dict[str, Any] | None:
    """Transparent POS-style score in [-100, +100] with fixed weights.

    Each component is bounded to [-1, +1] before weighting; components with
    missing inputs are dropped and the remaining weights renormalized. If no
    component is available the result is ``None`` (missing, never zero).
    """
    comps = pos_style_features(features)
    if not comps:
        return None
    used: dict[str, float] = {}
    weight_sum = 0.0
    for name, weight in POS_WEIGHTS.items():
        if name in comps:
            used[name] = max(-1.0, min(1.0, comps[name]))
            weight_sum += weight
    if weight_sum == 0:
        return None
    score = sum(POS_WEIGHTS[n] * v for n, v in used.items()) / weight_sum
    agreement_terms = [v for v in used.values()]
    disp = (
        math.sqrt(sum((v - score) ** 2 for v in agreement_terms) / len(agreement_terms))
        if agreement_terms
        else 0.0
    )
    return {
        "direction_score": score * 100.0,  # [-100, +100] per spec §8
        "component_scores": used,
        "weights_used": {n: POS_WEIGHTS[n] for n in used},
        "dispersion": disp,
        "disclaimer": POS_DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Model C — StrikeNova SOS
# ---------------------------------------------------------------------------

#: SOS component weights (documented initial values; §9).
SOS_WEIGHTS: dict[str, float] = {
    "delta": 0.18,
    "vega": 0.10,
    "oi": 0.16,
    "iv_skew": 0.10,
    "futures": 0.14,
    "flow": 0.10,
    "gex": 0.10,
    "vix": 0.06,
    "price_structure": 0.06,
}

#: NO_EDGE thresholds (spec §9/§12): weak direction or high dispersion ⇒ abstain.
SOS_MIN_DIRECTION = 0.12
SOS_MAX_DISPERSION = 0.45
SOS_MIN_AGREEMENT = 0.55

SOS_STATE_NO_EDGE = "NO_EDGE"
SOS_STATE_PREDICTED = "PREDICTED"


def _sign(v: float) -> float:
    return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)


def _sosi_map(value: float) -> float:
    """Map a z-like input to [-1, +1] via x/sqrt(1+x²) (documented squashing)."""
    return value / math.sqrt(1.0 + value * value)


def sos_components(
    normalized_features: Mapping[str, float | None],
    raw_features: Mapping[str, float | None],
) -> dict[str, float]:
    """Nine bounded [-1, +1] component scores (missing components absent).

    Uses causal normalized features where scale matters; falls back to
    documented deterministic transforms of raw features otherwise.
    """
    comps: dict[str, float] = {}

    def z_or(key: str) -> float | None:
        v = normalized_features.get(key)
        if v is not None:
            return _sosi_map(float(v))
        r = raw_features.get(key)
        if r is not None:
            return max(-1.0, min(1.0, float(r)))
        return None

    v = z_or("delta_pressure_z")
    if v is not None:
        comps["delta"] = v
    v = z_or("vega_pressure_z")
    if v is not None:
        comps["vega"] = v
    v = z_or("pcr_change_z")
    if v is not None:
        comps["oi"] = v
    elif raw_features.get("oi_migration") is not None:
        comps["oi"] = max(-1.0, min(1.0, float(raw_features["oi_migration"])))
    v = z_or("iv_skew_change_z")
    if v is not None:
        comps["iv_skew"] = v
    v = z_or("futures_basis_pct_z")
    if v is not None:
        comps["futures"] = v
    elif raw_features.get("futures_change_pct") is not None:
        comps["futures"] = max(-1.0, min(1.0, 250.0 * float(raw_features["futures_change_pct"])))
    v = z_or("premium_pressure_z")
    if v is not None:
        comps["flow"] = v
    elif raw_features.get("premium_pressure") is not None:
        comps["flow"] = max(-1.0, min(1.0, float(raw_features["premium_pressure"])))
    v = z_or("spot_to_flip_pct_z")
    if v is not None:
        comps["gex"] = v
    elif raw_features.get("spot_to_flip_pct") is not None:
        comps["gex"] = max(-1.0, min(1.0, 250.0 * float(raw_features["spot_to_flip_pct"])))
    v = z_or("vix_change_pct_z")
    if v is not None:
        comps["vix"] = v
    v = z_or("prev_day_change_pct_z")
    if v is not None:
        comps["price_structure"] = v
    elif raw_features.get("prev_day_change_pct") is not None:
        comps["price_structure"] = max(-1.0, min(1.0, 100.0 * float(raw_features["prev_day_change_pct"])))
    return comps


def sos_predict(
    normalized_features: Mapping[str, float | None],
    raw_features: Mapping[str, float | None],
) -> dict[str, Any] | None:
    """SOS prediction with direction/agreement/dispersion/confidence + NO_EDGE.

    Weights are renormalized over the *available* components so partial data
    degrades gracefully; a NO_EDGE state is returned (never a forced
    directional value) when direction is weak or dispersion is high. Returns
    ``None`` only when no component at all is available.
    """
    comps = sos_components(normalized_features, raw_features)
    if not comps:
        return None
    weight_sum = sum(SOS_WEIGHTS[k] for k in comps)
    if weight_sum == 0:
        return None
    direction = sum(SOS_WEIGHTS[k] * comps[k] for k in comps) / weight_sum
    vals = list(comps.values())
    mean = sum(vals) / len(vals)
    dispersion = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    agreement = max(0.0, 1.0 - dispersion)
    # Confidence: direction conviction × agreement, both bounded [0, 1].
    confidence = max(0.0, min(1.0, abs(direction) * agreement))
    state = SOS_STATE_PREDICTED
    if abs(direction) < SOS_MIN_DIRECTION or dispersion > SOS_MAX_DISPERSION or agreement < SOS_MIN_AGREEMENT:
        state = SOS_STATE_NO_EDGE
    return {
        "direction_score": direction,          # [-1, +1]
        "agreement_score": agreement,          # [0, 1]
        "dispersion": dispersion,              # >= 0
        "confidence": confidence,              # [0, 1]
        "state": state,
        "component_scores": comps,
        "weights_used": {k: SOS_WEIGHTS[k] for k in comps},
    }


# ---------------------------------------------------------------------------
# §10/§11 — probability and implied-move outputs
# ---------------------------------------------------------------------------


def probabilities_from_score(
    direction: float | None,
    vix_regime: float | None = None,
    flat_band_pct: float = 0.001,
    typical_gap_pct: float | None = None,
) -> dict[str, float] | None:
    """Transparent class probabilities from the signed direction score.

    Mechanism (documented, deliberately simple for Phase 1): the direction
    score tilts the *observed historical* class distribution of the matching
    VIX regime, keeping the class prior intact rather than fabricating a
    calibrated probability. ``typical_gap_pct`` is the causal mean |gap_pct|;
    the FLAT band uses the same neutral band as target generation.
    """
    if direction is None or typical_gap_pct is None:
        return None
    tilt = max(-1.0, min(1.0, direction))
    # Neutral-band width relative to the typical gap magnitude controls the
    # FLAT mass; regimes with larger typical gaps get proportionally less FLAT.
    band = max(0.05, min(0.9, (flat_band_pct / typical_gap_pct) if typical_gap_pct > 0 else 0.5))
    base_flat = band
    remaining = 1.0 - base_flat
    up = remaining * (1.0 + tilt) / 2.0
    down = remaining * (1.0 - tilt) / 2.0
    return {"p_up": up, "p_flat": base_flat, "p_down": down}


def tail_probabilities(
    expected_gap_points: float | None,
    typical_gap_points: float | None,
    thresholds: Sequence[float] = (-100.0, -50.0, 50.0, 100.0),
) -> dict[str, float] | None:
    """Laplace-tail estimates P(gap ≥ t) for configured point thresholds.

    Transparent Phase-1 mechanism: gaps are modeled Laplace around the
    expected gap with scale = causal mean |gap|. Not a calibration claim.
    """
    if expected_gap_points is None or typical_gap_points is None or typical_gap_points <= 0:
        return None
    b = typical_gap_points
    out: dict[str, float] = {}
    for t in thresholds:
        if t >= 0:
            out[f"p_gte_{int(t)}"] = math.exp(-abs(t - expected_gap_points) / b) if t > expected_gap_points else 1.0 - 0.5 * math.exp(-abs(expected_gap_points - t) / b)
        else:
            out[f"p_lte_{int(t)}"] = 0.5 * math.exp(-abs(expected_gap_points - t) / b) if expected_gap_points >= t else 1.0 - math.exp(-abs(t - expected_gap_points) / b)
    return out


def expected_gap_points(
    direction_score: float | None,
    typical_gap_points: float | None,
    flat_band_pct: float = 0.001,
) -> float | None:
    """Expected signed gap (points) from the direction score and causal scale.

    Deterministic mapping: E[gap] ≈ direction × typical |gap|, with a
    documented FLAT-band shrinkage when |direction| implies a sub-band move.
    """
    if direction_score is None or typical_gap_points is None:
        return None
    return direction_score * typical_gap_points


def model_vs_implied(
    model_expected_gap: float | None,
    implied_move_points: float | None,
) -> float | None:
    """§11 second-order feature: model_expected_gap / options_implied_move."""
    if model_expected_gap is None or implied_move_points in (None, 0):
        return None
    try:
        if float(implied_move_points) == 0:
            return None
    except (TypeError, ValueError):
        return None
    return model_expected_gap / float(implied_move_points)
