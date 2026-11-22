from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, normalize_database_url
from app.identity import BrokerConnection, User
from app.models import GexSnapshot
from tests import issue145_pg_support as support


@pytest.fixture(scope="module")
def postgres_target():
    """A disposable PostgreSQL database whose schema was built the supported way.

    The schema comes from ``python -m app.migrate upgrade head`` under the
    migration identity (Topology A): a bare ``alembic upgrade head`` is
    refused on PostgreSQL and no test may bypass that guard.
    """
    if support.admin_url() is None:
        pytest.skip(f"{support.ADMIN_ENV} is not configured (disposable PostgreSQL 16.16)")
    prov = support.provision_schema_ready()
    try:
        yield prov
    finally:
        support.drop(prov)


@pytest.fixture(scope="module")
def postgres_engine(postgres_target):
    engine = support.migrator_engine(postgres_target)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
        yield engine
    finally:
        engine.dispose()


def test_schema_is_built_by_the_guarded_migration_path(postgres_target, postgres_engine):
    """The fixture proves the lease and the Alembic chain both ran."""
    with postgres_engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        protocol = connection.execute(
            text(
                "SELECT installed_version, bootstrap_state, barrier_state "
                "FROM migration_internal.lease_protocol"
            )
        ).one()
        owner = connection.execute(
                text(
                    "SELECT current_user = :role AS is_creator, "
                    " (SELECT tableowner FROM pg_tables "
                    "   WHERE schemaname='public' AND tablename='positions') AS positions_owner"
                ),
                {"role": postgres_target.migrator_role},
            ).one()
    assert revision
    assert (protocol.installed_version, protocol.bootstrap_state, protocol.barrier_state) == (
        1,
        "ready",
        "open",
    )
    assert owner.is_creator is True
    assert owner.positions_owner == postgres_target.migrator_role


def test_postgres_url_normalization():
    assert (
        normalize_database_url("postgres://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )
    assert (
        normalize_database_url("postgresql://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )
    explicit = "postgresql+psycopg://user:pass@host:5432/db"
    assert normalize_database_url(explicit) == explicit
    sqlite = "sqlite:////tmp/test.db"
    assert normalize_database_url(sqlite) == sqlite


def test_full_alembic_schema_exists_on_postgres(postgres_engine):
    tables = set(inspect(postgres_engine).get_table_names())
    expected = set(Base.metadata.tables.keys())
    missing = sorted(expected - tables)
    assert not missing, f"PostgreSQL is missing ORM tables: {missing}"


def test_postgres_schema_supports_identity_and_gex_provenance(postgres_engine):
    session = sessionmaker(bind=postgres_engine, autoflush=False, autocommit=False)()
    user_id = "postgres-compat-user"
    connection_id = "postgres-compat-connection"

    try:
        # Flush the parent first so PostgreSQL's FK constraint is satisfied.
        session.add(
            User(
                id=user_id,
                email="postgres-compat@example.com",
                identity_source="email",
            )
        )
        session.flush()

        session.add(
            BrokerConnection(
                id=connection_id,
                user_id=user_id,
                broker="UPSTOX",
                broker_account_id="pending",
                is_default=True,
                status="pending",
                capability_mode="data",
                data_status="active",
                trading_status="inactive",
            )
        )
        session.flush()

        snapshot = GexSnapshot(
            owner_id=user_id,
            connection_id=connection_id,
            data_source="analytics_token",
            symbol="NIFTY",
            expiry="2026-12-31",
            spot=25000.0,
            methodology="GEX_STANDARD_V1",
            sign_convention="NAIVE_DEALER_CONVENTION",
            availability_status="available",
            valid_strike_count=2,
            total_strike_count=2,
            captured_at=datetime.now(timezone.utc),
            strike_data="[]",
            expiry_data="[]",
            methodology_metadata="{}",
        )
        session.add(snapshot)
        session.commit()

        loaded = session.get(GexSnapshot, snapshot.id)
        assert loaded is not None
        assert loaded.owner_id == user_id
        assert loaded.connection_id == connection_id
        assert loaded.data_source == "analytics_token"
        assert loaded.symbol == "NIFTY"
    finally:
        session.rollback()
        session.close()


def test_postgres_partial_unique_default_connection_is_enforced(postgres_engine):
    session: Session = sessionmaker(bind=postgres_engine, autoflush=False, autocommit=False)()
    user_id = "postgres-default-index-user"

    try:
        session.add(
            User(
                id=user_id,
                email="postgres-default@example.com",
                identity_source="email",
            )
        )
        session.flush()
        session.add(
            BrokerConnection(
                id="default-connection-1",
                user_id=user_id,
                broker="UPSTOX",
                broker_account_id="account-1",
                is_default=True,
                status="connected",
                capability_mode="trading",
            )
        )
        session.commit()

        session.add(
            BrokerConnection(
                id="default-connection-2",
                user_id=user_id,
                broker="UPSTOX",
                broker_account_id="account-2",
                is_default=True,
                status="connected",
                capability_mode="trading",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()


def test_postgres_unique_constraint_allows_multiple_non_default_connections(postgres_engine):
    session: Session = sessionmaker(bind=postgres_engine, autoflush=False, autocommit=False)()
    user_id = "postgres-multi-connection-user"

    try:
        session.add(
            User(
                id=user_id,
                email="postgres-multi@example.com",
                identity_source="email",
            )
        )
        session.flush()
        session.add_all(
            [
                BrokerConnection(
                    id="non-default-1",
                    user_id=user_id,
                    broker="UPSTOX",
                    broker_account_id="account-nd-1",
                    is_default=False,
                    status="connected",
                    capability_mode="trading",
                ),
                BrokerConnection(
                    id="non-default-2",
                    user_id=user_id,
                    broker="UPSTOX",
                    broker_account_id="account-nd-2",
                    is_default=False,
                    status="connected",
                    capability_mode="trading",
                ),
            ]
        )
        session.commit()
        count = (
            session.query(BrokerConnection)
            .filter(BrokerConnection.user_id == user_id)
            .count()
        )
        assert count == 2
    finally:
        session.rollback()
        session.close()
