"""Day 46 — tenant-isolated notification reads (``/api/v1/notifications``).

The user-facing surface is READ-ONLY and strictly tenant-scoped: every
route resolves the caller through the canonical session dependency and
returns only events whose scope is that user. Platform-operational
events (user_scope NULL — job/readiness alerts) are never exposed here;
they belong to admin/operational surfaces. Publishing is a backend/
domain operation: there is deliberately NO user-facing publish route,
so notifications can never be minted, forged, or used to escalate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers.deps import AuthenticatedUser, CurrentUser
from app.services import notifications

router = APIRouter()


@router.get("")
def my_notifications(
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    """The caller's own notification events (tenant-isolated)."""
    return {
        "notifications": notifications.list_for_user(
            db, user.user_id, limit=max(1, min(limit, 500))
        )
    }


@router.get("/{event_id}")
def my_notification(
    event_id: str,
    user: AuthenticatedUser = Depends(CurrentUser()),
    db: Session = Depends(get_db),
):
    """One of the caller's own events; foreign-scope events are 403/404."""
    return notifications.get_for_user(db, user.user_id, event_id)
