"""Finding 2 regression tests — upstream broker details not exposed in API errors."""

import pytest
from unittest.mock import AsyncMock

from app.main import app
from app.services import token_store
from app.brokers.domain.errors import BrokerError, BrokerErrorCode, PUBLIC_BROKER_ERROR_MESSAGE
from app.services import upstox as upstox_module
from app.routers.deps import SESSION_COOKIE_NAME
from fastapi.testclient import TestClient


def _upstox_error(status_code, message="generic error"):
    return upstox_module.UpstoxError(status_code, message)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def logged_in(client):
    session_id = token_store.set_token("tok-xyz")
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    return session_id


def test_upstream_error_returns_502_with_sanitized_message(client, logged_in, monkeypatch):
    """Broker error -> 502 with stable generic message, no upstream details."""
    malicious_msg = "SECRET-UPSTREAM-XYZ @url:`https://internal.example/path` authorization=Bearer-SECRET"
    monkeypatch.setattr(upstox_module, "get_option_chain", AsyncMock(side_effect=_upstox_error(500, malicious_msg)))
    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"] == PUBLIC_BROKER_ERROR_MESSAGE
    assert "SECRET-UPSTREAM-XYZ" not in body["detail"]
    assert "internal.example" not in body["detail"]
    assert "Bearer-SECRET" not in body["detail"]
    assert "authorization" not in body["detail"]


def test_upstream_error_does_not_expose_url(client, logged_in, monkeypatch):
    """Raw upstream URL must not appear in error response."""
    msg = "Request to https://internal.upstox.com/v2/option/chain failed with status 500"
    monkeypatch.setattr(upstox_module, "get_option_chain", AsyncMock(side_effect=_upstox_error(500, msg)))
    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 502
    assert "internal.upstox.com" not in resp.json()["detail"]


def test_upstream_error_does_not_expose_exception_repr(client, logged_in, monkeypatch):
    """Exception representations must not leak."""
    msg = "ValueError: invalid instrument_key=NSE_FO|12345"
    monkeypatch.setattr(upstox_module, "get_option_chain", AsyncMock(side_effect=_upstox_error(502, msg)))
    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 502
    assert "ValueError" not in resp.json()["detail"]
    assert "NSE_FO" not in resp.json()["detail"]


def test_upstream_error_preserves_status_code(client, logged_in, monkeypatch):
    """Error status code contract is preserved."""
    monkeypatch.setattr(upstox_module, "get_option_chain", AsyncMock(side_effect=_upstox_error(500, "internal fail")))
    resp = client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert resp.status_code == 502


def test_upstream_error_still_logs_diagnostics(client, logged_in, monkeypatch, caplog):
    """Detailed error still reaches server-side logs."""
    import logging
    with caplog.at_level(logging.WARNING):
        monkeypatch.setattr(upstox_module, "get_option_chain", AsyncMock(side_effect=_upstox_error(500, "SECRET")))
        client.get("/chains/NIFTY", params={"expiry_date": "2026-08-28"})
    assert "SECRET" in caplog.text


def test_sanitize_function_returns_stable_message():
    """sanitize_broker_error_message returns stable generic message."""
    exc = BrokerError(BrokerErrorCode.UPSTREAM_ERROR, "malicious @url:`https://evil.com` secret=Bearer-XYZ")
    from app.brokers.domain.errors import sanitize_broker_error_message
    assert sanitize_broker_error_message(exc) == PUBLIC_BROKER_ERROR_MESSAGE
    assert "malicious" not in sanitize_broker_error_message(exc)
    assert "evil.com" not in sanitize_broker_error_message(exc)
