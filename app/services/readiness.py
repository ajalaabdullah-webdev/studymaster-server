"""Readiness probability engine.

Estimates each student's probability of reaching their target score, from their
own activity. Designed to behave the way the product needs:

- **Earned, not given** — probability = mastery × confidence. High raw accuracy on
  a tiny sample does NOT produce a high probability; confidence must be built by
  volume, syllabus coverage, and consistency.
- **Smooth** — recency-weighted signals + an EMA + a hard daily-change clamp, so it
  never lurches (no "studied a month → suddenly 12%").
- **Responsive** — recent performance is weighted more, so a real decline shows.

This is a transparent statistical model (no labelled outcomes exist yet to train a
classifier). `compute_readiness` is a pure function, so a trained model can replace
it later without touching callers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# --- tunables ---
RECENCY_HALFLIFE_DAYS = 21.0  # older performance decays with this half-life
PRIOR_MASTERY = 0.35  # neutral prior a beginner is shrunk toward
PRIOR_PSEUDO_COUNT = 6.0  # strength of that prior (in "effective questions")
VOLUME_FULL_AT = 300.0  # questions answered for full volume-confidence
CONSISTENCY_WINDOW_DAYS = 14
CONSISTENCY_FULL_AT = 10  # active days in the window for full consistency
MAX_DAILY_CHANGE = 0.06  # probability can move at most this much per update
EMA_ALPHA = 0.4  # smoothing of raw vs previous
PROB_FLOOR, PROB_CEIL = 0.02, 0.97
LOGISTIC_K = 6.0  # steepness of mastery-vs-target mapping


@dataclass
class PerfSample:
    """One accuracy observation."""
    subject: Optional[str]
    correct: int
    total: int
    when: datetime
    source: str  # "diagnostic" | "exam" | "quiz" | "practice"


@dataclass
class ReadinessSignals:
    exams: list[str]
    subjects: list[str]
    target_scores: dict[str, Any]
    samples: list[PerfSample] = field(default_factory=list)
    questions_answered: int = 0
    active_days_recent: int = 0
    roadmap_done_fraction: float = 0.0
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _recency_weight(when: datetime, now: datetime) -> float:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - when).total_seconds() / 86400.0)
    return 0.5 ** (days / RECENCY_HALFLIFE_DAYS)


def _subject_mastery(samples: list[PerfSample], now: datetime) -> float:
    """Recency-weighted accuracy shrunk toward the beginner prior."""
    w_correct = 0.0
    w_total = 0.0
    for s in samples:
        if s.total <= 0:
            continue
        w = _recency_weight(s.when, now) * s.total
        acc = s.correct / s.total
        w_correct += w * acc
        w_total += w
    # Bayesian shrinkage toward PRIOR_MASTERY with PRIOR_PSEUDO_COUNT.
    num = w_correct + PRIOR_PSEUDO_COUNT * PRIOR_MASTERY
    den = w_total + PRIOR_PSEUDO_COUNT
    return num / den if den else PRIOR_MASTERY


def _target_threshold(exam: str, value: Any) -> float:
    """Normalise a target score to a 0..1 mastery threshold."""
    if isinstance(value, (int, float)):
        if exam.upper() == "JAMB":
            return max(0.1, min(0.95, float(value) / 400.0))  # JAMB is out of 400
        return max(0.1, min(0.95, float(value) / 100.0))
    # Non-numeric (e.g. WAEC "credits in 5 subjects") -> credit ≈ 50%.
    return 0.5


def compute_readiness(sig: ReadinessSignals) -> dict[str, Any]:
    now = sig.now

    # 1) Per-subject mastery.
    by_subject: dict[str, list[PerfSample]] = {s: [] for s in sig.subjects}
    general: list[PerfSample] = []
    for smp in sig.samples:
        if smp.subject and smp.subject in by_subject:
            by_subject[smp.subject].append(smp)
        else:
            general.append(smp)

    subject_mastery: dict[str, float] = {}
    for subj, smps in by_subject.items():
        # fold in general samples (e.g. quizzes) at lower influence
        subject_mastery[subj] = _subject_mastery(smps + [_scaled(g) for g in general], now)
    if not subject_mastery:
        subject_mastery = {"overall": _subject_mastery(general, now)}

    overall_mastery = sum(subject_mastery.values()) / len(subject_mastery)

    # 2) Confidence: how much we trust the mastery estimate.
    volume = min(1.0, sig.questions_answered / VOLUME_FULL_AT)
    coverage = max(0.0, min(1.0, sig.roadmap_done_fraction))
    consistency = min(1.0, sig.active_days_recent / CONSISTENCY_FULL_AT)
    subjects_with_data = sum(1 for smps in by_subject.values() if smps)
    breadth = (subjects_with_data / len(sig.subjects)) if sig.subjects else 0.0
    confidence = 0.35 * volume + 0.30 * coverage + 0.20 * consistency + 0.15 * breadth

    # 3) Per-exam probability = sigmoid(mastery vs target) × confidence.
    exams_out: dict[str, Any] = {}
    for exam in sig.exams:
        t = _target_threshold(exam, (sig.target_scores or {}).get(exam))
        raw = 1.0 / (1.0 + math.exp(-LOGISTIC_K * (overall_mastery - t)))
        prob = raw * confidence
        exams_out[exam] = {
            "probabilityRaw": round(max(PROB_FLOOR, min(PROB_CEIL, prob)), 4),
            "expected": round(overall_mastery, 4),
            "target": round(t, 4),
            "confidence": round(confidence, 4),
        }

    return {
        "exams": exams_out,
        "subjects": {k: round(v, 4) for k, v in subject_mastery.items()},
        "confidenceFactors": {
            "volume": round(volume, 3),
            "coverage": round(coverage, 3),
            "consistency": round(consistency, 3),
            "breadth": round(breadth, 3),
        },
        "overallMastery": round(overall_mastery, 4),
    }


def _scaled(g: PerfSample) -> PerfSample:
    """General (non-subject) samples influence a subject at reduced weight."""
    return PerfSample(subject=None, correct=g.correct, total=max(1, g.total * 1), when=g.when, source=g.source)


def smooth_probability(prev: Optional[float], raw: float) -> float:
    """EMA + hard daily-change clamp so the number never jumps."""
    if prev is None:
        # First reading starts conservative (never opens high).
        return round(max(PROB_FLOOR, min(raw, 0.15)), 4)
    ema = EMA_ALPHA * raw + (1 - EMA_ALPHA) * prev
    delta = max(-MAX_DAILY_CHANGE, min(MAX_DAILY_CHANGE, ema - prev))
    return round(max(PROB_FLOOR, min(PROB_CEIL, prev + delta)), 4)
