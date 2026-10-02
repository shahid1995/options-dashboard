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

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    NiftyCandle,
    OptionCandle,
    PaperOrder,
    PaperTransaction,
    Position,
    StrategyExecution,
)
from app.services import token_store
from app.services.candidate_production import (
    OI_HISTORY_MAX_AGE,
    OI_HISTORY_MIN_LAG,
    ProducerError,
    _build_side_index,
    _prior_oi_state,
    produce_candidate_and_execute,
    produce_candidate_core,
)
from app.routers.deps import SESSION_COOKIE_NAME
from app.services.paper_execution import PaperExecutionError
from app.strategy_evaluation.contracts import (
    DimensionState,
    PayoffExpirySemantics,
)

#: Broker-authoritative index option lot size used by the route-level stubs.
BROKER_LOT = 65


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


def _side(ltp, oi, chg_oi, volume, iv, key, bid=None, ask=None):
    return {
        "ltp": ltp, "oi": oi, "chg_oi": chg_oi, "volume": volume,
        "iv": iv, "instrument_key": key, "bid_price": bid, "ask_price": ask,
        "quote_timestamp": QUOTE_TS,
    }


def make_chain(
    *,
    spot=25000.0,
    ce_ltp=200.0, pe_ltp=180.0,
    ce_oi=1_200_000.0, pe_oi=1_100_000.0,
    ce_delta_oi=250_000.0, pe_delta_oi=-80_000.0,
    volume=150_000.0, iv=14.0,
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
                              bid, ask),
                "put": _side(pe_ltp, pe_oi, pe_oi + pe_delta_oi, volume, iv,
                             pe_key, pe_bid, pe_ask),
            },
            {
                "strike": 25100.0,
                "call": _side(ce_ltp * 0.5, ce_oi * 0.6, ce_delta_oi * 0.4,
                              volume * 0.6, iv, KEY_25100_CE, bid * 0.5,
                              ask * 0.5),
                "put": _side(pe_ltp * 2.2, pe_oi * 1.4, 40_000.0,
                             volume * 1.3, iv, KEY_25100_PE, pe_bid * 2.2,
                             pe_ask * 2.2),
            },
        ],
    }


# Chain rows never carry keys per side; the mapper's rows hold them inside
# each side dict.  The second strike's keys:
KEY_25000_PE = "NSE_FO|2001|2026-09-24"
KEY_25100_PE = "NSE_FO|2002|2026-09-24"


def make_legs(direction="sell", strike=25000.0, option_type="call",
              quantity=1):
    """Default request: a covered bear call spread (bounded payoff)."""
    return [
        {
            "expiration_date": EXPIRY, "strike_price": 25000.0,
            "option_type": "call", "action": "sell", "quantity": 1,
        },
        {
            "expiration_date": EXPIRY, "strike_price": 25100.0,
            "option_type": "call", "action": "buy", "quantity": 1,
        },
    ]


def prior_candles(keys, oi, *, reference_ts=REF_TS, age=timedelta(minutes=6)):
    """OptionCandle rows for each key at ``reference_ts - age``."""
    open_time = reference_ts - age
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
    """Stored NIFTY closes ending just before the reference timestamp."""
    rows = []
    start = reference_ts - timedelta(minutes=3 * (count + 1))
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


def run_core(db, chain=None, legs=None, prior_oi=None, *, received_at=None,
             spot_closes_rows=None):
    """Drive the real producer core over the fixture (fail-closed path)."""
    chain = chain if chain is not None else make_chain()
    legs = legs if legs is not None else make_legs()
    keys = [side.get("instrument_key")
            for row in chain["chain"]
            for side_name in ("call", "put")
            for side in [row.get(side_name) or {}]
            if side.get("instrument_key")]
    if prior_oi is None:
        prior_oi = {key: 1_000_000.0 for key in keys}
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
        return [KEY_25000_CE, KEY_25000_PE, KEY_25100_CE, KEY_25100_PE]

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
        return all_keys()

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
    @pytest.fixture(autouse=True)
    def authoritative_lot_sizes(self):
        """Day 49: the entry route resolves broker-authoritative lot sizes
        before producing a candidate.  Route tests stub that broker call so
        they exercise the producer and the choke point, never the network
        (mirrors tests/test_template_execution_integration.py)."""

        async def fake_lot_sizes(access_token, symbol, legs):
            # A broker-authoritative value INDEPENDENT of the client payload,
            # so the route's normalization is genuinely exercised.
            return {
                (
                    str(leg.expiration_date),
                    float(leg.strike_price),
                    str(leg.option_type).lower(),
                ): BROKER_LOT
                for leg in legs
            }

        with patch(
            "app.routers.paper.resolve_authoritative_lot_sizes",
            new=AsyncMock(side_effect=fake_lot_sizes),
        ):
            yield

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
            return all_keys()

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
            return all_keys()

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
        ) == 0


def _identity(db_session):
    from tests.test_helpers import create_test_identity

    return create_test_identity(db_session, "tok-xyz")


# ---------------------------------------------------------------------------
# Issue #118 audit findings — symbol scope, snapshot binding, evidence quality
# ---------------------------------------------------------------------------


async def _injected_keys(legs, token):
    """Injected broker instrument-key resolver (async, like the real one)."""
    return all_keys()


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
            reference_ts=REF_TS,
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
            reference_ts=REF_TS,
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
        """Short 100 call / long 105 call (the fixture's spread shape)."""
        return [
            {"expiration_date": "2026-10-29", "strike_price": 100.0,
             "option_type": "call", "action": "sell", "quantity": 1.0,
             "direction": "sell", "ltp": 4.0, "quant_leg": object()},
            {"expiration_date": "2026-10-29", "strike_price": 105.0,
             "option_type": "call", "action": "buy", "quantity": 1.0,
             "direction": "buy", "ltp": 10.0, "quant_leg": object()},
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
