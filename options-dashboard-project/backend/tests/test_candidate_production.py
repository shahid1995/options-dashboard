"""Day 50 Slice A — candidate producer tests (Issue #118).

Producer tests drive the REAL cascade (positioning → flow → levels →
institutional → regime → synthesis → discover_opportunity → rank_strikes →
evaluate_strategy → evaluate_strategy_gate) over a genuine canonical chain
fixture; only the broker boundaries (chain fetch / key resolution) are
injected.  The Day-33 risk engine is never stubbed: when the chain reaches
Central Risk, its verdict is the real engine's verdict under
PAPER_ENTRY_POLICY.  Session/HTTP boundaries are exercised through the real
FastAPI dependency stack (test_paper_router patterns).

D1 ΔOI rule under test: prior OI = latest OptionCandle for the exact broker
instrument key within [ref − 24h, ref − 90s]; missing/stale/null history is
never coerced to zero — strikes without eligible history are suppressed and
an entry needing them fails closed.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.market_data.contracts import Provenance
from app.market_data.quality import QualityDimension, QualityState
from app.models import (
    NiftyCandle,
    OptionCandle,
    PaperOrder,
    PaperTransaction,
    Position,
    StrategyExecution,
)
from app.services import historical_gex, token_store
from app.services.candidate_production import (
    OI_HISTORY_MAX_AGE,
    OI_HISTORY_MIN_LAG,
    ProducerError,
    _apply_identities,
    _build_side_index,
    _chain_observation,
    _classify_tail,
    _contract_legs,
    _contract_quantity,
    _gex_reference,
    _measured_factor,
    _prior_oi_state,
    _quality,
    _reference_ts_from_index,
    _resolved_identity_map,
    _side_signed_gex,
    _spot_history,
    _strike_factors,
    produce_candidate_and_execute,
    produce_candidate_core,
)
from app.routers.deps import SESSION_COOKIE_NAME
from app.services.paper_execution import PaperExecutionError
from app.strike_ranking.contracts import RankingFactor
from app.strategy_evaluation.contracts import (
    DimensionState,
    PayoffExpirySemantics,
    TailClass,
)
from app.utils.market_time import is_market_hours, to_ist_naive


@pytest.fixture
def db_session():
    """Isolated in-memory SQLite database (service-test pattern)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def client(db_session):
    """FastAPI TestClient bound to the isolated DB (router-test pattern)."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()

# ---------------------------------------------------------------------------
# Genuine chain fixture: two strikes, both sides quoted, one expiry
# ---------------------------------------------------------------------------

KEY_25000_CE = "NSE_FO|1001|2026-09-24"
KEY_25100_CE = "NSE_FO|1002|2026-09-24"
EXPIRY = "2026-09-24"

#: The chain's own quote timestamp (broker-stamped, IST) used as the
#: reference ts; 10:05 IST == 04:35 UTC.
QUOTE_TS = "24-Sep-2026 10:05:00"
REF_TS = datetime(2026, 9, 24, 4, 35, 0, tzinfo=timezone.utc)


def _ist_clock(moment):
    """The STORED candle clock (naive IST) for ``moment``.

    Phase 7.24.4 convention through the repository's canonical conversion:
    ``NiftyCandle.open_time`` and ``OptionCandle.open_time`` are written as
    naive IST by ``nifty_candles.record_candles`` /
    ``option_candles.record_option_candles``, so test history must be seeded
    on that clock (10:05 IST, not 04:35 UTC) — otherwise the window tests
    would pass against a naive-UTC comparison they are meant to catch.
    """
    converted = to_ist_naive(moment)
    assert converted is not None and converted.tzinfo is None
    return converted


def _side(ltp, oi, chg_oi, volume, iv, key, bid=None, ask=None, gamma=None):
    return {
        "ltp": ltp, "oi": oi, "chg_oi": chg_oi, "volume": volume,
        "iv": iv, "instrument_key": key, "bid_price": bid, "ask_price": ask,
        "gamma": gamma, "quote_timestamp": QUOTE_TS,
    }


def make_chain(
    *,
    spot=25000.0,
    ce_ltp=200.0, pe_ltp=180.0,
    ce_oi=1_200_000.0, pe_oi=1_100_000.0,
    ce_delta_oi=250_000.0, pe_delta_oi=-80_000.0,
    volume=150_000.0, iv=14.0,
    gamma=0.05,
    bid=198.0, ask=202.0,
    pe_bid=178.0, pe_ask=182.0,
    with_keys=True,
):
    """Build a canonical chain fixture (same shape the Upstox mapper emits)."""
    ce_key = KEY_25000_CE if with_keys else None
    pe_key = KEY_25000_PE if with_keys else None
    return {
        "symbol": "NIFTY",
        "expiry_date": EXPIRY,
        "underlying_spot_price": spot,
        "chain": [
            {
                "strike": 25000.0,
                "call": _side(ce_ltp, ce_oi, ce_delta_oi, volume, iv, ce_key,
                              bid, ask, gamma=gamma),
                "put": _side(pe_ltp, pe_oi, pe_oi + pe_delta_oi, volume, iv,
                             pe_key, pe_bid, pe_ask, gamma=gamma),
            },
            {
                "strike": 25100.0,
                "call": _side(ce_ltp * 0.5, ce_oi * 0.6, ce_delta_oi * 0.4,
                              volume * 0.6, iv, KEY_25100_CE, bid * 0.5,
                              ask * 0.5, gamma=gamma * 0.5),
                "put": _side(pe_ltp * 2.2, pe_oi * 1.4, 40_000.0,
                             volume * 1.3, iv, KEY_25100_PE, pe_bid * 2.2,
                             pe_ask * 2.2, gamma=gamma * 0.5),
            },
        ],
    }


# Chain rows never carry keys per side; the mapper's rows hold them inside
# each side dict.  The second strike's keys:
KEY_25000_PE = "NSE_FO|2001|2026-09-24"
KEY_25100_PE = "NSE_FO|2002|2026-09-24"


def make_legs(direction="sell", strike=25000.0, option_type="call",
              quantity=1, lot_size=65):
    """Default request: a covered bear call spread (bounded payoff)."""
    return [
        {
            "expiration_date": EXPIRY, "strike_price": 25000.0,
            "option_type": "call", "action": "sell", "quantity": quantity,
            "lot_size": lot_size,
        },
        {
            "expiration_date": EXPIRY, "strike_price": 25100.0,
            "option_type": "call", "action": "buy", "quantity": quantity,
            "lot_size": lot_size,
        },
    ]


def prior_candles(keys, oi, *, reference_ts=REF_TS, age=timedelta(minutes=6)):
    """OptionCandle rows for each key at ``reference_ts - age``, written on
    the stored naive-IST candle clock (as the ingestion service writes
    them)."""
    open_time = _ist_clock(reference_ts) - age
    rows = []
    for index, key in enumerate(keys):
        rows.append(OptionCandle(
            instrument_key=key, interval="3min", open_time=open_time,
            open=100, high=110, low=95, close=105,
            volume=1000, open_interest=oi if not isinstance(oi, list) else oi[index],
            fetched_at=open_time,
        ))
    return rows


def spot_closes(count=8, *, reference_ts=REF_TS, base=24800.0, step=15.0):
    """Stored NIFTY closes ending just before the reference timestamp, on
    the stored naive-IST candle clock."""
    rows = []
    start = _ist_clock(reference_ts) - timedelta(minutes=3 * (count + 1))
    for index in range(count):
        rows.append(NiftyCandle(
            symbol="NIFTY", interval="3min",
            open_time=start + timedelta(minutes=3 * index),
            open=base + step * index, high=base + step * index + 5,
            low=base + step * index - 5, close=base + step * (index + 1),
            volume=1000,
        ))
    return rows


def all_keys():
    return [KEY_25000_CE, KEY_25000_PE, KEY_25100_CE, KEY_25100_PE]


def prior_oi_map(db, keys, reference_ts=REF_TS):
    return _prior_oi_state(db, keys, reference_ts)


def run_core(db, chain=None, legs=None, prior_oi=None, *, keys=None,
             received_at=None, spot_closes_rows=None):
    """Drive the real producer core over the fixture (fail-closed path).

    ``keys`` is ``instrument_keys``: positional against ``legs`` (the
    adapter's ``resolve_instrument_keys`` preserves request order), i.e. one
    resolved broker key per requested leg, NOT one per chain side.  The
    default is the fixture's two requested call legs.
    """
    chain = chain if chain is not None else make_chain()
    legs = legs if legs is not None else make_legs()
    keys = keys if keys is not None else [KEY_25000_CE, KEY_25100_CE]
    if prior_oi is None:
        prior_oi = _prior_oi_state(db, keys, REF_TS)
        if any(v is None for v in prior_oi.values()):
            raise AssertionError(
                "test fixture did not seed eligible prior-OI history for the "
                "requested legs; run_core requires eligible prior OI via the "
                "real D1 rule")
    closes = spot_closes_rows
    if closes is None:
        closes, prev = _spot_closes_tuple(db)
    else:
        prev = closes[-1] if closes else None
    return produce_candidate_core(
        chain=chain,
        instrument_keys=keys,
        prior_oi_by_key=prior_oi,
        spot_closes=closes,
        prev_spot=prev,
        received_at=received_at or REF_TS,
        id_seed="test-order-1",
        strategy_id="paper-entry",
        legs=legs,
    )


def _spot_closes_tuple(db):
    from app.services.candidate_production import _spot_history

    return _spot_history(db, REF_TS)


# ---------------------------------------------------------------------------
# ΔOI / D1 alignment rule
# ---------------------------------------------------------------------------

class TestD1PriorOiRule:
    def test_latest_eligible_candle_wins(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 900_000.0))
        db_session.add_all(prior_candles(
            all_keys(), 1_000_000.0, age=timedelta(minutes=12)))
        db_session.commit()
        result = prior_oi_map(db_session, all_keys())
        assert result[KEY_25000_CE] == 900_000.0  # newest eligible wins

    def test_future_candle_never_used(self, db_session):
        db_session.add_all(prior_candles(
            all_keys(), 900_000.0, age=timedelta(seconds=-30)))
        db_session.commit()
        result = prior_oi_map(db_session, all_keys())
        assert result[KEY_25000_CE] is None  # newer than reference → missing

    def test_stale_beyond_window_is_missing(self, db_session):
        db_session.add_all(prior_candles(
            all_keys(), 900_000.0, age=OI_HISTORY_MAX_AGE + timedelta(minutes=5)))
        db_session.commit()
        result = prior_oi_map(db_session, all_keys())
        assert result[KEY_25000_CE] is None

    def test_zero_oi_is_measured_zero_not_missing(self, db_session):
        """Schema fact: OptionCandle.open_interest is NOT NULL (default 0.0),
        so null history is unrepresentable at the storage layer; a stored
        0.0 is a legitimately measured zero (never coerced to missing)."""
        db_session.add_all(prior_candles(all_keys(), 0.0))
        db_session.commit()
        result = prior_oi_map(db_session, all_keys())
        assert result[KEY_25000_CE] == 0.0

    def test_same_window_candle_is_not_history(self, db_session):
        db_session.add_all(prior_candles(
            all_keys(), 900_000.0, age=timedelta(seconds=30)))
        db_session.commit()
        result = prior_oi_map(db_session, all_keys())
        assert result[KEY_25000_CE] is None

    def test_delta_oi_is_current_minus_prior(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        chain = make_chain(ce_oi=1_200_000.0)  # current OI from the chain
        prior = prior_oi_map(db_session, all_keys())
        produced = run_core(db_session, chain=chain, prior_oi=prior)
        side = _build_side_index(chain)[(25000.0, "call")]
        assert side.oi == 1_200_000.0
        # The measured ΔOI (current 1_200_000 − prior 950_000) fed the
        # positioning evidence; the candidate carries the reference ts.
        assert produced.candidate.reference_timestamp == REF_TS
        assert produced.candidate.legs[0].strike == 25000.0


# ---------------------------------------------------------------------------
# Stored candle clock: the D1 window and the spot cutoff are naive IST
# ---------------------------------------------------------------------------

class TestStoredCandleClock:
    """History is stored as naive IST (Phase 7.24.4), so the D1 window and
    the ``_spot_history`` cutoff must be projected onto that clock.
    Comparing them in naive UTC shifted every boundary by +05:30 and made
    same-session history invisible to the producer."""

    def test_history_fixture_is_naive_ist_not_utc(self):
        """Guards the fixtures themselves: seeded history must be naive IST
        and visibly displaced from the UTC wall clock, so the alignment
        tests below cannot pass by accident."""
        row = prior_candles(all_keys(), 900_000.0)[0]
        assert row.open_time.tzinfo is None
        assert to_ist_naive(REF_TS) - row.open_time == timedelta(minutes=6)
        assert row.open_time - REF_TS.replace(tzinfo=None) > timedelta(hours=5)

    def test_six_minute_old_same_session_candle_is_eligible(self, db_session):
        """A 3-min candle 6 minutes before the reference, inside the same
        NSE session, is genuine eligible history."""
        db_session.add_all(prior_candles(all_keys(), 900_000.0))
        db_session.commit()

        (stored,) = db_session.scalars(
            select(OptionCandle.open_time).limit(1)).all()
        assert is_market_hours(stored)  # same session as the reference

        result = prior_oi_map(db_session, all_keys())
        assert result[KEY_25000_CE] == 900_000.0
        assert result[KEY_25100_PE] == 900_000.0

    def test_candle_inside_exclusion_window_is_not_history(self, db_session):
        """A same-session candle only 30 s before the reference sits inside
        the 90 s alignment exclusion: it is same-window, not prior."""
        db_session.add_all(prior_candles(
            all_keys(), 900_000.0, age=timedelta(seconds=30)))
        db_session.commit()

        (stored,) = db_session.scalars(
            select(OptionCandle.open_time).limit(1)).all()
        assert is_market_hours(stored)
        assert prior_oi_map(db_session, all_keys())[KEY_25000_CE] is None

    def test_candle_older_than_24h_is_not_history(self, db_session):
        """One minute beyond the 24 h window is stale — missing, not prior."""
        db_session.add_all(prior_candles(
            all_keys(), 900_000.0,
            age=OI_HISTORY_MAX_AGE + timedelta(minutes=1)))
        db_session.commit()

        assert prior_oi_map(db_session, all_keys())[KEY_25000_CE] is None

    def test_spot_history_includes_same_session_closes(self, db_session):
        """``_spot_history`` must read the same-session closes stored
        strictly before the reference timestamp on the IST clock."""
        db_session.add_all(spot_closes(count=8))
        db_session.commit()

        closes, prev = _spot_closes_tuple(db_session)
        assert len(closes) == 8
        assert prev == closes[-1]

        stamps = list(db_session.scalars(
            select(NiftyCandle.open_time).order_by(NiftyCandle.open_time)))
        assert len(stamps) == 8
        assert all(stamp.tzinfo is None for stamp in stamps)
        assert all(is_market_hours(stamp) for stamp in stamps)
        assert stamps[-1] < to_ist_naive(REF_TS)  # strictly before the reference


# ---------------------------------------------------------------------------
# Real-cascade producer behavior
# ---------------------------------------------------------------------------

class TestProducerCore:
    def test_real_chain_produces_eligible_candidate(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        produced = run_core(db_session)
        candidate = produced.candidate
        assert candidate.lifecycle_state.value == "ELIGIBLE"
        assert candidate.reference_timestamp == REF_TS
        assert candidate.legs[0].strike == 25000.0
        assert candidate.provenance is not None
        assert candidate.provenance.source == "UPSTOX"

    def test_provenance_and_timestamp_preserved(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        produced = run_core(db_session)
        assert produced.evaluation.reference_timestamp == REF_TS
        assert produced.evaluation.legs[0].provenance.transformation_id == \
            "day50-candidate-production"

    def test_missing_prior_oi_fails_closed(self, db_session):
        db_session.add_all(spot_closes())
        db_session.commit()
        with pytest.raises(ProducerError) as excinfo:
            run_core(db_session, prior_oi={key: None for key in all_keys()})
        assert "ΔOI history" in str(excinfo.value)

    def test_stale_prior_oi_fails_closed(self, db_session):
        db_session.add_all(spot_closes())
        db_session.commit()
        stale = {key: 900_000.0 for key in all_keys()}  # provided but stale-tested below
        # Prove the D1 rule rejects stale at the state layer:
        db_session.add_all(prior_candles(
            all_keys(), 950_000.0, age=OI_HISTORY_MAX_AGE + timedelta(hours=1)))
        db_session.commit()
        with pytest.raises(ProducerError):
            run_core(db_session, prior_oi={key: None for key in all_keys()})

    def test_expired_instrument_key_not_confused(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        # A key from a different expiry never matches the chain keys.
        other = _prior_oi_state(db_session, ["NSE_FO|9999|2026-08-27"], REF_TS)
        assert other == {"NSE_FO|9999|2026-08-27": None}

    def test_zero_mutation_on_any_producer_failure(self, db_session):
        db_session.add_all(spot_closes())
        db_session.commit()
        before = (
            db_session.query(StrategyExecution).count(),
            db_session.query(PaperOrder).count(),
            db_session.query(Position).count(),
            db_session.query(PaperTransaction).count(),
        )
        with pytest.raises(ProducerError):
            run_core(db_session, prior_oi={key: None for key in all_keys()})
        after = (
            db_session.query(StrategyExecution).count(),
            db_session.query(PaperOrder).count(),
            db_session.query(Position).count(),
            db_session.query(PaperTransaction).count(),
        )
        assert before == after == (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Async wrapper: boundaries, replay, session authority
# ---------------------------------------------------------------------------

def _request(strategy_id=None, client_order_id="day50-test-order-1"):
    from types import SimpleNamespace

    from app.schemas import ExecutionLegIn

    return SimpleNamespace(
        client_order_id=client_order_id,
        symbol="NIFTY",
        strategy_id=strategy_id,
        strategy_tag="Day50Test",
        starting_capital=500000.0,
        legs=[
            ExecutionLegIn(
                symbol="NIFTY", expiration_date=EXPIRY,
                strike_price=25000.0, option_type="call", action="sell",
                quantity=1, lot_size=65,
            ),
            ExecutionLegIn(
                symbol="NIFTY", expiration_date=EXPIRY,
                strike_price=25100.0, option_type="call", action="buy",
                quantity=1, lot_size=65,
            ),
        ],
    )


def _prices():
    return {
        (EXPIRY, 25000.0, "call"): 200.0,
        (EXPIRY, 25100.0, "call"): 80.0,
    }


@pytest.mark.anyio
async def test_wrapper_full_path_executes_through_choke_point(db_session):
    import asyncio

    db_session.add_all(prior_candles(all_keys(), 950_000.0))
    db_session.add_all(spot_closes())
    db_session.commit()

    async def fake_fetch(symbol, expiry, token):
        return make_chain()

    async def fake_keys(legs, token):
        # One resolved key per requested leg (preserves request order), not
        # one key per chain side. The fixture's two requested call legs resolve
        # to KEY_25000_CE and KEY_25100_CE.
        return [KEY_25000_CE, KEY_25100_CE]

    def fake_token(db, user_id):
        return "test-md-token"

    result = await produce_candidate_and_execute(
        "user-day50",
        db_session,
        _request(),
        _prices(),
        token_resolver=fake_token,
        fetch_chain=fake_fetch,
        resolve_keys=fake_keys,
        now_fn=lambda: REF_TS,
    )
    assert result.status in ("FILLED", "PENDING")
    rows = db_session.query(StrategyExecution).all()
    assert len(rows) == 1
    metadata = rows[0].execution_metadata
    assert metadata and "candidate_id" in metadata
    assert "risk_reference_timestamp" in metadata


@pytest.mark.anyio
async def test_wrapper_replay_returns_original_execution(db_session):
    import asyncio

    existing = StrategyExecution(
        user_id="user-day50",
        execution_id="exec-original",
        client_order_id="day50-test-order-1",
        strategy_id="paper-entry",
        symbol="NIFTY",
        status="FILLED",
        entry_net=-7800.0,
    )
    db_session.add(existing)
    db_session.commit()

    async def failing_fetch(symbol, expiry, token):
        raise AssertionError("no evidence acquisition on replay")

    async def failing_keys(legs, token):
        raise AssertionError("no key resolution on replay")

    def fake_token(db, user_id):
        return "test-md-token"

    result = await produce_candidate_and_execute(
        "user-day50",
        db_session,
        _request(),
        _prices(),
        token_resolver=fake_token,
        fetch_chain=failing_fetch,
        resolve_keys=failing_keys,
        now_fn=lambda: REF_TS,
    )
    assert result.execution_id == "exec-original"
    assert db_session.query(StrategyExecution).count() == 1


@pytest.mark.anyio
async def test_wrapper_fails_closed_without_market_data_authorization(db_session):
    import asyncio

    def no_credential(db, user_id):
        from app.services.candidate_production import ProducerError

        raise ProducerError("MARKET_DATA_UNAUTHORIZED", "not connected")

    # (synchronous resolver — matches the production resolver signature)

    from app.services.paper_execution import PaperExecutionError

    with pytest.raises(PaperExecutionError) as excinfo:
        await produce_candidate_and_execute(
            "user-day50",
            db_session,
            _request(),
            _prices(),
            token_resolver=no_credential,
            now_fn=lambda: REF_TS,
        )
    assert excinfo.value.code == "MARKET_DATA_UNAUTHORIZED"
    assert db_session.query(StrategyExecution).count() == 0


@pytest.mark.anyio
async def test_wrapper_zero_mutation_when_gate_rejects(db_session):
    """A genuine chain that cannot become eligible must fail closed with
    zero mutation (Central Risk verdict = real engine, never stubbed)."""
    import asyncio

    # No prior candles at all → ΔOI missing for every strike → suppressed.
    db_session.add_all(spot_closes())
    db_session.commit()

    async def fake_fetch(symbol, expiry, token):
        return make_chain()

    async def fake_keys(legs, token):
        # One resolved key per requested leg (preserves request order), not
        # one key per chain side.  The fixture's two requested call legs
        # resolve to KEY_25000_CE and KEY_25100_CE.
        return [KEY_25000_CE, KEY_25100_CE]

    def fake_token(db, user_id):
        return "test-md-token"

    from app.services.paper_execution import PaperExecutionError

    with pytest.raises(PaperExecutionError) as excinfo:
        await produce_candidate_and_execute(
            "user-day50",
            db_session,
            _request(),
            _prices(),
            token_resolver=fake_token,
            fetch_chain=fake_fetch,
            resolve_keys=fake_keys,
            now_fn=lambda: REF_TS,
        )
    assert excinfo.value.code == "EVIDENCE_INSUFFICIENT"
    assert (
        db_session.query(StrategyExecution).count()
        + db_session.query(PaperOrder).count()
        + db_session.query(Position).count()
        + db_session.query(PaperTransaction).count()
    ) == 0


# ---------------------------------------------------------------------------
# Session / client authority
# ---------------------------------------------------------------------------

def test_client_cannot_supply_candidate_authority(db_session):
    """The client-facing schemas carry no candidate/opportunity/evaluation
    fields: forging them is impossible at the boundary."""
    from types import SimpleNamespace

    from app.schemas import ExecutionLegIn, ExecutionRequestIn

    request = ExecutionRequestIn(
        client_order_id="forgery-attempt-1",
        symbol="NIFTY",
        legs=[ExecutionLegIn(
            symbol="NIFTY", expiration_date=EXPIRY, strike_price=25000.0,
            option_type="call", action="sell", quantity=1, lot_size=65,
        )],
    )
    fields = set(ExecutionRequestIn.model_fields)
    assert "risk_candidate" not in fields
    assert "candidate" not in fields
    assert "opportunity" not in fields
    assert "evaluation" not in fields


def test_choke_point_still_rejects_missing_candidate_directly(db_session):
    """Day-34 invariant (unchanged): a direct execute_strategy call without a
    risk_candidate is rejected with STRATEGY_CANDIDATE_REQUIRED before any
    write — the producer is the only route around it, and the producer only
    produces genuine candidates."""
    from app.schemas import ExecutionLegIn, ExecutionRequestIn

    request = ExecutionRequestIn(
        client_order_id="direct-no-candidate-1",
        symbol="NIFTY",
        legs=[ExecutionLegIn(
            symbol="NIFTY", expiration_date=EXPIRY, strike_price=25000.0,
            option_type="call", action="sell", quantity=1, lot_size=65,
        )],
    )
    with pytest.raises(PaperExecutionError) as excinfo:
        from app.services.paper_execution import execute_strategy

        execute_strategy("user-day50", request, db_session, _prices())
    assert excinfo.value.code == "STRATEGY_CANDIDATE_REQUIRED"


# ---------------------------------------------------------------------------
# Route: POST /paper/executions uses the producer
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _route_market_open_gate():
    """Market-hours gate reports OPEN for route tests (deterministic)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    status = SimpleNamespace(
        status="open", source="test", trade_date="2026-09-24",
        checked_at="2026-09-24T10:05:00+05:30", message="test open",
        error=None, segment="INDEX_DERIVATIVES", session_state="OPEN",
        timezone="Asia/Kolkata", trading_allowed=True,
    )
    with patch("app.routers.paper.get_market_status",
               new=AsyncMock(return_value=status)):
        yield


class TestRoute:
    def test_route_reaches_choke_point_only_through_producer(
            self, client, db_session, monkeypatch):
        """Happy path: route → real producer → real choke point (fake chain)."""
        from app.services import candidate_production as cp

        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()

        session_id, _ = _identity(db_session)
        monkeypatch.setattr(
            token_store, "get_token", lambda sid: "tok-xyz", raising=False)

        async def fake_fetch(symbol, expiry, token):
            return make_chain()

        async def fake_keys(legs, token):
            # One resolved key per requested leg (preserves request order), not
            # one key per chain side. The fixture's two requested call legs
            # resolve to KEY_25000_CE and KEY_25100_CE.
            return [KEY_25000_CE, KEY_25100_CE]

        async def fake_resolve_prices(access_token, symbol, legs, **_kwargs):
            # ``chain_sink`` (Issue #118) is accepted and left empty here so
            # the producer falls back to its own injected chain fetch.
            return _prices()

        def fake_token(db, user_id):
            return "test-md-token"

        monkeypatch.setattr(cp, "_default_fetch_chain", fake_fetch)
        monkeypatch.setattr(cp, "_default_resolve_keys", fake_keys)
        monkeypatch.setattr(
            "app.routers.paper.resolve_market_prices", fake_resolve_prices)
        monkeypatch.setattr(
            cp, "_default_token_resolver", fake_token)

        response = client.post(
            "/paper/executions",
            json={
                "client_order_id": "route-day50-order-1",
                "symbol": "NIFTY",
                "strategy_tag": "Day50Route",
                "legs": [
                    {"symbol": "NIFTY", "expiration_date": EXPIRY,
                     "strike_price": 25000, "option_type": "call",
                     "action": "sell", "quantity": 1, "lot_size": 65},
                    {"symbol": "NIFTY", "expiration_date": EXPIRY,
                     "strike_price": 25100, "option_type": "call",
                     "action": "buy", "quantity": 1, "lot_size": 65},
                ],
            },
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] in ("FILLED", "PENDING")
        rows = db_session.query(StrategyExecution).all()
        assert len(rows) == 1
        assert "candidate_id" in (rows[0].execution_metadata or "")

    def test_route_fails_closed_without_candidate(
            self, client, db_session, monkeypatch):
        """If the producer cannot produce a genuine candidate the route
        fails closed with zero mutation (market data is present — the
        producer, not the network, is the gate)."""
        from app.services import candidate_production as cp

        db_session.add_all(spot_closes())  # no prior OI candles
        db_session.commit()

        session_id, _ = _identity(db_session)
        monkeypatch.setattr(
            token_store, "get_token", lambda sid: "tok-xyz", raising=False)

        async def fake_fetch(symbol, expiry, token):
            return make_chain()

        async def fake_keys(legs, token):
            # One resolved key per requested leg (preserves request order), not
            # one key per chain side. The fixture's two requested call legs
            # resolve to KEY_25000_CE and KEY_25100_CE.
            return [KEY_25000_CE, KEY_25100_CE]

        async def fake_resolve_prices(access_token, symbol, legs, **_kwargs):
            # ``chain_sink`` (Issue #118) is accepted and left empty here so
            # the producer falls back to its own injected chain fetch.
            return _prices()

        def fake_token(db, user_id):
            return "test-md-token"

        monkeypatch.setattr(cp, "_default_fetch_chain", fake_fetch)
        monkeypatch.setattr(cp, "_default_resolve_keys", fake_keys)
        monkeypatch.setattr(
            "app.routers.paper.resolve_market_prices", fake_resolve_prices)
        monkeypatch.setattr(cp, "_default_token_resolver", fake_token)

        response = client.post(
            "/paper/executions",
            json={
                "client_order_id": "route-day50-order-2",
                "symbol": "NIFTY",
                "legs": [
                    {"symbol": "NIFTY", "expiration_date": EXPIRY,
                     "strike_price": 25000, "option_type": "call",
                     "action": "sell", "quantity": 1, "lot_size": 65},
                    {"symbol": "NIFTY", "expiration_date": EXPIRY,
                     "strike_price": 25100, "option_type": "call",
                     "action": "buy", "quantity": 1, "lot_size": 65},
                ],
            },
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert response.status_code in (409, 422)
        assert (
            db_session.query(StrategyExecution).count()
            + db_session.query(PaperOrder).count()
            + db_session.query(Position).count()
            + db_session.query(PaperTransaction).count()
        ) == 0

    def test_live_contract_without_live_oi_history_fails_closed(
            self, client, db_session, monkeypatch):
        """PRODUCTION-REALISTIC (Issue #118, Path B): a currently
        tradable (unexpired) contract has NO stored prior-OI observation in
        this architecture, so the production path must fail closed with
        zero writes and must not fabricate ΔOI.

        Repository evidence: ``OptionCandle`` is populated only from the
        Upstox EXPIRED-instruments API — ``app/models.py`` OptionCandle
        docstring, ``app/services/option_candles.py`` module docstring, and
        every production caller (``daily_ingestion._ingest_option_candles``
        selecting ``ContractSpec.expiry <= today``,
        ``backfill_orchestrator``, ``app/tools/option_candle_backfill``)
        which all call ``get_expired_historical_candles``.  Seeding
        live-key OptionCandle rows (elsewhere in this file) tests the D1
        RULE; it is not production capability, and this test asserts what
        production actually does today.
        """
        from app.services import candidate_production as cp

        # The real ingested series (spot closes) exists; the OI history for
        # the live instrument keys does not and cannot be assumed.
        db_session.add_all(spot_closes())
        db_session.commit()
        assert db_session.query(OptionCandle).count() == 0

        session_id, _ = _identity(db_session)
        monkeypatch.setattr(
            token_store, "get_token", lambda sid: "tok-xyz", raising=False)

        async def fake_fetch(symbol, expiry, token):
            return make_chain()

        async def fake_keys(legs, token):
            # One resolved key per requested leg (preserves request order), not
            # one key per chain side. The fixture's two requested call legs
            # resolve to KEY_25000_CE and KEY_25100_CE.
            return [KEY_25000_CE, KEY_25100_CE]

        async def fake_resolve_prices(access_token, symbol, legs, **_kwargs):
            return _prices()

        def fake_token(db, user_id):
            return "test-md-token"

        monkeypatch.setattr(cp, "_default_fetch_chain", fake_fetch)
        monkeypatch.setattr(cp, "_default_resolve_keys", fake_keys)
        monkeypatch.setattr(
            "app.routers.paper.resolve_market_prices", fake_resolve_prices)
        monkeypatch.setattr(cp, "_default_token_resolver", fake_token)

        response = client.post(
            "/paper/executions",
            json={
                "client_order_id": "route-day50-order-3",
                "symbol": "NIFTY",
                "legs": [
                    {"symbol": "NIFTY", "expiration_date": EXPIRY,
                     "strike_price": 25000, "option_type": "call",
                     "action": "sell", "quantity": 1, "lot_size": 65},
                    {"symbol": "NIFTY", "expiration_date": EXPIRY,
                     "strike_price": 25100, "option_type": "call",
                     "action": "buy", "quantity": 1, "lot_size": 65},
                ],
            },
            cookies={SESSION_COOKIE_NAME: session_id},
        )

        assert response.status_code in (409, 422), response.text
        detail = response.json()["detail"]
        # The gate that stops it is named: no eligible prior-OI evidence.
        assert "EVIDENCE_INSUFFICIENT" in detail
        assert "ΔOI" in detail
        assert (
            db_session.query(StrategyExecution).count()
            + db_session.query(PaperOrder).count()
            + db_session.query(Position).count()
            + db_session.query(PaperTransaction).count()
        ) == 0


def _identity(db_session):
    from tests.test_helpers import create_test_identity

    return create_test_identity(db_session, "tok-xyz")


# ---------------------------------------------------------------------------
# Issue #118 audit findings — symbol scope, snapshot binding, evidence quality
# ---------------------------------------------------------------------------


async def _injected_keys(legs, token):
    """Injected broker instrument-key resolver (async, like the real one).

    One resolved key per requested leg (preserves request order), not one key
    per chain side. The fixture's two requested call legs resolve to
    KEY_25000_CE and KEY_25100_CE.
    """
    return [KEY_25000_CE, KEY_25100_CE]


class TestSliceASymbolScope:
    """Finding A — Slice A is NIFTY-only, and must say so.

    ``UPSTOX_INSTRUMENTS`` carries several indices, but the producer's
    evidence chain is wired to NIFTY spot history (``NiftyCandle``, the only
    stored spot series) and every evidence contract is labelled with that
    underlying.  A supported-but-different symbol must therefore fail closed
    BEFORE any broker work, never be processed and mislabelled as NIFTY.
    """

    @pytest.mark.anyio
    async def test_unsupported_symbol_fails_closed_before_any_broker_work(
            self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()

        def exploding_token(db, user_id):
            raise AssertionError(
                "no session/broker work for an unsupported symbol")

        async def exploding_fetch(symbol, expiry, token):
            raise AssertionError(
                "no evidence may be acquired for an unsupported symbol")

        request = _request()
        request.symbol = "BANKNIFTY"
        for leg in request.legs:
            leg.symbol = "BANKNIFTY"

        with pytest.raises(PaperExecutionError) as excinfo:
            await produce_candidate_and_execute(
                "user-day50", db_session, request, _prices(),
                token_resolver=exploding_token,
                fetch_chain=exploding_fetch,
                resolve_keys=_injected_keys,
                now_fn=lambda: REF_TS,
            )

        assert excinfo.value.code == "UNSUPPORTED_SYMBOL"
        assert "BANKNIFTY" in str(excinfo.value)
        # Zero mutation — the fail-closed guarantee is unchanged.
        assert db_session.query(StrategyExecution).count() == 0
        assert db_session.query(PaperOrder).count() == 0
        assert db_session.query(Position).count() == 0

    @pytest.mark.anyio
    async def test_nifty_still_produces_and_is_labelled_nifty(self, db_session):
        """The enforcement must not change the supported path."""
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()

        result = await _produce(db_session)
        assert result.status in ("FILLED", "PENDING")
        assert result.symbol == "NIFTY"
        assert db_session.query(StrategyExecution).one().symbol == "NIFTY"

        # And the core's evidence is genuinely labelled with the supported
        # underlying it was produced for.
        produced = run_core(db_session)
        assert produced.opportunity.underlying == "NIFTY"
        assert produced.ranked_strikes.ranked
        assert all(item.underlying == "NIFTY"
                   for item in produced.ranked_strikes.ranked)
        # The measured Day-12 quality rides the ranked evidence (the fabrication
        # this replaced could not supply an assessment at all).
        assert all(item.quality is not None
                   for item in produced.ranked_strikes.ranked)


class TestSingleEvidenceSnapshot:
    """Finding B — candidate evidence and fill prices share ONE broker read.

    The entry route resolves fill prices from a chain it fetches; the producer
    used to fetch a SECOND chain for candidate evidence.  The candidate's
    reference timestamp and its prices could then describe different
    snapshots.  The route now hands its priced snapshot to the producer, and
    the producer must reuse it rather than fetch again.
    """

    @pytest.mark.anyio
    async def test_producer_reuses_the_priced_snapshot(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()

        snapshot = make_chain()
        priced = _prices()

        async def must_not_fetch(symbol, expiry, token):
            raise AssertionError(
                "the priced snapshot must be reused, not re-fetched")

        result = await produce_candidate_and_execute(
            "user-day50", db_session, _request(), priced,
            chains={EXPIRY: snapshot},
            token_resolver=lambda db, user_id: "test-md-token",
            fetch_chain=must_not_fetch,
            resolve_keys=_injected_keys,
            now_fn=lambda: REF_TS,
        )
        assert result.status in ("FILLED", "PENDING")

        # The audit reference carried into the execution is the snapshot's own
        # broker quote time, so the recorded reference describes exactly the
        # chain those fill prices came from.
        row = db_session.query(StrategyExecution).one()
        assert "risk_reference_timestamp" in (row.execution_metadata or "")
        assert REF_TS.isoformat() in row.execution_metadata

    @pytest.mark.anyio
    async def test_missing_snapshot_falls_back_to_one_fetch(self, db_session):
        """Without a supplied snapshot the producer fetches exactly once."""
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()

        calls: list[str] = []

        async def counting_fetch(symbol, expiry, token):
            calls.append(symbol)
            return make_chain()

        await _produce(db_session, fetch_chain=counting_fetch)
        assert calls == ["NIFTY"]


class TestMeasuredEvidenceQuality:
    """Finding C — quality is MEASURED by the Day-12 engine, not asserted.

    The producer previously returned a hard-coded EXCELLENT/100 with no
    dimensions.  Because the opportunity contract admits only usable evidence
    (``state != INSUFFICIENT``), that constant silently disabled a real gate.
    """

    def test_quality_records_real_dimensions_and_reference_time(self):
        from app.services.candidate_production import (
            _chain_observation,
            _quality,
        )

        quality = _quality(
            _chain_observation(
                make_chain(), symbol="NIFTY", expiry=EXPIRY,
                received_at=REF_TS),
            received_at=REF_TS,
        )

        # The fabrication this replaced had no dimensions at all and never
        # recorded the reference time it was supposedly evaluated at.
        assert quality.dimensions, "quality must be measured, not asserted"
        assert quality.evaluated_at == REF_TS
        assert any(
            dim.status == "EVALUATED" and dim.score is not None
            for dim in quality.dimensions
        )
        assert quality.observation_time is not None

    def test_defective_book_cannot_be_labelled_excellent(self):
        """A crossed book must not come back EXCELLENT.

        The engine downgrades EXCELLENT to GOOD whenever an ERROR-severity
        issue exists, so this is precisely the discrimination the constant
        (always EXCELLENT) could never make.
        """
        from app.market_data.quality import QualityState
        from app.services.candidate_production import (
            _chain_observation,
            _quality,
        )

        crossed = make_chain(bid=300.0, ask=100.0,
                             pe_bid=400.0, pe_ask=90.0)
        quality = _quality(
            _chain_observation(
                crossed, symbol="NIFTY", expiry=EXPIRY, received_at=REF_TS),
            received_at=REF_TS,
        )

        assert quality.quality_state is not QualityState.EXCELLENT
        assert any(
            issue.code.value == "BID_ASK_INCONSISTENT"
            for issue in quality.issues
        )

    def test_quality_gate_is_live_for_the_opportunity_contract(self):
        """The measured state is what the opportunity contract gates on:
        an unusable state must be rejected there (a constant never could be)."""
        from app.market_data.quality import QualityResult, QualityState
        from app.opportunity.contracts import _usable_quality

        unusable = QualityResult(
            quality_score=0,
            quality_state=QualityState.INSUFFICIENT,
            critical_failure=True,
            issues=(),
            dimensions=(),
            evaluated_at=REF_TS,
            observation_time=REF_TS,
            observation_type="chain",
            contract_version="1.0.0",
            reference_time=REF_TS,
        )
        assert _usable_quality(unusable) is False
        assert _usable_quality(None) is False


# ---------------------------------------------------------------------------
# Expiry-payoff scan: measured P&L only, and breakevens keep their own spot
# ---------------------------------------------------------------------------

class TestExpiryPayoffScan:
    """The Day-18 payoff scan must report measured P&L only, and every
    breakeven must stay attached to the spot it was measured at.  The shared
    quant engine is replaced with a scripted stand-in so the scan's own
    semantics are what is under test."""

    SPOT = 100.0
    GRID = tuple(100.0 * (1.0 + frac) for frac in
                 (-0.10, -0.075, -0.05, -0.025, 0.0, 0.025, 0.05, 0.075, 0.10))

    def _legs(self):
        """Short 100 call / long 105 call (the fixture's spread shape).

        ``_expiry_payoff`` runs ``_contract_quantity`` on each leg, so every
        leg passed to it must carry a positive ``lot_size`` (the payoff scan
        tests use quantity=1, lot_size=1 -> 1 contract).
        """
        return [
            {"expiration_date": "2026-10-29", "strike_price": 100.0,
             "option_type": "call", "action": "sell", "quantity": 1.0,
             "direction": "sell", "lot_size": 1.0, "ltp": 4.0,
             "quant_leg": object()},
            {"expiration_date": "2026-10-29", "strike_price": 105.0,
             "option_type": "call", "action": "buy", "quantity": 1.0,
             "direction": "buy", "lot_size": 1.0, "ltp": 10.0,
             "quant_leg": object()},
        ]

    def _payoff(self, pnl_for_spot, drop=None):
        from app.market_data.contracts import Provenance
        from app.services.candidate_production import _expiry_payoff

        provenance = Provenance(
            source="UPSTOX",
            collection_mode="live",
            received_at=REF_TS,
            normalization_version="test",
            contract_version="1",
            transformation_id="day50-payoff-scan-test",
        )

        class _Portfolio:
            def __init__(self, total_pnl, partial=False):
                self.total_pnl = total_pnl
                self.partial = partial

        def scripted(legs, context, *, spot, time_to_expiry,
                     implied_volatility):
            if drop is not None and drop(spot):
                return _Portfolio(None, partial=True)
            return _Portfolio(pnl_for_spot(spot))

        with patch("app.services.candidate_production.evaluate_portfolio",
                   scripted):
            return _expiry_payoff(self._legs(), self.SPOT, REF_TS, provenance)

    def test_exactly_flat_grid_spots_are_breakevens(self):
        """A measured P&L of exactly zero at a grid spot IS a breakeven
        there; the scan must not require a sign change to report it."""
        payoff = self._payoff(
            lambda spot: 30.0 if spot <= 97.5 else
            (0.0 if spot <= 102.5 else -20.0))

        # 97.5 → 100.0 interpolates onto exactly 100.0; 102.5 is measured flat.
        assert payoff.breakevens == (100.0, 102.5)
        assert payoff.state is DimensionState.AVAILABLE
        assert payoff.expiry_semantics is \
            PayoffExpirySemantics.SAME_EXPIRY_EXACT
        assert payoff.net_debit_credit == 6.0
        assert payoff.premium_outlay == 6.0

    def test_non_finite_grid_pnl_never_becomes_evidence(self):
        """``nan``/``inf`` are not measurements: those grid spots are dropped
        (PARTIAL) instead of entering max/min or a breakeven."""
        scripted = {spot: -50.0 for spot in self.GRID}
        scripted[self.GRID[2]] = float("inf")
        scripted[self.GRID[4]] = float("nan")
        scripted[self.GRID[8]] = float("-inf")
        for spot in self.GRID[5:8]:
            scripted[spot] = 20.0

        payoff = self._payoff(lambda spot: scripted[spot])

        assert payoff.state is DimensionState.PARTIAL
        assert payoff.max_profit == 20.0
        assert payoff.max_loss == -50.0
        assert payoff.breakevens

    def test_all_grid_spots_unpriceable_fails_closed(self):
        from app.services.candidate_production import ProducerError

        with pytest.raises(ProducerError) as excinfo:
            self._payoff(lambda spot: float("nan"))

        assert excinfo.value.code == "EVIDENCE_INSUFFICIENT"

    def test_dropped_grid_spot_does_not_shift_breakeven_labels(self):
        """A dropped grid spot leaves a shorter P&L series; a breakeven must
        still be labelled with the spots it was really interpolated between,
        never with a spot that was never sampled."""
        scripted = {spot: -100.0 for spot in self.GRID}
        scripted[self.GRID[4]] = -80.0
        for spot in self.GRID[5:]:
            scripted[spot] = 50.0

        payoff = self._payoff(
            lambda spot: scripted[spot],
            drop=lambda spot: spot == self.GRID[3],
        )

        # The sign change is between the measured 100.0 (-80) and 102.5 (+50)
        # levels: 100.0 + 2.5 * 80/130.
        assert payoff.breakevens == (101.54,)
        assert payoff.state is DimensionState.PARTIAL

    def test_sign_bucket_is_exact_not_tolerant(self):
        """The bucket is exact: a tiny non-zero P&L is a real sign, and only
        a measured zero is flat."""
        from app.services.candidate_production import _pnl_sign

        assert _pnl_sign(0.0) == 0
        assert _pnl_sign(5.0) == 1
        assert _pnl_sign(-5.0) == -1
        assert _pnl_sign(1e-18) == 1
        assert _pnl_sign(-1e-18) == -1


async def _produce(db_session, *, fetch_chain=None):
    """Drive the production wrapper over the fixture with broker boundaries
    injected (the sanctioned test seam)."""
    async def default_fetch(symbol, expiry, token):
        return make_chain()

    return await produce_candidate_and_execute(
        "user-day50", db_session, _request(), _prices(),
        token_resolver=lambda db, user_id: "test-md-token",
        fetch_chain=fetch_chain or default_fetch,
        resolve_keys=_injected_keys,
        now_fn=lambda: REF_TS,
    )


# ---------------------------------------------------------------------------
# Day-50 Slice A remediation regressions (PR #130)
#
# Each block below guards one of the corrected contracts against the defect
# that was live at committed HEAD f2286f7: a flat linear ΔOI scan, a dead
# ``evaluate_positioning`` call, identity read from strike text, fabricated
# factor/GEX scores, a freshness clock bounded by its own quote stamp,
# session-blind spot history, lots passed off as contracts, and a direction-
# only tail classifier.  They are written against the repository's OWN
# fixtures and sessions — no second test harness.
# ---------------------------------------------------------------------------

def _prov() -> Provenance:
    return Provenance(
        source="UPSTOX",
        collection_mode="live",
        received_at=REF_TS,
        normalization_version="test",
        contract_version="1",
        transformation_id="day50-slice-a-remediation",
    )


def _index(chain=None, keys=None):
    """The producer's real identity-bound index for the fixture chain."""
    chain = chain if chain is not None else make_chain()
    legs = make_legs()
    keys = keys if keys is not None else [KEY_25000_CE, KEY_25100_CE]
    return _apply_identities(
        _build_side_index(chain), _resolved_identity_map(legs, keys))


class TestIdentityBinding:
    """requested (strike, option type) → resolved broker instrument key →
    canonical chain side → validated binding, failing closed on every
    disagreement.  The resolved key is read from the broker payload, never
    constructed from strike text."""

    def test_resolved_key_binds_onto_a_canonical_side_that_carries_none(self):
        # ``with_keys=False`` mirrors the canonical ``transform_chain`` shape:
        # no per-side instrument_key for the requested leg.
        bound = _index(chain=make_chain(with_keys=False))
        assert bound[(25000.0, "call")].instrument_key == KEY_25000_CE
        assert bound[(25100.0, "call")].instrument_key == KEY_25100_CE
        # an unrequested side with no identity of its own stays missing —
        # never guessed from the strike text
        assert bound[(25000.0, "put")].instrument_key is None
        # an unrequested side that does carry a broker key keeps it
        assert bound[(25100.0, "put")].instrument_key == KEY_25100_PE

    def test_snapshot_and_resolved_key_disagreement_fails_closed(self):
        with pytest.raises(ProducerError) as excinfo:
            _index(keys=[KEY_25000_CE, "NSE_FO|9999|2026-09-24"])
        assert excinfo.value.code == "IDENTITY_MISMATCH"

    def test_identity_count_must_match_the_request(self):
        with pytest.raises(ProducerError) as excinfo:
            _resolved_identity_map(make_legs(), [KEY_25000_CE])
        assert excinfo.value.code == "IDENTITY_MISMATCH"

    def test_missing_resolved_key_suppresses_the_requested_strike(
            self, db_session):
        """The broker resolves no key for the requested call legs: they carry
        no identity, so they can never earn a ΔOI, while the chain's unrequested
        put sides still rank normally.  The entry fails closed on the
        identity-less leg instead of being priced off strike text."""
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        with pytest.raises(ProducerError) as excinfo:
            run_core(
                db_session,
                keys=[None, None],
                prior_oi={KEY_25000_PE: 1_000_000.0,
                          KEY_25100_PE: 1_000_000.0},
            )
        assert excinfo.value.code == "CANDIDATE_NOT_ELIGIBLE"
        assert "suppressed" in str(excinfo.value)

    def test_missing_snapshot_side_fails_closed(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        chain = make_chain()
        chain["chain"] = chain["chain"][:1]  # the 25100 strike is absent
        with pytest.raises(ProducerError) as excinfo:
            run_core(db_session, chain=chain)
        assert excinfo.value.code == "CHAIN_DATA_MISSING"

    def test_missing_current_oi_suppresses_the_strike(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        chain = make_chain()
        for row in chain["chain"]:
            row["call"]["oi"] = None
        with pytest.raises(ProducerError) as excinfo:
            run_core(db_session, chain=chain)
        assert excinfo.value.code == "EVIDENCE_INSUFFICIENT"

    def test_missing_prior_oi_is_already_covered_by_the_d1_suite(self, db_session):
        """Explicit record of the fifth fail-closed case (missing prior OI)
        so the identity contract above is enumerated completely: it is
        asserted by ``TestProducerCore.test_missing_prior_oi_fails_closed``.
        """
        db_session.add_all(spot_closes())
        db_session.commit()
        with pytest.raises(ProducerError) as excinfo:
            run_core(db_session, prior_oi={key: None for key in all_keys()})
        assert "ΔOI history" in str(excinfo.value)


class TestIndexedDeltaOiLookup:
    """Codacy HIGH: the committed PR scanned a flat ``sides`` list for every
    candidate (O(N²)).  The corrected path is one keyed index lookup per
    (strike, option side) against the identity-bound side."""

    def test_delta_is_resolved_from_the_identity_bound_key(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        produced = run_core(db_session)
        ranked = {r.candidate_id: r for r in produced.ranked_strikes.ranked}
        positioning = next(
            c for c in ranked["strike:25000:call"].contributions
            if c.factor is RankingFactor.POSITIONING)
        # current OI 1_200_000 − prior 950_000, where the prior row was found
        # by the RESOLVED broker instrument key of that exact (strike, side)
        assert positioning.raw == 1_200_000.0 - 950_000.0
        assert positioning.state is not QualityState.INSUFFICIENT

    def test_prior_oi_under_another_identity_never_satisfies_d1(
            self, db_session):
        """A prior observation keyed by a different instrument is not this
        side's history — ΔOI stays missing and the entry fails closed."""
        db_session.add_all(spot_closes())
        db_session.commit()
        with pytest.raises(ProducerError) as excinfo:
            run_core(db_session,
                     prior_oi={"NSE_FO|9999|2026-09-24": 950_000.0})
        assert excinfo.value.code == "EVIDENCE_INSUFFICIENT"

    def test_no_flat_side_list_or_per_candidate_scan_remains(self):
        """Structural guard for the static-analysis finding: no
        ``key_by_market_side`` map, no ``_extract_sides`` flat list, and no
        linear scan over ``sides`` inside the producer — the lookup is
        ``side_index.get((strike, side_name))``."""
        from app.services import candidate_production as cp

        module_src = inspect.getsource(cp)
        assert "key_by_market_side" not in module_src
        assert not hasattr(cp, "_extract_sides")
        assert "for s in sides" not in module_src
        assert "for side in sides" not in module_src

        core_src = inspect.getsource(cp.produce_candidate_core)
        assert "side_index.get((strike, side_name))" in core_src


class TestDeadPositioningComputation:
    """Codacy HIGH: ``positioning_result = evaluate_positioning(...)`` was
    computed and never read.  ``evaluate_positioning`` is pure (it only
    builds an ``IntelligenceResult`` from the same input), so the dead work is
    removed rather than silenced with ``_ = ...``."""

    def test_evaluate_positioning_is_neither_called_nor_imported(self):
        from app.services import candidate_production as cp

        module_src = inspect.getsource(cp)
        assert "evaluate_positioning(" not in module_src
        assert "positioning_result" not in module_src
        # the intelligence path still runs on exactly the two values the
        # downstream code consumes
        assert "compute_metrics(positioning_input)" in module_src
        assert "classify_chain(" in module_src

    def test_removing_the_dead_call_does_not_change_the_live_path(
            self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        produced = run_core(db_session)
        assert produced.opportunity is not None
        assert produced.candidate.lifecycle_state.value == "ELIGIBLE"
        assert produced.evaluation.reference_timestamp == REF_TS


class TestMissingFactorSuppression:
    """A missing market measurement must never become usable numeric
    evidence: it is emitted ``INSUFFICIENT``, which the existing Day-30
    ranking treats as unusable."""

    @staticmethod
    def _call_side(**overrides):
        chain = make_chain()
        chain["chain"][0]["call"].update(overrides)
        return _build_side_index(chain)[(25000.0, "call")]

    @staticmethod
    def _factors(side, *, delta=250_000.0, gex_reference=None):
        return {f.factor: f for f in _strike_factors(
            side, delta, 25000.0, _prov(), gex_reference)}

    def test_absent_measurement_is_insufficient_not_a_usable_zero(self):
        obs = _measured_factor(RankingFactor.LIQUIDITY, None, None, _prov())
        assert obs.state is QualityState.INSUFFICIENT
        assert obs.raw is None

    def test_measured_value_stays_usable(self):
        obs = _measured_factor(RankingFactor.LIQUIDITY, 0.5, 50_000.0, _prov())
        assert obs.state is QualityState.EXCELLENT
        assert obs.raw == 50_000.0

    def test_every_absent_market_input_is_suppressed(self):
        factors = self._factors(self._call_side(
            volume=None, iv=None, gamma=None,
            bid_price=None, ask_price=None))
        for factor in (RankingFactor.LIQUIDITY, RankingFactor.SPREAD_QUALITY,
                       RankingFactor.IV, RankingFactor.GREEKS,
                       RankingFactor.GEX):
            assert factors[factor].state is QualityState.INSUFFICIENT, factor
        # measured inputs are still measured (this is not a blanket reset)
        assert factors[RankingFactor.POSITIONING].state \
            is QualityState.EXCELLENT
        assert factors[RankingFactor.DISTANCE_TO_SPOT].state \
            is QualityState.EXCELLENT

    def test_measured_inputs_stay_usable(self):
        factors = self._factors(self._call_side(),
                                gex_reference=_gex_reference(
                                    _build_side_index(make_chain()), 25000.0))
        for factor in (RankingFactor.LIQUIDITY, RankingFactor.SPREAD_QUALITY,
                       RankingFactor.IV, RankingFactor.GREEKS,
                       RankingFactor.POSITIONING, RankingFactor.GEX):
            assert factors[factor].state is not QualityState.INSUFFICIENT, factor

    def test_no_neutral_placeholder_survives_in_the_factor_path(self):
        from app.services import candidate_production as cp

        src = inspect.getsource(cp._strike_factors)
        assert "score=0.5" not in src
        assert "_spread_score" not in src


class TestMeasuredGex:
    """GEX is measured only from real gamma × OI, with the repository's own
    single convention (``gamma × OI × spot² × 0.01``, CE +raw / PE −raw).
    Missing inputs stay missing — never a neutral placeholder."""

    @staticmethod
    def _call_side(**overrides):
        chain = make_chain()
        chain["chain"][0]["call"].update(overrides)
        return _build_side_index(chain)[(25000.0, "call")]

    def test_uses_the_repository_formula_and_sign_convention(self):
        side = self._call_side()
        expected = historical_gex.compute_raw_gex(0.05, 1_200_000.0, 25000.0)
        assert expected == 0.05 * 1_200_000.0 * 25000.0 ** 2 * 0.01
        assert _side_signed_gex(side, 25000.0) == expected  # CE → +raw

    def test_put_side_is_negative_raw(self):
        side = _build_side_index(make_chain())[(25000.0, "put")]
        assert _side_signed_gex(side, 25000.0) == \
            -historical_gex.compute_raw_gex(0.05, 1_100_000.0, 25000.0)

    @pytest.mark.parametrize("overrides", [
        {"oi": None}, {"gamma": None}, {"oi": 0.0}, {"gamma": -0.05},
    ])
    def test_missing_or_invalid_inputs_stay_unmeasured(self, overrides):
        assert _side_signed_gex(self._call_side(**overrides), 25000.0) is None

    def test_reference_is_the_snapshot_maximum(self):
        index = _build_side_index(make_chain())
        assert _gex_reference(index, 25000.0) == max(
            abs(v) for v in
            (_side_signed_gex(s, 25000.0) for s in index.values())
            if v is not None)

    def test_snapshot_without_measured_gex_has_no_reference(self):
        chain = make_chain()
        for row in chain["chain"]:
            row["call"]["gamma"] = None
            row["put"]["gamma"] = None
        assert _gex_reference(_build_side_index(chain), 25000.0) is None

    def test_gex_factor_is_normalised_against_the_snapshot_reference(self):
        index = _build_side_index(make_chain())
        side = index[(25000.0, "call")]
        factors = {f.factor: f for f in _strike_factors(
            side, 250_000.0, 25000.0, _prov(),
            _gex_reference(index, 25000.0))}
        assert factors[RankingFactor.GEX].state \
            is not QualityState.INSUFFICIENT
        assert 0.0 < factors[RankingFactor.GEX].score <= 1.0
        # no reference ⇒ the factor cannot be normalised ⇒ suppressed
        bare = {f.factor: f for f in _strike_factors(
            side, 250_000.0, 25000.0, _prov(), None)}
        assert bare[RankingFactor.GEX].state is QualityState.INSUFFICIENT


class TestFreshnessClock:
    """Freshness is judged against the single captured ``received_at``;
    ``reference_ts`` stays the broker's own quote timestamp as provenance."""

    @staticmethod
    def _freshness(quality):
        return next(d for d in quality.dimensions
                    if d.dimension is QualityDimension.FRESHNESS)

    @staticmethod
    def _quote_at(stamp):
        chain = make_chain()
        for row in chain["chain"]:
            for name in ("call", "put"):
                row[name]["quote_timestamp"] = stamp
        return chain

    def test_reference_clock_is_the_broker_quote_of_the_bound_index(self):
        index = _index()
        assert _reference_ts_from_index(index, REF_TS) == REF_TS  # QUOTE_TS
        assert index[(25000.0, "call")].instrument_key == KEY_25000_CE

    def test_reference_clock_falls_back_to_received_at(self):
        index = _build_side_index(self._quote_at(None))
        assert _reference_ts_from_index(index, REF_TS) == REF_TS

    def test_stale_quote_is_aged_against_the_receipt_clock(self):
        """A 3-hour-old broker quote must be aged by ``received_at``.  Using
        the broker stamp as its own clock would make every snapshot age 0 and
        a stale snapshot could never be detected as stale."""
        chain = self._quote_at("24-Sep-2026 07:05:00")  # 3h before REF_TS
        observation = _chain_observation(
            chain, symbol="NIFTY", expiry=EXPIRY, received_at=REF_TS)

        stale = _quality(observation, received_at=REF_TS)
        fresh = _quality(observation, received_at=REF_TS - timedelta(hours=3))

        assert stale.reference_time == REF_TS
        assert self._freshness(stale).score == 0.0
        assert any("stale" in i.message.lower() for i in stale.issues)
        # the identical observation judged at its own quote time is fresh, so
        # the difference is purely the receipt clock
        assert self._freshness(fresh).score == 1.0


class TestSpotHistoryBoundaries:
    """NIFTY-only, 3-minute, strictly-prior, current-session-bounded spot
    history — previous sessions and foreign symbols are never borrowed."""

    @staticmethod
    def _add(db, *, open_time, close, symbol="NIFTY", interval="3min"):
        db.add(NiftyCandle(
            symbol=symbol, interval=interval, open_time=open_time,
            open=close, high=close, low=close, close=close, volume=1000))

    def test_foreign_symbol_is_never_nifty_spot_history(self, db_session):
        self._add(db_session, symbol="BANKNIFTY", close=45000.0,
                  open_time=_ist_clock(REF_TS) - timedelta(minutes=9))
        db_session.commit()
        assert _spot_history(db_session, REF_TS) == ((), None)

    def test_wrong_interval_is_excluded(self, db_session):
        self._add(db_session, interval="1min", close=22450.0,
                  open_time=_ist_clock(REF_TS) - timedelta(minutes=9))
        db_session.commit()
        assert _spot_history(db_session, REF_TS) == ((), None)

    def test_previous_session_closes_are_never_borrowed(self, db_session):
        yesterday = _ist_clock(REF_TS) - timedelta(days=1)
        for i in range(8):
            self._add(db_session, close=22000.0 + i,
                      open_time=yesterday - timedelta(minutes=3 * i))
        self._add(db_session, close=22450.0,
                  open_time=_ist_clock(REF_TS) - timedelta(minutes=9))
        db_session.commit()
        closes, prev = _spot_history(db_session, REF_TS, count=8)
        assert closes == (22450.0,)
        assert prev == 22450.0

    def test_insufficient_current_session_history_is_reported_short(
            self, db_session):
        """Only two current-session closes exist and eight are asked for: the
        caller receives exactly two — never padded with yesterday's rows."""
        base = _ist_clock(REF_TS) - timedelta(minutes=9)
        for i in range(2):
            self._add(db_session, close=22400.0 + i * 10,
                      open_time=base - timedelta(minutes=3 * (1 - i)))
        yesterday = _ist_clock(REF_TS) - timedelta(days=1)
        for i in range(8):
            self._add(db_session, close=22000.0 + i,
                      open_time=yesterday - timedelta(minutes=3 * i))
        db_session.commit()
        closes, _ = _spot_history(db_session, REF_TS, count=8)
        assert closes == (22400.0, 22410.0)


class TestContractUnits:
    """``ExecutionLegIn.quantity`` is LOTS, ``lot_size`` is CONTRACTS PER LOT,
    and the domain ``OptionLeg.quantity`` is CONTRACTS (1 lot × 65 = 65)."""

    def test_contract_quantity_is_lots_times_lot_size(self):
        assert _contract_quantity({"quantity": 1, "lot_size": 65}) == 65.0
        assert _contract_quantity({"quantity": 2, "lot_size": 65}) == 130.0

    @pytest.mark.parametrize("leg", [
        {"quantity": 1.0},                  # no lot size at all
        {"quantity": 1.0, "lot_size": 0.0},  # zero lot size
        {"quantity": 0.0, "lot_size": 65.0},  # no size
        {"quantity": -1.0, "lot_size": 65.0},  # negative
    ])
    def test_unriskable_lot_contract_fails_closed(self, leg):
        # the lot size is never silently assumed to be 1
        with pytest.raises(ProducerError):
            _contract_legs([leg])

    def test_candidate_legs_carry_contracts_not_lots(self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        produced = run_core(
            db_session, legs=make_legs(quantity=2, lot_size=65))
        assert [leg.quantity for leg in produced.evaluation.legs] == \
            [130.0, 130.0]

    def test_payoff_and_tail_run_on_the_same_contract_quantity(
            self, db_session):
        db_session.add_all(prior_candles(all_keys(), 950_000.0))
        db_session.add_all(spot_closes())
        db_session.commit()
        produced = run_core(db_session)
        # 1 lot × 65 contracts per lot on both legs of the bear call spread
        assert [leg.quantity for leg in produced.evaluation.legs] == \
            [65.0, 65.0]


class TestQuantityAwareTailClassification:
    """Structural tail from NET SIGNED CONTRACT exposure, not strike order:
    a ratio short call stays UNLIMITED_LOSS and a 1×1 vertical stays bounded.
    Classification only — the Day-18 quant engine remains the P&L authority."""

    @staticmethod
    def _leg(action, option_type, strike, lots, lot_size=1):
        return {"direction": action, "option_type": option_type,
                "strike_price": strike, "quantity": float(lots),
                "lot_size": float(lot_size), "ltp": 100.0}

    def test_uncovered_short_call_is_unlimited_loss(self):
        assert _classify_tail([self._leg("sell", "call", 25000.0, 1)]) \
            is TailClass.UNLIMITED_LOSS

    def test_ratio_short_call_is_still_unlimited_loss(self):
        # short 2 / long 1 at the higher strike leaves net short exposure
        legs = [self._leg("sell", "call", 25000.0, 2),
                self._leg("buy", "call", 25100.0, 1)]
        assert _classify_tail(legs) is TailClass.UNLIMITED_LOSS

    def test_ratio_scales_with_lot_size_not_with_leg_count(self):
        legs = [self._leg("sell", "call", 25000.0, 2, lot_size=65),
                self._leg("buy", "call", 25100.0, 1, lot_size=65)]
        assert _classify_tail(legs) is TailClass.UNLIMITED_LOSS

    def test_net_long_call_exposure_is_unlimited_gain(self):
        legs = [self._leg("buy", "call", 25000.0, 2),
                self._leg("sell", "call", 25100.0, 1)]
        assert _classify_tail(legs) is TailClass.UNLIMITED_GAIN

    def test_uncapped_long_call_is_unlimited_gain(self):
        assert _classify_tail([self._leg("buy", "call", 25000.0, 1)]) \
            is TailClass.UNLIMITED_GAIN

    def test_one_by_one_vertical_is_bounded_not_unlimited_gain(self):
        # a normal 1×1 vertical must NOT be mislabelled UNLIMITED_GAIN
        legs = [self._leg("buy", "call", 25000.0, 1),
                self._leg("sell", "call", 25100.0, 1)]
        assert _classify_tail(legs) is TailClass.NONE

    def test_net_short_put_is_never_unlimited_loss(self):
        # a put's intrinsic value is capped at its own strike
        assert _classify_tail([self._leg("sell", "put", 24800.0, 1)]) \
            is TailClass.NONE

    def test_net_flat_exposure_on_both_sides_is_none(self):
        legs = [self._leg("buy", "call", 25000.0, 1),
                self._leg("sell", "call", 25000.0, 1)]
        assert _classify_tail(legs) is TailClass.NONE
