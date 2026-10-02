"""Phase 6.9 HTTP integration tests — Dynamic Template Execution Bridge.

Tests the actual /paper/templates/:id/execute/preview and
/paper/templates/:id/execute endpoints with mocked broker responses
and a real in-memory SQLite database.

Covers:
- Normal V2 preview → execute → persisted records
- Preview unchanged → execution unchanged
- Preview unchanged → one-step strike change → executes
- Preview unchanged → >1-step strike change → HTTP 409, zero DB writes
- Expiry change → HTTP 409, zero DB writes
- Confirmed fresh strike → executes using fresh server value
- Multi-leg partial material change → entire strategy blocked
- Blocked attempt → subsequent valid attempt succeeds
- User/template isolation
- Stale quote → blocked with zero DB writes
- Unavailable contract/chain → blocked with zero DB writes
- Verification that persisted data comes from server resolution, not client
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import PaperOrder, Position, StrategyExecution, StrategyTemplate
from app.services import token_store

# Day 34: seed every bare entry intent through the genuine Day-28→Day-33
# chain into the real paper execution engine (no candidate fabrication).
from tests.day34_seeding import day34_gated_seeding  # noqa: F401  (autouse fixture)


LOT = 65
EXPIRY = "2026-08-27"
EXPIRY_OTHER = "2026-09-24"


@pytest.fixture(autouse=True)
def authoritative_lot_sizes():
    """Day 49: template execution resolves broker-authoritative lot sizes.

    ``execute_template`` now loads contract metadata through
    ``resolve_authoritative_lot_sizes`` before resolving chain prices. These
    tests stub ``resolve_market_prices`` but must not reach the real broker for
    the contract lookup either, so the lot sizes are stubbed the same way the
    paper-execution suite stubs contract metadata.
    """

    async def fake_lot_sizes(access_token, symbol, legs):
        return {
            (
                str(leg.expiration_date),
                float(leg.strike_price),
                str(leg.option_type).lower(),
            ): LOT
            for leg in legs
        }

    with patch(
        "app.services.paper_contracts.resolve_authoritative_lot_sizes",
        new=AsyncMock(side_effect=fake_lot_sizes),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    session_id, _ = create_test_identity(db_session, "tok-exec-integration")
    return session_id


def headers(session_id):
    return {"X-Session-Id": session_id}


# ---------------------------------------------------------------------------
# Mock result helpers
# ---------------------------------------------------------------------------

class _MockLeg:
    """Mimics a ResolvedLegOutput with all attributes used by the endpoint."""

    def __init__(
        self,
        position=0,
        action="buy",
        option_type="call",
        quantity=1,
        lot_size=LOT,
        resolved_strike=25000.0,
        resolved_expiry=EXPIRY,
        strike_mode_used="atm",
        expiry_mode_used="current_week",
        current_price=100.0,
        price_status="available",
        quote_timestamp="2026-08-20T10:00:00+05:30",
    ):
        self.position = position
        self.action = action
        self.option_type = option_type
        self.quantity = quantity
        self.lot_size = lot_size
        self.resolved_strike = resolved_strike
        self.resolved_expiry = resolved_expiry
        self.strike_mode_used = strike_mode_used
        self.expiry_mode_used = expiry_mode_used
        self.current_price = current_price
        self.price_status = price_status
        self.quote_timestamp = quote_timestamp
        self.ltp = current_price
        self.warnings = []
        self.symbol = "NIFTY"
        self.expiration_date = resolved_expiry
        self.strike_price = resolved_strike


class _MockResult:
    """Mimics a ResolutionResult with all attributes used by endpoint + validation."""

    def __init__(
        self,
        status="RESOLVED",
        legs=None,
        errors=None,
        warnings=None,
        chain_strike_step=50.0,
        template_id=None,
        template_name=None,
    ):
        self.status = status
        self.symbol = "NIFTY"
        self.legs = legs if legs is not None else [_MockLeg()]
        self.errors = errors or []
        self.warnings = warnings or []
        self.template_id = template_id
        self.template_name = template_name
        self.chain_strike_step = chain_strike_step


def _resolve_unchanged(template_id=None, template_name=None):
    """Resolution matches the template's stored strike/expiry (25000 / EXPIRY)."""
    return _MockResult(
        legs=[_MockLeg(resolved_strike=25000.0, resolved_expiry=EXPIRY)],
        template_id=template_id,
        template_name=template_name,
    )


def _resolve_strike_changed(
    template_id=None, template_name=None, new_strike=25050.0
):
    """Resolution has a different strike."""
    return _MockResult(
        legs=[_MockLeg(resolved_strike=new_strike, resolved_expiry=EXPIRY)],
        template_id=template_id,
        template_name=template_name,
    )


def _resolve_expiry_changed(template_id=None, template_name=None):
    """Resolution has a different expiry."""
    return _MockResult(
        legs=[_MockLeg(resolved_strike=25000.0, resolved_expiry=EXPIRY_OTHER)],
        template_id=template_id,
        template_name=template_name,
    )


def _resolve_stale(template_id=None, template_name=None):
    """Resolution has a stale quote."""
    return _MockResult(
        legs=[_MockLeg(resolved_strike=25000.0, resolved_expiry=EXPIRY,
                       price_status="stale")],
        template_id=template_id,
        template_name=template_name,
    )


def _resolve_unavailable(template_id=None, template_name=None):
    """Resolution has an unavailable price."""
    return _MockResult(
        legs=[_MockLeg(resolved_strike=25000.0, resolved_expiry=EXPIRY,
                       price_status="unavailable")],
        template_id=template_id,
        template_name=template_name,
    )


def _resolve_failed(template_id=None, template_name=None):
    """Resolution failed."""
    return _MockResult(
        status="FAILED",
        legs=[],
        errors=["No chain data available"],
        template_id=template_id,
        template_name=template_name,
    )


# ---------------------------------------------------------------------------
# Template creation helper
# ---------------------------------------------------------------------------

def _create_template(client, session_id, name="ATM Call", legs=None):
    """Create a V2 dynamic template via the API and return its ID."""
    if legs is None:
        legs = [{
            "action": "buy", "option_type": "call",
            "strike": 25000.0, "expiry": EXPIRY,
            "quantity": 1, "lot_size": LOT,
            "strike_mode": "atm", "expiry_mode": "current_week",
            "formula_version": 2,
        }]
    resp = client.post(
        "/paper/templates",
        headers=headers(session_id),
        json={"name": name, "symbol": "NIFTY", "legs": legs},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _db_counts(db_session):
    """Return current DB record counts."""
    return {
        "executions": db_session.query(StrategyExecution).count(),
        "orders": db_session.query(PaperOrder).count(),
        "positions": db_session.query(Position).count(),
    }


# ---------------------------------------------------------------------------
# Helper: async no-op for require_market_open
# ---------------------------------------------------------------------------

async def _noop_market_open(access_token):
    pass


# ===========================================================================
# Tests
# ===========================================================================


class TestPreviewEndpoint:
    """POST /paper/templates/:id/execute/preview"""

    def test_preview_unchanged(self, client, logged_in, db_session):
        tid = _create_template(client, logged_in)
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock:
            mock.return_value = _resolve_unchanged(tid, "ATM Call")
            resp = client.post(
                f"/paper/templates/{tid}/execute/preview",
                headers=headers(logged_in),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "UNCHANGED"
        assert body["template_id"] == tid
        assert len(body["legs"]) == 1
        assert body["legs"][0]["resolved_strike"] == 25000.0
        assert _db_counts(db_session) == before  # no DB writes

    def test_preview_changed(self, client, logged_in, db_session):
        tid = _create_template(client, logged_in)
        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock:
            mock.return_value = _resolve_strike_changed(tid, "ATM Call", 25100.0)
            resp = client.post(
                f"/paper/templates/{tid}/execute/preview",
                headers=headers(logged_in),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "CHANGED_STRIKE"
        assert len(body["changes"]) == 1
        assert body["changes"][0]["field"] == "strike"
        assert body["changes"][0]["fresh_value"] == 25100.0

    def test_preview_requires_auth(self, client):
        resp = client.post("/paper/templates/1/execute/preview")
        assert resp.status_code == 401

    def test_preview_nonexistent_template(self, client, logged_in):
        resp = client.post(
            "/paper/templates/99999/execute/preview",
            headers=headers(logged_in),
        )
        assert resp.status_code == 404


class TestExecuteEndpoint:
    """POST /paper/templates/:id/execute"""

    def test_execute_unchanged(self, client, logged_in, db_session):
        """Normal V2 execution: unchanged resolution → persisted records."""
        tid = _create_template(client, logged_in, name="Unchanged Strat")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unchanged(tid, "Unchanged Strat")
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-unchanged-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "FILLED"
        assert body["symbol"] == "NIFTY"

        # Verify DB records created
        after = _db_counts(db_session)
        assert after["executions"] == before["executions"] + 1
        assert after["orders"] == before["orders"] + 1
        assert after["positions"] == before["positions"] + 1

        # Verify persisted data comes from server resolution
        order = db_session.query(PaperOrder).order_by(PaperOrder.id.desc()).first()
        assert order.strike == 25000.0
        assert order.expiry == EXPIRY
        assert order.fill_price == 100.0
        assert order.action == "buy"
        assert order.option_type == "call"

    def test_execute_one_step_strike_change_auto_executes(self, client, logged_in, db_session):
        """Preview=25000, execute=25050 (1 step) → auto-execute."""
        tid = _create_template(client, logged_in, name="One Step")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_strike_changed(tid, "One Step", 25050.0)
            mock_prices.return_value = {(EXPIRY, 25050.0, "call"): 110.0}

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-one-step-001",
                    "starting_capital": 500000,
                    # Confirmation from preview (25000), fresh is 25050 (1 step diff)
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "FILLED"

        # Verify executed at the FRESH strike (25050), not the confirmed (25000)
        order = db_session.query(PaperOrder).order_by(PaperOrder.id.desc()).first()
        assert order.strike == 25050.0, f"Expected fresh strike 25050, got {order.strike}"
        assert order.fill_price == 110.0

    def test_execute_two_step_strike_change_blocks(self, client, logged_in, db_session):
        """Preview=25000, execute=25100 (2 steps) → HTTP 409, zero DB writes."""
        tid = _create_template(client, logged_in, name="Two Steps")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_strike_changed(tid, "Two Steps", 25100.0)

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-two-steps-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        assert _db_counts(db_session) == before  # zero DB writes

    def test_execute_expiry_change_blocks(self, client, logged_in, db_session):
        """Expiry changed → HTTP 409, zero DB writes."""
        tid = _create_template(client, logged_in, name="Expiry Change")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_expiry_changed(tid, "Expiry Change")

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-expiry-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        assert _db_counts(db_session) == before

    def test_execute_confirmed_fresh_strike(self, client, logged_in, db_session):
        """User confirmed the fresh strike (25050) → executes at 25050."""
        tid = _create_template(client, logged_in, name="Confirmed Fresh")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_strike_changed(tid, "Confirmed Fresh", 25050.0)
            mock_prices.return_value = {(EXPIRY, 25050.0, "call"): 110.0}

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-confirmed-001",
                    "starting_capital": 500000,
                    # User confirmed the fresh value, not the preview value
                    "confirmed_strikes": {0: 25050.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 200
        order = db_session.query(PaperOrder).order_by(PaperOrder.id.desc()).first()
        assert order.strike == 25050.0

    def test_execute_multi_leg_partial_material_change_blocks(self, client, logged_in, db_session):
        """One leg material change → entire strategy blocked."""
        tid = _create_template(client, logged_in, name="Multi Leg", legs=[
            {"action": "buy", "option_type": "call", "strike": 25000.0,
             "expiry": EXPIRY, "quantity": 1, "lot_size": LOT,
             "strike_mode": "atm", "expiry_mode": "fixed", "formula_version": 2},
            {"action": "sell", "option_type": "call", "strike": 25200.0,
             "expiry": EXPIRY, "quantity": 1, "lot_size": LOT,
             "strike_mode": "atm_offset_steps", "strike_offset": 2,
             "expiry_mode": "fixed", "formula_version": 2},
        ])
        before = _db_counts(db_session)

        # Leg 0: unchanged (25000), Leg 1: material change (25200 → 25400 = 4 steps)
        multi_legs = [
            _MockLeg(position=0, resolved_strike=25000.0, resolved_expiry=EXPIRY),
            _MockLeg(position=1, action="sell", resolved_strike=25400.0,
                     resolved_expiry=EXPIRY),
        ]

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _MockResult(
                legs=multi_legs, template_id=tid, template_name="Multi Leg",
            )

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-multi-001",
                    "starting_capital": 500000,
                    # Only leg 0 confirmed (25000 matches fresh), leg 1 not confirmed
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY, 1: EXPIRY},
                },
            )

        assert resp.status_code == 409
        assert _db_counts(db_session) == before  # zero partial writes

    def test_execute_blocked_then_valid_succeeds(self, client, logged_in, db_session):
        """Blocked attempt doesn't consume idempotency; subsequent valid attempt succeeds."""
        tid = _create_template(client, logged_in, name="Block Then Pass")
        before = _db_counts(db_session)

        # Attempt 1: blocked (material strike change)
        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_strike_changed(tid, "Block Then Pass", 25100.0)

            resp1 = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-block-then-pass-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )
        assert resp1.status_code == 409
        assert _db_counts(db_session) == before  # no DB writes

        # Attempt 2: same client_order_id, resolution now matches
        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unchanged(tid, "Block Then Pass")
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}

            resp2 = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-block-then-pass-001",  # same key
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "FILLED"
        after = _db_counts(db_session)
        assert after["executions"] == before["executions"] + 1

    def test_execute_user_isolation(self, client, logged_in, db_session):
        """Cannot execute another user's template."""
        tid = _create_template(client, logged_in, name="My Template")

        # Switch to user B
        from tests.test_helpers import create_test_identity
        other_sid, _ = create_test_identity(db_session, "tok-exec-other-user")
        with patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(other_sid),
                json={
                    "client_order_id": "exec-test-isolation-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )
        assert resp.status_code == 404

    def test_execute_stale_quote_blocks(self, client, logged_in, db_session):
        """Stale quote → HTTP 409, zero DB writes."""
        tid = _create_template(client, logged_in, name="Stale Quote")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_stale(tid, "Stale Quote")

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-stale-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        assert "stale" in resp.json()["detail"].lower()
        assert _db_counts(db_session) == before

    def test_execute_unavailable_price_blocks(self, client, logged_in, db_session):
        """Unavailable price → HTTP 409, zero DB writes."""
        tid = _create_template(client, logged_in, name="Unavailable")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unavailable(tid, "Unavailable")

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-unavail-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        assert _db_counts(db_session) == before

    def test_execute_resolution_failed_blocks(self, client, logged_in, db_session):
        """Failed resolution → HTTP 409, zero DB writes."""
        tid = _create_template(client, logged_in, name="Failed Resolution")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_failed(tid, "Failed Resolution")

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-failed-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        assert _db_counts(db_session) == before

    def test_execute_requires_auth(self, client):
        resp = client.post(
            "/paper/templates/1/execute",
            json={"client_order_id": "exec-test-noauth-001"},
        )
        assert resp.status_code == 401

    def test_execute_nonexistent_template(self, client, logged_in):
        with patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            resp = client.post(
                "/paper/templates/99999/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-noexist-001",
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )
        assert resp.status_code == 404

    def test_execute_no_confirmation_blocks(self, client, logged_in, db_session):
        """Changes detected but no confirmation values → blocked."""
        tid = _create_template(client, logged_in, name="No Confirm")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_strike_changed(tid, "No Confirm", 25050.0)

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-noconfirm-001",
                    "starting_capital": 500000,
                    # No confirmed_strikes or confirmed_expiries
                },
            )

        assert resp.status_code == 409
        assert _db_counts(db_session) == before

    def test_execute_idempotency_replay(self, client, logged_in, db_session):
        """Same client_order_id replay returns the original execution
        with zero additional DB writes."""
        tid = _create_template(client, logged_in, name="Idempotent")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unchanged(tid, "Idempotent")
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}

            resp1 = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-idempotent-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )
            assert resp1.status_code == 200
            exec_id_1 = resp1.json()["execution_id"]
            after_first = _db_counts(db_session)

            resp2 = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-idempotent-001",  # same key
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )
            assert resp2.status_code == 200
            assert resp2.json()["execution_id"] == exec_id_1
            assert resp2.json().get("duplicated") is True
            # Zero additional DB writes on replay
            after_second = _db_counts(db_session)
            assert after_second["executions"] == after_first["executions"]
            assert after_second["orders"] == after_first["orders"]
            assert after_second["positions"] == after_first["positions"]

    def test_execute_persisted_data_matches_server_resolution(self, client, logged_in, db_session):
        """Proof: persisted PaperOrder.strike, .expiry, .fill_price come from server."""
        tid = _create_template(client, logged_in, name="Server Values")

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            # Server resolves to 25050 / EXPIRY / price 125.0
            mock_resolve.return_value = _resolve_strike_changed(tid, "Server Values", 25050.0)
            mock_prices.return_value = {(EXPIRY, 25050.0, "call"): 125.0}

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-server-vals-001",
                    "starting_capital": 500000,
                    # Client says 25000 but server says 25050 (1 step diff)
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 200

        order = db_session.query(PaperOrder).order_by(PaperOrder.id.desc()).first()
        # These MUST be the server's values, not the client's confirmed values
        assert order.strike == 25050.0, f"Order strike should be server's 25050, got {order.strike}"
        assert order.expiry == EXPIRY
        assert order.fill_price == 125.0, f"Fill price should be server's 125.0, got {order.fill_price}"
        assert order.action == "buy"
        assert order.option_type == "call"
        assert order.quantity == 1
        assert order.lot_size == LOT


class TestExecuteEndpointPaperExecutionError:
    """Verify PaperExecutionError from resolve_market_prices and execute_strategy
    produces structured HTTP errors matching V1 behavior, with zero DB writes."""

    def test_resolve_market_prices_chain_data_missing(self, client, logged_in, db_session):
        """resolve_market_prices raises CHAIN_DATA_MISSING → 409 + zero DB writes."""
        from app.services.paper_execution import PaperExecutionError

        tid = _create_template(client, logged_in, name="Price Fail")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unchanged(tid, "Price Fail")
            mock_prices.side_effect = PaperExecutionError(
                "CHAIN_DATA_MISSING",
                "Leg CALL 25000: market data unavailable for expiry 2026-08-27.",
            )

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-price-fail-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        body = resp.json()
        assert "CHAIN_DATA_MISSING" in body["detail"]
        assert _db_counts(db_session) == before  # zero DB writes

    def test_execute_strategy_chain_data_missing(self, client, logged_in, db_session):
        """execute_strategy raises CHAIN_DATA_MISSING → 409 + zero DB writes.

        This tests the case where resolve_market_prices succeeds but
        execute_strategy's internal price validation fails.
        """
        from app.services.paper_execution import PaperExecutionError

        tid = _create_template(client, logged_in, name="Strategy Fail")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.services.paper_execution.execute_strategy") as mock_exec, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unchanged(tid, "Strategy Fail")
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}
            mock_exec.side_effect = PaperExecutionError(
                "CHAIN_DATA_MISSING",
                "Leg CALL 25000: market data unavailable for expiry 2026-08-27. Paper order was not executed.",
            )

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-strat-fail-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        body = resp.json()
        assert "CHAIN_DATA_MISSING" in body["detail"]
        assert _db_counts(db_session) == before  # zero DB writes

    def test_execute_strategy_execution_failed(self, client, logged_in, db_session):
        """execute_strategy raises EXECUTION_FAILED → 502 + zero DB writes."""
        from app.services.paper_execution import PaperExecutionError

        tid = _create_template(client, logged_in, name="Broker Fail")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.services.paper_execution.execute_strategy") as mock_exec, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unchanged(tid, "Broker Fail")
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}
            mock_exec.side_effect = PaperExecutionError(
                "EXECUTION_FAILED",
                "Could not load market data for NIFTY: Connection refused",
            )

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-broker-fail-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 502  # EXECUTION_FAILED → 502
        body = resp.json()
        assert "EXECUTION_FAILED" in body["detail"]
        assert _db_counts(db_session) == before  # zero DB writes

    def test_zero_partial_db_writes_on_price_failure(self, client, logged_in, db_session):
        """After resolve_market_prices failure, zero StrategyExecution/PaperOrder/Position records."""
        from app.services.paper_execution import PaperExecutionError

        tid = _create_template(client, logged_in, name="Partial Check")
        before = _db_counts(db_session)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _resolve_unchanged(tid, "Partial Check")
            mock_prices.side_effect = PaperExecutionError(
                "CHAIN_DATA_MISSING",
                "Leg CALL 25000: market data unavailable.",
            )

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-test-partial-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        after = _db_counts(db_session)
        assert after["executions"] == before["executions"]
        assert after["orders"] == before["orders"]
        assert after["positions"] == before["positions"]
