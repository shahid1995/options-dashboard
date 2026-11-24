"""Supplemental test: foreign-user rejection causes no downstream resolver or execution calls.

POST /paper/exit-intent as an authenticated caller who owns no relevant position
should REJECT the request with TARGET_NOT_FOUND and never trigger market-price
resolution or execution router construction.
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
    session_id, _ = create_test_identity(db_session, "tok-exit-intent-rejection-no-side-effects")
    return session_id


def _account(db, user_id):
    from app.models import PaperAccount

    acc = PaperAccount(user_id=user_id, starting_capital=500000)
    db.add(acc)
    db.flush()
    return acc


def _position(db, user_id, pos_id=None):
    pid = pos_id or str(uuid.uuid4())
    pos = Position(
        user_id=user_id, symbol="NIFTY", expiry=EXPIRY, strike=25000.0,
        option_type="call", net_quantity=2, lot_size=LOT,
        average_entry_price=150.0, realized_pnl=0.0, status="open",
    )
    # Use a deterministic id if provided
    if pos_id is not None:
        pos.id = int(pos_id) if isinstance(pos_id, (int, str)) and str(pos_id).isdigit() else uuid.UUID(pos_id).int % 1000000
    db.add(pos)
    db.flush()
    return pos


@pytest.fixture()
def foreign_position_data(client, db_session):
    """Set up a foreign user + position; returns (caller_uid, foreign_uid, foreign_pos, session_id)."""
    session_id, caller_uid = create_test_identity(db_session, "tok-rejection-foreign-setup")
    _account(db_session, caller_uid)

    foreign_uid = str(uuid.uuid4())
    from app.identity import User

    db_session.add(User(id=foreign_uid, status="active", identity_source="email",
                        broker_provider=None))
    db_session.flush()
    foreign_pos = _position(db_session, foreign_uid)
    db_session.commit()

    # Ensure caller's identity is preserved on the session
    assert db_session._test_user_id == caller_uid

    return caller_uid, foreign_uid, foreign_pos, session_id


@pytest.fixture()
def mock_market_prices():
    """Return a dict mapping (symbol, expiry, strike, option_type) -> price."""
    return {("NIFTY", EXPIRY, 25000.0, "call"): 155.0}


@pytest.mark.asyncio
async def test_foreign_user_rejection_no_side_effects(
    client, db_session, logged_in, foreign_position_data, mock_market_prices
):
    """Foreign-user exit-intent must REJECT with no downstream calls.

    Verifies:
      - HTTP 200 + status=="REJECTED" + targets_resolved==0 + TARGET_NOT_FOUND error
      - resolve_bulk_market_prices not called (not even as a coroutine)
      - ExecutionRouter factory not invoked
      - execute_intent not called / not awaited
      - Caller identity unchanged after the request
    """
    caller_uid, foreign_uid, foreign_pos, session_id = foreign_position_data
    prices = mock_market_prices

    # Mock resolve_bulk_market_prices to verify it is never called
    mock_resolve = AsyncMock(return_value=prices)

    # Mock ExecutionRouter so we can verify its factory is not called
    mock_router_factory = MagicMock()
    mock_router_instance = MagicMock()
    mock_router_instance.execute_intent = AsyncMock()
    mock_router_factory.return_value = mock_router_instance

    with patch("app.routers.paper.resolve_bulk_market_prices", new=mock_resolve), \
         patch("app.services.execution_intent.ExecutionRouter", mock_router_factory):
        resp = client.post(
            "/paper/exit-intent",
            headers={"X-Session-Id": session_id},
            json={
                "client_order_id": "ei-rejection-001",
                "scope": "POSITION",
                "position_id": foreign_pos.id,
                "option_type": "CALL",
                "action": "BUY",
                "quantity_mode": "ALL",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "REJECTED", f"Expected REJECTED, got {data.get('status')}"
    assert data["targets_resolved"] == 0, f"Expected targets_resolved==0, got {data.get('targets_resolved')}"

    errors = data.get("errors") or []
    assert any("TARGET_NOT_FOUND" in e for e in errors), f"Expected TARGET_NOT_FOUND in errors: {errors}"

    # Prove no market-price resolution occurred
    mock_resolve.assert_not_called()
    mock_resolve.assert_not_awaited()

    # Prove the execution router was never constructed (factory not called)
    mock_router_factory.assert_not_called()

    # Prove execution was never invoked
    mock_router_instance.execute_intent.assert_not_called()
    mock_router_instance.execute_intent.assert_not_awaited()

    # Verify the caller's identity is unchanged after the request
    assert db_session._test_user_id == caller_uid
