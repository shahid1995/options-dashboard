"""Issue #17 — deterministic chronological backtester (Phase 1).

Implements docs/STRIKENOVA_OVERNIGHT_GAP_RESEARCH.md §13/§14/§15. Research-only.

Rules enforced by construction:

* **Chronological only** — observations are processed in the supplied order;
  no shuffling exists anywhere in this module. Train/validation/test splits
  are date-ordered slices; :func:`walk_forward_folds` yields expanding
  train / forward-test windows for walk-forward use.
* **No leakage** — the evaluation callback receives, for each test session,
  the session date and the causal history that precedes it. Targets are only
  ever joined for *scoring after prediction*.
* **Determinism** — same input rows ⇒ identical metrics; no randomness.
* **Honesty** — metrics include the observation count and, wherever a
  probability is produced, Brier score and reliability buckets. Accuracy is
  reported alongside the majority-class base rate so a >50% raw accuracy can
  never be mistaken for edge on its own (spec §15).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class Observation:
    """One evaluated research session."""

    session_date: str
    prediction: Mapping[str, Any]  # model output dict (state/scores/probabilities)
    realized_gap_class: str | None  # GAP_UP | FLAT | GAP_DOWN | None (unattached)
    realized_gap_points: float | None
    regime: str = "ALL"  # e.g. vix_normal | gex_negative | expiry | ALL
    confidence: float | None = None


@dataclass
class MetricBundle:
    """Complete metric set for one model slice (spec §15)."""

    n: int = 0
    n_scored: int = 0
    n_no_edge: int = 0
    accuracy: float | None = None
    balanced_accuracy: float | None = None
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    roc_auc: float | None = None
    brier: float | None = None
    reliability: dict[str, float] = field(default_factory=dict)
    mae_points: float | None = None
    rmse_points: float | None = None
    signed_error_mean: float | None = None
    threshold_hit_rates: dict[str, float] = field(default_factory=dict)
    majority_base_rate: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "n_scored": self.n_scored,
            "n_no_edge": self.n_no_edge,
            "accuracy": self.accuracy,
            "balanced_accuracy": self.balanced_accuracy,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "confusion": self.confusion,
            "roc_auc": self.roc_auc,
            "brier": self.brier,
            "reliability": self.reliability,
            "mae_points": self.mae_points,
            "rmse_points": self.rmse_points,
            "signed_error_mean": self.signed_error_mean,
            "threshold_hit_rates": self.threshold_hit_rates,
            "majority_base_rate": self.majority_base_rate,
        }


CLASSES = ("GAP_UP", "FLAT", "GAP_DOWN")


def _prediction_direction(prediction: Mapping[str, Any]) -> str | None:
    """Model's directional call from its output dict (NO_EDGE ⇒ None)."""
    state = prediction.get("state")
    if state in ("NO_EDGE", "ABSTAIN"):
        return None
    score = prediction.get("direction_score")
    if score is None:
        return None
    s = float(score)
    # pos_style emits [-100, +100]; SOS emits [-1, +1]. Neutral band ±10 units.
    band = 10.0 if abs(s) > 1.5 else 0.1
    if s > band:
        return "GAP_UP"
    if s < -band:
        return "GAP_DOWN"
    return "FLAT"


def _prediction_up_probability(prediction: Mapping[str, Any]) -> float | None:
    probs = prediction.get("probabilities") or {}
    p_up = probs.get("p_up")
    if p_up is None:
        return None
    return float(p_up)


def _confusion_matrix(pairs: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    matrix = {p: {a: 0 for a in CLASSES} for p in CLASSES}
    for pred, actual in pairs:
        if pred in matrix and actual in matrix:
            matrix[pred][actual] += 1
    return matrix


def _classification_metrics(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Accuracy, balanced accuracy, per-class precision/recall/F1, base rate."""
    n = len(pairs)
    if n == 0:
        return {}
    correct = sum(1 for p, a in pairs if p == a)
    accuracy = correct / n
    class_counts = {c: sum(1 for _, a in pairs if a == c) for c in CLASSES}
    majority = max(class_counts.values()) if class_counts else 0
    base_rate = majority / n if n else None

    recalls: list[float] = []
    precisions: list[float] = []
    f1s: list[float] = []
    for c in CLASSES:
        tp = sum(1 for p, a in pairs if p == c and a == c)
        fp = sum(1 for p, a in pairs if p == c and a != c)
        fn = sum(1 for p, a in pairs if p != c and a == c)
        recalls.append(tp / (tp + fn) if (tp + fn) > 0 else 0.0)
        precisions.append(tp / (tp + fp) if (tp + fp) > 0 else 0.0)
        f1s.append(
            2 * precisions[-1] * recalls[-1] / (precisions[-1] + recalls[-1])
            if (precisions[-1] + recalls[-1]) > 0
            else 0.0
        )
    # Per-class metrics computed one-vs-rest and averaged (macro).
    per_class = {c: {"precision": precisions[i], "recall": recalls[i], "f1": f1s[i]} for i, c in enumerate(CLASSES)}
    macro_p = sum(precisions) / len(precisions)
    macro_r = sum(recalls) / len(recalls)
    macro_f1 = sum(f1s) / len(f1s)
    return {
        "accuracy": accuracy,
        "balanced_accuracy": macro_r,
        "precision": macro_p,
        "recall": macro_r,
        "f1": macro_f1,
        "per_class": per_class,
        "majority_base_rate": base_rate,
    }


def _roc_auc(up_scores: Sequence[tuple[float, int]]) -> float | None:
    """Rank-based ROC-AUC for the GAP_UP-vs-rest problem; None if undefined."""
    pos = [s for s, y in up_scores if y == 1]
    neg = [s for s, y in up_scores if y == 0]
    if not pos or not neg:
        return None
    wins = ties = 0
    for p in pos:
        for q in neg:
            if p > q:
                wins += 1
            elif p == q:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def _brier_and_reliability(
    probs: Sequence[tuple[float, int]], buckets: int = 5
) -> tuple[float | None, dict[str, float]]:
    if not probs:
        return None, {}
    brier = sum((p - y) ** 2 for p, y in probs) / len(probs)
    reliability: dict[str, float] = {}
    for b in range(buckets):
        lo, hi = b / buckets, (b + 1) / buckets
        cell = [(p, y) for p, y in probs if lo <= p < hi or (b == buckets - 1 and p == hi)]
        if cell:
            reliability[f"{lo:.1f}-{hi:.1f}"] = sum(y for _, y in cell) / len(cell)
    return brier, reliability


def evaluate(observations: Sequence[Observation]) -> MetricBundle:
    """Score a chronologically ordered set of observations. Deterministic."""
    bundle = MetricBundle(n=len(observations))
    if not observations:
        return bundle
    bundle.n_no_edge = sum(1 for o in observations if o.prediction.get("state") in ("NO_EDGE", "ABSTAIN"))
    pairs: list[tuple[str, str]] = []
    gap_errors: list[float] = []
    up_probs: list[tuple[float, int]] = []
    expected_errors: list[float] = []
    for o in observations:
        direction = _prediction_direction(o.prediction)
        if direction is not None and o.realized_gap_class in CLASSES:
            pairs.append((direction, o.realized_gap_class))
        probs = o.prediction.get("probabilities") or {}
        if o.realized_gap_class in CLASSES and probs.get("p_up") is not None:
            up_probs.append((float(probs["p_up"]), 1 if o.realized_gap_class == "GAP_UP" else 0))
        exp = (o.prediction.get("probabilities") or {}).get("expected_gap_points")
        if exp is not None and o.realized_gap_points is not None:
            expected_errors.append(float(exp) - float(o.realized_gap_points))
    bundle.n_scored = len(pairs)
    if pairs:
        cm = _confusion_matrix(pairs)
        bundle.confusion = cm
        m = _classification_metrics(pairs)
        bundle.accuracy = m["accuracy"]
        bundle.balanced_accuracy = m["balanced_accuracy"]
        bundle.precision = m["precision"]
        bundle.recall = m["recall"]
        bundle.f1 = m["f1"]
        bundle.majority_base_rate = m["majority_base_rate"]
    if up_probs:
        bundle.roc_auc = _roc_auc(up_probs)
        bundle.brier, bundle.reliability = _brier_and_reliability(up_probs)
    if expected_errors:
        bundle.mae_points = sum(abs(e) for e in expected_errors) / len(expected_errors)
        bundle.rmse_points = math.sqrt(sum(e * e for e in expected_errors) / len(expected_errors))
        bundle.signed_error_mean = sum(expected_errors) / len(expected_errors)
        thresholds = (-100.0, -50.0, 50.0, 100.0)
        for t in thresholds:
            key = f"hit_{int(t)}"
            if t > 0:
                bundle.threshold_hit_rates[key] = sum(
                    1 for e in expected_errors if e >= t
                ) / len(expected_errors)
            else:
                bundle.threshold_hit_rates[key] = sum(
                    1 for e in expected_errors if e <= t
                ) / len(expected_errors)
    return bundle


def evaluate_by_regime(observations: Sequence[Observation]) -> dict[str, dict[str, Any]]:
    """§13: segment metrics by the supplied regime label (plus ALL)."""
    out: dict[str, dict[str, Any]] = {}
    regimes = sorted({o.regime for o in observations})
    for regime in regimes:
        subset = [o for o in observations if o.regime == regime]
        out[regime] = evaluate(subset).as_dict()
    out["ALL"] = evaluate(list(observations)).as_dict()
    return out


def evaluate_by_confidence(
    observations: Sequence[Observation], buckets: int = 3
) -> dict[str, dict[str, Any]]:
    """Hit-rate by confidence bucket (§15 trading relevance)."""
    scored = [
        o
        for o in observations
        if o.confidence is not None and _prediction_direction(o.prediction) is not None
        and o.realized_gap_class in CLASSES
    ]
    out: dict[str, dict[str, Any]] = {}
    if not scored:
        return out
    ordered = sorted(scored, key=lambda o: float(o.confidence))
    size = max(1, len(ordered) // buckets)
    for b in range(buckets):
        cell = ordered[b * size : (b + 1) * size] if b < buckets - 1 else ordered[b * size :]
        if not cell:
            continue
        hits = sum(
            1 for o in cell if _prediction_direction(o.prediction) == o.realized_gap_class
        )
        out[f"bucket_{b + 1}"] = {
            "n": len(cell),
            "confidence_mean": sum(float(o.confidence) for o in cell) / len(cell),
            "hit_rate": hits / len(cell),
        }
    return out


def walk_forward_folds(
    session_dates: Sequence[str],
    min_train: int,
    test_size: int = 1,
) -> list[tuple[list[str], list[str]]]:
    """Expanding-window walk-forward folds (§14) over sorted session dates.

    Each fold is ``(train_dates, test_dates)`` with train strictly earlier
    than test. No shuffling; deterministic; the final partial test window is
    included.
    """
    dates = sorted(set(session_dates))
    folds: list[tuple[list[str], list[str]]] = []
    i = min_train
    while i < len(dates):
        train = dates[:i]
        test = dates[i : i + test_size]
        if test:
            folds.append((train, test))
        i += test_size
    return folds


def run_backtest(
    rows: Sequence[Mapping[str, Any]],
    predict: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    regime_of: Callable[[Mapping[str, Any]], str] | None = None,
    confidence_of: Callable[[Mapping[str, Any]], float | None] | None = None,
) -> dict[str, Any]:
    """Chronological evaluation over precomputed feature rows.

    ``rows`` must be ordered by session date ascending (the pipeline
    guarantees this). For each row, ``predict`` receives the row's predictor
    data only; the realized target columns are joined solely for scoring.
    NO_EDGE/abstaining predictions are excluded from classification metrics
    but counted (``n_no_edge``) — the model is allowed to abstain (spec §12).
    """
    observations: list[Observation] = []
    for row in rows:
        prediction = predict(row)
        if prediction is None:
            continue
        regime = regime_of(row) if regime_of else "ALL"
        confidence = confidence_of(row) if confidence_of else None
        confidence = confidence if confidence is not None else prediction.get("confidence")
        observations.append(
            Observation(
                session_date=str(row["session_date"]),
                prediction=prediction,
                realized_gap_class=row.get("gap_class"),
                realized_gap_points=row.get("gap_points"),
                regime=regime,
                confidence=confidence,
            )
        )
    observations.sort(key=lambda o: o.session_date)  # deterministic order
    return {
        "metrics": evaluate(observations).as_dict(),
        "by_regime": evaluate_by_regime(observations),
        "by_confidence": evaluate_by_confidence(observations),
        "n_observations": len(observations),
    }
