"""Mastery engine.

Two research-backed mechanisms:

1. Bayesian Knowledge Tracing (Corbett & Anderson 1995) — per-concept
   posterior P(known), updated after every answer. The learner sees this
   number move: the interface itself is a Bayesian update.
2. SM-2-lite spaced review (Ebbinghaus spacing effect; SuperMemo SM-2) —
   mastered concepts come due for interleaved review at expanding
   intervals, which is where long-term retention is actually won
   (Roediger & Karpicke 2006 on retrieval practice; Cepeda et al. 2006
   on spacing).

Gate: a concept unlocks its successor when P(known) >= MASTERY_P and the
learner has a streak of STREAK_GATE consecutive correct answers. Bloom's
mastery-learning studies used ~90% gates; we hold 95% posterior + streak
so a lucky guess can't open the gate.
"""
import time

# BKT parameters — deliberately conservative (slip/guess bounded away from 0)
P_INIT = 0.15      # P(known before any evidence)
P_TRANSIT = 0.20   # P(learn on a correct attempt)
P_TRANSIT_WRONG = 0.05  # small learning from reading feedback on a miss
P_SLIP = 0.10      # P(wrong | known)
P_GUESS = 0.20     # P(right | not known) — 4-option MCQ baseline

MASTERY_P = 0.95
STREAK_GATE = 3
# P2.6 (iss_0824d5ad): a review failure that drops the posterior below this
# demotes the concept to RELEARN (cards re-served, gate re-run). One slip from
# solid mastery lands ~0.72-0.93 and only shortens the review interval; it
# takes repeated failure to fall through 0.70 — demotion means the knowledge
# is genuinely gone, not that the learner blinked.
RELEARN_P = 0.70


def bkt_update(p: float, correct: bool) -> float:
    """One Bayes update on P(known), then apply learning transition."""
    if correct:
        num = p * (1 - P_SLIP)
        den = p * (1 - P_SLIP) + (1 - p) * P_GUESS
    else:
        num = p * P_SLIP
        den = p * P_SLIP + (1 - p) * (1 - P_GUESS)
    post = num / den if den > 0 else p
    t = P_TRANSIT if correct else P_TRANSIT_WRONG
    updated = post + (1 - post) * t
    if not correct:
        # SLIP-INVARIANT CLAMP (iss_67193f36, 2026-07-23): below p≈0.0526 the
        # transit term outweighs the Bayes decrement, so a WRONG answer would
        # RAISE the displayed posterior (0.03 → 0.054) — contradicting the
        # meter's stated invariant. A miss may never move the meter up.
        # (The 0.95 gate is unaffected either way; this protects the
        # transparency-critical low end.)
        return min(updated, p)
    return updated


def is_mastered(p: float, streak: int) -> bool:
    return p >= MASTERY_P and streak >= STREAK_GATE


def schedule_review(interval_d: float, ease: float, correct: bool):
    """SM-2-lite. Returns (interval_d, ease, due_at)."""
    if not correct:
        interval_d, ease = 0.5, max(1.3, ease - 0.2)
    elif interval_d < 1:
        interval_d = 1.0
    elif interval_d < 3:
        interval_d = 3.0
    else:
        interval_d = interval_d * ease
        ease = min(3.0, ease + 0.05)
    return interval_d, ease, time.time() + interval_d * 86400


def decay_for_review(p: float, due_at: float | None) -> float:
    """Soft forgetting: if a mastered concept is past due, shade the
    displayed posterior down so the dashboard tells the truth about
    retention risk. Storage keeps the raw value; this is display-side."""
    if not due_at:
        return p
    overdue_d = max(0.0, (time.time() - due_at) / 86400)
    return max(0.5, p - 0.03 * overdue_d)
