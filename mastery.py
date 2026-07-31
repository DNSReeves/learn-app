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
import math
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
    """SM-2-lite (legacy — P2.7 replaced the live scheduler with FSRS below;
    kept for comparison tests and the migration story)."""
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


# ── P2.7 (iss_d13d1882): FSRS-4.5 replaces SM-2-lite as the live scheduler ────
# Two-component memory model (stability S = interval at 90% retention,
# difficulty D ∈ [1,10]) fitted on ~700M Anki reviews — strictly better
# interval placement than SM-2's ease heuristic, especially after lapses.
# Binary grading here: correct → Good(3), wrong → Again(1); the Hard/Easy
# weights (w15/w16) are therefore unused. Default FSRS-4.5 weights (py-fsrs).
FSRS_W = (0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.0310,
          1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.5870, 0.2272,
          2.8755)
FSRS_RETENTION = 0.90        # scheduled retention target; at 0.90, interval == S exactly
FSRS_DECAY = -0.5
FSRS_FACTOR = 19.0 / 81.0    # makes R(S, S) = 0.90
FSRS_MIN_INTERVAL = 0.5      # matches the old fail floor; review never lands <12h out
FSRS_MAX_INTERVAL = 365.0    # single-learner app — a year is far enough


def _fsrs_d0(grade: int) -> float:
    return min(10.0, max(1.0, FSRS_W[4] - (grade - 3) * FSRS_W[5]))


def fsrs_retrievability(elapsed_d: float, stability: float) -> float:
    return (1.0 + FSRS_FACTOR * max(0.0, elapsed_d) / stability) ** FSRS_DECAY


def fsrs_interval(stability: float) -> float:
    iv = stability / FSRS_FACTOR * (FSRS_RETENTION ** (1.0 / FSRS_DECAY) - 1.0)
    return min(FSRS_MAX_INTERVAL, max(FSRS_MIN_INTERVAL, iv))


def fsrs_review(stability: float | None, difficulty: float | None,
                elapsed_d: float, correct: bool, now: float | None = None):
    """One FSRS step. Returns (stability, difficulty, interval_d, due_at).

    stability None/<=0 → first review: state initialized from the grade
    (legacy SM-2 rows migrate by passing their interval_d as stability —
    exact at the 0.90 retention target, where interval == S by construction).
    """
    now = time.time() if now is None else now
    g = 3 if correct else 1
    if not stability or stability <= 0:
        s2 = FSRS_W[g - 1]
        d2 = _fsrs_d0(g)
    else:
        d = difficulty if difficulty and 1.0 <= difficulty <= 10.0 else _fsrs_d0(3)
        r = fsrs_retrievability(elapsed_d, stability)
        if correct:
            s2 = stability * (1.0 + math.exp(FSRS_W[8]) * (11.0 - d)
                              * stability ** (-FSRS_W[9])
                              * (math.exp(FSRS_W[10] * (1.0 - r)) - 1.0))
        else:
            s2 = (FSRS_W[11] * d ** (-FSRS_W[12])
                  * ((stability + 1.0) ** FSRS_W[13] - 1.0)
                  * math.exp(FSRS_W[14] * (1.0 - r)))
            s2 = min(s2, stability)          # a lapse may never raise stability
        nd = d - FSRS_W[6] * (g - 3)
        d2 = min(10.0, max(1.0, FSRS_W[7] * _fsrs_d0(4) + (1.0 - FSRS_W[7]) * nd))
    iv = fsrs_interval(s2)
    return s2, d2, iv, now + iv * 86400


def decay_for_review(p: float, due_at: float | None) -> float:
    """Soft forgetting: if a mastered concept is past due, shade the
    displayed posterior down so the dashboard tells the truth about
    retention risk. Storage keeps the raw value; this is display-side."""
    if not due_at:
        return p
    overdue_d = max(0.0, (time.time() - due_at) / 86400)
    return max(0.5, p - 0.03 * overdue_d)
