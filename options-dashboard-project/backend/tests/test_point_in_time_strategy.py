from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import HistoricalGexSnapshot, NiftyCandle, OptionCandle, OptionGreeks
from app.services.point_in_time_strategy import build_point_in_time_strategy_inputs


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def test_build_point_in_time_strategy_inputs_uses_completed_bar(db_session):
    decision_ts = datetime(2026, 8, 27, 10, 3)
    open_ts = datetime(2026, 8, 27, 10, 0)
    future_ts = datetime(2026, 8, 27, 10, 3)

    db_session.add_all([
        NiftyCandle(
            symbol="NIFTY", interval="3min", open_time=open_ts,
            open=24500, high=24510, low=24490, close=24505, volume=1000,
        ),
        NiftyCandle(
            symbol="NIFTY", interval="3min", open_time=future_ts,
            open=24505, high=24520, low=24500, close=24515, volume=1000,
        ),
        OptionCandle(
            instrument_key="TEST|CE", interval="3min", open_time=open_ts,
            open=100, high=105, low=95, close=102, volume=10,
            open_interest=100, fetched_at=open_ts,
        ),
        OptionCandle(
            instrument_key="TEST|CE", interval="3min", open_time=future_ts,
            open=102, high=110, low=100, close=108, volume=10,
            open_interest=150, fetched_at=future_ts,
        ),
        OptionGreeks(
            instrument_key="TEST|CE", interval="3min", open_time=open_ts,
            spot=24505, strike=24500, expiry="2026-09-03", option_type="CE",
            option_price=102, lot_size=65, time_to_expiry=0.1,
            risk_free_rate=0.065, intrinsic_value=5, implied_volatility=0.2,
            delta=0.5, gamma=0.001, vega=10, theta=-5,
            calc_model="BLACK_SCHOLES_EUROPEAN", calc_version="greeks_v3",
            calculated_at=future_ts, status="SUCCESS",
        ),
        HistoricalGexSnapshot(
            instrument_key="TEST|CE", interval="3min", open_time=open_ts,
            spot=24505, strike=24500, expiry="2026-09-03", option_type="CE",
            gamma=0.001, open_interest=100, option_price=102, lot_size=65,
            raw_gex=1000, signed_gex=1000, calc_version="h_gex_v1",
            calculated_at=future_ts, status="SUCCESS",
        ),
    ])
    db_session.commit()

    result = build_point_in_time_strategy_inputs(
        db_session,
        decision_ts,
        instrument_keys=["TEST|CE"],
    )

    assert result.spot == 24505
    assert {row.open_time for row in result.option_candles} == {open_ts}
    assert {row.open_time for row in result.option_greeks} == {open_ts}
    assert {row.open_time for row in result.historical_gex} == {open_ts}


# ---------------------------------------------------------------------------
# Day 49 remediation: the strategy-input seam must honor instrument_keys for
# GEX exactly as it does for candles and Greeks.
# ---------------------------------------------------------------------------


def _gex(ts, key, signed=1000):
    return HistoricalGexSnapshot(
        instrument_key=key, interval="3min", open_time=ts,
        spot=24505, strike=24500, expiry="2026-09-03", option_type="CE",
        gamma=0.001, open_interest=100, option_price=102, lot_size=65,
        raw_gex=signed, signed_gex=signed, calc_version="h_gex_v1",
        calculated_at=ts, status="SUCCESS",
    )


def test_strategy_inputs_exclude_unrelated_gex_instruments(db_session):
    """REQUIRED (Day 49 remediation): the strategy-input bundle must not carry
    GEX rows for instruments outside the requested universe. Before the fix it
    called ``historical_gex_at`` (which has no instrument filter), so every
    instrument with any eligible GEX history — including unrelated and expired
    contracts — leaked into ``historical_gex``."""
    decision_ts = datetime(2026, 8, 27, 10, 3)
    open_ts = datetime(2026, 8, 27, 10, 0)
    db_session.add_all([
        _gex(open_ts, "WANT|CE", signed=1000),
        _gex(open_ts, "OTHER|CE", signed=9999),
        _gex(open_ts, "EXPIRED|PE", signed=5555),
    ])
    db_session.commit()

    result = build_point_in_time_strategy_inputs(
        db_session, decision_ts, instrument_keys=["WANT|CE"],
    )

    # Only the requested instrument is present.
    assert [row.instrument_key for row in result.historical_gex] == ["WANT|CE"]
    assert result.historical_gex[0].open_time == open_ts


def test_strategy_inputs_empty_universe_yields_no_gex(db_session):
    """An empty requested universe returns no GEX — never the whole snapshot
    set (the same empty-universe contract as the candle/Greeks selections)."""
    decision_ts = datetime(2026, 8, 27, 10, 3)
    open_ts = datetime(2026, 8, 27, 10, 0)
    db_session.add(_gex(open_ts, "SOME|CE", signed=1000))
    db_session.commit()

    result = build_point_in_time_strategy_inputs(
        db_session, decision_ts, instrument_keys=[],
    )

    assert result.historical_gex == ()
    assert result.option_candles == ()
    assert result.option_greeks == ()


def test_strategy_inputs_keep_the_pit_timestamp_boundary(db_session):
    """The PIT boundary still applies to GEX: a snapshot whose source bar has
    not completed by the decision time stays invisible, and a requested
    instrument that only has future GEX contributes nothing."""
    decision_ts = datetime(2026, 8, 27, 10, 3)
    open_ts = datetime(2026, 8, 27, 10, 0)
    future_ts = datetime(2026, 8, 27, 10, 3)
    db_session.add_all([
        _gex(open_ts, "WANT|CE", signed=1000),
        _gex(future_ts, "WANT|CE", signed=2000),
        _gex(future_ts, "LATE|CE", signed=3000),
    ])
    db_session.commit()

    result = build_point_in_time_strategy_inputs(
        db_session, decision_ts, instrument_keys=["WANT|CE", "LATE|CE"],
    )

    # Only WANT|CE has a completed bar; it resolves to its 10:00 source row and
    # is never relabeled to the decision time. LATE|CE has only a future bar.
    assert [
        (row.instrument_key, row.open_time, row.signed_gex)
        for row in result.historical_gex
    ] == [("WANT|CE", open_ts, 1000)]
