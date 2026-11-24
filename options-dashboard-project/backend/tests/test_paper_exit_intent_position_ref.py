"""Regression test for the paper exit-intent Position reference defect.

POST /paper/exit-intent previously referenced an undefined ``Position`` name
inside the market-price resolution loop, raising ``NameError`` and returning
HTTP 500. This test fails against the original implementation and passes once
the mapped SQLAlchemy ``Position`` model is resolved at that point.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Position
from app.services import token_store
from app.services.execution_intent import ExecutionResult, ExecutionStatus, ExecutionMode
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
    from app.models import StrategyLegExposure, StrategyExecution
    # ensure a strategy execution exists for attribution
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

    # Spy on db.get to prove the mapped Position model and expected id are used.
    original_get = db_session.get
    get_calls = []

    def spy(model, ident, *a, **k):
        get_calls.append((model, ident))
        return original_get(model, ident, *a, **k)

    db_session.get = spy

    mock_router = MagicMock()
    mock_router.execute_intent = AsyncMock(return_value=ExecutionResult(
        intent_id="intent-test", status=ExecutionStatus.SUCCESS,
        mode=ExecutionMode.PAPER, targets_succeeded=1, results=[],
    ))

    prices = {("NIFTY", EXPIRY, 25000.0, "call"): 155.0}

    with patch("app.routers.paper.resolve_bulk_market_prices", new=AsyncMock(return_value=prices)), \
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

    # The endpoint's market-price loop must have looked up the real Position model
    # with the expected position identifier.
    matching = [c for c in get_calls if c[1] == pos.id]
    assert matching, "db.get was not called with the expected position id"
    assert any(c[0] is Position for c in matching), \
        "db.get was not called with the mapped Position model"


def test_exit_intent_requires_authentication(client, db_session):
    resp = client.post("/paper/exit-intent", json={
        "client_order_id": "ei-unauth-001",
        "scope": "PORTFOLIO",
        "quantity_mode": "ALL",
    })
    assert resp.status_code == 401
