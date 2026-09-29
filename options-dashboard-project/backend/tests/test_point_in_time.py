from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    HistoricalGexSnapshot,
    IVObservation,
    NiftyCandle,
    OptionCandle,
    OptionGreeks,
)
from app.services import iv_history
from app.services.point_in_time import PointInTimeDataset


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
        "2026-08-27T04:33:00Z",
    )

    assert [row.open_time for row in rows] == [BASE_TS]

    with pytest.raises(ValueError, match="decision timestamp"):
        pit.nifty_candles("NIFTY", "")

def test_completed_candle_boundary_excludes_unfinished_intrabar_data(db_session):
    db_session.add_all([
        _nifty(BASE_TS, 24500),
        _nifty(FUTURE_TS, 24510),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)

    assert pit.nifty_candles("NIFTY", datetime(2026, 8, 27, 10, 1)) == []
    assert [row.open_time for row in pit.nifty_candles(
        "NIFTY", FUTURE_TS,
    )] == [BASE_TS]


def test_off_grid_lookup_uses_latest_completed_bar(db_session):
    db_session.add_all([
        _nifty(BASE_TS, 24500),
        _nifty(FUTURE_TS, 24510),
        _option(BASE_TS),
        _option(FUTURE_TS),
        _option(datetime(2026, 8, 27, 9, 57), key="TEST|PE"),
        _greeks(BASE_TS),
        _greeks(FUTURE_TS),
        _gex(BASE_TS),
        _gex(FUTURE_TS),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decision = datetime(2026, 8, 27, 10, 4)

    assert [row.open_time for row in pit.nifty_candles_at(decision)] == [BASE_TS]
    option_rows = pit.option_candles_at(
        decision, instrument_keys=["TEST|CE", "TEST|PE"],
    )
    assert {row.instrument_key: row.open_time for row in option_rows} == {
        "TEST|CE": BASE_TS,
        "TEST|PE": datetime(2026, 8, 27, 9, 57),
    }
    assert [row.open_time for row in pit.option_greeks_at(
        decision, instrument_keys=["TEST|CE"],
    )] == [BASE_TS]
    assert [row.open_time for row in pit.historical_gex_at(decision)] == [BASE_TS]



@pytest.mark.parametrize(
    ("method_name", "builder", "decision_timestamp"),
    [
        ("nifty_candles", lambda ts: _nifty(ts, 24500), FUTURE_TS),
        ("option_candles", lambda ts: _option(ts), FUTURE_TS),
        ("iv_observations", lambda ts: _iv(ts), BASE_TS),
        ("option_greeks", lambda ts: _greeks(ts), FUTURE_TS),
        ("historical_gex", lambda ts: _gex(ts), FUTURE_TS),
    ],
)
def test_future_observations_are_invisible_and_completed_boundary_is_respected(
    db_session,
    method_name,
    builder,
    decision_timestamp,
):
    db_session.add_all([builder(BASE_TS), builder(FUTURE_TS)])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    method = getattr(pit, method_name)

    kwargs = {}
    if method_name in {"option_candles", "option_greeks", "historical_gex"}:
        kwargs["instrument_key"] = "TEST|CE"
    else:
        kwargs["symbol"] = "NIFTY"

    kwargs["decision_timestamp"] = decision_timestamp
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
    rows = pit.option_greeks("TEST|CE", FUTURE_TS, calc_version="greeks_v3")

    assert len(rows) == 1
    assert rows[0].open_time == BASE_TS
    assert rows[0].calculated_at == FUTURE_TS

def test_bulk_feature_reads_preserve_timestamp_cutoff(db_session):
    """Bulk PIT reads include multiple historical timestamps without widening their boundaries."""
    db_session.add_all([
        _greeks(BASE_TS),
        _greeks(FUTURE_TS),
        _option(BASE_TS),
        _option(FUTURE_TS),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decision_times = [FUTURE_TS, datetime(2026, 8, 27, 10, 6)]
    greeks = pit.option_greeks_at_many(decision_times, calc_version="greeks_v3")
    candles = pit.option_candles_at_many(decision_times)

    assert {row.open_time for row in greeks} == {BASE_TS, FUTURE_TS}
    assert {row.open_time for row in candles} == {BASE_TS, FUTURE_TS}


def test_empty_instrument_selection_returns_no_option_features(db_session):
    db_session.add_all([
        _option(BASE_TS, key="TEST|CE"),
        _option(BASE_TS, key="TEST|PE"),
        _greeks(BASE_TS),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decision = FUTURE_TS

    assert pit.option_candles_at(decision, instrument_keys=[]) == []
    assert pit.option_candles_at_many(
        [decision], instrument_keys=[],
    ) == []
    assert pit.option_greeks_at(decision, instrument_keys=[]) == []
    assert pit.option_greeks_at_many(
        [decision], instrument_keys=[],
    ) == []
    assert pit.option_candles_selections_at_many(
        [decision], instrument_keys=[],
    ) == []
    assert pit.option_greeks_selections_at_many(
        [decision], instrument_keys=[],
    ) == []


def test_bulk_fallback_preserves_decision_to_source_association(db_session):
    """A fallback 10:00 candle selected by two later decisions keeps its 10:00
    source observation time for each decision — the bulk pairing must expose
    which decision requested which source row so research can never relabel a
    stale bar as a fresh observation."""
    db_session.add(_option(datetime(2026, 8, 27, 10, 0), oi=700))
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decisions = [
        datetime(2026, 8, 27, 10, 3),
        datetime(2026, 8, 27, 10, 4),
    ]
    selections = pit.option_candles_selections_at_many(
        decisions, instrument_keys=None,
    )

    assert [decision for decision, _ in selections] == decisions
    for decision, selection in selections:
        assert set(selection) == {"TEST|CE"}
        assert selection["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 0)
        assert selection["TEST|CE"].open_interest == 700


def test_bulk_selection_honors_completed_bar_boundaries(db_session):
    """Decision 10:04 selects the 10:00 bar, 10:06 selects 10:03 when it
    exists, and a future bar (10:06) stays invisible to earlier decisions."""
    db_session.add_all([
        _option(datetime(2026, 8, 27, 10, 0), oi=700),
        _option(datetime(2026, 8, 27, 10, 3), oi=900),
        _option(datetime(2026, 8, 27, 10, 6), oi=1200),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    by_decision = dict(pit.option_candles_selections_at_many(
        [
            datetime(2026, 8, 27, 10, 4),
            datetime(2026, 8, 27, 10, 5),
            datetime(2026, 8, 27, 10, 6),
        ],
        instrument_keys=None,
    ))

    # 10:03 is not yet completed at 10:04 or 10:05; the 10:06 bar is future.
    assert by_decision[datetime(2026, 8, 27, 10, 4)]["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 0)
    assert by_decision[datetime(2026, 8, 27, 10, 4)]["TEST|CE"].open_interest == 700
    assert by_decision[datetime(2026, 8, 27, 10, 5)]["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 0)
    # At 10:06 the 10:03 bar has completed and is selected; 10:06 is still future.
    assert by_decision[datetime(2026, 8, 27, 10, 6)]["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert by_decision[datetime(2026, 8, 27, 10, 6)]["TEST|CE"].open_interest == 900


def test_bulk_selections_are_decision_major_when_instruments_appear_later(db_session):
    """An instrument with no completed row yet is absent from that decision's
    selection instead of shifting rows across decisions."""
    db_session.add_all([
        _option(datetime(2026, 8, 27, 10, 3), key="TEST|PE", oi=60),
        _option(datetime(2026, 8, 27, 10, 3), oi=900),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decisions = [
        datetime(2026, 8, 27, 10, 4),
        datetime(2026, 8, 27, 10, 6),
    ]
    selections = pit.option_candles_selections_at_many(
        decisions, instrument_keys=None,
    )

    assert [decision for decision, _ in selections] == decisions
    # At 10:04 no 3-minute bar has completed yet (10:03 completes at 10:06),
    # so the first decision's selection is empty — rows from later decisions
    # must not shift backwards into it.
    assert selections[0][1] == {}
    assert set(selections[1][1]) == {"TEST|CE", "TEST|PE"}
    assert selections[1][1]["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert selections[1][1]["TEST|PE"].open_time == datetime(2026, 8, 27, 10, 3)


def test_bulk_equivalence_between_paired_and_flat_reads(db_session):
    """The paired bulk API and the flat bulk API must agree exactly."""
    db_session.add_all([
        _option(datetime(2026, 8, 27, 9, 57), key="TEST|PE", oi=50),
        _option(datetime(2026, 8, 27, 10, 0), oi=700),
        _option(datetime(2026, 8, 27, 10, 3), oi=900),
        _greeks(datetime(2026, 8, 27, 10, 0)),
        _greeks(datetime(2026, 8, 27, 10, 3)),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decisions = [
        datetime(2026, 8, 27, 10, 3),
        datetime(2026, 8, 27, 10, 4),
        datetime(2026, 8, 27, 10, 6),
        datetime(2026, 8, 27, 10, 4),  # duplicate decision timestamp
    ]

    paired_candles = pit.option_candles_selections_at_many(
        decisions, instrument_keys=None,
    )
    flat_candles = pit.option_candles_at_many(decisions, instrument_keys=None)
    assert [row.open_time for row in flat_candles] == [
        row.open_time
        for _, selection in paired_candles
        for _, row in sorted(selection.items())
    ]
    assert [row.open_interest for row in flat_candles] == [
        row.open_interest
        for _, selection in paired_candles
        for _, row in sorted(selection.items())
    ]

    by_decision = dict(paired_candles)
    assert by_decision[decisions[0]]["TEST|CE"].open_time == BASE_TS
    assert by_decision[decisions[0]]["TEST|CE"].open_interest == 700
    assert by_decision[decisions[1]]["TEST|CE"].open_time == BASE_TS
    # 10:06 resolves to the freshly completed 10:03 bar plus the stale 09:57
    # PE bar with its original source observation time intact.
    assert by_decision[decisions[2]]["TEST|CE"].open_time == FUTURE_TS
    assert by_decision[decisions[2]]["TEST|CE"].open_interest == 900
    assert by_decision[decisions[2]]["TEST|PE"].open_time == datetime(2026, 8, 27, 9, 57)
    assert by_decision[decisions[3]] == by_decision[decisions[1]]

    by_greek_decision = dict(pit.option_greeks_selections_at_many(
        decisions, instrument_keys=None,
    ))
    assert by_greek_decision[decisions[0]]["TEST|CE"].open_time == BASE_TS
    assert by_greek_decision[decisions[1]]["TEST|CE"].open_time == BASE_TS
    assert by_greek_decision[decisions[2]]["TEST|CE"].open_time == FUTURE_TS
    assert by_greek_decision[decisions[3]] == by_greek_decision[decisions[1]]


def test_iv_timestamp_is_normalized_before_pit_comparison(db_session):
    """A UTC IV timestamp is normalized to IST before applying the PIT cutoff."""
    iv_history.record_iv_observations(
        db_session,
        [{
            "timestamp": "2026-08-27T04:33:00Z",
            "symbol": "NIFTY",
            "expiry": "2026-09-03",
            "strike": 24500,
            "optionType": "call",
            "iv": 0.18,
            "spot": 24500,
            "source": "test",
        }],
    )

    pit = PointInTimeDataset(db_session)
    rows = pit.iv_observations("NIFTY", BASE_TS)

    assert rows == []


def test_bulk_at_many_uses_one_loaded_history_per_instrument(db_session):
    """Bulk PIT reads preserve latest-completed semantics across off-grid cutoffs."""
    db_session.add_all([
        _option(datetime(2026, 8, 27, 10, 0), key="TEST|CE", oi=100),
        _option(datetime(2026, 8, 27, 10, 3), key="TEST|CE", oi=110),
        _greeks(datetime(2026, 8, 27, 10, 0)),
        _greeks(datetime(2026, 8, 27, 10, 3)),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decisions = [
        datetime(2026, 8, 27, 10, 4),
        datetime(2026, 8, 27, 10, 6),
    ]

    candle_rows = pit.option_candles_at_many(decisions, instrument_keys=["TEST|CE"])
    greek_rows = pit.option_greeks_at_many(decisions, instrument_keys=["TEST|CE"])

    assert [row.open_time for row in candle_rows] == [
        datetime(2026, 8, 27, 10, 0),
        datetime(2026, 8, 27, 10, 3),
    ]
    assert [row.open_time for row in greek_rows] == [
        datetime(2026, 8, 27, 10, 0),
        datetime(2026, 8, 27, 10, 3),
    ]
