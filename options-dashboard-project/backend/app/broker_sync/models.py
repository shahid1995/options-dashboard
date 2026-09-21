"""Broker-sync idempotency model.

Durable idempotency record for canonical broker events.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BrokerSyncIdempotency(Base):
    """Durable idempotency record for canonical broker events.

    Persisted alongside the normalized projection and Day38 lifecycle
    event within a single caller-owned transaction.  The ``canonical_id``
    primary key enforces durability: a duplicate identity cannot be
    inserted twice, so even after process restart the idempotency state
    survives.
    """

    __tablename__ = "broker_sync_idempotency"

    canonical_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    broker: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    canonical_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_version: Mapped[str] = mapped_column(String(16), nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Day41.2 — durable S2 evidence (frozen r2 design §12): the provider/
    # exchange event timestamp, populated STRICTLY from ``event.event_timestamp``.
    # NULL means S2 evidence is unavailable (structurally distinct from any
    # timestamp value).  ``received_at`` remains provenance only and must never
    # substitute for S2 authority.
    event_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Day41.2 — UNRESOLVED resolution evidence (frozen r2 design §13):
    # NULL until an S3/S4 resolution occurs; then e.g. "S3:<canonical_id>"
    # or "S4:<operator evidence reference>".  UNRESOLVED is transient —
    # resolvable through S3/S4; STALE and APPLIED are terminal.
    resolution_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("canonical_id", name="uq_broker_sync_idempotency_canonical_id"),
    )


class BrokerOrderProjection(Base):
    """Normalized broker-order state (current-state representation).

    One row per canonical event, but the *current* state is reconstructed
    deterministically via the ``canonical_sequence`` ordering (not insertion
    time).  The row with the highest ``canonical_sequence`` for a given
    tenant+broker+broker_order_id is the current state.
    """

    __tablename__ = "broker_order_projection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    broker: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    total_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cumulative_filled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remaining_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_fill_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_terminal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_fill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Day41.2 — durable S2 evidence (frozen r2 design §12): the provider/
    # exchange event timestamp, populated STRICTLY from ``event.event_timestamp``.
    # NULL means S2 evidence is unavailable.  ``occurred_at`` (and its
    # event_timestamp-or-received_at fallback) remain display/derived only and
    # must never become S2 authority.
    event_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class BrokerSyncSequenceAnchor(Base):
    """Durable monotonic sequence for broker-order event ordering.

    Uses an upsert-based counter so the sequence allocation is atomic
    at the database level.  The anchor stores the last applied
    ``canonical_sequence`` for a given tenant+broker+broker_order_id,
    enabling gap/stale detection.
    """

    __tablename__ = "broker_sync_sequence_anchor"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    broker: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker_order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "broker", "broker_order_id",
            name="uq_broker_sync_sequence_anchor_identity",
        ),
    )


class OrderFamilySyncLock(Base):
    """D-1 dedicated order-family synchronization lock (Day41.2).

    Per the authoritative human architecture decision
    (``2026-09-12-strikenova-cross-d1-locking-human-architecture-decision.md``):
    a dedicated durable row keyed ``(tenant_id, broker, broker_order_id)``
    exists **solely** to serialize synchronization decisions for one broker
    order family.  It is a mutex/serialization record, NOT a state record.

    Non-purpose (decision memo): it must never represent or store provider
    sequence/S1 state, lifecycle state, projection state, D1, CEID, FPv2, or
    any semantic ordering evidence.  ``broker_sync_sequence_anchor`` remains
    strictly the S1/provider-sequence authority; sequence-less observations
    may acquire this lock but never mutate anchor state because of it.

    Concurrency contract (frozen r2 design §7): created via atomic
    ``INSERT … ON CONFLICT DO NOTHING`` (unique-key insert is the
    first-observer arbitration), then re-selected ``FOR UPDATE`` inside the
    Task2 transaction and held until commit/rollback.  Acquisition order
    confers no semantic meaning.
    """

    __tablename__ = "order_family_sync_lock"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    broker: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker_order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
