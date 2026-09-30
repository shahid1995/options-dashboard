from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import aliased, sessionmaker
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


# ---------------------------------------------------------------------------
# CodeRabbit #3 — bounded history loads (lower bound)
# ---------------------------------------------------------------------------


def test_bulk_selections_exclude_history_below_the_earliest_target(db_session):
    """CodeRabbit #3: the bounded load must never materialize history older
    than the earliest decision's target — except the single latest row at
    or before it, which seeds the fallback for that decision.

    Data (3min interval; decision T resolves to target T−3min):
      TEST|CE : 07:00 (old, huge gap) 09:54 10:00 10:03 10:12(future)
      TEST|PE : 07:00 (old, huge gap) 10:03
    Decisions: 10:04 (target 10:01), 10:06 (target 10:03).
      — Each instrument's own latest row ≤ 10:01 is its seed: CE→10:00,
        PE→07:00.  CE's 07:00/09:54 rows are below CE's seed and must NOT
        be loaded (a global open_time <= upper scan would load them).
      — 10:12 is beyond the latest target and must not be loaded.
    Observable selections stay identical to the unbounded contract.
    """
    db_session.add_all([
        _option(datetime(2026, 8, 27, 7, 0), key="TEST|CE", oi=1),
        _option(datetime(2026, 8, 27, 9, 54), key="TEST|CE", oi=500),
        _option(datetime(2026, 8, 27, 10, 0), key="TEST|CE", oi=700),
        _option(datetime(2026, 8, 27, 10, 3), key="TEST|CE", oi=900),
        _option(datetime(2026, 8, 27, 10, 12), key="TEST|CE", oi=9_999),
        _option(datetime(2026, 8, 27, 7, 0), key="TEST|PE", oi=2),
        _option(datetime(2026, 8, 27, 10, 3), key="TEST|PE", oi=55),
        _greeks(datetime(2026, 8, 27, 7, 0)),
        _greeks(datetime(2026, 8, 27, 10, 3)),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decisions = [datetime(2026, 8, 27, 10, 4), datetime(2026, 8, 27, 10, 6)]

    candle_selections = pit.option_candles_selections_at_many(
        decisions, instrument_keys=["TEST|CE", "TEST|PE"])
    greek_selections = pit.option_greeks_selections_at_many(
        decisions, instrument_keys=["TEST|CE", "TEST|PE"])

    # Decision-major pairing is unchanged.
    assert [ts for ts, _ in candle_selections] == decisions

    # 10:04 → target 10:01: CE falls back to the 10:00 source bar (700);
    # PE falls back to its own latest row ≤ 10:01 — the 07:00 bar (oi=2).
    # The load bound is PER INSTRUMENT: PE's 07:00 row is PE's seed and is
    # still served, while CE's 07:00/09:54 rows (below CE's own seed) are
    # never loaded — a global open_time <= upper scan would have loaded them.
    first_candles = candle_selections[0][1]
    assert set(first_candles) == {"TEST|CE", "TEST|PE"}
    assert first_candles["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 0)
    assert first_candles["TEST|CE"].open_interest == 700
    assert first_candles["TEST|PE"].open_time == datetime(2026, 8, 27, 7, 0)
    assert first_candles["TEST|PE"].open_interest == 2

    # 10:06 → target 10:03: both instruments select their 10:03 rows.
    second_candles = candle_selections[1][1]
    assert set(second_candles) == {"TEST|CE", "TEST|PE"}
    assert second_candles["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert second_candles["TEST|CE"].open_interest == 900
    assert second_candles["TEST|PE"].open_time == datetime(2026, 8, 27, 10, 3)

    # Greeks behave identically (per-instrument fallback seed + window row).
    assert [ts for ts, _ in greek_selections] == decisions
    assert set(greek_selections[0][1]) == {"TEST|CE"}
    assert greek_selections[0][1]["TEST|CE"].open_time == datetime(
        2026, 8, 27, 7, 0)  # greeks' latest row ≤ 10:01 IS the 07:00 row
    assert greek_selections[1][1]["TEST|CE"].open_time == datetime(
        2026, 8, 27, 10, 3)


def test_bulk_selections_seed_fallback_from_the_load_floor(db_session):
    """CodeRabbit #3 companion: the latest row at or before the earliest
    target (the load floor) is still available as the fallback seed, and
    window rows after it remain selectable at the appropriate decisions.

    Decisions 10:04/10:06 → targets 10:01/10:03. The 10:00 row (oi=700) is
    the seed for 10:04's fallback; the 10:03 row (oi=900) is the window row
    for 10:06. The 09:00 row is below the floor and must not be loaded.
    """
    db_session.add_all([
        _option(datetime(2026, 8, 27, 9, 0), key="TEST|CE", oi=42),
        _option(datetime(2026, 8, 27, 10, 0), key="TEST|CE", oi=700),
        _option(datetime(2026, 8, 27, 10, 3), key="TEST|CE", oi=900),
    ])
    db_session.commit()

    pit = PointInTimeDataset(db_session)
    decisions = [datetime(2026, 8, 27, 10, 4), datetime(2026, 8, 27, 10, 6)]
    selections = pit.option_candles_selections_at_many(
        decisions, instrument_keys=["TEST|CE"])

    assert [ts for ts, _ in selections] == decisions
    assert selections[0][1]["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 0)
    assert selections[0][1]["TEST|CE"].open_interest == 700
    assert selections[1][1]["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert selections[1][1]["TEST|CE"].open_interest == 900


def test_bulk_selections_bounded_load_source_contract(db_session):
    """Source-contract companion (repo precedent:
    test_production_init_db_has_no_create_all): both paired bulk methods
    must issue a bounded load — a lower-bound seed query plus a windowed
    history query — never an unbounded open_time <= max(targets) scan."""
    import inspect

    from app.services import point_in_time as pit_module

    for method_name in (
        "option_greeks_selections_at_many",
        "option_candles_selections_at_many",
    ):
        source = inspect.getsource(
            getattr(pit_module.PointInTimeDataset, method_name))
        assert "_load_bounded_histories" in source


# ---------------------------------------------------------------------------
# Greptile P2 — grouped seed (portable GROUP BY/max-join, no correlated scan)
# ---------------------------------------------------------------------------


def _correlated_seed_load(self, model, targets, histories, eligibility_filters):
    """The previous seed implementation (correlated scalar MAX), kept as the
    reference the grouped implementation must be behaviorally identical to."""
    lower = min(targets)
    upper = max(targets)
    candidate = aliased(model)
    latest_open_time = (
        select(func.max(candidate.open_time))
        .where(
            candidate.instrument_key == model.instrument_key,
            candidate.open_time <= lower,
            *eligibility_filters(candidate),
        )
        .correlate(model)
        .scalar_subquery()
    )
    seed_statement = select(model).where(
        model.open_time == latest_open_time,
        *eligibility_filters(model),
    )
    for row in self.db.scalars(seed_statement):
        histories.setdefault(row.instrument_key, []).append(
            (row.open_time, row))
    window_statement = select(model).where(
        model.open_time > lower,
        model.open_time <= upper,
        *eligibility_filters(model),
    )
    for row in self.db.scalars(window_statement.order_by(
        model.instrument_key, model.open_time
    )):
        histories.setdefault(row.instrument_key, []).append(
            (row.open_time, row))


SEED_MATRIX_ROWS = [
    # (ts, key, oi) — 3min interval; decision T resolves to target T−3min.
    (datetime(2026, 8, 27, 9, 0), "TEST|CE", 123),    # below CE's seed floor
    (datetime(2026, 8, 27, 10, 0), "TEST|CE", 700),   # CE's seed (target 10:01)
    (datetime(2026, 8, 27, 10, 3), "TEST|CE", 900),   # window row (target 10:03)
    (datetime(2026, 8, 27, 10, 12), "TEST|CE", 9_999),  # beyond latest target
    (datetime(2026, 8, 27, 7, 0), "TEST|PE", 2),      # PE's seed (target 10:01)
    (datetime(2026, 8, 27, 10, 3), "TEST|PE", 55),    # window row (target 10:03)
]
SEED_MATRIX_DECISIONS = [
    datetime(2026, 8, 27, 10, 4),   # target 10:01 → both instruments fall back
    datetime(2026, 8, 27, 10, 6),   # target 10:03 → both select window rows
    datetime(2026, 8, 27, 10, 12),  # target 10:09 → fallback to the 10:03 rows
]


def _seed_matrix_setup(db_session):
    db_session.add_all([
        _option(ts, key=key, oi=oi) for ts, key, oi in SEED_MATRIX_ROWS
    ])
    # Greeks exist for TEST|CE only: 07:00 (its seed) and 10:03 (window row).
    db_session.add_all([
        _greeks(datetime(2026, 8, 27, 7, 0)),
        _greeks(datetime(2026, 8, 27, 10, 3)),
    ])
    db_session.commit()


@pytest.mark.parametrize("use_grouped", [False, True])
def test_bounded_seed_matrix_identical_selections(
    db_session, monkeypatch, use_grouped
):
    """The grouped seed and the previous correlated seed produce identical
    decisions→selections — same keys, same rows, same SOURCE open_time.

    use_grouped=True runs the production implementation; use_grouped=False
    monkeypatches the previous implementation as the reference.
    """
    _seed_matrix_setup(db_session)
    if use_grouped:
        loader = PointInTimeDataset._load_bounded_histories
    else:
        loader = _correlated_seed_load
        monkeypatch.setattr(
            PointInTimeDataset, "_load_bounded_histories", _correlated_seed_load
        )
    pit = PointInTimeDataset(db_session)

    candle_selections = pit.option_candles_selections_at_many(
        SEED_MATRIX_DECISIONS, instrument_keys=["TEST|CE", "TEST|PE"])
    greek_selections = pit.option_greeks_selections_at_many(
        SEED_MATRIX_DECISIONS, instrument_keys=["TEST|CE", "TEST|PE"])

    # Decision-major pairing is preserved.
    assert [ts for ts, _ in candle_selections] == SEED_MATRIX_DECISIONS
    assert [ts for ts, _ in greek_selections] == SEED_MATRIX_DECISIONS

    # Decision 1 (target 10:01): per-instrument fallback to each instrument's
    # own latest eligible row ≤ 10:01, with original source open_time.
    first = candle_selections[0][1]
    assert set(first) == {"TEST|CE", "TEST|PE"}
    assert first["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 0)
    assert first["TEST|CE"].open_interest == 700
    assert first["TEST|PE"].open_time == datetime(2026, 8, 27, 7, 0)
    assert first["TEST|PE"].open_interest == 2

    # Decision 2 (target 10:03): both select their exact 10:03 window rows.
    second = candle_selections[1][1]
    assert set(second) == {"TEST|CE", "TEST|PE"}
    assert second["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert second["TEST|CE"].open_interest == 900
    assert second["TEST|PE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert second["TEST|PE"].open_interest == 55

    # Decision 3 (target 10:09): fallback again — to the 10:03 rows, never to
    # the 10:12 row beyond the latest target.
    third = candle_selections[2][1]
    assert set(third) == {"TEST|CE", "TEST|PE"}
    assert third["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert third["TEST|CE"].open_interest == 900
    assert third["TEST|PE"].open_time == datetime(2026, 8, 27, 10, 3)
    assert third["TEST|PE"].open_interest == 55

    # Greeks: TEST|PE has no rows and must never appear; TEST|CE falls back
    # to its 07:00 seed and then selects the 10:03 window row.
    assert set(greek_selections[0][1]) == {"TEST|CE"}
    assert greek_selections[0][1]["TEST|CE"].open_time == datetime(
        2026, 8, 27, 7, 0)
    assert set(greek_selections[1][1]) == {"TEST|CE"}
    assert greek_selections[1][1]["TEST|CE"].open_time == datetime(
        2026, 8, 27, 10, 3)
    assert set(greek_selections[2][1]) == {"TEST|CE"}
    assert greek_selections[2][1]["TEST|CE"].open_time == datetime(
        2026, 8, 27, 10, 3)


@pytest.mark.parametrize("model_name", ["candles", "greeks"])
def test_grouped_seed_materializes_the_same_history_as_the_correlated_seed(
    db_session, model_name
):
    """Strongest equality proof: for the same data and targets, the grouped
    seed loads EXACTLY the history the correlated seed loaded — per
    instrument, the same (open_time, row) sequence, nothing more, nothing
    less (rows below the per-instrument seed floor and rows beyond the
    latest target stay unloaded in both)."""
    _seed_matrix_setup(db_session)
    pit = PointInTimeDataset(db_session)
    targets = [
        datetime(2026, 8, 27, 10, 1),
        datetime(2026, 8, 27, 10, 3),
        datetime(2026, 8, 27, 10, 9),
    ]

    def _load(loader, model, eligibility):
        histories = {}
        loader(pit, model, targets, histories, eligibility)
        return {
            key: [(open_time, row.id) for open_time, row in history]
            for key, history in histories.items()
        }

    if model_name == "candles":
        eligibility = lambda entity: [entity.interval == "3min"]  # noqa: E731
        model = OptionCandle
    else:
        eligibility = lambda entity: [  # noqa: E731
            entity.interval == "3min",
            entity.status == "SUCCESS",
        ]
        model = OptionGreeks

    correlated = _load(_correlated_seed_load, model, eligibility)
    histories = {}
    pit._load_bounded_histories(model, targets, histories, eligibility)
    grouped = {
        key: [(open_time, row.id) for open_time, row in history]
        for key, history in histories.items()
    }

    assert grouped == correlated
    # And the loaded history is exactly the bounded set: seeds at or before
    # the floor plus window rows — never the below-floor or future rows.
    if model_name == "candles":
        assert {key: [open_time for open_time, _ in seq]
                for key, seq in grouped.items()} == {
            "TEST|CE": [
                datetime(2026, 8, 27, 10, 0),
                datetime(2026, 8, 27, 10, 3),
            ],
            "TEST|PE": [
                datetime(2026, 8, 27, 7, 0),
                datetime(2026, 8, 27, 10, 3),
            ],
        }
    else:
        assert {key: [open_time for open_time, _ in seq]
                for key, seq in grouped.items()} == {
            "TEST|CE": [
                datetime(2026, 8, 27, 7, 0),
                datetime(2026, 8, 27, 10, 3),
            ],
        }


def test_bounded_seed_uses_grouped_max_join_not_correlated_scan(db_session):
    """Implementation contract (repo precedent:
    test_production_init_db_has_no_create_all): the seed must be one portable
    GROUP BY/max-join bulk query — no correlated scalar MAX, and no extra
    seed queries per instrument."""
    import inspect

    source = inspect.getsource(
        PointInTimeDataset._load_bounded_histories)
    assert ".correlate(" not in source, (
        "the seed must not use a correlated scalar subquery"
    )
    assert "group_by(" in source
    assert "func.max(" in source
    # The (lower, upper] window query is retained.
    assert "> lower" in source and "<= upper" in source

    _seed_matrix_setup(db_session)
    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        pit = PointInTimeDataset(db_session)
        pit.option_candles_selections_at_many(
            SEED_MATRIX_DECISIONS, instrument_keys=["TEST|CE", "TEST|PE"])
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    grouped = [s for s in statements
               if "GROUP BY" in s.upper() and "MAX(" in s.upper()]
    assert len(grouped) == 1, (
        f"exactly one grouped seed query expected, got {len(grouped)}"
    )
    # Every MAX-carrying statement must be the grouped one — a correlated
    # scalar MAX would reach the database without a GROUP BY.
    max_statements = [s for s in statements if "MAX(" in s.upper()]
    assert len(max_statements) == len(grouped)
    assert all("GROUP BY" in s.upper() for s in max_statements)


def test_bounded_seed_outer_scan_explicitly_bounded_by_lower(
    db_session, monkeypatch
):
    """Structural regression (Greptile P2): the OUTER ``seed_statement``
    itself must carry a direct ``model.open_time <= lower`` criterion.

    The grouped subquery's own ``open_time <= lower`` bound does not prove
    the outer model scan is bounded, so this inspects the captured
    ``Select`` object's top-level whereclause criteria — never recursively
    through the grouped subquery. Removing only the outer predicate while
    leaving the grouped subquery intact must fail this test.
    """
    from sqlalchemy import Select
    from sqlalchemy.sql import operators as sa_operators
    from sqlalchemy.sql.elements import BooleanClauseList

    _seed_matrix_setup(db_session)
    captured = []
    real_scalars = db_session.scalars

    def _capturing_scalars(statement, *args, **kwargs):
        if isinstance(statement, Select):
            captured.append(statement)
        return real_scalars(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalars", _capturing_scalars)
    pit = PointInTimeDataset(db_session)
    pit.option_candles_selections_at_many(
        SEED_MATRIX_DECISIONS, instrument_keys=["TEST|CE", "TEST|PE"])

    # One grouped seed query plus one window query — the seed runs first.
    assert len(captured) == 2
    seed_statement = captured[0]

    whereclause = seed_statement.whereclause
    criteria = (
        list(whereclause.clauses)
        if isinstance(whereclause, BooleanClauseList)
        else [whereclause]
    )

    def _as_column(expr):
        return (expr.__clause_element__()
                if hasattr(expr, "__clause_element__") else expr)

    direct_bounds = [
        criterion
        for criterion in criteria
        if criterion.operator is sa_operators.le
        and _as_column(criterion.left).name == "open_time"
        and _as_column(criterion.left).table is OptionCandle.__table__
    ]
    assert len(direct_bounds) == 1, (
        "the outer seed_statement must contain exactly one direct "
        "model.open_time <= lower criterion at its top level"
    )
    # The bound is the earliest requested target, not some other constant.
    assert direct_bounds[0].right.value == datetime(2026, 8, 27, 10, 1)


@pytest.mark.parametrize("use_grouped", [False, True])
def test_bounded_seed_edge_cases_preserved(db_session, monkeypatch, use_grouped):
    """instrument_keys=None (no filter), an empty explicit selection, an
    unknown instrument, and an empty database all behave identically under
    both seed implementations."""
    if not use_grouped:
        monkeypatch.setattr(
            PointInTimeDataset, "_load_bounded_histories", _correlated_seed_load
        )
    pit = PointInTimeDataset(db_session)

    # Empty database first: every decision resolves to an empty selection.
    selections = pit.option_candles_selections_at_many(SEED_MATRIX_DECISIONS)
    assert [ts for ts, _ in selections] == SEED_MATRIX_DECISIONS
    assert all(selection == {} for _, selection in selections)

    _seed_matrix_setup(db_session)

    # instrument_keys=None: every instrument is served by its own seed.
    selections = pit.option_candles_selections_at_many(SEED_MATRIX_DECISIONS)
    assert set(selections[0][1]) == {"TEST|CE", "TEST|PE"}
    assert selections[0][1]["TEST|CE"].open_time == datetime(2026, 8, 27, 10, 0)
    assert selections[0][1]["TEST|PE"].open_time == datetime(2026, 8, 27, 7, 0)

    # Empty explicit selection: identical to the long-standing contract.
    assert pit.option_candles_selections_at_many(
        SEED_MATRIX_DECISIONS, instrument_keys=[]) == []

    # Unknown instrument: decision-major empty selections, no rows.
    selections = pit.option_candles_selections_at_many(
        SEED_MATRIX_DECISIONS, instrument_keys=["NOPE|CE"])
    assert [ts for ts, _ in selections] == SEED_MATRIX_DECISIONS
    assert all(selection == {} for _, selection in selections)

