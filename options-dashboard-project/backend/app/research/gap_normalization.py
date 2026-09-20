"""Issue #17 — timestamp-respecting research normalization (Phase 1).

Causal rolling z-score normalization (spec §7)::

    z = (x - rolling_mean) / rolling_std

Rules (all documented and tested):

* **No future observations** — the window fed to :func:`rolling_zscore` must
  contain only values observed at or before the prediction timestamp. This
  module enforces nothing about ordering (it cannot know timestamps); the
  pipeline and the leak tests enforce it. A ``min_history`` guard refuses to
  normalize when insufficient causal history exists.
* **Missing is not zero** — ``None`` inputs are preserved as ``None``; a
  missing feature is never normalized into a zero.
* **Robust alternative** — :func:`robust_zscore` (median/MAD) for
  heavy-tailed series; same causality contract.
* **Winsorization** — optional clipping of the *normalized* value to
  ``±z_clip`` (documented transformation applied after normalization, never
  to the raw observation).
* **Zero-variance windows** — a zero std produces ``0.0`` when the value
  equals the window mean, else the clipped bound (deterministic, documented).
"""

from __future__ import annotations

import math
from typing import Sequence

#: Default minimum causal history before z-scores are produced.
DEFAULT_MIN_HISTORY = 20

#: Default normalized-value clipping bound.
DEFAULT_Z_CLIP = 5.0


def _finite(v: float | None) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def rolling_zscore(
    value: float | None,
    history: Sequence[float | None],
    min_history: int = DEFAULT_MIN_HISTORY,
    z_clip: float | None = DEFAULT_Z_CLIP,
) -> float | None:
    """Causal z-score of ``value`` against a strictly-prior ``history``.

    ``history`` must already be filtered by the caller to observations at or
    before the prediction timestamp. Returns ``None`` when the value is
    missing or history is shorter than ``min_history`` — never a fabricated 0.
    """
    v = _finite(value)
    if v is None:
        return None
    clean = [x for x in (_finite(h) for h in history) if x is not None]
    if len(clean) < min_history:
        return None
    mean = sum(clean) / len(clean)
    variance = sum((x - mean) ** 2 for x in clean) / len(clean)
    std = math.sqrt(variance)
    if std == 0:
        if v == mean:
            return 0.0
        return z_clip if v > mean else (-z_clip if z_clip is not None else None)
    z = (v - mean) / std
    if z_clip is not None:
        z = max(-z_clip, min(z_clip, z))
    return z


def robust_zscore(
    value: float | None,
    history: Sequence[float | None],
    min_history: int = DEFAULT_MIN_HISTORY,
    z_clip: float | None = DEFAULT_Z_CLIP,
) -> float | None:
    """Median/MAD z-score (robust alternative for heavy-tailed series).

    Same causality and missing-data contract as :func:`rolling_zscore`.
    """
    v = _finite(value)
    if v is None:
        return None
    clean = sorted(x for x in (_finite(h) for h in history) if x is not None)
    if len(clean) < min_history:
        return None
    n = len(clean)
    median = (
        clean[n // 2]
        if n % 2 == 1
        else (clean[n // 2 - 1] + clean[n // 2]) / 2.0
    )
    abs_dev = sorted(abs(x - median) for x in clean)
    mad = (
        abs_dev[n // 2]
        if n % 2 == 1
        else (abs_dev[n // 2 - 1] + abs_dev[n // 2]) / 2.0
    )
    if mad == 0:
        if v == median:
            return 0.0
        return z_clip if v > median else (-z_clip if z_clip is not None else None)
    z = (v - median) / (1.4826 * mad)  # 1.4826 ≈ consistency with std under normality
    if z_clip is not None:
        z = max(-z_clip, min(z_clip, z))
    return z


def normalize_feature_series(
    values: Sequence[float | None],
    min_history: int = DEFAULT_MIN_HISTORY,
    z_clip: float | None = DEFAULT_Z_CLIP,
) -> list[float | None]:
    """Walk a chronologically ordered series causally.

    Element i is normalized against elements ``max(0, i - window) .. i-1``
    (a rolling causal window), never against anything at or after i.
    """
    out: list[float | None] = []
    for i, v in enumerate(values):
        start = max(0, i - max(min_history, 1))
        # Use up to `min_history` immediately-prior observations (rolling).
        history = list(values[start:i])
        if len(history) > min_history:
            history = history[-min_history:]
        out.append(rolling_zscore(v, history, min_history=min_history, z_clip=z_clip))
    return out
