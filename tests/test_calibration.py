"""P2.4 + P2.5 (2026-07-23) — item calibration + adaptive checkpoint ordering.

Hermetic: temp answers DB, no network. Defends:
  - classical stats: Laplace-smoothed p_correct, difficulty, MIN_N calibration gate
  - point-biserial rest-score discrimination, incl. the NEGATIVE flag (the
    miskeyed-item smell) and None on zero variance
  - the report acceptance: hardest-first ranking + negative-disc flagging
  - P2.5 ordering: fresh concepts keep AUTHORED order byte-identically; a
    mid-ability learner gets the informative (moderate) item first; ordering is
    deterministic (stable sort).
"""
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import calibration
import mastery


def _db(tmp_path, rows):
    p = str(tmp_path / "learn_test.db")
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE answers (id INTEGER PRIMARY KEY, user_id INTEGER,
        topic_id TEXT, concept_id TEXT, question_id TEXT, given TEXT,
        is_correct INTEGER, latency_ms INTEGER, at REAL)""")
    c.executemany(
        "INSERT INTO answers (user_id, topic_id, concept_id, question_id, is_correct, at)"
        " VALUES (?,?,?,?,?,0)", rows)
    c.commit(); c.close()
    return p


# ── P2.4 stats ───────────────────────────────────────────────────────────────

def test_smoothed_difficulty_and_calibration_gate(tmp_path):
    rows = [(1, "t", "c", "q1", 1), (1, "t", "c", "q1", 1), (1, "t", "c", "q1", 0),
            (1, "t", "c", "q2", 1)]                     # q2: single attempt
    stats = calibration.item_stats(_db(tmp_path, rows))
    q1 = stats[("t", "c", "q1")]
    assert q1["n"] == 3 and q1["calibrated"] is True
    assert q1["p_correct"] == round(3 / 5, 4)           # (2+1)/(3+2) — smoothed
    q2 = stats[("t", "c", "q2")]
    assert q2["calibrated"] is False and q2["discrimination"] is None
    assert q2["p_correct"] == round(2 / 3, 4), "1-for-1 must not pin to 1.0"


def test_negative_discrimination_flags_miskeyed_item(tmp_path):
    # strong performers (rest-score high) MISS q_bad; weak performer gets it right
    rows = [
        # user 1: aces everything else, misses q_bad
        (1, "t", "c", "q_bad", 0),
        (1, "t", "c", "q1", 1), (1, "t", "c", "q2", 1), (1, "t", "c", "q3", 1),
        # user 2: same shape
        (2, "t", "c", "q_bad", 0),
        (2, "t", "c", "q1", 1), (2, "t", "c", "q2", 1), (2, "t", "c", "q3", 1),
        # user 3: weak elsewhere, "gets" q_bad
        (3, "t", "c", "q_bad", 1),
        (3, "t", "c", "q1", 0), (3, "t", "c", "q2", 0), (3, "t", "c", "q3", 0),
    ]
    db = _db(tmp_path, rows)
    disc = calibration.item_stats(db)[("t", "c", "q_bad")]["discrimination"]
    assert disc is not None and disc < 0
    rep = calibration.report(db)
    assert "NEGATIVE-DISC" in rep


def test_discrimination_none_on_zero_variance(tmp_path):
    rows = [(1, "t", "c", "q1", 1), (2, "t", "c", "q1", 1), (3, "t", "c", "q1", 1),
            (1, "t", "c", "q2", 1), (2, "t", "c", "q2", 1), (3, "t", "c", "q2", 1)]
    stats = calibration.item_stats(_db(tmp_path, rows))
    assert stats[("t", "c", "q1")]["discrimination"] is None   # all-correct: no variance


def test_report_ranks_hardest_first(tmp_path):
    rows = ([(1, "t", "c", "easy", 1)] * 4 + [(1, "t", "c", "hard", 0)] * 4
            + [(1, "t", "c", "mid", 1), (1, "t", "c", "mid", 0)])
    rep = calibration.report(_db(tmp_path, rows))
    assert rep.index("t/c/hard") < rep.index("t/c/mid") < rep.index("t/c/easy")


# ── P2.5 ordering ────────────────────────────────────────────────────────────

_QS = [{"id": "q1"}, {"id": "q2"}, {"id": "q3"}]


def test_fresh_concept_keeps_authored_order(tmp_path):
    db = _db(tmp_path, [(1, "t", "c", "q1", 1)])        # n=1 → nothing calibrated
    out = calibration.order_questions(list(_QS), "t", "c", 0.5, db_path=db)
    assert [q["id"] for q in out] == ["q1", "q2", "q3"], "byte-identical fallback"


def test_informative_item_first_for_mid_learner(tmp_path):
    # q1 easy (4/4), q3 hard (0/4), q2 moderate (2/4) — a mid-ability learner's
    # most informative item is the moderate one (predicted ≈ 0.5)
    rows = ([(1, "t", "c", "q1", 1)] * 4 + [(1, "t", "c", "q3", 0)] * 4
            + [(1, "t", "c", "q2", 1), (1, "t", "c", "q2", 0),
               (1, "t", "c", "q2", 1), (1, "t", "c", "q2", 0)])
    db = _db(tmp_path, rows)
    out = calibration.order_questions(list(_QS), "t", "c", 0.5, db_path=db)
    assert out[0]["id"] == "q2", f"moderate item first, got {[q['id'] for q in out]}"


def test_ordering_is_deterministic(tmp_path):
    rows = [(1, "t", "c", "q1", 1)] * 3 + [(1, "t", "c", "q2", 0)] * 3
    db = _db(tmp_path, rows)
    a = calibration.order_questions(list(_QS), "t", "c", 0.7, db_path=db)
    b = calibration.order_questions(list(_QS), "t", "c", 0.7, db_path=db)
    assert [q["id"] for q in a] == [q["id"] for q in b]


def test_bkt_forecast_uses_engine_constants():
    """The base forecast must ride mastery's OWN slip/guess — a drifted copy here
    would silently decouple ordering from the engine."""
    src = open(os.path.join(BASE, "calibration.py")).read()
    assert "mastery.P_SLIP" in src and "mastery.P_GUESS" in src
