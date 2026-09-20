"""Issue #17 — research pipeline service (Phase 1).

Orchestrates the research flow for the overnight-gap study
(docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md). Research-only: nothing here is
exposed through routers, the dashboard, or any execution path.

Flow per session (strict order; targets attach LAST)::

    1. persist immutable raw snapshots (underlying + option chain)
    2. build features (causal; app.research.gap_features)
    3. causal normalization (app.research.gap_normalization) + model inputs
    4. store predictions (baseline / pos_style / sos)
    5. attach the realized next-open target (Phase F, separate step)
    6. backtest comparison output (app.research.gap_backtest)

Immutability: raw snapshot writes are append-only; re-ingesting a session
raises ``SessionExistsError`` unless ``replace=True`` is explicit.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from app.models import (
    GapBacktestResult,
    GapFeatures,
    GapPrediction,
    GapPredictionSession,
    OptionChainSnapshot,
    UnderlyingSnapshot,
)
from app.research.gap_backtest import Observation, evaluate, run_backtest
from app.research.gap_features import (
    FEATURE_VERSION,
    delta_features,
    flow_features,
    futures_features,
    gex_features,
    iv_features,
    oi_features,
    vega_features,
    vix_features,
)
from app.research.gap_models import (
    POS_DISCLAIMER,
    baseline_expected_gap,
    baseline_futures_direction,
    baseline_previous_day_direction,
    baseline_previous_gap_direction,
    baseline_unconditional_distribution,
    expected_gap_points,
    model_vs_implied,
    pos_style_score,
    probabilities_from_score,
    sos_predict,
    straddle_implied_move,
    tail_probabilities,
)
from app.research.gap_normalization import rolling_zscore
from app.research.gap_targets import compute_gap_target

MODEL_VERSION = "v1"

BASELINE = "baseline"
POS_STYLE = "pos_style"
SOS = "sos"


class SessionExistsError(RuntimeError):
    """Raised when a session's immutable snapshots already exist."""


def _json_sanitize(value: Any) -> Any:
    """JSON-safe conversion: floats finite, NaN/inf → None, no silent zeros."""
    if isinstance(value, dict):
        return {k: _json_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_sanitize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    return value


def _chain_to_dicts(chain: Sequence[Any]) -> list[dict[str, Any]]:
    """Normalize ORM snapshot rows or dicts into plain dicts for the engine."""
    out: list[dict[str, Any]] = []
    for r in chain:
        if isinstance(r, Mapping):
            out.append(dict(r))
            continue
        out.append(
            {
                "expiry": getattr(r, "expiry", None),
                "strike": getattr(r, "strike", None),
                "option_type": getattr(r, "option_type", None),
                "ltp": getattr(r, "ltp", None),
                "bid": getattr(r, "bid", None),
                "ask": getattr(r, "ask", None),
                "bid_qty": getattr(r, "bid_qty", None),
                "ask_qty": getattr(r, "ask_qty", None),
                "volume": getattr(r, "volume", None),
                "open_interest": getattr(r, "open_interest", None),
                "change_in_oi": getattr(r, "change_in_oi", None),
                "iv": getattr(r, "iv", None),
                "delta": getattr(r, "delta", None),
                "gamma": getattr(r, "gamma", None),
                "vega": getattr(r, "vega", None),
                "theta": getattr(r, "theta", None),
                "timestamp": str(getattr(r, "timestamp", "") or ""),
            }
        )
    return out


def ingest_session_snapshots(
    db: Session,
    session_date: str,
    cutoff_timestamp: datetime,
    prior_close: float,
    underlying: Mapping[str, Any],
    chain: Sequence[Mapping[str, Any]],
    replace: bool = False,
) -> GapPredictionSession:
    """Step 1 — persist immutable raw snapshots for one research session.

    ``underlying`` keys: spot_ltp/open/high/low/close, futures_ltp/oi/volume,
    india_vix. ``chain`` rows: the raw strike observations (strike, option_type
    CALL|PUT, expiry, ltp/bid/ask/qty/volume/OI/change_in_oi/iv/greeks…).
    """
    existing = (
        db.query(GapPredictionSession)
        .filter(GapPredictionSession.symbol == "NIFTY", GapPredictionSession.session_date == session_date)
        .one_or_none()
    )
    if existing is not None and not replace:
        raise SessionExistsError(
            f"research session {session_date} already exists; snapshots are immutable"
        )
    if existing is not None:
        db.query(UnderlyingSnapshot).filter(
            UnderlyingSnapshot.session_date == session_date
        ).delete()
        db.query(OptionChainSnapshot).filter(
            OptionChainSnapshot.session_date == session_date
        ).delete()
        db.delete(existing)

    def f(v: Any) -> float | None:
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if math.isfinite(x) else None

    spot_close = f(underlying.get("spot_close")) or f(underlying.get("spot_ltp"))
    session = GapPredictionSession(
        symbol="NIFTY",
        session_date=session_date,
        cutoff_timestamp=cutoff_timestamp,
        prior_close=spot_close if spot_close is not None else 0.0,
        completeness="UNKNOWN",
    )
    db.add(session)

    db.add(
        UnderlyingSnapshot(
            symbol="NIFTY",
            session_date=session_date,
            timestamp=cutoff_timestamp,
            spot_ltp=f(underlying.get("spot_ltp")),
            spot_open=f(underlying.get("spot_open")),
            spot_high=f(underlying.get("spot_high")),
            spot_low=f(underlying.get("spot_low")),
            spot_close=f(underlying.get("spot_close")),
            futures_ltp=f(underlying.get("futures_ltp")),
            futures_oi=f(underlying.get("futures_oi")),
            futures_volume=f(underlying.get("futures_volume")),
            futures_basis=None,  # derived in features; raw store stays raw
            india_vix=f(underlying.get("india_vix")),
            option_chain=json.dumps(_json_sanitize(list(chain)), default=str),
        )
    )

    for row in chain:
        strike = f(row.get("strike"))
        otype = row.get("option_type")
        expiry = row.get("expiry") or session_date
        if strike is None or otype not in ("CALL", "PUT"):
            continue
        db.add(
            OptionChainSnapshot(
                symbol="NIFTY",
                session_date=session_date,
                timestamp=cutoff_timestamp,
                expiry=str(expiry),
                strike=strike,
                option_type=str(otype),
                ltp=f(row.get("ltp")),
                bid=f(row.get("bid")),
                ask=f(row.get("ask")),
                bid_qty=f(row.get("bid_qty")),
                ask_qty=f(row.get("ask_qty")),
                volume=f(row.get("volume")),
                open_interest=f(row.get("open_interest")),
                change_in_oi=f(row.get("change_in_oi")),
                iv=f(row.get("iv")),
                delta=f(row.get("delta")),
                gamma=f(row.get("gamma")),
                vega=f(row.get("vega")),
                theta=f(row.get("theta")),
            )
        )
    db.commit()
    return session


def build_and_store_features(db: Session, session_date: str) -> dict[str, Any] | None:
    """Steps 2–3 — compute features from the stored snapshots and persist them.

    Returns the feature dict (or ``None`` when the session has no snapshots).
    Prior-session features are read from the DB so every derived quantity is
    computed causally from stored, timestamped data.
    """
    us = (
        db.query(UnderlyingSnapshot)
        .filter(UnderlyingSnapshot.session_date == session_date)
        .one_or_none()
    )
    chain_rows = (
        db.query(OptionChainSnapshot)
        .filter(OptionChainSnapshot.session_date == session_date)
        .all()
    )
    if us is None or not chain_rows:
        return None
    chain = _chain_to_dicts(chain_rows)
    spot = us.spot_close or us.spot_ltp
    if spot is None or spot <= 0:
        return None

    # Prior session raw data for "change vs previous snapshot" features.
    prev_us = (
        db.query(UnderlyingSnapshot)
        .filter(UnderlyingSnapshot.session_date < session_date)
        .order_by(UnderlyingSnapshot.session_date.desc())
        .first()
    )
    prev_chain_rows = (
        db.query(OptionChainSnapshot)
        .filter(OptionChainSnapshot.session_date == (prev_us.session_date if prev_us else ""))
        .all()
    ) if prev_us else []
    prev_chain = _chain_to_dicts(prev_chain_rows)
    prev_underlying = (
        {
            "spot_close": prev_us.spot_close or prev_us.spot_ltp,
            "futures_ltp": prev_us.futures_ltp,
            "futures_oi": prev_us.futures_oi,
            "futures_volume": prev_us.futures_volume,
            "india_vix": prev_us.india_vix,
        }
        if prev_us
        else None
    )

    raw: dict[str, Any] = {}
    raw.update(delta_features(chain, spot, prev_chain))
    raw.update(vega_features(chain, spot, prev_chain))
    raw.update(oi_features(chain, spot, prev_chain))
    raw.update(iv_features(chain, spot, prev_chain))
    raw.update(gex_features(chain, spot, session_date, prev_chain))
    raw.update(futures_features({"futures_ltp": us.futures_ltp, "futures_oi": us.futures_oi, "futures_volume": us.futures_volume}, spot, prev_underlying))
    raw.update(flow_features(chain, spot, prev_chain))
    vix_hist = [
        v for (v,) in db.query(UnderlyingSnapshot.india_vix)
        .filter(
            UnderlyingSnapshot.session_date < session_date,  # strictly prior: causal
            UnderlyingSnapshot.india_vix.isnot(None),
        )
        .order_by(UnderlyingSnapshot.session_date)
        .all()
    ]
    raw.update(vix_features({"india_vix": us.india_vix}, prev_underlying, vix_hist))

    # Price-structure context (causal): the PREVIOUS session's close-to-close
    # change requires the T-2 close, never the current session's values.
    if prev_us is not None:
        prev2_us = (
            db.query(UnderlyingSnapshot)
            .filter(
                UnderlyingSnapshot.session_date < prev_us.session_date,
                UnderlyingSnapshot.spot_close.isnot(None),
            )
            .order_by(UnderlyingSnapshot.session_date.desc())
            .first()
        )
        prev_close = prev_us.spot_close or prev_us.spot_ltp
        prev2_close = (prev2_us.spot_close or prev2_us.spot_ltp) if prev2_us else None
        if prev_close and prev2_close:
            raw["prev_day_change_pct"] = (prev_close - prev2_close) / prev2_close

    features_json = json.dumps(_json_sanitize(raw), default=str)
    existing = (
        db.query(GapFeatures)
        .filter(GapFeatures.session_date == session_date, GapFeatures.feature_version == FEATURE_VERSION)
        .one_or_none()
    )
    if existing is None:
        db.add(
            GapFeatures(
                session_date=session_date,
                feature_version=FEATURE_VERSION,
                features=features_json,
                completeness="PARTIAL",
            )
        )
    else:
        existing.features = features_json
        existing.completeness = "PARTIAL"
    db.commit()
    return raw


def generate_and_store_predictions(
    db: Session,
    session_date: str,
    thresholds: Sequence[float] = (-100.0, -50.0, 50.0, 100.0),
) -> dict[str, dict[str, Any]] | None:
    """Step 4 — compute and store the three models' predictions for a session.

    Uses ONLY features of sessions strictly earlier for normalization history
    (causality); the session's own raw features drive its component values.
    """
    feat_row = (
        db.query(GapFeatures)
        .filter(GapFeatures.session_date == session_date, GapFeatures.feature_version == FEATURE_VERSION)
        .one_or_none()
    )
    if feat_row is None:
        return None
    raw = json.loads(feat_row.features)

    # Causal feature history for normalization (strictly earlier sessions).
    hist_rows = (
        db.query(GapFeatures)
        .filter(GapFeatures.session_date < session_date, GapFeatures.feature_version == FEATURE_VERSION)
        .order_by(GapFeatures.session_date)
        .all()
    )
    history: dict[str, list[Any]] = {}
    for h in hist_rows:
        for k, v in json.loads(h.features).items():
            history.setdefault(k, []).append(v)

    def z(key: str, min_history: int = 1) -> float | None:
        v = raw.get(key)
        if v is None:
            return None
        return rolling_zscore(v, history.get(key, []), min_history=min_history)

    normalized = {
        "delta_pressure_z": z("delta_pressure"),
        "vega_pressure_z": z("vega_pressure"),
        "pcr_change_z": z("pcr_change"),
        "iv_skew_change_z": z("iv_skew_change"),
        "futures_basis_pct_z": z("futures_basis_pct"),
        "premium_pressure_z": z("premium_pressure"),
        "spot_to_flip_pct_z": z("spot_to_flip_pct"),
        "vix_change_pct_z": z("vix_change_pct"),
        "prev_day_change_pct_z": z("prev_day_change_pct"),
    }

    # Causal gap history for baseline distributions (strictly earlier sessions
    # WITH attached targets only).
    prior_sessions = (
        db.query(GapPredictionSession)
        .filter(
            GapPredictionSession.session_date < session_date,
            GapPredictionSession.gap_class.isnot(None),
        )
        .order_by(GapPredictionSession.session_date)
        .all()
    )
    gap_history = [s.gap_points for s in prior_sessions]
    class_history = [s.gap_class for s in prior_sessions]

    us = db.query(UnderlyingSnapshot).filter(UnderlyingSnapshot.session_date == session_date).one_or_none()
    spot = us.spot_close or us.spot_ltp if us else None

    stored: dict[str, dict[str, Any]] = {}

    # ----- Model A: baselines -------------------------------------------------
    baseline_out: dict[str, Any] = {
        "previous_day_direction": baseline_previous_day_direction(raw),
        "previous_gap_direction": baseline_previous_gap_direction(raw),
        "futures_direction": baseline_futures_direction(raw),
    }
    implied = None
    if spot and raw.get("atm_iv") is not None:
        implied = straddle_implied_move(float(raw["atm_iv"]), float(spot))
        baseline_out["implied_move_points"] = implied
    dist = baseline_unconditional_distribution(class_history)
    if dist:
        baseline_out["unconditional_distribution"] = dist
    exp_gap = baseline_expected_gap(gap_history)
    if exp_gap is not None:
        baseline_out["expected_gap_points"] = exp_gap
    baseline_score = (
        baseline_out.get("futures_direction")
        if baseline_out.get("futures_direction") is not None
        else baseline_out.get("previous_day_direction")
    )
    stored[BASELINE] = {
        # Reference direction for backtest comparability: the futures-basis
        # baseline, falling back to previous-day direction (documented picks).
        "direction_score": baseline_score,
        # Never claim PREDICTED without an actual directional call — an
        # unscored prediction must be an explicit abstention (NO_EDGE).
        "state": "NO_EDGE" if baseline_score is None else "PREDICTED",
        "component_scores": baseline_out,
        "probabilities": _json_sanitize(dist) if dist else {},
    }

    # ----- Model B: POS-style -------------------------------------------------
    pos = pos_style_score(raw)
    if pos is not None:
        probs = None
        typical = (
            sum(abs(g) for g in gap_history if g is not None) / len([g for g in gap_history if g is not None])
            if gap_history
            else None
        )
        if typical:
            band_gap = typical
            probs = probabilities_from_score(
                pos["direction_score"] / 100.0, typical_gap_pct=band_gap / spot if spot else None
            )
        stored[POS_STYLE] = {
            "direction_score": pos["direction_score"],
            "state": "PREDICTED",
            "component_scores": pos["component_scores"],
            "probabilities": _json_sanitize(probs) if probs else {},
            "disclaimer": POS_DISCLAIMER,
        }
    else:
        stored[POS_STYLE] = {"state": "NO_EDGE", "component_scores": {}, "probabilities": {}}

    # ----- Model C: SOS --------------------------------------------------------
    sos = sos_predict(normalized, raw)
    if sos is not None:
        typical_points = (
            sum(abs(g) for g in gap_history if g is not None) / len([g for g in gap_history if g is not None])
            if gap_history
            else None
        )
        probs: dict[str, Any] = {}
        if typical_points and spot:
            probs.update(
                probabilities_from_score(
                    sos["direction_score"],
                    typical_gap_pct=typical_points / spot,
                )
                or {}
            )
            exp = expected_gap_points(sos["direction_score"], typical_points)
            if exp is not None:
                probs["expected_gap_points"] = exp
            tails = tail_probabilities(exp, typical_points, thresholds)
            if tails:
                probs.update(tails)
            if probs.get("expected_gap_points") is not None and implied is not None:
                ratio = model_vs_implied(probs["expected_gap_points"], implied)
                if ratio is not None:
                    probs["model_vs_implied"] = ratio
        stored[SOS] = {
            "direction_score": sos["direction_score"],
            "agreement_score": sos["agreement_score"],
            "dispersion": sos["dispersion"],
            "confidence": sos["confidence"],
            "state": sos["state"],
            "component_scores": sos["component_scores"],
            "probabilities": _json_sanitize(probs),
        }
    else:
        stored[SOS] = {"state": "NO_EDGE", "component_scores": {}, "probabilities": {}}

    for model_name, payload in stored.items():
        existing = (
            db.query(GapPrediction)
            .filter(
                GapPrediction.session_date == session_date,
                GapPrediction.model_name == model_name,
                GapPrediction.model_version == MODEL_VERSION,
            )
            .one_or_none()
        )
        if existing is None:
            db.add(
                GapPrediction(
                    session_date=session_date,
                    model_name=model_name,
                    model_version=MODEL_VERSION,
                    direction_score=payload.get("direction_score"),
                    agreement_score=payload.get("agreement_score"),
                    dispersion=payload.get("dispersion"),
                    confidence=payload.get("confidence"),
                    state=payload.get("state", "PREDICTED"),
                    component_scores=json.dumps(_json_sanitize(payload.get("component_scores", {})), default=str),
                    probabilities=json.dumps(_json_sanitize(payload.get("probabilities", {})), default=str),
                )
            )
        else:
            existing.direction_score = payload.get("direction_score")
            existing.agreement_score = payload.get("agreement_score")
            existing.dispersion = payload.get("dispersion")
            existing.confidence = payload.get("confidence")
            existing.state = payload.get("state", "PREDICTED")
            existing.component_scores = json.dumps(_json_sanitize(payload.get("component_scores", {})), default=str)
            existing.probabilities = json.dumps(_json_sanitize(payload.get("probabilities", {})), default=str)
    db.commit()
    return stored


def attach_realized_target(
    db: Session,
    session_date: str,
    next_session_date: str,
    next_open: float,
    next_open_timestamp: datetime | None = None,
    flat_band_pct: float = 0.001,
) -> GapPredictionSession | None:
    """Step 5 — attach the realized T+1 open (target) to session T. Separate
    step by design: it must run after features and predictions exist."""
    session = (
        db.query(GapPredictionSession)
        .filter(GapPredictionSession.symbol == "NIFTY", GapPredictionSession.session_date == session_date)
        .one_or_none()
    )
    if session is None:
        return None
    target = compute_gap_target(
        session.prior_close,
        next_open,
        next_open_timestamp=str(next_open_timestamp) if next_open_timestamp else None,
        next_session_date=next_session_date,
        flat_band_pct=flat_band_pct,
    )
    if target is None:
        return session
    session.next_session_date = target.next_session_date
    session.next_open_timestamp = (
        datetime.fromisoformat(target.next_open_timestamp) if target.next_open_timestamp else None
    )
    session.next_open = target.gap_points + session.prior_close
    session.gap_points = target.gap_points
    session.gap_pct = target.gap_pct
    session.gap_class = target.gap_class
    db.commit()
    return session


def run_comparison_backtest(
    db: Session,
    model_name: str,
    regime_fn: Any = None,
) -> dict[str, Any]:
    """Step 6 — chronological backtest of one stored model over all sessions
    with attached targets. Reads predictions from the DB; deterministic."""
    rows: list[dict[str, Any]] = []
    sessions = (
        db.query(GapPredictionSession, GapPrediction)
        .join(
            GapPrediction,
            (GapPrediction.session_date == GapPredictionSession.session_date)
            & (GapPrediction.model_name == model_name),
        )
        .filter(GapPredictionSession.gap_class.isnot(None))
        .order_by(GapPredictionSession.session_date)
        .all()
    )
    for session, prediction in sessions:
        rows.append(
            {
                "session_date": session.session_date,
                "gap_class": session.gap_class,
                "gap_points": session.gap_points,
                "prediction": {
                    "direction_score": prediction.direction_score,
                    "state": prediction.state,
                    "confidence": prediction.confidence,
                    "probabilities": json.loads(prediction.probabilities or "{}"),
                },
                "india_vix": None,
            }
        )
    if regime_fn is None:
        regime_fn = lambda row: "ALL"  # noqa: E731 — Phase-1 default; regime enrichment lands with realized VIX storage
    result = run_backtest(
        rows,
        predict=lambda row: row["prediction"],
        regime_of=regime_fn,
        confidence_of=lambda row: row["prediction"].get("confidence"),
    )
    # Actual evaluated range — consumed by persistence (CLI backtest) so the
    # stored period metadata reflects the real sessions, never a placeholder.
    result["period_start"] = rows[0]["session_date"] if rows else None
    result["period_end"] = rows[-1]["session_date"] if rows else None

    # Phase-2 regime segmentation (report-only — never a model input): when
    # stored features exist, segment metrics by data-supported dimensions.
    # VIX regime remains unavailable until VIX history exists; GEX/IV/DTE/
    # prior-day-movement regimes activate when their features are present.
    result["by_regime_dims"] = _regime_dimension_breakdown(db, rows)
    return result


def _regime_dimension_breakdown(db: Session, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Segment metrics along feature-derived dimensions (report-only).

    Segmentation uses stored, already-causal features. Cross-sectional
    medians are computed over the evaluated sample for reporting only —
    they never influence predictions, features, or normalization.
    """
    if not rows:
        return {}
    dates = [r["session_date"] for r in rows]
    feat_rows = (
        db.query(GapFeatures.session_date, GapFeatures.features)
        .filter(
            GapFeatures.session_date.in_(dates),
            GapFeatures.feature_version == FEATURE_VERSION,
        )
        .all()
    )
    feats = {d: json.loads(f or "{}") for d, f in feat_rows}
    expiry_rows = (
        db.query(OptionChainSnapshot.session_date, OptionChainSnapshot.expiry)
        .filter(OptionChainSnapshot.session_date.in_(dates))
        .distinct()
        .all()
    )
    expiry_by_session: dict[str, str] = dict(expiry_rows)

    def dte_of(d: str) -> float | None:
        e = expiry_by_session.get(d)
        if not e:
            return None
        try:
            return float((date.fromisoformat(e) - date.fromisoformat(d)).days)
        except ValueError:
            return None

    def median(values: list[float]) -> float | None:
        if not values:
            return None
        s = sorted(values)
        m = len(s) // 2
        return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0

    dims: dict[str, dict[str, Any]] = {}

    def add_dim(name: str, label_of):
        labels = {d: label_of(d) for d in dates}
        if all(v is None for v in labels.values()):
            return
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            lab = labels.get(r["session_date"])
            if lab is None:
                continue
            buckets.setdefault(lab, []).append(r)
        if len(buckets) < 2:
            return
        dims[name] = {
            label: evaluate(
                [
                    Observation(
                        session_date=r["session_date"],
                        realized_gap_class=r["gap_class"],
                        realized_gap_points=r["gap_points"],
                        prediction=r["prediction"],
                        regime=label,
                        confidence=r["prediction"].get("confidence"),
                    )
                    for r in bucket
                ]
            ).as_dict()
            for label, bucket in sorted(buckets.items())
        }

    # GEX regime: net dealer gamma positive (short-gamma below flip) vs negative.
    add_dim(
        "gex",
        lambda d: ("NET_GEX_POS" if (feats.get(d, {}).get("net_gex") or 0) >= 0 else "NET_GEX_NEG")
        if feats.get(d, {}).get("net_gex") is not None
        else None,
    )
    # IV regime: ATM IV above/below the sample median.
    iv_med = median(
        [feats[d]["atm_iv"] for d in dates if feats.get(d, {}).get("atm_iv") is not None]
    )
    if iv_med is not None:
        add_dim(
            "iv",
            lambda d: ("IV_HIGH" if feats[d]["atm_iv"] >= iv_med else "IV_LOW")
            if feats.get(d, {}).get("atm_iv") is not None
            else None,
        )
    # Expiry proximity: front expiry within 2 trading-agnostic calendar days.
    add_dim(
        "dte",
        lambda d: ("DTE_NEAR" if (dte_of(d) or 99) <= 2 else "DTE_FAR")
        if dte_of(d) is not None
        else None,
    )
    # Prior-day movement regime.
    add_dim(
        "prevday",
        lambda d: ("PREV_UP" if feats[d]["prev_day_change_pct"] > 0 else "PREV_DOWN")
        if feats.get(d, {}).get("prev_day_change_pct") is not None
        else None,
    )
    return dims


def store_backtest_result(
    db: Session,
    model_name: str,
    period_start: str,
    period_end: str,
    result: Mapping[str, Any],
    regime: str = "ALL",
) -> GapBacktestResult:
    """Persist one aggregate backtest result (idempotent upsert)."""
    existing = (
        db.query(GapBacktestResult)
        .filter(
            GapBacktestResult.model_name == model_name,
            GapBacktestResult.period_start == period_start,
            GapBacktestResult.period_end == period_end,
            GapBacktestResult.regime == regime,
        )
        .one_or_none()
    )
    payload = json.dumps(_json_sanitize(dict(result)), default=str)
    if existing is None:
        row = GapBacktestResult(
            model_name=model_name,
            period_start=period_start,
            period_end=period_end,
            regime=regime,
            metrics=payload,
        )
        db.add(row)
    else:
        existing.metrics = payload
        row = existing
    db.commit()
    return row
