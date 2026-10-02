"""Phase 5.0 tests — server-authoritative paper trading engine.

Covers the spec's §36 matrix: order lifecycle, idempotency, positions
(netting / weighted average / partial & full exits / reversal), P&L
(realized / unrealized), portfolio summary, multi-leg grouping, atomic
failure, market-hours safety, chain-data gating, concurrency-style
duplicates, user isolation, journal linkage and reconciliation.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.routers.paper import MARKET_CLOSED_MSG, MARKET_UNKNOWN_MSG
from app.services import token_store

# Day 34: seed every bare entry intent through the genuine Day-28→Day-33
# chain into the real paper execution engine (no candidate fabrication).
from tests.day34_seeding import day34_gated_seeding  # noqa: F401  (autouse fixture)

LOT = 65
EXPIRY = "2026-08-27"
EXPIRY2 = "2026-09-03"

# Strike -> {side: ltp} per expiry. Tests mutate this dict between calls to
# simulate the market price moving (weighted averages, realized P&L).
DEFAULT_CHAIN = {
    EXPIRY: {
        24350: {"call": 125.25, "put": 90.0},
        24550: {"call": 35.60, "put": 200.0},
        25000: {"call": 200.0, "put": 80.0},
        25100: {"call": 80.0, "put": 210.0},
        26000: {"call": 100.0, "put": 150.0},
    },
    EXPIRY2: {
        24350: {"call": 130.0, "put": 95.0},
        24400: {"call": 105.0, "put": 85.0},
    },
}


def chain_payload(expiry: str, quotes: dict) -> dict:
    """Raw Upstox-style chain payload consumed by ``transform_chain``."""
    data = []
    for strike, sides in quotes.items():
        item = {"strike_price": strike, "underlying_spot_price": 24000.0}
        item["call_options"] = {"market_data": {"ltp": sides["call"]}, "option_greeks": {}}
        item["put_options"] = {"market_data": {"ltp": sides["put"]}, "option_greeks": {}}
        data.append(item)
    return {"data": data}


@pytest.fixture(autouse=True)
def market_open_gate():
    with gate_status("open"):
        yield


def gate_status(status_value):
    status = SimpleNamespace(
        status=status_value,
        source="test",
        trade_date="2026-08-14",
        checked_at="2026-08-14T10:00:00+05:30",
        message=f"test market status: {status_value}",
        error=None,
    )
    return patch("app.routers.paper.get_market_status", new=AsyncMock(return_value=status))


@pytest.fixture
def chain_quotes():
    return deepcopy(DEFAULT_CHAIN)


@pytest.fixture(autouse=True)
def chain_mock(chain_quotes):
    """Backend chain fetches are the authoritative fill-price source."""

    async def fake(token, instrument_key, expiry):
        return chain_payload(expiry, chain_quotes.get(expiry, {}))

    async def fake_contracts(token, instrument_key):
        data = []
        for expiry, strikes in chain_quotes.items():
            for strike in strikes:
                data.extend((
                    {"expiry": expiry, "strike_price": strike, "instrument_type": "CE", "lot_size": LOT},
                    {"expiry": expiry, "strike_price": strike, "instrument_type": "PE", "lot_size": LOT},
                ))
        return {"data": data}

    with patch("app.services.upstox.get_option_chain", new=AsyncMock(side_effect=fake)) as m, \
         patch("app.services.upstox.get_option_contracts", new=AsyncMock(side_effect=fake_contracts)):
        yield m


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def logged_in(client, db_session):
    from tests.test_helpers import create_test_identity
    session_id, _ = create_test_identity(db_session, "tok-phase5")
    return session_id


def headers(session_id):
    return {"X-Session-Id": session_id}


# ---- payload builders -------------------------------------------------------


def exec_payload(**overrides):
    payload = {
        "client_order_id": "exec-phase5-0001",
        "symbol": "NIFTY",
        "strategy_tag": "Bull Call Spread",
        "strategy_id": "strat-1",
        "starting_capital": 500000,
        "legs": [
            {
                "symbol": "NIFTY",
                "expiration_date": EXPIRY,
                "strike_price": 24350,
                "option_type": "call",
                "action": "buy",
                "quantity": 1,
                "lot_size": LOT,
            },
            {
                "symbol": "NIFTY",
                "expiration_date": EXPIRY,
                "strike_price": 24550,
                "option_type": "call",
                "action": "sell",
                "quantity": 1,
                "lot_size": LOT,
            },
        ],
    }
    payload.update(overrides)
    return payload


def single_leg_payload(**overrides):
    payload = {
        "client_order_id": "exec-phase5-single",
        "symbol": "NIFTY",
        "strategy_tag": "Long Call",
        "starting_capital": 500000,
        "legs": [
            {
                "symbol": "NIFTY",
                "expiration_date": EXPIRY,
                "strike_price": 24350,
                "option_type": "call",
                "action": "buy",
                "quantity": 1,
                "lot_size": LOT,
            }
        ],
    }
    payload.update(overrides)
    return payload


def execute(client, session_id, payload):
    return client.post("/paper/executions", headers=headers(session_id), json=payload)


def exit_position(client, session_id, position_id, payload):
    return client.post(
        f"/paper/positions/{position_id}/exit", headers=headers(session_id), json=payload
    )


def first_position(db_session):
    from app.models import Position

    return db_session.query(Position).order_by(Position.id).first()



def test_client_lot_size_cannot_change_server_accounting(client, logged_in, db_session):
    """Persist the broker lot size even when the client submits a false value."""
    from app.models import PaperOrder, Position

    payload = single_leg_payload(
        client_order_id="exec-authoritative-lot-size",
        legs=[{
            "symbol": "NIFTY",
            "expiration_date": EXPIRY,
            "strike_price": 24350,
            "option_type": "call",
            "action": "buy",
            "quantity": 1,
            "lot_size": 1,
        }],
    )
    response = execute(client, logged_in, payload)
    assert response.status_code == 200, response.text

    order = db_session.query(PaperOrder).one()
    position = db_session.query(Position).one()
    assert order.lot_size == LOT
    assert position.lot_size == LOT
    # The response is an ExecutionOut (a group of orders), not a single
    # order — read the returned orders through the real API contract.
    body = response.json()
    assert len(body["orders"]) == 1
    # The client submitted lot_size=1; the broker-authoritative LOT must win.
    assert body["orders"][0]["lot_size"] == LOT

# ---- Order lifecycle (§5) ----------------------------------------------------


def test_order_status_constants():
    from app.services.paper_execution import EXECUTION_STATUSES, ORDER_STATUSES

    assert ORDER_STATUSES == {"PENDING", "FILLED", "PARTIALLY_FILLED", "CANCELLED", "REJECTED"}
    assert EXECUTION_STATUSES == {"PENDING", "FILLED", "PARTIAL", "FAILED", "CANCELLED"}


def test_transition_pending_to_filled():
    from app.services.paper_execution import can_transition, transition

    assert can_transition("PENDING", "FILLED")
    assert transition("PENDING", "FILLED") == "FILLED"


def test_transition_pending_to_rejected_and_cancelled():
    from app.services.paper_execution import can_transition

    assert can_transition("PENDING", "REJECTED")
    assert can_transition("PENDING", "CANCELLED")


def test_transition_partial_to_filled():
    from app.services.paper_execution import can_transition

    assert can_transition("PARTIALLY_FILLED", "FILLED")


def test_invalid_transition_cancelled_to_filled():
    from app.services.paper_execution import PaperExecutionError, can_transition, transition

    assert not can_transition("CANCELLED", "FILLED")
    assert not can_transition("REJECTED", "FILLED")
    with pytest.raises(PaperExecutionError) as exc:
        transition("CANCELLED", "FILLED")
    assert exc.value.code == "INVALID_STATE_TRANSITION"


# ---- Idempotency (§6) -------------------------------------------------------


def test_duplicate_same_request_is_idempotent(client, logged_in, db_session):
    from app.models import PaperOrder, PaperTransaction, Position, StrategyExecution

    first = execute(client, logged_in, exec_payload())
    assert first.status_code == 200
    exec_id = first.json()["execution_id"]

    replay = execute(client, logged_in, exec_payload())  # same client_order_id
    assert replay.status_code == 200
    body = replay.json()
    assert body["execution_id"] == exec_id
    assert body["duplicated"] is True

    # Nothing was created twice: one execution, its orders, positions, ledger.
    assert db_session.query(StrategyExecution).count() == 1
    assert db_session.query(PaperOrder).count() == 2
    assert db_session.query(Position).count() == 2
    assert db_session.query(PaperTransaction).count() == 2


def test_duplicate_exit_is_idempotent(client, logged_in, db_session):
    from app.models import PaperOrder, PaperTransaction, Position

    execute(client, logged_in, single_leg_payload(client_order_id="exit-idem-entry"))
    pos = first_position(db_session)

    first = exit_position(
        client, logged_in, pos.id, {"client_order_id": "exit-idem-1", "quantity": 1}
    )
    assert first.status_code == 200
    assert first.json()["duplicated"] is False

    replay = exit_position(
        client, logged_in, pos.id, {"client_order_id": "exit-idem-1", "quantity": 1}
    )
    assert replay.status_code == 200
    assert replay.json()["duplicated"] is True
    assert replay.json()["position"]["status"] == "closed"

    # Only one exit order + one exit transaction; the position wasn't re-closed.
    assert db_session.query(PaperOrder).filter(PaperOrder.kind == "exit").count() == 1
    assert (
        db_session.query(PaperTransaction).filter(PaperTransaction.type.like("EXIT_%")).count()
        == 1
    )
    assert db_session.query(Position).count() == 1


# ---- Positions (§12-§18) ----------------------------------------------------


def test_open_long(client, logged_in, db_session):
    execute(client, logged_in, single_leg_payload())
    pos = first_position(db_session)
    assert pos.net_quantity == 1
    assert pos.average_entry_price == 125.25
    assert pos.lot_size == LOT
    assert pos.status == "open"


def test_open_short(client, logged_in, db_session):
    execute(
        client,
        logged_in,
        single_leg_payload(client_order_id="exec-short", legs=[{
            "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
            "option_type": "call", "action": "sell", "quantity": 1, "lot_size": LOT,
        }]),
    )
    pos = first_position(db_session)
    assert pos.net_quantity == -1
    assert pos.average_entry_price == 125.25


def test_add_same_direction_nets_into_one_position(client, logged_in, db_session):
    from app.models import Position

    execute(client, logged_in, single_leg_payload(client_order_id="exec-net-1"))
    execute(client, logged_in, single_leg_payload(client_order_id="exec-net-2"))
    positions = db_session.query(Position).all()
    assert len(positions) == 1  # same instrument nets, no duplicate open row
    assert positions[0].net_quantity == 2


def test_weighted_average_entry_price(client, logged_in, db_session, chain_quotes):
    # BUY 1 @ 125.25 then BUY 2 @ 110.00 → (1*125.25 + 2*110) / 3 = 115.0833.
    execute(client, logged_in, single_leg_payload(client_order_id="exec-wavg-1"))
    chain_quotes[EXPIRY][24350]["call"] = 110.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-wavg-2", legs=[{
        "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
        "option_type": "call", "action": "buy", "quantity": 2, "lot_size": LOT,
    }]))
    pos = first_position(db_session)
    assert pos.net_quantity == 3
    assert abs(pos.average_entry_price - 115.083333) < 0.01


def test_partial_exit_keeps_remaining_and_average(client, logged_in, db_session, chain_quotes):
    chain_quotes[EXPIRY][24350]["call"] = 100.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-partial", legs=[{
        "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
        "option_type": "call", "action": "buy", "quantity": 5, "lot_size": LOT,
    }]))
    pos = first_position(db_session)
    assert pos.net_quantity == 5

    chain_quotes[EXPIRY][24350]["call"] = 120.0
    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-partial", "quantity": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["position"]["net_quantity"] == 3
    assert body["position"]["average_entry_price"] == 100.0  # unchanged for the remainder
    assert body["position"]["realized_pnl"] == (120.0 - 100.0) * 2 * LOT  # 2600
    assert body["position"]["status"] == "open"


def test_full_exit_closes_position_but_keeps_record(client, logged_in, db_session):
    execute(client, logged_in, single_leg_payload())
    pos = first_position(db_session)
    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-full", "quantity": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["position"]["net_quantity"] == 0
    assert body["position"]["status"] == "closed"
    assert body["position"]["closed_at"] is not None
    # Historical record remains queryable.
    from app.models import Position

    assert db_session.query(Position).count() == 1
    assert db_session.query(Position).first().status == "closed"


def test_reverse_position_flips_direction(client, logged_in, db_session, chain_quotes):
    chain_quotes[EXPIRY][24350]["call"] = 100.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-rev-1", legs=[{
        "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
        "option_type": "call", "action": "buy", "quantity": 1, "lot_size": LOT,
    }]))
    chain_quotes[EXPIRY][24350]["call"] = 120.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-rev-2", legs=[{
        "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
        "option_type": "call", "action": "sell", "quantity": 3, "lot_size": LOT,
    }]))
    pos = first_position(db_session)
    # 1 covered at +1300 realized; 2 leftover short at 120.
    assert pos.net_quantity == -2
    assert pos.average_entry_price == 120.0
    assert pos.realized_pnl == (120.0 - 100.0) * 1 * LOT


# ---- P&L (§15-§18) ----------------------------------------------------------


def test_long_realized_pnl(client, logged_in, db_session, chain_quotes):
    chain_quotes[EXPIRY][24350]["call"] = 100.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-rl-1"))
    pos = first_position(db_session)
    chain_quotes[EXPIRY][24350]["call"] = 120.0
    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-rl-1", "quantity": 1})
    assert resp.json()["order"]["realized_pnl"] == (120.0 - 100.0) * 1 * LOT


def test_short_realized_pnl(client, logged_in, db_session, chain_quotes):
    chain_quotes[EXPIRY][24350]["call"] = 200.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-rs-1", legs=[{
        "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
        "option_type": "call", "action": "sell", "quantity": 1, "lot_size": LOT,
    }]))
    pos = first_position(db_session)
    chain_quotes[EXPIRY][24350]["call"] = 150.0
    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-rs-1", "quantity": 1})
    assert resp.json()["order"]["realized_pnl"] == (200.0 - 150.0) * 1 * LOT


def test_unrealized_pnl_helper():
    from app.services.paper_execution import unrealized_pnl

    assert unrealized_pnl(2, 100.0, 120.0, LOT) == 20.0 * 2 * LOT  # long
    assert unrealized_pnl(-2, 100.0, 90.0, LOT) == 10.0 * 2 * LOT  # short
    assert unrealized_pnl(0, 100.0, 120.0, LOT) == 0.0  # closed


def test_execution_realized_includes_partial_exits(client, logged_in, db_session, chain_quotes):
    """Regression: an execution's realized P&L must accumulate on PARTIAL exits
    too, so it always equals the sum of its positions' realizations (Phase 5.1
    analytics treats that sum as the authoritative strategy result)."""
    from app.models import StrategyExecution

    chain_quotes[EXPIRY][24350]["call"] = 100.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-partial-real", legs=[{
        "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
        "option_type": "call", "action": "buy", "quantity": 5, "lot_size": LOT,
    }]))
    pos = first_position(db_session)

    chain_quotes[EXPIRY][24350]["call"] = 120.0
    exit_position(client, logged_in, pos.id, {"client_order_id": "exit-partial-real-1", "quantity": 2})
    chain_quotes[EXPIRY][24350]["call"] = 130.0
    exit_position(client, logged_in, pos.id, {"client_order_id": "exit-partial-real-2", "quantity": 3})

    execution = db_session.query(StrategyExecution).first()
    # (120-100)*2 + (130-100)*3 realized, in rupees.
    assert execution.realized_pnl == pytest.approx((20 * 2 + 30 * 3) * LOT, abs=0.01)
    assert execution.exit_at is not None  # set only when fully closed


def test_full_exit_realized_matches_partial_sum(client, logged_in, db_session, chain_quotes):
    chain_quotes[EXPIRY][24350]["call"] = 100.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-sum", legs=[{
        "symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
        "option_type": "call", "action": "buy", "quantity": 3, "lot_size": LOT,
    }]))
    pos = first_position(db_session)
    chain_quotes[EXPIRY][24350]["call"] = 110.0
    exit_position(client, logged_in, pos.id, {"client_order_id": "exit-sum-1", "quantity": 2})
    pos = db_session.query(type(pos)).get(pos.id)
    chain_quotes[EXPIRY][24350]["call"] = 115.0
    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-sum-2", "quantity": 1})
    assert resp.json()["position"]["realized_pnl"] == (110 - 100) * 2 * LOT + (115 - 100) * 1 * LOT


# ---- Portfolio & cash (§20-§24) ---------------------------------------------


def test_cash_updates_for_debit_and_credit(client, logged_in):
    # Debit spread: BUY 125.25 + SELL 35.60 → net debit 89.65 × 65 = 5827.25.
    execute(client, logged_in, exec_payload(client_order_id="exec-cash-1"))
    port = client.get("/paper/portfolio", headers=headers(logged_in)).json()["summary"]
    assert port["available_cash"] == pytest.approx(500000 - (125.25 - 35.60) * LOT)

    # Credit spread: sell 25000 CE @ 200, buy 25100 CE @ 80 → net credit 7800.
    execute(client, logged_in, exec_payload(client_order_id="exec-cash-2", legs=[
        {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 25000,
         "option_type": "call", "action": "sell", "quantity": 1, "lot_size": LOT},
        {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 25100,
         "option_type": "call", "action": "buy", "quantity": 1, "lot_size": LOT},
    ]))
    port = client.get("/paper/portfolio", headers=headers(logged_in)).json()["summary"]
    assert port["available_cash"] == pytest.approx(
        500000 - (125.25 - 35.60) * LOT + (200.0 - 80.0) * LOT
    )


def test_portfolio_summary_counts_and_realized(client, logged_in, db_session, chain_quotes):
    chain_quotes[EXPIRY][24350]["call"] = 100.0
    execute(client, logged_in, single_leg_payload(client_order_id="exec-ps-1"))
    pos = first_position(db_session)
    chain_quotes[EXPIRY][24350]["call"] = 120.0
    exit_position(client, logged_in, pos.id, {"client_order_id": "exit-ps-1", "quantity": 1})

    body = client.get("/paper/portfolio", headers=headers(logged_in)).json()
    summary = body["summary"]
    assert summary["realized_pnl"] == (120.0 - 100.0) * LOT
    assert summary["unrealized_pnl"] is None  # never fabricated without marks
    assert summary["total_pnl"] == summary["realized_pnl"]
    assert summary["open_position_count"] == 0
    assert summary["open_strategy_count"] == 0
    assert summary["available_cash"] == pytest.approx(500000 - 100.0 * LOT + 120.0 * LOT)


def test_portfolio_summary_after_multiple_positions(client, logged_in):
    execute(client, logged_in, exec_payload(client_order_id="exec-multi-1"))
    body = client.get("/paper/portfolio", headers=headers(logged_in)).json()
    summary = body["summary"]
    assert summary["open_position_count"] == 2
    assert summary["open_strategy_count"] == 1
    assert summary["invested_value"] == pytest.approx(125.25 * LOT + 35.60 * LOT)
    assert summary["realized_pnl"] == 0.0


def test_portfolio_groups_group_multi_leg(client, logged_in):
    execute(client, logged_in, exec_payload(client_order_id="exec-grp-1"))
    body = client.get("/paper/portfolio", headers=headers(logged_in)).json()
    groups = body["groups"]
    assert len(groups) == 1
    g = groups[0]
    assert g["strategy_tag"] == "Bull Call Spread"
    assert g["entry_net"] == pytest.approx((125.25 - 35.60) * LOT)
    assert len(g["legs"]) == 2
    assert {o["action"] for o in g["legs"]} == {"buy", "sell"}
    assert {o["option_type"] for o in g["legs"]} == {"call"}


# ---- Multi-leg execution (§7/§8) ---------------------------------------------


def test_grouped_execution_creates_execution_orders_and_positions(client, logged_in, db_session):
    from app.models import PaperOrder, PaperTransaction, Position, StrategyExecution

    resp = execute(client, logged_in, exec_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FILLED"
    assert body["filled_count"] == 2
    assert body["failed_count"] == 0
    assert len(body["orders"]) == 2
    # All generated orders share the execution id.
    assert {o["execution_id"] for o in body["orders"]} == {body["execution_id"]}
    assert body["entry_net"] == pytest.approx((125.25 - 35.60) * LOT)

    assert db_session.query(StrategyExecution).count() == 1
    assert db_session.query(PaperOrder).count() == 2
    assert db_session.query(Position).count() == 2
    assert db_session.query(PaperTransaction).count() == 2


def test_individual_leg_positions(client, logged_in, db_session):
    from app.models import Position

    execute(client, logged_in, exec_payload())
    positions = db_session.query(Position).order_by(Position.strike).all()
    assert len(positions) == 2
    assert positions[0].strike == 24350 and positions[0].net_quantity == 1
    assert positions[1].strike == 24550 and positions[1].net_quantity == -1


def _assert_no_partial_writes(db_session):
    """Zero writes — never a misleading partial success."""
    from app.models import PaperOrder, PaperTransaction, Position, StrategyExecution, Trade

    assert db_session.query(StrategyExecution).count() == 0
    assert db_session.query(PaperOrder).count() == 0
    assert db_session.query(Position).count() == 0
    assert db_session.query(PaperTransaction).count() == 0
    assert db_session.query(Trade).count() == 0


def test_partial_strategy_failure_is_atomic(client, logged_in, db_session, chain_quotes):
    # Day 49: broker-authoritative contract metadata is resolved BEFORE chain
    # prices, and both guards are fail-closed. Dropping strike 24550 from the
    # fixture removes its price AND its contract metadata, so the metadata
    # guard fires first. That ordering is intentional: an unknown lot size
    # must never reach the price/quantity computation at all.
    chain_quotes[EXPIRY] = {24350: {"call": 125.25, "put": 90.0}}  # 24550 gone
    resp = execute(client, logged_in, exec_payload())
    assert resp.status_code == 409
    assert "CONTRACT_DATA_MISSING" in resp.json()["detail"]
    _assert_no_partial_writes(db_session)


def test_missing_price_for_valid_contract_is_atomic(client, logged_in, db_session, chain_quotes):
    """The chain-price guard stays covered on its own.

    ``chain_mock`` derives BOTH the chain payload and the contract payload
    from ``chain_quotes``, so removing a strike from the fixture loses its
    contract metadata too and the metadata guard masks the price guard. Here
    the contracts are left fully intact and only the market price for strike
    24550 is withheld, so execution reaches ``resolve_market_prices`` and the
    CHAIN_DATA_MISSING path is genuinely exercised.
    """
    async def chain_without_24550(token, instrument_key, expiry):
        quotes = {k: v for k, v in chain_quotes.get(expiry, {}).items() if k != 24550}
        return chain_payload(expiry, quotes)

    with patch(
        "app.services.upstox.get_option_chain",
        new=AsyncMock(side_effect=chain_without_24550),
    ):
        resp = execute(client, logged_in, exec_payload())

    assert resp.status_code == 409
    assert "CHAIN_DATA_MISSING" in resp.json()["detail"]
    _assert_no_partial_writes(db_session)


def test_multi_expiry_execution_uses_each_expirys_chain(client, logged_in, db_session):
    # Calendar: BUY 24350 CE on Aug 27 @125.25, SELL 24350 CE on Sep 3 @130.
    resp = execute(client, logged_in, exec_payload(legs=[
        {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
         "option_type": "call", "action": "buy", "quantity": 1, "lot_size": LOT},
        {"symbol": "NIFTY", "expiration_date": EXPIRY2, "strike_price": 24350,
         "option_type": "call", "action": "sell", "quantity": 1, "lot_size": LOT},
    ]))
    assert resp.status_code == 200
    prices = {o["expiry"]: o["fill_price"] for o in resp.json()["orders"]}
    assert prices[EXPIRY] == 125.25
    assert prices[EXPIRY2] == 130.0
    from app.models import Position

    assert db_session.query(Position).count() == 2


# ---- Safety (§9/§10) ---------------------------------------------------------


def test_market_closed_blocks_execution(client, logged_in, db_session):
    from app.models import PaperOrder, Position, StrategyExecution

    with gate_status("closed"):
        resp = execute(client, logged_in, exec_payload())
    assert resp.status_code == 409
    assert resp.json()["detail"] == MARKET_CLOSED_MSG
    assert db_session.query(StrategyExecution).count() == 0
    assert db_session.query(PaperOrder).count() == 0
    assert db_session.query(Position).count() == 0


def test_market_unknown_blocks_execution(client, logged_in, db_session):
    from app.models import Position

    with gate_status("unknown"):
        resp = execute(client, logged_in, exec_payload())
    assert resp.status_code == 409
    assert resp.json()["detail"] == MARKET_UNKNOWN_MSG
    assert db_session.query(Position).count() == 0


def test_market_closed_blocks_exit(client, logged_in, db_session):
    execute(client, logged_in, single_leg_payload())
    pos = first_position(db_session)
    with gate_status("closed"):
        resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-gate"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == MARKET_CLOSED_MSG
    assert db_session.query(type(pos)).get(pos.id).status == "open"


def test_missing_chain_blocks_exit(client, logged_in, db_session, chain_quotes):
    execute(client, logged_in, single_leg_payload())
    pos = first_position(db_session)
    chain_quotes[EXPIRY] = {}  # chain no longer resolves the instrument
    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-noc"})
    assert resp.status_code == 409
    assert "CHAIN_DATA_MISSING" in resp.json()["detail"]
    assert db_session.query(type(pos)).get(pos.id).status == "open"


def test_invalid_quantity_and_insufficient_position(client, logged_in, db_session):
    execute(client, logged_in, single_leg_payload())
    pos = first_position(db_session)

    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-qty-0", "quantity": 0})
    assert resp.status_code == 422  # schema: quantity ge=1

    resp = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-qty-5", "quantity": 5})
    assert resp.status_code == 400
    assert "INSUFFICIENT_POSITION" in resp.json()["detail"]
    assert db_session.query(type(pos)).get(pos.id).net_quantity == 1


def test_exit_unknown_position_returns_404(client, logged_in):
    resp = exit_position(client, logged_in, 99999, {"client_order_id": "exit-404"})
    assert resp.status_code == 404
    assert "POSITION_NOT_FOUND" in resp.json()["detail"]


# ---- Concurrency (§34) -------------------------------------------------------


def test_two_different_exits_only_one_succeeds(client, logged_in, db_session):
    execute(client, logged_in, single_leg_payload())
    pos = first_position(db_session)

    # Simulates two simultaneous close attempts on a +1 position.
    first = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-race-1", "quantity": 1})
    second = exit_position(client, logged_in, pos.id, {"client_order_id": "exit-race-2", "quantity": 1})
    assert first.status_code == 200
    assert second.status_code == 400
    assert "INSUFFICIENT_POSITION" in second.json()["detail"]
    assert db_session.query(type(pos)).get(pos.id).net_quantity == 0


def test_simultaneous_duplicate_entries_single_execution(client, logged_in, db_session):
    from app.models import PaperOrder, StrategyExecution

    # Same idempotency key submitted back-to-back (double click / retry).
    a = execute(client, logged_in, single_leg_payload(client_order_id="exec-race-entry"))
    b = execute(client, logged_in, single_leg_payload(client_order_id="exec-race-entry"))
    assert a.json()["execution_id"] == b.json()["execution_id"]
    assert db_session.query(StrategyExecution).count() == 1
    assert db_session.query(PaperOrder).count() == 1


# ---- Isolation (§35) ---------------------------------------------------------


def test_user_b_cannot_see_user_a_positions(client, db_session):
    from tests.test_helpers import create_test_identity
    session_a, _ = create_test_identity(db_session, "tok-user-a")
    execute(client, session_a, single_leg_payload(client_order_id="exec-iso-a"))

    session_b, _ = create_test_identity(db_session, "tok-user-b")
    resp = client.get("/paper/positions", headers=headers(session_b))
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.get("/paper/orders", headers=headers(session_b))
    assert resp.status_code == 200
    assert resp.json() == []

    port = client.get("/paper/portfolio", headers=headers(session_b)).json()["summary"]
    assert port["available_cash"] == 500000
    assert port["open_position_count"] == 0


def test_user_b_cannot_exit_user_a_position(client, db_session):
    from app.models import Position
    from tests.test_helpers import create_test_identity

    session_a, _ = create_test_identity(db_session, "tok-user-a")
    execute(client, session_a, single_leg_payload(client_order_id="exec-iso-b"))
    pos = db_session.query(Position).first()

    session_b, _ = create_test_identity(db_session, "tok-user-b")
    resp = exit_position(client, session_b, pos.id, {"client_order_id": "exit-iso-b"})
    assert resp.status_code == 404
    assert db_session.query(Position).first().status == "open"


# ---- Journal linkage (§22) ---------------------------------------------------


def test_execution_creates_journal_record_with_same_execution_id(client, logged_in, db_session):
    from app.models import Leg, Trade

    resp = execute(client, logged_in, exec_payload())
    exec_id = resp.json()["execution_id"]

    trades = db_session.query(Trade).all()
    assert len(trades) == 1
    assert trades[0].strategy_execution_id == exec_id
    assert trades[0].client_order_id == "exec-phase5-0001"
    assert trades[0].status == "open"
    assert trades[0].entry_net == pytest.approx((125.25 - 35.60) * LOT)
    legs = db_session.query(Leg).all()
    assert len(legs) == 2
    # Journal legs carry the AUTHORITATIVE fill prices.
    assert {l.premium for l in legs} == {125.25, 35.60}


def test_full_exit_closes_journal_trade(client, logged_in, db_session):
    from app.models import Leg, Trade

    execute(client, logged_in, single_leg_payload(client_order_id="exec-jrnl"))
    pos = first_position(db_session)
    exit_position(client, logged_in, pos.id, {"client_order_id": "exit-jrnl", "quantity": 1})

    trade = db_session.query(Trade).first()
    assert trade.status == "closed"
    assert trade.exit_at is not None
    assert trade.realized_pnl == (125.25 - 125.25) * LOT  # flat exit at same price
    leg = db_session.query(Leg).first()
    assert leg.exit_price == 125.25
    assert leg.realized_pnl == 0.0


# ---- Reconciliation (§23) ----------------------------------------------------


def test_reconcile_valid_after_trades(client, logged_in):
    execute(client, logged_in, exec_payload(client_order_id="exec-rec-1"))
    body = client.get("/paper/reconcile", headers=headers(logged_in)).json()
    assert body["valid"] is True
    assert body["discrepancies"] == []


def test_reconcile_detects_cash_mismatch(client, logged_in, db_session):
    from app.models import PaperTransaction

    execute(client, logged_in, exec_payload(client_order_id="exec-rec-2"))
    txn = db_session.query(PaperTransaction).first()
    db_session.delete(txn)
    db_session.commit()

    body = client.get("/paper/reconcile", headers=headers(logged_in)).json()
    assert body["valid"] is False
    assert any(d["type"] == "CASH_MISMATCH" for d in body["discrepancies"])


# ---- Reset (§30 UI support) --------------------------------------------------


def test_portfolio_reset_clears_state(client, logged_in, db_session):
    from app.models import PaperOrder, Position, StrategyExecution, Trade

    execute(client, logged_in, exec_payload(client_order_id="exec-reset"))
    resp = client.post("/paper/portfolio/reset", headers=headers(logged_in))
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["available_cash"] == 500000
    assert summary["open_position_count"] == 0
    assert db_session.query(StrategyExecution).count() == 0
    assert db_session.query(PaperOrder).count() == 0
    assert db_session.query(Position).count() == 0
    assert db_session.query(Trade).count() == 0# ---- Auth guard --------------------------------------------------------------
def test_execution_requires_login(client):
    resp = client.post("/paper/executions", json=exec_payload())
    assert resp.status_code == 401


def test_positions_requires_login(client):
    assert client.get("/paper/positions").status_code == 401


def test_portfolio_requires_login(client):
    assert client.get("/paper/portfolio").status_code == 401


# ---- Phase 5.2.1: strategy identity (§3/§4) ---------------------------------

# Long Seagull legs: BUY 24350 CE, SELL 24550 CE, SELL 25000 PE (EXPIRY).
# Every strategy below uses instruments that no other strategy in this file
# touches — positions net by (symbol, expiry, strike, option_type), so
# overlapping instruments would merge rows and blur execution identity.
SEAGULL_LEGS = [
    {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24350,
     "option_type": "call", "action": "buy", "quantity": 1, "lot_size": LOT},
    {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24550,
     "option_type": "call", "action": "sell", "quantity": 1, "lot_size": LOT},
    {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 25000,
     "option_type": "put", "action": "sell", "quantity": 1, "lot_size": LOT},
]

# Bull Put Spread legs: SELL 25100 PE, BUY 26000 PE (EXPIRY).
BULL_PUT_LEGS = [
    {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 25100,
     "option_type": "put", "action": "sell", "quantity": 1, "lot_size": LOT},
    {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 26000,
     "option_type": "put", "action": "buy", "quantity": 1, "lot_size": LOT},
]

# Bull Condor legs on EXPIRY2 (distinct instruments): BUY 24350 CE, SELL 24400
# CE, SELL 24350 PE, BUY 24400 PE.
BULL_CONDOR_LEGS = [
    {"symbol": "NIFTY", "expiration_date": EXPIRY2, "strike_price": 24350,
     "option_type": "call", "action": "buy", "quantity": 1, "lot_size": LOT},
    {"symbol": "NIFTY", "expiration_date": EXPIRY2, "strike_price": 24400,
     "option_type": "call", "action": "sell", "quantity": 1, "lot_size": LOT},
    {"symbol": "NIFTY", "expiration_date": EXPIRY2, "strike_price": 24350,
     "option_type": "put", "action": "sell", "quantity": 1, "lot_size": LOT},
    {"symbol": "NIFTY", "expiration_date": EXPIRY2, "strike_price": 24400,
     "option_type": "put", "action": "buy", "quantity": 1, "lot_size": LOT},
]


def test_named_strategy_tags_preserved_on_positions(client, logged_in):
    """Long Seagull / Bull Put Spread / Bull Condor never render as Custom."""
    seagull = execute(client, logged_in, exec_payload(
        client_order_id="exec-tag-seagull", strategy_tag="Long Seagull", legs=SEAGULL_LEGS
    )).json()
    bps = execute(client, logged_in, exec_payload(
        client_order_id="exec-tag-bps", strategy_tag="Bull Put Spread", legs=BULL_PUT_LEGS
    )).json()
    condor = execute(client, logged_in, exec_payload(
        client_order_id="exec-tag-condor", strategy_tag="Bull Condor", legs=BULL_CONDOR_LEGS
    )).json()

    positions = client.get("/paper/positions", headers=headers(logged_in)).json()
    assert len(positions) == 3 + 2 + 4
    tags = {p["strategy_tag"] for p in positions}
    assert tags == {"Long Seagull", "Bull Put Spread", "Bull Condor"}
    # Every position carries its execution id — the authoritative grouping key.
    by_exec = {}
    for p in positions:
        by_exec.setdefault(p["strategy_execution_id"], set()).add(p["strategy_tag"])
    assert by_exec[seagull["execution_id"]] == {"Long Seagull"}
    assert by_exec[bps["execution_id"]] == {"Bull Put Spread"}
    assert by_exec[condor["execution_id"]] == {"Bull Condor"}


def test_custom_unnamed_strategy_remains_custom(client, logged_in):
    # No real strategy name in the request → backend falls back to "Custom"
    # (never a lie, never inferred from the legs).
    payload = exec_payload(client_order_id="exec-tag-custom")
    del payload["strategy_tag"]
    resp = execute(client, logged_in, payload)
    assert resp.status_code == 200
    positions = client.get("/paper/positions", headers=headers(logged_in)).json()
    assert len(positions) == 2
    assert {p["strategy_tag"] for p in positions} == {"Custom"}


def test_legacy_position_without_execution_falls_back_to_custom(client, logged_in, db_session):
    from datetime import datetime, timezone

    from app.models import Position

    now = datetime.now(timezone.utc)
    db_session.add(Position(
        user_id=db_session._test_user_id, symbol="NIFTY", expiry=EXPIRY, strike=24400,
        option_type="call", net_quantity=1, average_entry_price=50.0,
        lot_size=LOT, realized_pnl=0.0, status="open",
        strategy_execution_id=None, opened_at=now,
    ))
    db_session.commit()
    positions = client.get("/paper/positions", headers=headers(logged_in)).json()
    assert any(p["strategy_tag"] == "Custom" for p in positions)


def test_dangling_execution_id_falls_back_to_custom(client, logged_in, db_session):
    from datetime import datetime, timezone

    from app.models import Position

    now = datetime.now(timezone.utc)
    db_session.add(Position(
        user_id=db_session._test_user_id, symbol="NIFTY", expiry=EXPIRY, strike=24450,
        option_type="put", net_quantity=-1, average_entry_price=60.0,
        lot_size=LOT, realized_pnl=0.0, status="open",
        strategy_execution_id="exec-no-longer-exists", opened_at=now,
    ))
    db_session.commit()
    positions = client.get("/paper/positions", headers=headers(logged_in)).json()
    assert any(p["strategy_tag"] == "Custom" for p in positions)


def test_same_execution_groups_and_distinct_executions_stay_separate(client, logged_in):
    # Two SEPARATE Long Seagull executions: same strategy name, different ids,
    # non-overlapping instruments (positions net by instrument, so a repeat of
    # identical legs would merge into one group — this test uses distinct
    # strikes to prove different executions never mix).
    first = execute(client, logged_in, exec_payload(
        client_order_id="exec-seagull-a", strategy_tag="Long Seagull", legs=SEAGULL_LEGS
    )).json()
    second_legs = [
        {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 25100,
         "option_type": "call", "action": "buy", "quantity": 1, "lot_size": LOT},
        {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 26000,
         "option_type": "call", "action": "sell", "quantity": 1, "lot_size": LOT},
        {"symbol": "NIFTY", "expiration_date": EXPIRY, "strike_price": 24550,
         "option_type": "put", "action": "sell", "quantity": 1, "lot_size": LOT},
    ]
    second = execute(client, logged_in, exec_payload(
        client_order_id="exec-seagull-b", strategy_tag="Long Seagull", legs=second_legs
    )).json()
    assert first["execution_id"] != second["execution_id"]

    positions = client.get("/paper/positions", headers=headers(logged_in)).json()
    ids = {p["strategy_execution_id"] for p in positions}
    assert ids == {first["execution_id"], second["execution_id"]}
    # The three legs of one execution group together; the other execution's
    # legs never mix in.
    for exec_id in ids:
        legs = [p for p in positions if p["strategy_execution_id"] == exec_id]
        assert len(legs) == 3
        assert {p["strategy_tag"] for p in legs} == {"Long Seagull"}


def test_positions_response_exposes_execution_id_and_strategy_tag(client, logged_in):
    execute(client, logged_in, exec_payload(client_order_id="exec-tag-expose"))
    pos = client.get("/paper/positions", headers=headers(logged_in)).json()[0]
    assert pos["strategy_execution_id"]
    assert pos["strategy_tag"] == "Bull Call Spread"


# ---- Phase 5.2.1: option tick-size fills (§17/§18/§20) ------------------------


def test_entry_fill_price_is_tick_rounded(client, logged_in, chain_quotes, db_session):
    from app.models import PaperOrder

    # Raw broker LTP 125.23 is NOT a valid ₹0.05 tick → fill at 125.25.
    chain_quotes[EXPIRY][24350]["call"] = 125.23
    resp = execute(client, logged_in, exec_payload(client_order_id="exec-tick-entry"))
    assert resp.status_code == 200
    fills = {o["strike"]: o["fill_price"] for o in resp.json()["orders"]}
    assert fills[24350] == 125.25
    assert fills[24550] == pytest.approx(35.60)
    # The persisted authoritative order agrees (fill boundary == display).
    order = db_session.query(PaperOrder).filter_by(kind="entry", strike=24350).one()
    assert order.fill_price == 125.25


def test_exit_fill_price_is_tick_rounded(client, logged_in, chain_quotes):
    execute(client, logged_in, exec_payload(client_order_id="exec-tick-exit"))
    positions = client.get("/paper/positions", headers=headers(logged_in)).json()
    pos = next(p for p in positions if p["strike"] == 24550)
    # Raw LTP 35.62 → valid tick 35.60.
    chain_quotes[EXPIRY][24550]["call"] = 35.62
    resp = exit_position(client, logged_in, pos["id"], {"client_order_id": "exit-tick-1", "quantity": 1})
    assert resp.status_code == 200
    assert resp.json()["order"]["fill_price"] == 35.60


def test_bulk_exit_fill_prices_are_tick_rounded(client, logged_in, chain_quotes):
    execute(client, logged_in, exec_payload(client_order_id="exec-tick-bulk"))
    # 24350 call 125.23 → 125.25; 24550 call 35.62 → 35.60.
    chain_quotes[EXPIRY][24350]["call"] = 125.23
    chain_quotes[EXPIRY][24550]["call"] = 35.62
    resp = client.post(
        "/paper/positions/exit-all",
        headers=headers(logged_in),
        json={"client_order_id": "exitall-tick-1"},
    )
    assert resp.status_code == 200
    fills = {p["strike"]: p["fill_price"] for p in resp.json()["positions"]}
    assert fills[24350] == 125.25
    assert fills[24550] == 35.60


# ---- Phase 5.2.1: active-position semantics (§7) ------------------------------


def test_zero_quantity_positions_never_appear_as_active(client, logged_in, db_session):
    from app.models import Position

    execute(client, logged_in, exec_payload(client_order_id="exec-zero-qty"))
    # Force one instrument to zero quantity while keeping status="open" —
    # it must never surface in the ACTIVE list (the server-side invariant is
    # status == "open" AND net_quantity != 0).
    pos = db_session.query(Position).filter_by(strike=24350).one()
    pos.net_quantity = 0
    pos.status = "open"
    db_session.commit()

    active = client.get("/paper/positions", headers=headers(logged_in)).json()
    assert all(p["net_quantity"] != 0 for p in active)
    assert all(p["status"] == "open" for p in active)
    assert len(active) == 1  # only the 24550 leg remains


def test_open_positions_are_user_isolated(client, logged_in, db_session):
    from datetime import datetime, timezone

    from app.models import Position

    other_user = "tok-other-user"
    now = datetime.now(timezone.utc)
    db_session.add(Position(
        user_id=other_user, symbol="NIFTY", expiry=EXPIRY, strike=24600,
        option_type="call", net_quantity=3, average_entry_price=40.0,
        lot_size=LOT, realized_pnl=0.0, status="open",
        strategy_execution_id=None, opened_at=now,
    ))
    db_session.commit()

    execute(client, logged_in, exec_payload(client_order_id="exec-isolation"))
    active = client.get("/paper/positions", headers=headers(logged_in)).json()
    assert all(p["strategy_execution_id"] is not None for p in active)
    assert all(p["strike"] != 24600 for p in active)
    assert len(active) == 2

def test_position_mutation_queries_use_row_locking_for_postgresql():
    """Paper position mutations must request row locks on PG/CRDB paths."""
    from sqlalchemy.dialects.postgresql import dialect

    from app.services.paper_execution import _get_position

    captured = {}

    class ScalarProbe:
        def scalar(self, statement):
            captured["statement"] = statement
            return None

    _get_position(
        ScalarProbe(),
        "user-1",
        "NIFTY",
        EXPIRY,
        24350,
        "call",
        for_update=True,
    )

    statement = captured["statement"]
    assert "FOR UPDATE" in str(statement.compile(dialect=dialect())).upper()
    assert statement.get_execution_options()["populate_existing"] is True
