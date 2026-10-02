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
