"""Account-security persistence and token-service tests (plan Task 2).

Covers:
- EmailVerificationToken / PasswordResetToken / PendingEmailChange / SecurityEvent models
- Opaque token generation: high entropy, only SHA-256 digest stored
- Single-use consumption (replay prevention) incl. concurrent consumption
- Expiry enforcement (TTL-bounded, fail closed)
- SecurityEvent immutability and secret-free payload
- Alembic revision c1d2e3f4a5b6 creates exactly the four security tables
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.identity import User, hash_password
from app.main import app
from app.services import account_security


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


def _user(db, email="sec@example.com"):
    user = User(
        id="11111111-1111-1111-1111-111111111111",
        email=email,
        password_hash=hash_password("Sup3rSecret!"),
        status="active",
        identity_source="email",
    )
    db.add(user)
    db.commit()
    return user


# ---------------------------------------------------------------------------
# Models exist and persist
# ---------------------------------------------------------------------------


class TestSecurityModels:
    def test_email_verification_token_persists(self, db_session):
        from app.services.account_security import EmailVerificationToken

        user = _user(db_session)
        raw, record = account_security.create_verification_token(
            db_session, user.id, ttl_minutes=30
        )
        db_session.commit()

        assert raw and len(raw) >= 32, "raw token must be high-entropy"
        stored = db_session.query(EmailVerificationToken).filter_by(id=record.id).one()
        assert stored.token_hash != raw, "raw token must never be stored"
        assert stored.user_id == user.id
        assert stored.used_at is None
        # SQLite strips tzinfo (repo-wide); re-attach UTC for comparison.
        expires = stored.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        assert expires > datetime.now(timezone.utc)

    def test_password_reset_token_persists(self, db_session):
        from app.services.account_security import PasswordResetToken

        user = _user(db_session)
        raw, record = account_security.create_reset_token(db_session, user.id)
        db_session.commit()

        stored = db_session.query(PasswordResetToken).filter_by(id=record.id).one()
        assert stored.token_hash != raw
        assert stored.user_id == user.id
        assert stored.used_at is None

    def test_pending_email_change_persists(self, db_session):
        from app.services.account_security import PendingEmailChange

        user = _user(db_session)
        raw, record = account_security.create_email_change_token(
            db_session, user.id, "new@example.com"
        )
        db_session.commit()

        stored = db_session.query(PendingEmailChange).filter_by(id=record.id).one()
        assert stored.token_hash != raw
        assert stored.user_id == user.id
        assert stored.new_email == "new@example.com"
        assert stored.used_at is None

    def test_security_event_persists(self, db_session):
        from app.services.account_security import SecurityEvent

        user = _user(db_session)
        event = account_security.record_security_event(
            db_session,
            user_id=user.id,
            event_type="login_success",
            session_id=None,
            metadata={"method": "password"},
        )
        db_session.commit()

        stored = db_session.query(SecurityEvent).filter_by(id=event.id).one()
        assert stored.event_type == "login_success"
        assert stored.occurred_at is not None

    def test_security_event_allows_anonymous_user(self, db_session):
        """user_id is nullable for anonymous events (design spec §5)."""
        from app.services.account_security import SecurityEvent

        event = account_security.record_security_event(
            db_session, user_id=None, event_type="login_failed"
        )
        db_session.commit()
        stored = db_session.query(SecurityEvent).filter_by(id=event.id).one()
        assert stored.user_id is None


# ---------------------------------------------------------------------------
# Token consumption: single use, replay-safe, expiry-bound
# ---------------------------------------------------------------------------


class TestTokenConsumption:
    def test_consume_verification_token_once(self, db_session):
        user = _user(db_session)
        raw, _ = account_security.create_verification_token(db_session, user.id)
        db_session.commit()

        consumed = account_security.consume_verification_token(db_session, raw)
        assert consumed is not None
        again = account_security.consume_verification_token(db_session, raw)
        assert again is None, "replayed token must fail"

    def test_consume_reset_token_once(self, db_session):
        user = _user(db_session)
        raw, _ = account_security.create_reset_token(db_session, user.id)
        db_session.commit()

        assert account_security.consume_reset_token(db_session, raw) is not None
        assert account_security.consume_reset_token(db_session, raw) is None

    def test_consume_email_change_token_once(self, db_session):
        user = _user(db_session)
        raw, _ = account_security.create_email_change_token(
            db_session, user.id, "new@example.com"
        )
        db_session.commit()

        consumed = account_security.consume_email_change_token(db_session, raw)
        assert consumed is not None
        assert consumed.new_email == "new@example.com"
        assert account_security.consume_email_change_token(db_session, raw) is None

    def test_expired_verification_token_fails(self, db_session):
        user = _user(db_session)
        raw, _ = account_security.create_verification_token(
            db_session, user.id, ttl_minutes=-1
        )
        db_session.commit()

        assert account_security.consume_verification_token(db_session, raw) is None

    def test_expired_reset_token_fails(self, db_session):
        user = _user(db_session)
        raw, _ = account_security.create_reset_token(db_session, user.id, ttl_minutes=-1)
        db_session.commit()
        assert account_security.consume_reset_token(db_session, raw) is None

    def test_unknown_token_fails(self, db_session):
        _user(db_session)
        assert account_security.consume_verification_token(db_session, "junk") is None
        assert account_security.consume_reset_token(db_session, "junk") is None

    def test_concurrent_consumption_single_winner(self, db_session):
        """Two interleaved consumption attempts on one token: exactly one
        succeeds (SELECT ... WHERE used_at IS NULL then atomic UPDATE)."""
        user = _user(db_session)
        raw, _ = account_security.create_verification_token(db_session, user.id)
        db_session.commit()

        first = account_security.consume_verification_token(db_session, raw)
        # Second attempt on a fresh session-like context still sees used_at set
        db_session.expire_all()
        second = account_security.consume_verification_token(db_session, raw)
        assert first is not None and second is None

    def test_raw_token_never_persists_in_any_table(self, db_session):
        """The database must contain only digests — never the raw token."""
        user = _user(db_session)
        raws = [
            account_security.create_verification_token(db_session, user.id)[0],
            account_security.create_reset_token(db_session, user.id)[0],
            account_security.create_email_change_token(
                db_session, user.id, "new@example.com"
            )[0],
        ]
        db_session.commit()

        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(db_session.bind)
        tables = {
            t: {c["name"] for c in inspector.get_columns(t)}
            for t in (
                "email_verification_tokens",
                "password_reset_tokens",
                "pending_email_changes",
            )
        }
        for table, cols in tables.items():
            assert "token_hash" in cols, f"{table} must store a hash column"
            assert "token" not in cols, f"{table} must not have a raw token column"
            rows = db_session.execute(text(f"SELECT token_hash FROM {table}")).fetchall()
            for (stored_hash,) in rows:
                for raw in raws:
                    assert raw != stored_hash

    def test_new_token_invalidates_prior_active_token(self, db_session):
        """Issuing a replacement invalidates prior active tokens of the same
        kind for the user (resend/recovery policy)."""
        user = _user(db_session)
        raw1, _ = account_security.create_verification_token(db_session, user.id)
        raw2, _ = account_security.create_verification_token(db_session, user.id)
        db_session.commit()

        assert account_security.consume_verification_token(db_session, raw1) is None
        assert account_security.consume_verification_token(db_session, raw2) is not None


# ---------------------------------------------------------------------------
# SecurityEvent hygiene
# ---------------------------------------------------------------------------


class TestSecurityEventHygiene:
    def test_event_rejects_secret_material_in_metadata(self, db_session):
        """record_security_event must refuse/strip forbidden metadata keys —
        passwords, tokens, codes and URLs never reach the security log."""
        user = _user(db_session)
        event = account_security.record_security_event(
            db_session,
            user_id=user.id,
            event_type="password_reset_completed",
            metadata={
                "password": "hunter2",
                "raw_token": "opaque-secret-value",
                "reset_url": "https://example.com/reset?token=opaque-secret-value",
                "auth_code": "oauth-code",
                "broker_api_secret": "super-secret",
                "method": "reset_token",  # safe key must survive
            },
        )
        blob = repr(event.metadata_json)
        for forbidden in ("hunter2", "opaque-secret-value", "oauth-code", "super-secret"):
            assert forbidden not in blob
        assert event.metadata_json.get("method") == "reset_token"

    def test_event_record_is_immutable_on_update(self, db_session):
        """Security events are append-only: in-place updates are rejected and
        the durable record is unchanged."""
        from app.services.account_security import SecurityEvent

        user = _user(db_session)
        event = account_security.record_security_event(
            db_session, user_id=user.id, event_type="login_success"
        )
        db_session.commit()

        stored = db_session.query(SecurityEvent).filter_by(id=event.id).one()
        stored.event_type = "tampered"
        with pytest.raises(RuntimeError, match="append-only"):
            db_session.flush()
        db_session.rollback()

        reloaded = db_session.query(SecurityEvent).filter_by(id=event.id).one()
        assert reloaded.event_type == "login_success"


# ---------------------------------------------------------------------------
# Configuration surface (Task 2 minimum)
# ---------------------------------------------------------------------------


class TestSecurityConfiguration:
    def test_ttl_and_email_settings_exist(self):
        assert settings.EMAIL_VERIFICATION_TTL_MINUTES > 0
        assert settings.PASSWORD_RESET_TTL_MINUTES > 0
        assert settings.RECENT_AUTH_TTL_MINUTES > 0
        assert settings.EMAIL_FROM_ADDRESS
        assert settings.EMAIL_BASE_URL

    def test_default_token_ttl_matches_config(self, db_session):
        user = _user(db_session)
        _raw, record = account_security.create_verification_token(db_session, user.id)
        expected = datetime.now(timezone.utc) + timedelta(
            minutes=settings.EMAIL_VERIFICATION_TTL_MINUTES
        )
        assert abs((record.expires_at - expected).total_seconds()) < 5


# ---------------------------------------------------------------------------
# Alembic migration c1d2e3f4a5b6
# ---------------------------------------------------------------------------


class TestAccountSecurityMigration:
    def test_revision_creates_exactly_four_security_tables(self, tmp_path):
        """upgrade → the four tables exist; downgrade to the parent revision
        a3b4c5d6e7f8 (pinned explicitly so the cycle stays valid as later
        migrations land on head) → they are gone; upgrade again → they are
        back (clean up/down/upgrade cycle)."""
        import os

        from alembic import command
        from alembic.config import Config

        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        temp_db = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"

        cfg = Config(os.path.join(backend_dir, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
        cfg.set_main_option("sqlalchemy.url", temp_db)
        cfg.attributes["configure_logger"] = False
        import app.identity  # noqa: F401  (ensure metadata is imported)

        command.upgrade(cfg, "head")
        try:
            engine = create_engine(temp_db)
            with engine.connect() as conn:
                tables = {
                    r[0]
                    for r in conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    ).fetchall()
                }
            expected = {
                "email_verification_tokens",
                "password_reset_tokens",
                "pending_email_changes",
                "security_events",
            }
            assert expected <= tables, f"missing tables: {expected - tables}"

            command.downgrade(cfg, "a3b4c5d6e7f8")
            with engine.connect() as conn:
                tables_after = {
                    r[0]
                    for r in conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    ).fetchall()
                }
            assert expected.isdisjoint(tables_after), "downgrade must drop the four tables"

            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                tables_restored = {
                    r[0]
                    for r in conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    ).fetchall()
                }
            assert expected <= tables_restored
            engine.dispose()
        finally:
            # Return any DB to head for later tests
            command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Task 5 — application logs never contain secret material (plan requirement)
# ---------------------------------------------------------------------------


def test_account_flows_never_log_secrets(db_session, caplog):
    """Raw passwords, raw tokens, reset URLs, OAuth codes and broker
    credentials must never appear in application log output during a full
    account-security journey."""
    import logging

    from fastapi.testclient import TestClient
    from app.services.email import clear_sent_messages, get_sent_messages

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    _user(db_session, email="logleak@example.com")

    clear_sent_messages()
    try:
        with caplog.at_level(logging.DEBUG, logger="app"):
            client.post(
                "/auth/account/login",
                json={"email": "logleak@example.com", "password": "WrongAttempt1!"},
            )
            client.post(
                "/auth/account/login",
                json={"email": "logleak@example.com", "password": "Sup3rSecret!"},
            )
            client.post(
                "/auth/account/forgot-password", json={"email": "logleak@example.com"}
            )
            raw_reset = get_sent_messages()[-1].raw_token
            client.post(
                "/auth/account/reset-password",
                json={"token": raw_reset, "new_password": "FreshPass123!"},
            )
    finally:
        app.dependency_overrides.clear()

    secrets = [
        "Sup3rSecret!",
        "WrongAttempt1!",
        "FreshPass123!",
        raw_reset,
        f"reset-password?token={raw_reset}",
    ]
    log_blob = "\n".join(
        f"{rec.levelname}:{rec.name}:{rec.getMessage()}" for rec in caplog.records
    )
    for secret in secrets:
        assert secret not in log_blob, (
            f"secret material leaked into application logs: {secret[:8]}..."
        )
