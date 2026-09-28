from datetime import datetime, timezone

import pytest

from app.models import (
    HistoricalGexSnapshot,
    IVObservation,
    NiftyCandle,
    OptionCandle,
    OptionGreeks,
)
from app.services.point_in_time import PointInTimeDataset


BASE_TS = datetime(2026, 8, 27, 10, 0)
FUTURE_TS = datetime(2026, 8, 27, 10, 3)


def _nifty(ts, close):
    return NiftyCandle(
        symbol="NIFTY",
        interval="3min",
        open_time=ts,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=1000,
    )


def _option(ts, key="TEST|CE", oi=100):
    return OptionCandle(
        instrument_key=key,
        interval="3min",
        open_time=ts,
        open=100,
        high=105,
        low=95,
        close=102,
        volume=10,
        open_interest=oi,
        fetched_at=ts,
    )


def _iv(ts, iv=0.18):
    return IVObservation(
        symbol="NIFTY",
        expiry="2026-09-03",
        strike=24500,
        option_type="call",
        iv=iv,
        spot=24500,
        source="test",
        observed_at=ts,
    )


def _greeks(ts):
    return OptionGreeks(
        instrument_key="TEST|CE",
        interval="3min",
        open_time=ts,
        spot=24500,
        strike=24500,
        expiry="2026-09-03",
        option_type="CE",
        option_price=100,
        lot_size=65,
        time_to_expiry=0.1,
        risk_free_rate=0.065,
        intrinsic_value=0,
        implied_volatility=0.2,
        delta=0.5,
        gamma=0.001,
        vega=10,
        theta=-5,
        calc_model="BLACK_SCHOLES_EUROPEAN",
        calc_version="greeks_v3",
        calculated_at=ts,
        status="SUCCESS",
    )


def _gex(ts):
    return HistoricalGexSnapshot(
        instrument_key="TEST|CE",
        interval="3min",
        open_time=ts,
        spot=24500,
        strike=24500,
        expiry="2026-09-03",
        option_type="CE",
        gamma=0.001,
        open_interest=100,
        option_price=100,
        lot_size=65,
        raw_gex=1000,
        signed_gex=1000,
        calc_version="h_gex_v1",
        calculated_at=ts,
        status="SUCCESS",
    )


def test_cutoff_is_mandatory_and_timezone_normalized(db_session):
    db_session.add(_nifty(BASE_TS, 24500))
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    rows = pit.nifty_candles(
        "NIFTY",
        "2026-08-27T04:30:00Z",
    )

    assert [row.open_time for row in rows] == [BASE_TS]

    with pytest.raises(ValueError, match="decision timestamp"):
        pit.nifty_candles("NIFTY", "")


@pytest.mark.parametrize(
    ("method_name", "builder"),
    [
        ("nifty_candles", lambda ts: _nifty(ts, 24500)),
        ("option_candles", lambda ts: _option(ts)),
        ("iv_observations", lambda ts: _iv(ts)),
        ("option_greeks", lambda ts: _greeks(ts)),
        ("historical_gex", lambda ts: _gex(ts)),
    ],
)
def test_future_observations_are_invisible_and_boundary_is_inclusive(
    db_session,
    method_name,
    builder,
):
    db_session.add_all([builder(BASE_TS), builder(FUTURE_TS)])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    method = getattr(pit, method_name)

    kwargs = {}
    if method_name == "option_candles":
        kwargs["instrument_key"] = "TEST|CE"
    elif method_name == "option_greeks":
        kwargs["instrument_key"] = "TEST|CE"
    elif method_name == "historical_gex":
        kwargs["instrument_key"] = "TEST|CE"
    else:
        kwargs["symbol"] = "NIFTY"

    kwargs["decision_timestamp"] = BASE_TS
    rows = method(**kwargs)

    assert len(rows) == 1
    if method_name == "iv_observations":
        assert rows[0].observed_at == BASE_TS
    else:
        assert rows[0].open_time == BASE_TS


def test_derived_processing_time_does_not_widen_market_time_visibility(db_session):
    row = _greeks(BASE_TS)
    row.calculated_at = FUTURE_TS
    db_session.add(row)
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    rows = pit.option_greeks("TEST|CE", BASE_TS, calc_version="greeks_v3")

    assert len(rows) == 1
    assert rows[0].open_time == BASE_TS
    assert rows[0].calculated_at == FUTURE_TS
