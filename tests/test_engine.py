"""P5.19 (iss_a7a07ee3) — engine test suite: BKT gate, SM-2 scheduling, decay,
placement search, auth. Runs as part of the pre-deploy gate (predeploy.sh)
alongside pack validation. Hermetic — temp LEARN_DB, no network.
"""
import importlib
import os
import sys
import time

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import mastery


# ── gate logic ───────────────────────────────────────────────────────────────

def test_gate_needs_both_posterior_and_streak():
    assert mastery.is_mastered(0.96, 3)
    assert not mastery.is_mastered(0.96, 2)      # streak alone insufficient…
    assert not mastery.is_mastered(0.94, 5)      # …and so is posterior alone
    assert mastery.is_mastered(mastery.MASTERY_P, mastery.STREAK_GATE)  # boundary inclusive


def test_three_straight_from_cold_is_mastery_by_design():
    """CHARACTERIZATION: from P_INIT, exactly STREAK_GATE straight corrects
    clears BOTH conditions (posterior ≈0.976 ≥ 0.95 and streak 3). This is
    the engine's stated design point — guessing straight through is
    P_GUESS^3 = 0.8%, tighter than Bloom's ~90% gates. Fewer than 3 can
    never pass (streak), whatever the posterior does."""
    p = mastery.P_INIT
    for n in range(1, mastery.STREAK_GATE + 1):
        p = mastery.bkt_update(p, correct=True)
        if n < mastery.STREAK_GATE:
            assert not mastery.is_mastered(p, n)
    assert p >= mastery.MASTERY_P
    assert mastery.is_mastered(p, mastery.STREAK_GATE)


# ── SM-2-lite scheduling ────────────────────────────────────────────────────

def test_schedule_review_expansion_and_reset():
    i, e, due = mastery.schedule_review(0, 2.5, True)
    assert i == 1.0                               # first mastery → tomorrow
    i, e, due = mastery.schedule_review(i, e, True)
    assert i == 3.0                               # then 3 days
    i2, e2, _ = mastery.schedule_review(i, e, True)
    assert i2 == pytest.approx(3.0 * e) and e2 > e  # then multiplicative + ease growth
    # a miss collapses the interval and docks ease (floored at 1.3)
    i3, e3, _ = mastery.schedule_review(i2, 1.35, False)
    assert i3 == 0.5 and e3 == 1.3


def test_schedule_review_due_at_is_future():
    _, _, due = mastery.schedule_review(3.0, 2.5, True)
    assert due > time.time()


def test_ease_capped():
    e = 2.5
    for _ in range(30):
        _, e, _ = mastery.schedule_review(10, e, True)
    assert e <= 3.0


# ── display decay ────────────────────────────────────────────────────────────

def test_decay_shades_overdue_but_floors():
    now = time.time()
    assert mastery.decay_for_review(0.97, now + 86400) == 0.97      # not due → untouched
    ten_days_over = mastery.decay_for_review(0.97, now - 10 * 86400)
    assert 0.5 <= ten_days_over < 0.97
    assert mastery.decay_for_review(0.97, now - 1000 * 86400) == 0.5  # floor
    assert mastery.decay_for_review(0.9, None) == 0.9               # never scheduled


# ── placement binary search (pure fn via app import) ────────────────────────

@pytest.fixture()
def appmod(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import app as amod
    importlib.reload(amod)
    return amod


def _pack8():
    return {"concepts": [
        {"id": f"c{i}", "questions": [{"id": "q1", "answer": 0, "options": ["a", "b"]}]}
        for i in range(8)]}


def test_placement_search_probes_and_levels(appmod):
    pack = _pack8()
    # no history → first probe is the middle
    nxt, lvl = appmod._placement_search(pack, [])
    assert nxt == 3 and lvl is None
    # all probes correct → level lands past the last probed rung
    hist = []
    while True:
        nxt, lvl = appmod._placement_search(pack, hist)
        if nxt is None:
            break
        hist.append({"concept_id": f"c{nxt}", "question_id": "q1", "choice": 0})
    assert lvl == 8                                # aced everything → start past the end
    # all wrong → level 0
    hist = []
    while True:
        nxt, lvl = appmod._placement_search(pack, hist)
        if nxt is None:
            break
        hist.append({"concept_id": f"c{nxt}", "question_id": "q1", "choice": 1})
    assert lvl == 0


# ── auth (db layer) ─────────────────────────────────────────────────────────

@pytest.fixture()
def dbmod(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as d
    importlib.reload(d)
    d.init()
    return d


def test_auth_round_trip_and_wrong_password(dbmod):
    dbmod.create_user("dave", "hunter22")
    tok = dbmod.authenticate("dave", "hunter22")
    assert tok and dbmod.user_for_token(tok)["username"] == "dave"
    assert dbmod.authenticate("dave", "wrong") is None
    assert dbmod.authenticate("ghost", "hunter22") is None


def test_auth_validation_rules(dbmod):
    with pytest.raises(ValueError):
        dbmod.create_user("", "longenough")
    with pytest.raises(ValueError):
        dbmod.create_user("ok", "short")
    dbmod.create_user("Taken", "longenough")
    with pytest.raises(ValueError):
        dbmod.create_user("taken", "longenough")   # case-insensitive uniqueness


def test_logout_kills_token(dbmod):
    dbmod.create_user("t", "longenough")
    tok = dbmod.authenticate("t", "longenough")
    dbmod.logout(tok)
    assert dbmod.user_for_token(tok) is None


def test_expired_session_rejected(dbmod):
    dbmod.create_user("t", "longenough")
    tok = dbmod.authenticate("t", "longenough")
    with dbmod.conn() as c:
        # Sessions are stored as sha256(token) since 2026-08-08; a test reaching into
        # the table must address the STORED key, not the one the client holds.
        c.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                  (time.time() - 1, dbmod._tok_hash(tok)))
    assert dbmod.user_for_token(tok) is None


# ── P2.8 (iss_a617455d): placement v2 — gappy-knowledge sweep ────────────────

def _drive_v2(appmod, pack, answer_fn):
    """Run the stateless v2 protocol to completion; returns (level, gaps, hist)."""
    hist = []
    while True:
        nxt, lvl, gaps = appmod._placement_v2(pack, hist)
        if nxt is None:
            return lvl, gaps, hist
        hist.append({"concept_id": f"c{nxt}", "question_id": "q1",
                     "choice": answer_fn(nxt)})


def test_v2_monotone_learner_matches_v1_with_no_gaps(appmod):
    pack = _pack8()
    lvl, gaps, hist = _drive_v2(appmod, pack, lambda i: 0)       # all correct
    assert lvl == 8 and gaps == []
    # the sweep probed every below-frontier concept the search skipped (≤ cap)
    assert len(hist) <= appmod._placement_max_q(pack)


def test_v2_no_knowledge_has_no_sweep(appmod):
    pack = _pack8()
    lvl, gaps, hist = _drive_v2(appmod, pack, lambda i: 1)       # all wrong
    assert lvl == 0 and gaps == []
    assert len(hist) == 3                        # pure binary search, no sweep


def test_v2_detects_a_below_frontier_gap(appmod):
    pack = _pack8()
    # knows everything EXCEPT concept c1 (a hole the binary search never probes
    # on an all-correct path: probes are 3,5,6,7 then the sweep hits 0,1,2)
    lvl, gaps, hist = _drive_v2(appmod, pack, lambda i: 1 if i == 1 else 0)
    assert lvl == 8
    assert gaps == [1]


def test_v2_respects_the_question_budget(appmod):
    pack = {"concepts": [
        {"id": f"c{i}", "questions": [{"id": "q1", "answer": 0, "options": ["a", "b"]}]}
        for i in range(30)]}
    lvl, gaps, hist = _drive_v2(appmod, pack, lambda i: 0)
    assert lvl == 30
    assert len(hist) <= appmod._placement_max_q(pack)            # cap holds
    # un-swept below-frontier rungs default to trusted (v1 behavior), not gaps
    assert gaps == []


def test_v2_gap_application_uses_relearn_state(appmod, tmp_path):
    import db as dbmod
    pack = _pack8()
    # simulate the endpoint's apply loop for level=8, gaps=[1]
    with dbmod.conn() as c:
        c.execute("INSERT INTO users (username, pw_hash, pw_salt, created_at) "
                  "VALUES ('t', x'00', x'00', 1)")
        uid = c.execute("SELECT id FROM users").fetchone()[0]
    now = time.time()
    for i in range(8):
        if i == 1:
            dbmod.upsert_state(uid, "t", f"c{i}", p_mastery=0.30, relearn=1)
        else:
            dbmod.upsert_state(uid, "t", f"c{i}", p_mastery=0.90,
                               mastered_at=now, interval_d=0.5, due_at=now)
    s = dbmod.get_states(uid, "t")
    assert s["c1"]["relearn"] == 1 and not s["c1"]["mastered_at"]
    assert s["c2"]["mastered_at"]                # successors keep their placement
