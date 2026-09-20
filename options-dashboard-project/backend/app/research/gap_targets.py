"""Issue #17 — research target generation (Phase 1).

Pure, deterministic target construction for the next-session opening gap
(docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md §2). The realized target is ALWAYS
computed in its own step, after features/predictions for session T are stored,
so no target value can leak into feature generation.

Formulas (exact, tested)::

    gap_points = next_open - prior_close
    gap_pct    = gap_points / prior_close

Missing inputs stay ``None`` — missing is never zero. The gap class uses a
configurable neutral band (spec §2: no hard-coded arbitrary threshold).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: Neutral band as a fraction of prior close (|gap_pct| <= band ⇒ FLAT).
FLAT_BAND_PCT = 0.001  # 0.1% of prior close


@dataclass(frozen=True)
class GapTarget:
    """The realized next-session target for one research session."""

    gap_points: float
    gap_pct: float
    gap_class: str  # GAP_UP | FLAT | GAP_DOWN
    next_open_timestamp: str | None
    next_session_date: str | None


def compute_gap_target(
    prior_close: float,
    next_open: float,
    next_open_timestamp: str | None = None,
    next_session_date: str | None = None,
    flat_band_pct: float = FLAT_BAND_PCT,
) -> GapTarget | None:
    """Compute the exact gap target; ``None`` when an input is missing/invalid.

    The class boundary is the configurable neutral band: a |gap| within
    ``flat_band_pct`` of the prior close is FLAT — never silently classified
    by sign.
    """
    if prior_close is None or next_open is None:
        return None
    try:
        pc = float(prior_close)
        no = float(next_open)
    except (TypeError, ValueError):
        return None
    if pc == 0:
        return None
    gap_points = no - pc
    gap_pct = gap_points / pc
    if abs(gap_pct) <= flat_band_pct:
        gap_class = "FLAT"
    elif gap_points > 0:
        gap_class = "GAP_UP"
    else:
        gap_class = "GAP_DOWN"
    return GapTarget(
        gap_points=gap_points,
        gap_pct=gap_pct,
        gap_class=gap_class,
        next_open_timestamp=next_open_timestamp,
        next_session_date=next_session_date,
    )


def attach_target_to_session_row(
    session_row: Mapping[str, Any], target: GapTarget | None
) -> dict[str, Any]:
    """Return a copy of the session row with target columns attached.

    Pure helper used by the pipeline's target-attachment step; the predictor
    columns are untouched, so calling this after feature/prediction storage
    cannot contaminate them.
    """
    row = dict(session_row)
    if target is None:
        row.update(
            {
                "next_open": None,
                "gap_points": None,
                "gap_pct": None,
                "gap_class": None,
            }
        )
        return row
    row.update(
        {
            "next_open": target.gap_points + row.get("prior_close"),
            "gap_points": target.gap_points,
            "gap_pct": target.gap_pct,
            "gap_class": target.gap_class,
            "next_open_timestamp": target.next_open_timestamp,
            "next_session_date": target.next_session_date,
        }
    )
    return row
