"""Phase 6.10: Execution audit trail tests.

Covers:
- _build_execution_metadata produces expected structure
- Metadata serialization/deserialization roundtrip
- _persist_execution_metadata writes to DB
- V2 execution persists metadata
- Metadata contains preview, confirmed, execution resolution
- Metadata contains formula information
- Execution failure does not create metadata
- V1 execution returns null metadata
- Metadata persistence failure does not convert success to failure
- Actual PaperOrder values match metadata
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import PaperOrder, Position, StrategyExecution
from app.routers.templates import (
    _build_execution_metadata,
    _load_execution_metadata,
    _persist_execution_metadata,
)
from app.services import token_store
from fastapi.testclient import TestClient


LOT = 65
EXPIRY = "2026-08-27"


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
    session_id, _ = create_test_identity(db_session, "tok-metadata-test")
    return session_id


def headers(session_id):
    return {"X-Session-Id": session_id}


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class _MockLeg:
    def __init__(self, **kwargs):
        self.position = kwargs.get("position", 0)
        self.action = kwargs.get("action", "buy")
        self.option_type = kwargs.get("option_type", "call")
        self.quantity = kwargs.get("quantity", 1)
        self.lot_size = kwargs.get("lot_size", LOT)
        self.resolved_strike = kwargs.get("resolved_strike", 25000.0)
        self.resolved_expiry = kwargs.get("resolved_expiry", EXPIRY)
        self.strike_mode_used = kwargs.get("strike_mode_used", "atm")
        self.expiry_mode_used = kwargs.get("expiry_mode_used", "current_week")
        self.current_price = kwargs.get("current_price", 100.0)
        self.price_status = kwargs.get("price_status", "available")
        self.quote_timestamp = kwargs.get("quote_timestamp", "2026-08-21T10:00:00+05:30")
        self.ltp = self.current_price
        self.warnings = []
        self.symbol = "NIFTY"
        self.expiration_date = self.resolved_expiry
        self.strike_price = self.resolved_strike


class _MockResult:
    def __init__(self, **kwargs):
        self.status = kwargs.get("status", "RESOLVED")
        self.symbol = "NIFTY"
        self.legs = kwargs.get("legs", [_MockLeg()])
        self.errors = kwargs.get("errors", [])
        self.warnings = kwargs.get("warnings", [])
        self.template_id = kwargs.get("template_id", None)
        self.template_name = kwargs.get("template_name", None)
        self.chain_strike_step = kwargs.get("chain_strike_step", 50.0)


class _MockTemplate:
    """Minimal template mock for _build_execution_metadata."""
    def __init__(self):
        self.legs = [_MockTemplateLeg()]


class _MockTemplateLeg:
    def __init__(self):
        self.formula_version = 2
        self.strike_mode = "atm"
        self.expiry_mode = "current_week"
        self.strike_offset = None
        self.target_delta = None
        self.expiry_dte_min = None
        self.expiry_dte_max = None


class _MockExecResult:
    def __init__(self):
        self.execution_id = "test-exec-001"
        self.status = "FILLED"


def _create_template(client, session_id, name="ATM Call"):
    resp = client.post(
        "/paper/templates",
        headers=headers(session_id),
        json={
            "name": name, "symbol": "NIFTY",
            "legs": [{
                "action": "buy", "option_type": "call",
                "strike": 25000.0, "expiry": EXPIRY,
                "quantity": 1, "lot_size": LOT,
                "strike_mode": "atm", "expiry_mode": "current_week",
                "formula_version": 2,
            }],
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Tests: _build_execution_metadata
# ---------------------------------------------------------------------------

class TestBuildExecutionMetadata:
    def test_produces_expected_structure(self):
        """Metadata has all required top-level keys."""
        template = _MockTemplate()
        preview = _MockResult()
        exec_legs = [{"expiration_date": EXPIRY, "strike_price": 25000.0, "option_type": "call"}]
        prices = {(EXPIRY, 25000.0, "call"): 100.0}

        metadata = _build_execution_metadata(
            template=template,
            preview_result=preview,
            comparison_changes=[],
            confirmed_strikes={0: 25000.0},
            confirmed_expiries={0: EXPIRY},
            exec_result=_MockExecResult(),
            exec_legs=exec_legs,
            prices=prices,
        )

        assert "formula_version" in metadata
        assert "formula" in metadata
        assert "preview_resolution" in metadata
        assert "confirmed_values" in metadata
        assert "execution_resolution" in metadata
        assert "broker_data" in metadata

    def test_formula_version_and_fields(self):
        template = _MockTemplate()
        metadata = _build_execution_metadata(
            template=template,
            preview_result=_MockResult(),
            comparison_changes=[],
            confirmed_strikes=None,
            confirmed_expiries=None,
            exec_result=_MockExecResult(),
            exec_legs=[],
            prices={},
        )
        assert metadata["formula_version"] == 2
        assert metadata["formula"]["strike_mode"] == "atm"
        assert metadata["formula"]["expiry_mode"] == "current_week"

    def test_preview_resolution_captures_legs(self):
        preview = _MockResult(legs=[_MockLeg(resolved_strike=25050.0)])
        metadata = _build_execution_metadata(
            template=_MockTemplate(),
            preview_result=preview,
            comparison_changes=[],
            confirmed_strikes=None,
            confirmed_expiries=None,
            exec_result=_MockExecResult(),
            exec_legs=[],
            prices={},
        )
        assert metadata["preview_resolution"]["status"] == "RESOLVED"
        assert len(metadata["preview_resolution"]["legs"]) == 1
        assert metadata["preview_resolution"]["legs"][0]["resolved_strike"] == 25050.0
        assert metadata["preview_resolution"]["chain_strike_step"] == 50.0

    def test_confirmed_values_serialized(self):
        metadata = _build_execution_metadata(
            template=_MockTemplate(),
            preview_result=_MockResult(),
            comparison_changes=[],
            confirmed_strikes={0: 25000.0},
            confirmed_expiries={0: EXPIRY},
            exec_result=_MockExecResult(),
            exec_legs=[],
            prices={},
        )
        # Keys are stringified for JSON compatibility
        assert metadata["confirmed_values"]["confirmed_strikes"] == {"0": 25000.0}
        assert metadata["confirmed_values"]["confirmed_expiries"] == {"0": EXPIRY}

    def test_execution_resolution_captures_fill_prices(self):
        exec_legs = [{"expiration_date": EXPIRY, "strike_price": 25050.0, "option_type": "call"}]
        prices = {(EXPIRY, 25050.0, "call"): 125.0}
        metadata = _build_execution_metadata(
            template=_MockTemplate(),
            preview_result=_MockResult(),
            comparison_changes=[],
            confirmed_strikes=None,
            confirmed_expiries=None,
            exec_result=_MockExecResult(),
            exec_legs=exec_legs,
            prices=prices,
        )
        exec_leg = metadata["execution_resolution"]["legs"][0]
        assert exec_leg["resolved_strike"] == 25050.0
        assert exec_leg["fill_price"] == 125.0
        assert exec_leg["price_source"] == "market"

    def test_changes_from_preview_stored(self):
        changes = [{"position": 0, "field": "strike", "preview_value": 25000, "fresh_value": 25050}]
        metadata = _build_execution_metadata(
            template=_MockTemplate(),
            preview_result=_MockResult(),
            comparison_changes=changes,
            confirmed_strikes=None,
            confirmed_expiries=None,
            exec_result=_MockExecResult(),
            exec_legs=[],
            prices={},
        )
        assert len(metadata["execution_resolution"]["changes_from_preview"]) == 1
        assert metadata["execution_resolution"]["changes_from_preview"][0]["field"] == "strike"

    def test_json_serialization_roundtrip(self):
        """Metadata can be serialized to JSON and back."""
        metadata = _build_execution_metadata(
            template=_MockTemplate(),
            preview_result=_MockResult(),
            comparison_changes=[],
            confirmed_strikes={0: 25000.0},
            confirmed_expiries={0: EXPIRY},
            exec_result=_MockExecResult(),
            exec_legs=[{"expiration_date": EXPIRY, "strike_price": 25000.0, "option_type": "call"}],
            prices={(EXPIRY, 25000.0, "call"): 100.0},
        )
        serialized = json.dumps(metadata)
        deserialized = json.loads(serialized)
        assert deserialized == metadata

    def test_broker_data_has_null_spot_price(self):
        """spot_price is null (not available at router level)."""
        metadata = _build_execution_metadata(
            template=_MockTemplate(),
            preview_result=_MockResult(),
            comparison_changes=[],
            confirmed_strikes=None,
            confirmed_expiries=None,
            exec_result=_MockExecResult(),
            exec_legs=[],
            prices={},
        )
        assert metadata["broker_data"]["spot_price"] is None
        assert "chain_fetched_at" in metadata["broker_data"]


# ---------------------------------------------------------------------------
# Tests: _persist_execution_metadata
# ---------------------------------------------------------------------------

class TestPersistExecutionMetadata:
    def test_persists_to_db(self, db_session):
        """Metadata is written to the execution record."""
        from app.models import StrategyExecution
        import secrets

        exec_id = secrets.token_hex(16)
        db_session.add(StrategyExecution(
            user_id="user-1", execution_id=exec_id,
            client_order_id="test-coid", symbol="NIFTY",
            status="FILLED",
        ))
        db_session.commit()

        metadata = {"formula_version": 2, "test": True}
        _persist_execution_metadata(db_session, exec_id, metadata)

        refreshed = db_session.get(StrategyExecution, db_session.query(StrategyExecution).filter_by(execution_id=exec_id).first().id)
        assert refreshed.execution_metadata is not None
        parsed = json.loads(refreshed.execution_metadata)
        assert parsed["formula_version"] == 2
        assert parsed["test"] is True

    def test_handles_nonexistent_execution(self, db_session):
        """Persisting to a nonexistent execution does not raise."""
        # Should not raise — the update affects 0 rows
        _persist_execution_metadata(db_session, "nonexistent-id", {"test": True})
        # The commit may succeed (0 rows updated) or fail gracefully
        # Either way, no exception should propagate


# ---------------------------------------------------------------------------
# Tests: hostile input is bound, never interpolated (Issue: PR #121 finding)
# ---------------------------------------------------------------------------

#: Classic SQL-injection payloads. None of these may influence SQL structure.
HOSTILE_EXECUTION_IDS = [
    "x' OR '1'='1",
    "'; DROP TABLE strategy_executions; --",
    "1) OR 1=1 --",
    "abc'--",
    "abc\nUNION SELECT 1",
    "abc/*comment*/",
    "%27%20OR%201%3D1",
]

#: Hostile metadata value; JSON encoding must neutralise it before storage.
HOSTILE_METADATA = {
    "note": "'; DROP TABLE strategy_executions; --",
    "expr": "1 OR 1=1",
}


def _execution_row(db_session, execution_id: str, coid: str):
    from app.models import StrategyExecution

    db_session.add(StrategyExecution(
        user_id="user-1", execution_id=execution_id,
        client_order_id=coid, symbol="NIFTY", status="FILLED",
    ))
    db_session.commit()


class TestExecutionMetadataIsParameterBound:
    """The UPDATE must bind every value — never build SQL from input.

    These tests assert observable behaviour (which rows changed, and how the
    value round-trips), not the presence of a suppression comment.
    """

    def test_statement_compiles_to_bound_parameters(self):
        """SQL structure is fixed; hostile values appear only as parameters."""
        from sqlalchemy import update
        from sqlalchemy.dialects import sqlite

        from app.models import StrategyExecution

        for hostile in HOSTILE_EXECUTION_IDS:
            stmt = (
                update(StrategyExecution)
                .where(StrategyExecution.execution_id == hostile)
                .values(execution_metadata=json.dumps(HOSTILE_METADATA))
            )
            compiled = stmt.compile(dialect=sqlite.dialect())
            sql = str(compiled)
            # Structure is invariant across every payload...
            assert sql == (
                "UPDATE strategy_executions SET updated_at=?, "
                "execution_metadata=? WHERE strategy_executions.execution_id = ?"
            ), sql
            # ...and the hostile string is a parameter value, never SQL text.
            assert hostile not in sql
            assert compiled.params["execution_id_1"] == hostile
            assert "OR '1'='1'" not in sql
            assert ";" not in sql

    @pytest.mark.parametrize("hostile", HOSTILE_EXECUTION_IDS)
    def test_hostile_execution_id_updates_no_rows(self, db_session, hostile):
        """A hostile execution_id must not match, widen, or create anything."""
        from app.models import StrategyExecution

        target = "target-exec-0001"
        decoy_a = "decoy-exec-a"
        decoy_b = "decoy-exec-b"
        for eid, coid in ((target, "c1"), (decoy_a, "c2"), (decoy_b, "c3")):
            _execution_row(db_session, eid, coid)

        _persist_execution_metadata(db_session, hostile, HOSTILE_METADATA)
        db_session.expire_all()

        rows = db_session.query(StrategyExecution).all()
        assert len(rows) == 3  # nothing created
        assert all(r.execution_metadata is None for r in rows)  # nothing updated
        assert {r.execution_id for r in rows} == {target, decoy_a, decoy_b}

    def test_hostile_metadata_is_stored_opaquely_and_only_touches_its_row(
        self, db_session
    ):
        """Hostile metadata round-trips as data and reaches exactly one row."""
        from app.models import StrategyExecution

        target = "target-exec-0002"
        decoy = "decoy-exec-c"
        _execution_row(db_session, target, "c4")
        _execution_row(db_session, decoy, "c5")

        _persist_execution_metadata(db_session, target, HOSTILE_METADATA)
        db_session.expire_all()

        rows = {r.execution_id: r.execution_metadata
                for r in db_session.query(StrategyExecution).all()}
        # Unrelated rows are untouched.
        assert rows[decoy] is None
        # The hostile payload is stored verbatim and decodes back exactly.
        assert rows[target] is not None
        assert json.loads(rows[target]) == HOSTILE_METADATA
        assert db_session.query(StrategyExecution).count() == 2

    def test_intended_row_update_is_preserved(self, db_session):
        """The legitimate write path is unchanged by the hardening."""
        from app.models import StrategyExecution

        target = "target-exec-0003"
        _execution_row(db_session, target, "c6")
        _persist_execution_metadata(db_session, target, {"formula_version": 2})

        db_session.expire_all()
        row = db_session.query(StrategyExecution).filter_by(
            execution_id=target).first()
        assert json.loads(row.execution_metadata) == {"formula_version": 2}

    def test_source_contains_no_raw_sql_interpolation(self):
        """Guard against reintroducing f-string/text() SQL in the helper."""
        import inspect

        from app.routers import templates

        source = inspect.getsource(templates._persist_execution_metadata)
        assert "text(" not in source
        assert "raw_connection" not in source
        assert ".execute(f\"" not in source
        # The statement is the SQLAlchemy expression API, not a SQL literal.
        assert "update(StrategyExecution)" in source
        assert "db.execute(" in source


# ---------------------------------------------------------------------------
# Tests: V2 integration with metadata
# ---------------------------------------------------------------------------

class TestV2ExecutionMetadata:
    def _create_template(self, client, session_id):
        return _create_template(client, session_id)

    def test_execute_unchanged_persists_metadata(self, client, logged_in, db_session):
        """Successful V2 execution persists metadata."""
        tid = self._create_template(client, logged_in)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _MockResult()
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-md-test-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["execution_metadata"] is not None
        meta = body["execution_metadata"]
        assert meta["formula_version"] == 2
        assert meta["formula"]["strike_mode"] == "atm"
        assert meta["preview_resolution"]["status"] == "RESOLVED"
        assert meta["confirmed_values"]["confirmed_strikes"] == {"0": 25000.0}
        assert len(meta["execution_resolution"]["legs"]) == 1
        assert meta["execution_resolution"]["legs"][0]["fill_price"] == 100.0

    def test_execute_persisted_in_db(self, client, logged_in, db_session):
        """Metadata is persisted in the DB, not just in the response."""
        tid = self._create_template(client, logged_in)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _MockResult()
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-md-test-002",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 200
        exec_id = resp.json()["execution_id"]

        # Verify metadata is in the DB
        execution = db_session.query(StrategyExecution).filter_by(execution_id=exec_id).first()
        assert execution is not None
        assert execution.execution_metadata is not None
        meta = json.loads(execution.execution_metadata)
        assert meta["formula_version"] == 2

    def test_blocked_execution_no_metadata(self, client, logged_in, db_session):
        """When validation fails, no metadata is persisted."""
        tid = self._create_template(client, logged_in)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            # 2-step strike change → blocked
            leg = _MockLeg(resolved_strike=25100.0)
            mock_resolve.return_value = _MockResult(legs=[leg])

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-md-test-003",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 409
        # No execution should exist
        assert db_session.query(StrategyExecution).count() == 0

    def test_paper_order_values_match_metadata(self, client, logged_in, db_session):
        """Actual PaperOrder strike/expiry/fill matches metadata execution_resolution."""
        tid = self._create_template(client, logged_in)

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            leg = _MockLeg(resolved_strike=25050.0)
            mock_resolve.return_value = _MockResult(legs=[leg])
            mock_prices.return_value = {(EXPIRY, 25050.0, "call"): 115.0}

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-md-test-004",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        meta = body["execution_metadata"]

        # Check metadata matches DB records
        order = db_session.query(PaperOrder).order_by(PaperOrder.id.desc()).first()
        exec_leg = meta["execution_resolution"]["legs"][0]
        assert order.strike == exec_leg["resolved_strike"] == 25050.0
        assert order.expiry == exec_leg["resolved_expiry"] == EXPIRY
        assert order.fill_price == exec_leg["fill_price"] == 115.0

    def test_v1_execution_returns_null_metadata(self, client, logged_in, db_session):
        """V1 POST /paper/executions returns execution_metadata: null."""
        # Execute via V1 path (not through V2 template endpoint)
        with patch("app.routers.paper.require_market_open", new_callable=AsyncMock), \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices:
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}

            resp = client.post(
                "/paper/executions",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-v1-md-test-001",
                    "symbol": "NIFTY",
                    "starting_capital": 500000,
                    "legs": [{
                        "symbol": "NIFTY",
                        "expiration_date": EXPIRY,
                        "strike_price": 25000.0,
                        "option_type": "call",
                        "action": "buy",
                        "quantity": 1,
                        "lot_size": LOT,
                    }],
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("execution_metadata") is None


class TestConcurrentExecution:
    """Two different client_order_ids can execute independently."""

    def test_two_different_client_order_ids_both_succeed(self, client, logged_in, db_session):
        tid = _create_template(client, logged_in, name="Concurrent Test")
        before = db_session.query(StrategyExecution).count()

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock):
            mock_resolve.return_value = _MockResult()
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}

            resp1 = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-concurrent-A",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )
            resp2 = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-concurrent-B",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["execution_id"] != resp2.json()["execution_id"]
        after = db_session.query(StrategyExecution).count()
        assert after == before + 2  # two independent executions


class TestLoadExecutionMetadataContract:
    """``_load_execution_metadata`` must never return a non-dict.

    The declared contract is ``dict | None``. Stored JSON that decodes to a
    list, a scalar, or ``null`` is not an audit trail, so it must be reported
    as absent instead of being handed to the replay response.
    """

    def _seed(self, db_session, raw, user_id="user-1"):
        """Store ``raw`` verbatim as the execution's metadata (bypassing JSON
        encoding so malformed / non-object payloads can be exercised)."""
        import secrets

        from app.models import StrategyExecution

        exec_id = secrets.token_hex(16)
        db_session.add(
            StrategyExecution(
                user_id=user_id,
                execution_id=exec_id,
                client_order_id="load-contract-coid",
                symbol="NIFTY",
                status="FILLED",
            )
        )
        db_session.commit()
        execution = (
            db_session.query(StrategyExecution)
            .filter_by(execution_id=exec_id)
            .first()
        )
        execution.execution_metadata = raw
        db_session.commit()
        return exec_id

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ('{"formula_version": 2, "formula": {"strike_mode": "atm"}}',
             {"formula_version": 2, "formula": {"strike_mode": "atm"}}),
            ('{"formula_version": 2}', {"formula_version": 2}),
        ],
    )
    def test_json_object_returns_dict(self, db_session, raw, expected):
        """A valid JSON object is returned as the metadata dict."""
        exec_id = self._seed(db_session, raw)
        assert _load_execution_metadata(db_session, "user-1", exec_id) == expected

    @pytest.mark.parametrize("raw", ["[]", '[{"strike_mode": "atm"}]'])
    def test_json_array_returns_none(self, db_session, raw):
        """A JSON array is NOT a valid audit trail."""
        exec_id = self._seed(db_session, raw)
        assert _load_execution_metadata(db_session, "user-1", exec_id) is None

    @pytest.mark.parametrize("raw", ['"atm"', "42", "3.14", "true", "false"])
    def test_json_scalar_returns_none(self, db_session, raw):
        """A JSON string / number / bool is NOT a valid audit trail."""
        exec_id = self._seed(db_session, raw)
        assert _load_execution_metadata(db_session, "user-1", exec_id) is None

    def test_json_null_returns_none(self, db_session):
        """JSON ``null`` decodes to None and stays None."""
        exec_id = self._seed(db_session, "null")
        assert _load_execution_metadata(db_session, "user-1", exec_id) is None

    @pytest.mark.parametrize(
        "raw", ['{"formula_version": 2', "not json at all", "{,}"]
    )
    def test_malformed_json_returns_none(self, db_session, raw):
        """Malformed JSON is reported as absent, never raised or guessed."""
        exec_id = self._seed(db_session, raw)
        assert _load_execution_metadata(db_session, "user-1", exec_id) is None

    def test_missing_row_and_null_column_return_none(self, db_session):
        """No row, or a NULL column, both mean 'no metadata'."""
        assert _load_execution_metadata(db_session, "user-1", "no-such-id") is None
        exec_id = self._seed(db_session, None)
        assert _load_execution_metadata(db_session, "user-1", exec_id) is None


class TestIdempotentReplayMetadata:
    """An idempotent replay must never rewrite the stored audit trail.

    ``execute_strategy()`` treats a retried ``client_order_id`` as a replay:
    it returns the ORIGINAL execution untouched (``duplicated=True``) and
    writes no new rows. The audit trail persisted alongside that execution is
    therefore immutable history — a replay must not overwrite it with the
    replay attempt's own (later) resolution, which would leave the stored
    strikes/prices contradicting the actual ``PaperOrder`` fills.
    """

    def _execute(self, client, session_id, tid, coid, strike, price, confirmed_strike):
        with patch(
            "app.services.template_resolution.resolve_legs", new_callable=AsyncMock
        ) as mock_resolve, patch(
            "app.routers.paper.resolve_market_prices", new_callable=AsyncMock
        ) as mock_prices, patch(
            "app.routers.paper.require_market_open", new_callable=AsyncMock
        ):
            mock_resolve.return_value = _MockResult(
                legs=[_MockLeg(resolved_strike=strike)]
            )
            mock_prices.return_value = {(EXPIRY, strike, "call"): price}
            return client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(session_id),
                json={
                    "client_order_id": coid,
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: confirmed_strike},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

    def test_replay_does_not_overwrite_persisted_metadata(
        self, client, logged_in, db_session
    ):
        tid = _create_template(client, logged_in, name="Replay Metadata")

        first = self._execute(
            client, logged_in, tid, "exec-replay-md-001", 25000.0, 100.0, 25000.0
        )
        assert first.status_code == 200
        exec_id = first.json()["execution_id"]

        stored_first = db_session.query(StrategyExecution).filter_by(
            execution_id=exec_id
        ).first().execution_metadata
        assert json.loads(stored_first)["execution_resolution"]["legs"][0][
            "resolved_strike"
        ] == 25000.0

        # Same idempotency key, chain has since moved one step (25000 -> 25050).
        replay = self._execute(
            client, logged_in, tid, "exec-replay-md-001", 25050.0, 115.0, 25050.0
        )
        assert replay.status_code == 200
        assert replay.json()["execution_id"] == exec_id
        assert replay.json()["duplicated"] is True

        db_session.expire_all()
        stored_after = (
            db_session.query(StrategyExecution)
            .filter_by(execution_id=exec_id)
            .first()
            .execution_metadata
        )
        assert stored_after == stored_first

        # The stored trail must still describe what was ACTUALLY filled.
        leg = json.loads(stored_after)["execution_resolution"]["legs"][0]
        order = db_session.query(PaperOrder).filter_by(
            execution_id=exec_id
        ).first()
        assert leg["resolved_strike"] == order.strike == 25000.0
        assert leg["fill_price"] == order.fill_price == 100.0

    def test_replay_returns_original_persisted_metadata(
        self, client, logged_in, db_session
    ):
        """The replay response echoes the ORIGINAL audit trail, not the
        replay attempt's fresh resolution."""
        tid = _create_template(client, logged_in, name="Replay Response")

        first = self._execute(
            client, logged_in, tid, "exec-replay-md-002", 25000.0, 100.0, 25000.0
        )
        assert first.status_code == 200
        original_meta = first.json()["execution_metadata"]

        replay = self._execute(
            client, logged_in, tid, "exec-replay-md-002", 25050.0, 115.0, 25050.0
        )
        assert replay.status_code == 200
        assert replay.json()["duplicated"] is True
        assert replay.json()["execution_metadata"] == original_meta
        leg = replay.json()["execution_metadata"]["execution_resolution"]["legs"][0]
        assert leg["resolved_strike"] == 25000.0
        assert leg["fill_price"] == 100.0

    def test_replay_writes_no_extra_rows(self, client, logged_in, db_session):
        """The replay still writes nothing (no second execution/orders/meta)."""
        tid = _create_template(client, logged_in, name="Replay No Writes")

        first = self._execute(
            client, logged_in, tid, "exec-replay-md-003", 25000.0, 100.0, 25000.0
        )
        assert first.status_code == 200
        exec_id = first.json()["execution_id"]
        counts_first = (
            db_session.query(StrategyExecution).count(),
            db_session.query(PaperOrder).count(),
        )

        replay = self._execute(
            client, logged_in, tid, "exec-replay-md-003", 25050.0, 115.0, 25050.0
        )
        assert replay.status_code == 200

        db_session.expire_all()
        assert (
            db_session.query(StrategyExecution).count(),
            db_session.query(PaperOrder).count(),
        ) == counts_first
        record = (
            db_session.query(StrategyExecution)
            .filter_by(execution_id=exec_id)
            .first()
        )
        assert record.execution_metadata is not None


    def test_replay_response_never_carries_non_dict_metadata(
        self, client, logged_in, db_session
    ):
        """End-to-end: a stored non-object never reaches the replay response.

        ``execution_metadata`` is declared ``dict | None``, so a stored JSON
        array (or any other non-object) must surface as ``null`` on the replay,
        not as the decoded value.
        """
        tid = _create_template(client, logged_in, name="Replay Non Dict")

        first = self._execute(
            client, logged_in, tid, "exec-replay-md-004", 25000.0, 100.0, 25000.0
        )
        assert first.status_code == 200
        exec_id = first.json()["execution_id"]

        # Corrupt the stored trail into a JSON array (outside the API — the
        # write path only ever stores dicts).
        record = (
            db_session.query(StrategyExecution)
            .filter_by(execution_id=exec_id)
            .first()
        )
        record.execution_metadata = '[{"resolved_strike": 99999.0}]'
        db_session.commit()

        replay = self._execute(
            client, logged_in, tid, "exec-replay-md-004", 25000.0, 100.0, 25000.0
        )
        assert replay.status_code == 200
        assert replay.json()["duplicated"] is True
        assert replay.json()["execution_metadata"] is None

        # The replay still never rewrites the stored record.
        db_session.expire_all()
        assert (
            db_session.query(StrategyExecution)
            .filter_by(execution_id=exec_id)
            .first()
            .execution_metadata
            == '[{"resolved_strike": 99999.0}]'
        )


class TestMetadataPersistenceFailure:
    """Metadata persistence failure does not convert a successful execution into a failure."""

    def test_execution_succeeds_when_metadata_persistence_fails(self, client, logged_in, db_session):
        """If _persist_execution_metadata raises, the execution is still returned successfully
        and the response contains execution_metadata=null."""
        tid = _create_template(client, logged_in, name="Meta Fail Test")

        with patch("app.services.template_resolution.resolve_legs", new_callable=AsyncMock) as mock_resolve, \
             patch("app.routers.paper.resolve_market_prices", new_callable=AsyncMock) as mock_prices, \
             patch("app.routers.paper.require_market_open", new_callable=AsyncMock), \
             patch("app.routers.templates._persist_execution_metadata") as mock_persist:
            mock_resolve.return_value = _MockResult()
            mock_prices.return_value = {(EXPIRY, 25000.0, "call"): 100.0}
            mock_persist.side_effect = RuntimeError("DB write failed")

            resp = client.post(
                f"/paper/templates/{tid}/execute",
                headers=headers(logged_in),
                json={
                    "client_order_id": "exec-meta-fail-001",
                    "starting_capital": 500000,
                    "confirmed_strikes": {0: 25000.0},
                    "confirmed_expiries": {0: EXPIRY},
                },
            )

        # Execution should still succeed
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "FILLED"
        assert body["execution_id"] is not None

        # Metadata should NOT be in the response (persistence failed)
        assert body.get("execution_metadata") is None

        # But the execution record should still exist in DB
        exec_record = db_session.query(StrategyExecution).filter_by(
            execution_id=body["execution_id"]
        ).first()
        assert exec_record is not None
        assert exec_record.execution_metadata is None  # not persisted

        # PaperOrder should exist
        order = db_session.query(PaperOrder).filter_by(
            execution_id=body["execution_id"]
        ).first()
        assert order is not None
        assert order.strike == 25000.0
        assert order.fill_price == 100.0
