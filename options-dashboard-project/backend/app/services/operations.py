"""Day 46 — operational alert conditions (Issue #92).

Deterministic, individually named conditions for the material failure
classes the SaaS gate requires — no opaque aggregate score. Each
condition maps to one notification event with:

* source domain, occurred-at timestamp, severity;
* a concise sanitized reason;
* a stable deduplication identity so retries/alert-loops collapse;
* the correlation/operation ID where one exists.

Conditions
----------
===============================  =======================  =========
Condition                        Event type               Scope
===============================  =======================  =========
Market-data staleness            ``market_data.stale``    user
Broker adapter/auth failure      ``broker.auth_failed``   user
Execution failure                ``execution.failed``     user
Ingestion/background job failure ``ingestion.job_failed`` platform
Readiness degradation            ``readiness.degraded``   platform
===============================  =======================  =========

User-scoped conditions notify the affected tenant; platform-scoped
conditions (jobs, readiness) are operational events with no user scope
— they are visible on admin/operational surfaces only, never through a
tenant's notification reads.
"""

from __future__ import annotations

from app.services.notifications import publish


def record_market_data_stale(
    db, *, user_scope: str, symbol: str, age_seconds: float, correlation_id: str | None = None
) -> dict:
    """Stale/insufficient market data for one symbol (user scope)."""
    return publish(
        db,
        event_type="market_data.stale",
        severity="warning" if age_seconds < 1800 else "error",
        source="market_data",
        summary=f"{symbol} market data is stale",
        details={"symbol": symbol, "age_seconds": round(age_seconds, 1)},
        user_scope=user_scope,
        correlation_id=correlation_id,
        dedup_key=f"market_data.stale:{symbol}",
    )


def record_broker_failure(
    db, *, user_scope: str, broker: str, reason: str, correlation_id: str | None = None
) -> dict:
    """Broker adapter/authentication failure (user scope)."""
    return publish(
        db,
        event_type="broker.auth_failed",
        severity="error",
        source="broker_adapter",
        summary=f"{broker} adapter failure",
        details={"broker": broker, "reason": reason},
        user_scope=user_scope,
        correlation_id=correlation_id,
        dedup_key=f"broker.auth_failed:{broker}",
    )


def record_execution_failure(
    db, *, user_scope: str, order_family: str, reason: str, correlation_id: str | None = None
) -> dict:
    """Execution failure for one order family (user scope)."""
    return publish(
        db,
        event_type="execution.failed",
        severity="error",
        source="execution",
        summary=f"Execution failed for {order_family}",
        details={"order_family": order_family, "reason": reason},
        user_scope=user_scope,
        correlation_id=correlation_id,
        dedup_key=f"execution.failed:{order_family}",
    )


def record_job_failure(
    db, *, job: str, reason: str, correlation_id: str | None = None
) -> dict:
    """Background ingestion/job failure (platform scope — no user)."""
    return publish(
        db,
        event_type="ingestion.job_failed",
        severity="error",
        source="background_jobs",
        summary=f"Background job failed: {job}",
        details={"job": job, "reason": reason},
        user_scope=None,
        correlation_id=correlation_id,
        dedup_key=f"ingestion.job_failed:{job}",
    )


def record_readiness_degradation(
    db, *, component: str, reason: str, correlation_id: str | None = None
) -> dict:
    """Readiness degradation for one dependency (platform scope)."""
    return publish(
        db,
        event_type="readiness.degraded",
        severity="critical",
        source="readiness",
        summary=f"Readiness degraded: {component}",
        details={"component": component, "reason": reason},
        user_scope=None,
        correlation_id=correlation_id,
        dedup_key=f"readiness.degraded:{component}",
    )


__all__ = [
    "record_broker_failure",
    "record_execution_failure",
    "record_job_failure",
    "record_market_data_stale",
    "record_readiness_degradation",
]
