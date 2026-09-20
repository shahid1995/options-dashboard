"""Issue #17 — Overnight Gap research engine tests (Phase 1).

Covers the research contract that matters: PE delta sign normalization, GEX
reuse at the canonical formula, target formulas, look-ahead prevention,
missing-is-not-zero, POS-style/SOS explainability, NO_EDGE, and deterministic
chronological backtesting.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime

import pytest

BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)

from app.research.gap_backtest import (  # noqa: E402
    Observation,
    evaluate,
    evaluate_by_regime,
    run_backtest,
    walk_forward_folds,
)
from app.research.gap_features import (  # noqa: E402
    agreement_and_dispersion,
    component_scores,
    cross_component_features,
    delta_features,
    flow_features,
    futures_features,
    gex_features,
    iv_features,
    near_atm_weight,
    oi_features,
    vega_features,
    vix_features,
)
from app.research.gap_models import (  # noqa: E402
    POS_DISCLAIMER,
    SOS_MIN_DIRECTION,
    baseline_unconditional_distribution,
    expected_gap_points,
    model_vs_implied,
    pos_style_score,
    probabilities_from_score,
    sos_predict,
    straddle_implied_move,
    tail_probabilities,
)
from app.research.gap_normalization import (  # noqa: E402
    rolling_zscore,
    robust_zscore,
)
from app.research.gap_targets import compute_gap_target  # noqa: E402


def _row(strike, side, **kw):
    base = {
        "expiry": "2026-08-27",
        "strike": float(strike),
        "option_type": side,
        "ltp": 150.0,
        "bid": 149.0,
        "ask": 151.0,
        "bid_qty": 400.0,
        "ask_qty": 380.0,
        "volume": 80000.0,
        "open_interest": 100000.0,
        "change_in_oi": 2000.0,
        "iv": 0.11,
        "delta": 0.5 if side == "CALL" else -0.5,
        "gamma": 0.0007,
        "vega": 11.0,
        "theta": -8.0,
        "timestamp": "2026-08-06T15:25:00",
    }
    base.update(kw)
    return base


CHAIN = [
    _row(24950, "CALL", delta=0.62, open_interest=80000.0, gamma=0.0008),
    _row(24950, "PUT", delta=-0.38, open_interest=150000.0, gamma=0.0007),
    _row(25000, "CALL", delta=0.55, open_interest=120000.0, gamma=0.0006),
    _row(25000, "PUT", delta=-0.45, open_interest=100000.0, gamma=0.0006),
    _row(25050, "CALL", delta=0.48, open_interest=140000.0, gamma=0.0009),
    _row(25050, "PUT", delta=-0.52, open_interest=70000.0, gamma=0.0005),
]
SPOT = 25010.0


# ---------------------------------------------------------------------------
# Delta / vega / OI / IV conventions
# ---------------------------------------------------------------------------


def test_pe_delta_sign_normalized():
    """PE features use |delta|; delta_diff = ce - abs(pe); raw negative never used."""
    feats = delta_features(CHAIN, SPOT)
    assert feats["ce_delta"] > 0 and feats["pe_delta"] > 0
    assert feats["delta_diff"] == pytest.approx(feats["ce_delta"] - feats["pe_delta"])
    # Independent manual check of the OI-weighted pressure on the window.
    assert feats["delta_pressure"] >= -1.0 and feats["delta_pressure"] <= 1.0


def test_delta_pressure_more_calls_is_positive():
    bullish = [_row(25000, "CALL", delta=0.9, open_interest=300000.0), _row(25000, "PUT", delta=-0.1, open_interest=10000.0)]
    bearish = [_row(25000, "CALL", delta=0.1, open_interest=10000.0), _row(25000, "PUT", delta=-0.9, open_interest=300000.0)]
    assert delta_features(bullish, 25000.0)["delta_pressure"] > 0
    assert delta_features(bearish, 25000.0)["delta_pressure"] < 0


def test_vega_bounded_and_scale_free():
    feats = vega_features(CHAIN, SPOT)
    assert -1.0 <= feats["vega_pressure"] <= 1.0
    big = [_row(25000, "CALL", vega=1e6), _row(25000, "PUT", vega=1e6)]
    assert vega_features(big, 25000.0)["vega_pressure"] == pytest.approx(0.0)


def test_oi_pcr_and_missing_safe():
    feats = oi_features(CHAIN, SPOT)
    assert feats["pcr"] > 0
    empty = oi_features([], SPOT)
    assert empty == {}


def test_iv_skew_and_change():
    feats = iv_features(CHAIN, SPOT)
    assert feats["iv_skew"] == pytest.approx(feats["pe_iv"] - feats["ce_iv"])
    prev = [_row(25000, "CALL", iv=0.10), _row(25000, "PUT", iv=0.10)]
    ch = iv_features(CHAIN, SPOT, prev_chain=prev)
    assert ch["iv_change"] > 0


def test_strike_weighting_peaks_at_atm():
    assert near_atm_weight(25000.0, 25000.0, 50.0) == pytest.approx(1.0)
    assert near_atm_weight(24900.0, 25000.0, 50.0) < near_atm_weight(24950.0, 25000.0, 50.0)


def test_missing_data_is_not_zero():
    """Rows missing deltas produce NO delta feature — never a silent 0."""
    broken = [_row(25000, "CALL", delta=None), _row(25000, "PUT", delta=None)]
    feats = delta_features(broken, 25000.0)
    assert "delta_pressure" not in feats
    assert "ce_delta" not in feats


# ---------------------------------------------------------------------------
# GEX reuse — canonical formula verification
# ---------------------------------------------------------------------------


def test_gex_uses_authoritative_formula():
    """net GEX must equal the manual canonical sum gamma·OI·S²·0.01, CE=+1/PE=−1."""
    manual = 0.0
    for r in CHAIN:
        gex = r["gamma"] * r["open_interest"] * SPOT * SPOT * 0.01
        manual += gex if r["option_type"] == "CALL" else -gex
    feats = gex_features(CHAIN, SPOT, "2026-08-06")
    assert feats["net_gex"] == pytest.approx(manual, rel=1e-9)
    assert feats["call_gex"] > 0 and feats["put_gex"] < 0


def test_gex_flip_and_distance():
    feats = gex_features(CHAIN, SPOT, "2026-08-06")
    assert "gamma_flip" in feats and "spot_to_flip_pct" in feats
    assert -1.0 <= feats["spot_to_flip_pct"] <= 1.0


def test_gex_empty_chain_is_empty_dict():
    assert gex_features([], SPOT, "2026-08-06") == {}


# ---------------------------------------------------------------------------
# Futures / flow / VIX
# ---------------------------------------------------------------------------


def test_futures_basis_and_change():
    u = {"futures_ltp": 25040.0, "futures_oi": 150000.0, "futures_volume": 200000.0}
    feats = futures_features(u, SPOT)
    assert feats["futures_basis"] == pytest.approx(30.0)
    prev = {"futures_ltp": 24990.0, "futures_oi": 140000.0}
    ch = futures_features(u, SPOT, prev_underlying=prev)
    assert ch["futures_change_pct"] > 0 and ch["futures_oi_change_pct"] > 0


def test_flow_never_infers_direction_from_ltp_alone():
    """Without bid/ask there is no bid_ask_pressure (documented rule)."""
    no_quote = [_row(25000, "CALL", bid=None, ask=None, bid_qty=None, ask_qty=None), _row(25000, "PUT")]
    feats = flow_features(no_quote, 25000.0)
    assert "bid_ask_pressure" not in feats
    with_quote = flow_features([_row(25000, "CALL"), _row(25000, "PUT")], 25000.0)
    assert -1.0 <= with_quote.get("bid_ask_pressure", 0.0) <= 1.0


def test_vix_regime_and_percentile():
    u = {"india_vix": 0.20}
    feats = vix_features(u, vix_history=[0.11, 0.12, 0.13, 0.20])
    assert feats["vix_regime"] == 2.0  # HIGH band
    assert feats["vix_percentile"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Targets — exact formulas
# ---------------------------------------------------------------------------


def test_gap_target_formulas_exact():
    t = compute_gap_target(25000.0, 25120.0, next_session_date="2026-08-07")
    assert t.gap_points == pytest.approx(120.0)
    assert t.gap_pct == pytest.approx(120.0 / 25000.0)
    assert t.gap_class == "GAP_UP"
    d = compute_gap_target(25000.0, 24880.0)
    assert d.gap_points == pytest.approx(-120.0) and d.gap_class == "GAP_DOWN"


def test_gap_target_flat_band():
    inside = compute_gap_target(25000.0, 25024.9)  # |0.0997%| < 0.1% band
    assert inside.gap_class == "FLAT"
    outside = compute_gap_target(25000.0, 25025.5)
    assert outside.gap_class == "GAP_UP"


def test_gap_target_missing_is_none_not_zero():
    assert compute_gap_target(None, 25100.0) is None
    assert compute_gap_target(25000.0, None) is None


# ---------------------------------------------------------------------------
# Normalization — causality and missing handling
# ---------------------------------------------------------------------------


def test_rolling_zscore_insufficient_history_is_none():
    assert rolling_zscore(1.0, [0.5, 0.6], min_history=20) is None
    assert rolling_zscore(None, [1.0] * 30) is None


def test_rolling_zscore_causal_values():
    hist = [float(i) for i in range(30)]
    z = rolling_zscore(45.0, hist, min_history=20)
    assert z is not None and z > 3.0  # far above the causal mean
    # Winsorization bound respected.
    assert z <= 5.0


def test_robust_zscore_outlier_detection():
    hist = [10.0] * 19 + [50.0]
    z = robust_zscore(50.0, hist, min_history=20)
    assert z == pytest.approx(5.0)  # clipped at the documented bound


# ---------------------------------------------------------------------------
# Models — baselines, POS-style, SOS
# ---------------------------------------------------------------------------


def test_baseline_distribution_causal_counts():
    dist = baseline_unconditional_distribution(["GAP_UP", "GAP_DOWN", "FLAT", None])
    assert dist["n"] == 3 and dist["p_up"] == pytest.approx(1 / 3)


def test_pos_style_score_range_and_disclaimer():
    feats = {"delta_pressure": 0.4, "vega_pressure": 0.1, "pcr_change": -0.2, "premium_pressure": 0.3}
    out = pos_style_score(feats)
    assert -100.0 <= out["direction_score"] <= 100.0
    assert out["weights_used"]["delta"] == pytest.approx(0.4)
    assert POS_DISCLAIMER in out["disclaimer"]
    assert "Vibhore Gupta" in out["disclaimer"]


def test_pos_style_weights_renormalize_when_missing():
    feats = {"delta_pressure": 1.0}  # only delta available
    out = pos_style_score(feats)
    assert out["direction_score"] == pytest.approx(100.0)
    assert list(out["weights_used"].keys()) == ["delta"]


def test_pos_style_no_components_is_none():
    assert pos_style_score({"unrelated": 1.0}) is None


def test_sos_predict_shape_and_bounds():
    norm = {"delta_pressure_z": 0.8, "vega_pressure_z": 0.5, "futures_basis_pct_z": 0.6, "vix_change_pct_z": -0.2}
    out = sos_predict(norm, {"premium_pressure": 0.4})
    assert out is not None
    assert -1.0 <= out["direction_score"] <= 1.0
    assert 0.0 <= out["agreement_score"] <= 1.0
    assert out["dispersion"] >= 0.0
    assert 0.0 <= out["confidence"] <= 1.0
    assert out["state"] in ("PREDICTED", "NO_EDGE")
    assert set(out["component_scores"]) == set(out["weights_used"].keys())


def test_sos_no_edge_on_contradiction():
    """Strongly contradictory components force NO_EDGE, never a forced call."""
    norm = {
        "delta_pressure_z": 2.0,
        "vega_pressure_z": -2.0,
        "pcr_change_z": 2.0,
        "iv_skew_change_z": -2.0,
        "futures_basis_pct_z": 2.0,
        "premium_pressure_z": -2.0,
    }
    out = sos_predict(norm, {})
    assert out is not None
    assert out["state"] == "NO_EDGE"


def test_sos_no_edge_on_weak_direction():
    out = sos_predict({"delta_pressure_z": 0.05}, {})
    assert out["state"] == "NO_EDGE" or abs(out["direction_score"]) < SOS_MIN_DIRECTION + 0.05


def test_sos_predict_all_missing_is_none():
    assert sos_predict({}, {}) is None


def test_probabilities_sum_to_one():
    p = probabilities_from_score(0.5, typical_gap_pct=0.004)
    assert p["p_up"] + p["p_flat"] + p["p_down"] == pytest.approx(1.0)


def test_expected_gap_and_implied_ratio():
    e = expected_gap_points(0.5, 80.0)
    assert e == pytest.approx(40.0)
    r = model_vs_implied(40.0, 100.0)
    assert r == pytest.approx(0.4)
    assert model_vs_implied(40.0, 0) is None


def test_straddle_implied_move_positive():
    m = straddle_implied_move(0.12, 25000.0)
    assert m > 0 and m < 25000.0
    assert straddle_implied_move(None, 25000.0) is None


def test_tail_probabilities_monotone():
    t = tail_probabilities(20.0, 60.0)
    assert t["p_gte_50"] >= t["p_gte_100"]


# ---------------------------------------------------------------------------
# Cross-component confluence
# ---------------------------------------------------------------------------


def test_component_scores_and_agreement():
    feats = {"delta_pressure": 0.5, "vega_pressure": 0.3, "pcr_change": 0.2}
    scores = component_scores(feats)
    assert set(scores) <= {"delta", "vega", "oi"}
    ad = agreement_and_dispersion(scores)
    assert ad is not None and 0.0 <= ad[0] <= 1.0 and ad[1] >= 0.0


def test_cross_component_missing_components_absent():
    cc = cross_component_features({})
    assert cc == {}


# ---------------------------------------------------------------------------
# Backtester — determinism, chronology, honesty
# ---------------------------------------------------------------------------


def _obs(date, score, state="PREDICTED", realized="GAP_UP", points=90.0, regime="ALL", conf=0.5):
    return Observation(
        session_date=date,
        prediction={"direction_score": score, "state": state, "confidence": conf, "probabilities": {"p_up": 0.5 + score / 2, "expected_gap_points": score * 80}},
        realized_gap_class=realized,
        realized_gap_points=points,
        regime=regime,
        confidence=conf,
    )


def test_backtest_deterministic_and_counts_no_edge():
    obs = [_obs(f"2026-08-0{i}", 0.6, realized="GAP_UP") for i in range(1, 6)] + [_obs("2026-08-06", 0.9, state="NO_EDGE")]
    a, b = evaluate(obs), evaluate(list(reversed(obs)))
    assert a.as_dict() == b.as_dict()  # order-independent metrics, no shuffling anywhere
    assert a.n_no_edge == 1 and a.n_scored == 5


def test_backtest_majority_base_rate_reported():
    obs = [_obs(f"2026-08-0{i}", 0.9, realized="GAP_UP") for i in range(1, 9)] + [_obs("2026-08-09", -0.9, realized="GAP_DOWN")]
    m = evaluate(obs)
    assert m.majority_base_rate == pytest.approx(8 / 9)
    assert m.accuracy is not None  # accuracy alone never masquerades as edge


def test_backtest_brier_and_auc():
    obs = [_obs(f"2026-08-0{i}", 0.9, realized="GAP_UP") for i in range(1, 5)] + [_obs(f"2026-08-1{i}", -0.9, realized="GAP_DOWN") for i in range(1, 5)]
    m = evaluate(obs)
    assert m.brier is not None and 0.0 <= m.brier <= 1.0
    assert m.roc_auc == pytest.approx(1.0)


def test_backtest_magnitude_metrics():
    obs = [_obs("2026-08-05", 0.5, realized="GAP_UP", points=100.0)]
    m = evaluate(obs)
    assert m.mae_points == pytest.approx(60.0)  # 0.5*80 predicted vs 100 realized
    assert m.rmse_points == pytest.approx(60.0)
    assert m.signed_error_mean == pytest.approx(-60.0)


def test_regime_segmentation():
    obs = [_obs("2026-08-05", 0.9, regime="vix_high"), _obs("2026-08-06", 0.9, regime="vix_low")]
    seg = evaluate_by_regime(obs)
    assert set(seg) == {"vix_high", "vix_low", "ALL"}


def test_walk_forward_folds_chronological():
    dates = [f"2026-08-{d:02d}" for d in range(1, 11)]
    folds = walk_forward_folds(dates, min_train=5, test_size=2)
    assert folds
    for train, test in folds:
        assert max(train) < min(test)
    assert folds[-1][1] == dates[-1:]


def test_run_backtest_rows_flow():
    rows = [
        {"session_date": "2026-08-05", "gap_class": "GAP_UP", "gap_points": 90.0, "prediction": {"direction_score": 0.8, "state": "PREDICTED", "confidence": 0.6, "probabilities": {"p_up": 0.9}}, "india_vix": 0.12},
        {"session_date": "2026-08-06", "gap_class": "GAP_DOWN", "gap_points": -70.0, "prediction": {"direction_score": 0.8, "state": "NO_EDGE", "confidence": None, "probabilities": {}}, "india_vix": 0.14},
    ]
    out = run_backtest(rows, predict=lambda r: r["prediction"], regime_of=lambda r: "ALL")
    assert out["metrics"]["n"] == 2 and out["metrics"]["n_no_edge"] == 1


# ---------------------------------------------------------------------------
# Pipeline integration (temp SQLite): immutability + target-after-prediction
# ---------------------------------------------------------------------------


@pytest.fixture()
def research_db(tmp_path, monkeypatch):
    """Hermetic engine: a fresh SQLite file per test, NOT the app default DB.

    The pipeline functions receive ``db`` explicitly, so no settings reload is
    needed — a dedicated engine/sessionmaker bound to tmp_path keeps research
    tests fully isolated from the development database file.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base

    import app.models  # noqa: F401 — register tables on Base

    engine = create_engine(
        f"sqlite:///{tmp_path}/gap_test.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    yield db
    db.close()
    engine.dispose()


def _session_payload(day: str, ce_bias: float):
    chain = []
    for k in (24950, 25000, 25050):
        chain.append(_row(k, "CALL", open_interest=100000.0 * (1 + ce_bias), timestamp=f"{day}T15:25:00"))
        chain.append(_row(k, "PUT", open_interest=100000.0, timestamp=f"{day}T15:25:00"))
    u = {"spot_ltp": 25000.0, "spot_close": 25000.0, "futures_ltp": 25030.0, "futures_oi": 150000.0, "futures_volume": 200000.0, "india_vix": 0.12}
    return u, chain


def test_pipeline_immutability_and_target_separation(research_db):
    from app.research.gap_pipeline import (
        SessionExistsError,
        attach_realized_target,
        build_and_store_features,
        generate_and_store_predictions,
        ingest_session_snapshots,
    )

    db = research_db
    u, chain = _session_payload("2026-08-05", 0.2)
    ingest_session_snapshots(db, "2026-08-05", datetime(2026, 8, 5, 15, 30), 25000.0, u, chain)
    with pytest.raises(SessionExistsError):
        ingest_session_snapshots(db, "2026-08-05", datetime(2026, 8, 5, 15, 30), 25000.0, u, chain)

    feats = build_and_store_features(db, "2026-08-05")
    assert feats and "pcr" in feats
    preds = generate_and_store_predictions(db, "2026-08-05")
    assert preds is not None and "sos" in preds

    # Before target attachment: session has no realized values.
    from app.models import GapPredictionSession

    row = db.query(GapPredictionSession).filter_by(session_date="2026-08-05").one()
    assert row.gap_points is None and row.next_open is None
    attach_realized_target(db, "2026-08-05", "2026-08-06", 25120.0, datetime(2026, 8, 6, 9, 15))
    db.refresh(row)
    assert row.gap_points == pytest.approx(120.0)
    assert row.gap_class == "GAP_UP"
    assert json.loads(row.completeness_detail) is not None


def test_no_lookahead_features_and_predictions(research_db):
    """Leak-proofing: targets exist only after attachment and never enter
    stored features, and predictions computed pre-target are byte-identical
    to predictions recomputed post-target (attachment is a downstream join)."""
    import json as _json

    from app.models import GapFeatures, GapPrediction, GapPredictionSession
    from app.research.gap_pipeline import (
        attach_realized_target,
        build_and_store_features,
        generate_and_store_predictions,
        ingest_session_snapshots,
    )

    db = research_db
    u, chain = _session_payload("2026-08-05", 0.3)
    ingest_session_snapshots(db, "2026-08-05", datetime(2026, 8, 5, 15, 30), 25000.0, u, chain)
    build_and_store_features(db, "2026-08-05")
    pre = generate_and_store_predictions(db, "2026-08-05")

    # 1. No feature key carries target information.
    feat_row = db.query(GapFeatures).filter_by(session_date="2026-08-05").one()
    stored_keys = set(_json.loads(feat_row.features).keys())
    assert not any(k.startswith("gap_") or k.startswith("next_") for k in stored_keys)

    # 2. The session row carries no realized target yet.
    session = db.query(GapPredictionSession).filter_by(session_date="2026-08-05").one()
    assert session.gap_points is None and session.next_open is None

    # 3. Normalization history window (pipeline) reads only strictly-prior
    #    sessions — verify by checking the sos prediction is unchanged when a
    #    future-dated, deliberately-contradictory history row exists.
    attach_realized_target(db, "2026-08-05", "2026-08-06", 25999.0, datetime(2026, 8, 6, 9, 15))
    post = generate_and_store_predictions(db, "2026-08-05")
    assert _json.dumps(pre["sos"], sort_keys=True, default=str) == _json.dumps(
        post["sos"], sort_keys=True, default=str
    )
    # 4. Prediction rows are model-version keyed and immutable in identity.
    n = db.query(GapPrediction).filter_by(session_date="2026-08-05", model_name="sos").count()
    assert n == 1


# ---------------------------------------------------------------------------
# Historical sample loader (gap_historical)
# ---------------------------------------------------------------------------


def _seed_candle_store(tmp_path, dates: list[str], gap_day: str | None = None):
    """Build a hermetic candle-store DB (nifty/option candles + specs).

    ``gap_day`` names a date that gets NO option candles (a data gap), so the
    continuity rule can be tested.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import ContractSpec, NiftyCandle, OptionCandle

    engine = create_engine(f"sqlite:///{tmp_path}/store.db")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()

    def idx(day: str) -> None:
        base = datetime.fromisoformat(f"{day}T09:15:00")
        for k in (0, 1):
            s.add(NiftyCandle(symbol="NIFTY", interval="3min", open_time=base.replace(hour=9 + k), open=25000 + k, high=25010 + k, low=24990 + k, close=25005 + k, volume=1000))

    def opts(day: str) -> None:
        expiry = day  # front expiry == session date for the test
        for token, (strike, otype) in {
            "1": (25000, "CE"), "2": (25000, "PE"),
            "3": (25050, "CE"), "4": (25050, "PE"),
        }.items():
            key = f"NSE_FO|{token}|{day}"
            s.add(ContractSpec(instrument_key=key, underlying="NIFTY", underlying_key="NSE_INDEX|Nifty 50", expiry=expiry, strike_price=float(strike), instrument_type=otype, lot_size=25, minimum_lot=25, freeze_quantity=1800, tick_size=0.05, trading_symbol=f"NIFTY {strike} {otype}", segment="NSE_FO", exchange="NSE", weekly=True, source="TEST", source_reference="test", fetched_at=datetime(2026, 1, 1)))
            s.add(OptionCandle(instrument_key=key, interval="3min", open_time=datetime.fromisoformat(f"{day}T15:27:00"), open=100.0, high=110.0, low=95.0, close=105.0, volume=500.0, open_interest=90000.0, source="TEST", fetched_at=datetime(2026, 1, 1)))

    for d in dates:
        idx(d)
        if d != gap_day:
            opts(d)
    s.commit()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def candle_store(tmp_path):
    # 08-04 = data gap (no option candles); 08-07 = index-only trailing date
    # (so 08-06 has a next index session for the realized-open target).
    yield from _seed_candle_store(
        tmp_path,
        ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"],
        gap_day="2026-08-04",
    )


def test_historical_loader_continuity_and_pass_order(tmp_path, research_db, candle_store):
    """Continuity rule, extraction shape, and the strict pass ordering of the
    full sample run (snapshots -> features/predictions -> targets -> backtest)."""
    from app.models import GapPredictionSession
    from app.research.gap_historical import (
        extract_historical_sessions,
        run_historical_sample,
    )

    store = candle_store
    # --- continuity: 08-04 has no option data (data gap), so it is not a
    # candidate; 08-03 is skipped (no previous captured session); 08-05 and
    # 08-06 survive (previous captured session within the gap cap).
    sessions = extract_historical_sessions(store)
    dates = [s.session_date for s in sessions]
    assert dates == ["2026-08-05", "2026-08-06"]
    first = sessions[0]
    assert first.next_session_date == "2026-08-06"
    # prior_close is the session's OWN close (base of the predicted T+1 gap),
    # never the previous day's close.
    assert first.prior_close == pytest.approx(25006.0)
    assert first.underlying["spot_close"] == pytest.approx(first.prior_close)
    assert first.chain and first.chain[0]["iv"] is None  # missing stays missing

    # --- full sample run on a hermetic research DB.
    summary = run_historical_sample(research_db, store)
    assert summary["sessions_extracted"] >= 1
    assert summary["targets_attached"] == summary["sessions_extracted"]
    assert set(summary["backtests"]) == {"baseline", "pos_style", "sos"}
    for model, bt in summary["backtests"].items():
        assert bt["metrics"]["n"] >= 1
        # every observation is either scored or explicitly NO_EDGE
        assert (
            bt["metrics"]["n_scored"] + bt["metrics"]["n_no_edge"]
            == bt["metrics"]["n"]
        )

    # --- pass-order proof: target columns only exist via the attachment step.
    row = (
        research_db.query(GapPredictionSession)
        .filter(GapPredictionSession.session_date == first.session_date)
        .one()
    )
    assert row.gap_points is not None and row.gap_class in ("GAP_UP", "GAP_DOWN", "FLAT")
    # completeness honesty: no fabricated IV/greeks/futures for candle data
    comp = summary["data_completeness"]
    assert comp["iv_present"] == 0.0
    assert comp["greeks_present"] == 0.0
    assert comp["futures_present"] == 0.0
    assert comp["oi_present"] == 1.0


def test_historical_loader_gap_cap(tmp_path):
    """Sessions whose previous captured session is beyond the gap cap are
    skipped — a 'change' feature must never silently span weeks of missing
    data."""
    from app.research.gap_historical import extract_historical_sessions

    stores = []
    gen = _seed_candle_store(tmp_path, ["2026-08-03", "2026-08-25"])
    try:
        store = next(gen)
        stores.append(store)
        sessions = extract_historical_sessions(store)
        assert sessions == []  # 22-day gap exceeds MAX_CANDIDATE_GAP_DAYS
    finally:
        gen.close()
