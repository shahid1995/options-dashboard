"""Regression tests for the paper exit-intent Position reference defect.

POST /paper/exit-intent previously referenced an undefined ``Position`` name
inside the market-price resolution loop, raising ``NameError`` and returning
HTTP 500. These tests fail against the original implementation and pass once
the mapped SQLAlchemy ``Position`` model is resolved at that point.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Position
from app.services.execution_intent import (
    ExecutionMode,
    ExecutionResult,
    ExecutionStatus,
)
from tests.test_helpers import create_test_identity

EXPIRY = "2026-08-21"
LOT = 65


@pytest.fixture(autouse=True)
def market_open_gate():
    status = SimpleNamespace(
        status="open", source="test", trade_date="2026-08-14",
        checked_at="2026-08-14T10:00:00+05:30",
        message="test market status: open", error=None,
        segment="INDEX_DERIVATIVES", session_state="OPEN",
        timezone="Asia/Kolkata", trading_allowed=True,
    )
    with patch("app.routers.paper.get_market_status", new=AsyncMock(return_value=status)):
        yield


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def logged_in(client, db_session):
    session_id, _ = create_test_identity(db_session, "tok-exit-intent-ref")
    return session_id


def _account(db, user_id):
    from app.models import PaperAccount

    acc = PaperAccount(user_id=user_id, starting_capital=500000)
    db.add(acc)
    db.flush()
    return acc


def _position(db, user_id):
    pos = Position(
        user_id=user_id, symbol="NIFTY", expiry=EXPIRY, strike=25000.0,
        option_type="call", net_quantity=2, lot_size=LOT,
        average_entry_price=150.0, realized_pnl=0.0, status="open",
    )
    db.add(pos)
    db.flush()
    return pos


def _exposure(db, user_id, position_id):
    from app.models import StrategyExecution, StrategyLegExposure

    se = StrategyExecution(
        user_id=user_id, execution_id="exec-ei", client_order_id="coid-ei",
        strategy_tag="Bull Call", symbol="NIFTY", status="FILLED", entry_net=0.0,
    )
    db.add(se)
    db.flush()
    exp = StrategyLegExposure(
        user_id=user_id, execution_id="exec-ei", position_id=position_id,
        order_id=1, symbol="NIFTY", expiry=EXPIRY, strike=25000.0,
        option_type="call", action="buy", original_quantity=2,
        remaining_quantity=2, status="open",
    )
    db.add(exp)
    db.flush()
    return exp


def test_exit_intent_resolves_position_model(client, logged_in, db_session):
    uid = db_session._test_user_id
    _account(db_session, uid)
    pos = _position(db_session, uid)
    _exposure(db_session, uid, pos.id)
    db_session.commit()

    mock_router = MagicMock()
    mock_router.execute_intent = AsyncMock(return_value=ExecutionResult(
        intent_id="intent-test", status=ExecutionStatus.SUCCESS,
        mode=ExecutionMode.PAPER, targets_succeeded=1, results=[],
    ))

    prices = {("NIFTY", EXPIRY, 25000.0, "call"): 155.0}
    mock_resolve = AsyncMock(return_value=prices)

    with patch("app.routers.paper.resolve_bulk_market_prices", new=mock_resolve), \
         patch("app.services.execution_intent.ExecutionRouter", return_value=mock_router):
        resp = client.post(
            "/paper/exit-intent",
            headers={"X-Session-Id": logged_in},
            json={
                "client_order_id": "ei-ref-001",
                "scope": "POSITION",
                "position_id": pos.id,
                "option_type": "CALL",
                "action": "BUY",
                "quantity_mode": "ALL",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "SUCCESS"

    # The market-price resolver must be awaited exactly once.
    mock_resolve.assert_awaited_once()

    # Its position collection must contain the actual expected mapped Position
    # instance associated with pos.id, distinct from the earlier selector
    # resolution lookup.
    call = mock_resolve.await_args
    if call.args and len(call.args) >= 2:
        positions_arg = call.args[1]
    else:
        positions_arg = call.kwargs.get("positions")
    positions_list = list(positions_arg)
    assert pos in positions_list, "expected Position instance not passed to resolver"
    assert any(
        isinstance(p, Position) and getattr(p, "id", None) == pos.id
        for p in positions_list
    ), "resolver did not receive the mapped Position model for pos.id"


def test_exit_intent_rejects_foreign_position(client, db_session):
    session_id, caller_uid = create_test_identity(db_session, "tok-exit-intent-foreign")
    _account(db_session, caller_uid)

    # Create a foreign user + position without overwriting the caller's identity
    # stored on the test session.
    foreign_uid = str(uuid.uuid4())
    from app.identity import User

    db_session.add(User(id=foreign_uid, status="active", identity_source="email",
                        broker_provider=None))
    db_session.flush()
    foreign_pos = _position(db_session, foreign_uid)
    db_session.commit()

    assert db_session._test_user_id == caller_uid

    mock_resolve = AsyncMock(return_value={})
    mock_router = MagicMock()
    mock_router.execute_intent = AsyncMock()

    with patch("app.routers.paper.resolve_bulk_market_prices", new=mock_resolve), \
         patch("app.services.execution_intent.ExecutionRouter", return_value=mock_router):
        resp = client.post(
            "/paper/exit-intent",
            headers={"X-Session-Id": session_id},
            json={
                "client_order_id": "ei-foreign-001",
                "scope": "POSITION",
                "position_id": foreign_pos.id,
                "option_type": "CALL",
                "action": "BUY",
                "quantity_mode": "ALL",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert data["targets_resolved"] == 0
    errors = data.get("errors") or []
    assert any("TARGET_NOT_FOUND" in e for e in errors), errors

    # Rejection must occur before market-price resolution or execution.
    mock_resolve.assert_not_awaited()
    mock_router.execute_intent.assert_not_awaited()


def test_exit_intent_requires_authentication(client, db_session):
    resp = client.post("/paper/exit-intent", json={
        "client_order_id": "ei-unauth-001",
        "scope": "PORTFOLIO",
        "quantity_mode": "ALL",
    })
    assert resp.status_code == 401
