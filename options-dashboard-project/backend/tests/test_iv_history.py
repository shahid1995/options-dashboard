"""Tests for the Phase 4.1 IV-history persistence repository."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.services import iv_history


@pytest.fixture
def db_session():
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


def obs(**overrides):
    base = {
        "timestamp": "2026-08-16T09:00:00+00:00",
        "symbol": "NIFTY",
        "expiry": "2026-08-20",
        "strike": 25000,
        "optionType": "call",
        "iv": 0.1824,  # canonical decimal (18.24%)
        "spot": 25000,
        "source": "upstox",
    }
    base.update(overrides)
    return base


def test_record_and_query_roundtrip(db_session):
    assert iv_history.record_iv_observations(db_session, [obs()]) == 1
    rows = iv_history.get_iv_observations(db_session, "NIFTY")
    assert len(rows) == 1
    assert rows[0]["iv"] == 0.1824  # canonical decimal, never the raw percent
    assert rows[0]["strike"] == 25000
    assert rows[0]["optionType"] == "call"
    assert rows[0]["source"] == "upstox"
    assert rows[0]["expiry"] == "2026-08-20"


def test_query_filters_by_expiry_and_option_type(db_session):
    iv_history.record_iv_observations(
        db_session,
        [
            obs(timestamp="2026-08-16T09:00:00+00:00"),
            obs(timestamp="2026-08-16T09:00:01+00:00", optionType="put", iv=0.1881),
            obs(timestamp="2026-08-16T09:00:02+00:00", expiry="2026-08-27", iv=0.19),
        ],
    )
    assert len(iv_history.get_iv_observations(db_session, "NIFTY")) == 3
    puts = iv_history.get_iv_observations(db_session, "NIFTY", option_type="put")
    assert len(puts) == 1 and puts[0]["optionType"] == "put"
    far = iv_history.get_iv_observations(db_session, "NIFTY", expiry="2026-08-27")
    assert len(far) == 1 and far[0]["expiry"] == "2026-08-27"


def test_invalid_iv_and_identity_rows_are_skipped(db_session):
    n = iv_history.record_iv_observations(
        db_session,
        [
            obs(),  # valid
            obs(iv=0),  # invalid: never store a 0% IV
            obs(iv=-1),  # invalid
            obs(iv=None),  # missing
            obs(optionType="straddle"),  # invalid identity
            obs(strike=None),  # invalid identity
            obs(symbol=""),  # invalid identity
        ],
    )
    assert n == 1
    assert len(iv_history.get_iv_observations(db_session, "NIFTY")) == 1


def test_prune_removes_old_observations_only(db_session):
    now = datetime.now(timezone.utc)
    iv_history.record_iv_observations(
        db_session,
        [
            obs(timestamp=(now - timedelta(days=200)).isoformat()),
            obs(timestamp=(now - timedelta(days=10)).isoformat()),
            obs(timestamp=now.isoformat()),
        ],
    )
    assert iv_history.prune_iv_observations(db_session, retention_days=90) == 1
    rows = iv_history.get_iv_observations(db_session, "NIFTY")
    assert len(rows) == 2
    # SQLite stores naive datetimes, so compare against a naive now.
    naive_now = now.replace(tzinfo=None)
    assert all((datetime.fromisoformat(r["timestamp"]) - naive_now).days > -90 for r in rows)


def test_prune_uses_canonical_ist_clock(db_session, monkeypatch):
    fixed_now = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(iv_history, "_utcnow", lambda: fixed_now)

    iv_history.record_iv_observations(
        db_session,
        [
            obs(timestamp="2026-06-30T02:00:00+05:30"),
            obs(timestamp="2026-06-30T06:00:00+05:30"),
        ],
    )

    assert iv_history.prune_iv_observations(db_session, retention_days=91) == 1
    rows = iv_history.get_iv_observations(db_session, "NIFTY")
    assert len(rows) == 1


def test_empty_input_records_nothing(db_session):
    assert iv_history.record_iv_observations(db_session, []) == 0
    assert iv_history.record_iv_observations(db_session, None) == 0
    assert len(iv_history.get_iv_observations(db_session, "NIFTY")) == 0

def test_record_normalizes_observation_timestamp_to_ist(db_session):
    """Persisted IV timestamps use the canonical naive-IST market-data clock."""
    n = iv_history.record_iv_observations(db_session, [
        obs(timestamp="2026-08-27T04:33:00Z"),
    ])
    assert n == 1
    row = db_session.query(iv_history.IVObservation).one()
    assert row.observed_at == datetime(2026, 8, 27, 10, 3)
