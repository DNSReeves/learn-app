"""calibration.py — P2.4: per-item difficulty + discrimination from the answer log.

Classical test theory first (the staged plan from the issue): proportion-correct
difficulty + point-biserial discrimination. 2PL IRT waits until the log's volume
justifies it — at single-operator scale (a handful of answers per item) IRT fits
would be noise wearing a Greek letter.

Definitions (keys are the (topic_id, concept_id, question_id) TRIPLE — question
ids are concept-local "q1"/"q2", not globally unique):

  n, n_correct     attempts / correct attempts across all users
  p_correct        Laplace-smoothed (n_correct+1)/(n+2) — a 1-for-1 item must not
                   pin to 1.0 difficulty-0 on a single lucky answer
  difficulty       1 − p_correct
  discrimination   point-biserial r between item correctness and the answerer's
                   REST-SCORE (their proportion correct on every OTHER item in the
                   same topic). Attempt-level on purpose: with one operator there
                   is no across-learner variance to correlate, but item-vs-rest
                   still answers the question that matters — "do misses on this
                   item track generally-weak performance (good item) or hit strong
                   performers too (ambiguous/miskeyed item)?" None when undefined
                   (fewer than MIN_N attempts, or zero variance on either side).
  calibrated       n >= MIN_N — below that, consumers must treat the item as
                   uncalibrated (P2.5 falls back to authored order for it)

Report (the acceptance): ``python calibration.py`` ranks items hardest-first and
flags negative discrimination (the miskeyed/ambiguous-item smell).
"""
from __future__ import annotations

import math
import os
import sqlite3
from typing import Any

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LEARN_DB", os.path.join(BASE, "learn.db"))

MIN_N = 3          # attempts before an item counts as calibrated


def _conn(db_path: str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(db_path or DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def item_stats(db_path: str | None = None) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Per-item classical stats from the answers table. Pure read; cheap enough to
    compute per request at this log's scale (recompute > stale cache)."""
    with _conn(db_path) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT user_id, topic_id, concept_id, question_id, is_correct FROM answers")]

    by_item: dict[tuple[str, str, str], list[dict]] = {}
    by_user_topic: dict[tuple[int, str], list[dict]] = {}
    for r in rows:
        by_item.setdefault((r["topic_id"], r["concept_id"], r["question_id"]), []).append(r)
        by_user_topic.setdefault((r["user_id"], r["topic_id"]), []).append(r)

    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, attempts in by_item.items():
        n = len(attempts)
        n_correct = sum(a["is_correct"] for a in attempts)
        p_smooth = (n_correct + 1) / (n + 2)
        # rest-score pairs: (item correctness, answerer's mean on OTHER topic items)
        pairs: list[tuple[float, float]] = []
        for a in attempts:
            peers = [x["is_correct"] for x in by_user_topic[(a["user_id"], a["topic_id"])]
                     if (x["concept_id"], x["question_id"]) != (a["concept_id"], a["question_id"])]
            if peers:
                pairs.append((float(a["is_correct"]), sum(peers) / len(peers)))
        out[key] = {
            "n": n, "n_correct": n_correct,
            "p_correct": round(p_smooth, 4),
            "difficulty": round(1 - p_smooth, 4),
            "discrimination": _point_biserial(pairs) if n >= MIN_N else None,
            "calibrated": n >= MIN_N,
        }
    return out


def _point_biserial(pairs: list[tuple[float, float]]) -> float | None:
    """r between a dichotomous item score and the continuous rest-score.
    None when either side has zero variance (r would be undefined)."""
    if len(pairs) < MIN_N:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    return round(cov / math.sqrt(vx * vy), 4)


def report(db_path: str | None = None) -> str:
    """The P2.4 acceptance artifact: items ranked hardest-first; negative
    discrimination flagged (miskeyed/ambiguous suspects); uncalibrated marked."""
    stats = item_stats(db_path)
    if not stats:
        return "No answers logged yet — nothing to calibrate."
    lines = [f"{'item':<40} {'n':>3} {'p':>7} {'diff':>6} {'disc':>7}  flags",
             "-" * 78]
    flagged = 0
    for key, s in sorted(stats.items(), key=lambda kv: -kv[1]["difficulty"]):
        flags = []
        if not s["calibrated"]:
            flags.append(f"uncalibrated(n<{MIN_N})")
        if s["discrimination"] is not None and s["discrimination"] < 0:
            flags.append("NEGATIVE-DISC ⚠ (miskeyed/ambiguous?)")
            flagged += 1
        disc = "—" if s["discrimination"] is None else f"{s['discrimination']:+.3f}"
        lines.append(f"{'/'.join(key):<40} {s['n']:>3} {s['p_correct']:>7.3f} "
                     f"{s['difficulty']:>6.3f} {disc:>7}  {' '.join(flags)}")
    lines.append(f"\n{len(stats)} items · {sum(1 for s in stats.values() if s['calibrated'])} "
                 f"calibrated · {flagged} negative-discrimination flag(s)")
    return "\n".join(lines)



# ---------- P2.5: adaptive question ordering for checkpoints ----------

def order_questions(questions: list[dict], tid: str, cid: str, p_mastery: float,
                    db_path: str | None = None) -> list[dict]:
    """Serve the item most informative at the learner's current P(known) instead
    of authored order (P2.5). Information is max where the predicted chance of a
    correct answer is ~0.5 — too-easy and too-hard items both teach the engine
    (and the learner) the least.

    predicted_i = BKT forecast for this concept  [p·(1−slip) + (1−p)·guess]
                  + the item's calibrated difficulty offset vs its concept mean
    score_i     = |predicted_i − 0.5|   (ascending; stable sort)

    HONEST FALLBACKS: a concept with NO calibrated items returns AUTHORED ORDER
    byte-identically (fresh packs behave exactly as before this feature);
    uncalibrated items compete on the bare BKT forecast (no offset), so new
    items still get served and accumulate the attempts that calibrate them.
    """
    try:
        import mastery
    except ImportError:                        # package-style import (tests)
        from learn_app import mastery          # type: ignore[no-redef]
    stats = item_stats(db_path)
    keyed = {q["id"]: stats.get((tid, cid, q["id"])) for q in questions}
    if not any(s and s["calibrated"] for s in keyed.values()):
        return list(questions)                 # fresh concept → authored order
    base = p_mastery * (1 - mastery.P_SLIP) + (1 - p_mastery) * mastery.P_GUESS
    cal_ps = [s["p_correct"] for s in keyed.values() if s and s["calibrated"]]
    concept_mean = sum(cal_ps) / len(cal_ps)

    def score(q: dict) -> float:
        s = keyed.get(q["id"])
        offset = (s["p_correct"] - concept_mean) if (s and s["calibrated"]) else 0.0
        predicted = min(0.95, max(0.05, base + offset))
        return abs(predicted - 0.5)

    return sorted(questions, key=score)        # stable: ties keep authored order


if __name__ == "__main__":
    print(report())
