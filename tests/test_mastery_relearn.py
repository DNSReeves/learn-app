"""iss_67193f36 (BKT slip-invariant clamp) + iss_0824d5ad (P2.6 relearn state).

Hermetic — temp LEARN_DB per test; mastery math tested pure.
"""
import importlib
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import mastery


def _answer_key(tid, cid, qid):
    """Read the correct index straight from the pack file — probing choices
    through the API mutates BKT state and corrupts the test arrangement."""
    import json
    with open(os.path.join(BASE, "topics", f"{tid}.json")) as f:
        pack = json.load(f)
    c = next(c for c in pack["concepts"] if c["id"] == cid)
    q = next(q for q in c["questions"] if q["id"] == qid)
    return q["answer"]


# ── the slip-invariant clamp ────────────────────────────────────────────────

def test_wrong_answer_never_raises_posterior_prior_sweep():
    """The filed invariant, swept across the whole prior range — including the
    p<0.0526 region where the un-clamped update ROSE (0.03 → 0.054)."""
    p = 0.001
    while p < 0.999:
        assert mastery.bkt_update(p, correct=False) <= p + 1e-12, f"rose at p={p}"
        p += 0.007


def test_correct_answer_still_raises():
    for p in (0.05, 0.15, 0.5, 0.9):
        assert mastery.bkt_update(p, correct=True) > p


def test_gate_unaffected_by_clamp():
    # around the gate, a wrong answer drops well below 0.95 either way
    post = mastery.bkt_update(0.96, correct=False)
    assert post < mastery.MASTERY_P
    assert not mastery.is_mastered(post, 0)


# ── P2.6 relearn lifecycle (through the API layer) ──────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app), dbmod


def _auth(c, dbmod=None):
    # Phase A (2026-07-31): /api/register is invite-gated now — tests create
    # the account at the db layer and log in like a real client would.
    if dbmod is None:
        import db as dbmod
    dbmod.create_user("t", "secret1")
    tok = dbmod.authenticate("t", "secret1")
    return {"Authorization": f"Bearer {tok}"}


def _first_topic_concepts(c, H):
    tid = c.get("/api/topics", headers=H).json()["topics"][0]["id"]
    t = c.get(f"/api/topic/{tid}", headers=H).json()
    return tid, t["concepts"]


def test_review_failure_demotes_to_relearn_and_ladder_stays_open(client):
    c, dbmod = client
    H = _auth(c)
    tid, concepts = _first_topic_concepts(c, H)
    c1, c2 = concepts[0]["id"], concepts[1]["id"]
    uid = dbmod.user_for_token(H["Authorization"].split()[-1])["id"]

    # arrange: c1 mastered long ago (due for review), c2 mastered too
    now_mastered = {"p_mastery": 0.97, "streak": 3, "unlocked": 1,
                    "mastered_at": 1.0, "interval_d": 1.0, "due_at": 2.0}
    dbmod.upsert_state(uid, tid, c1, **now_mastered)
    dbmod.upsert_state(uid, tid, c2, **now_mastered)

    # fail c1's review twice — first slip only reschedules, second demotes
    pack = c.get(f"/api/topic/{tid}/concept/{c1}", headers=H).json()
    q = pack["questions"][0]
    wrong = (_answer_key(tid, c1, q["id"]) + 1) % len(q["options"])
    for _ in range(2):
        r = c.post(f"/api/topic/{tid}/concept/{c1}/answer", headers=H,
                   json={"question_id": q["id"], "choice": wrong, "latency_ms": 1})
        out = r.json()
    assert out["demoted"] is True
    assert out["mastered"] is False

    st = dbmod.get_states(uid, tid)[c1]
    assert st["mastered_at"] is None and st["relearn"] == 1 and st["streak"] == 0

    # the ladder stays traversable: c2 still accessible (prev in relearn)
    r2 = c.get(f"/api/topic/{tid}/concept/{c2}", headers=H)
    assert r2.status_code == 200
    # and the topic view marks c1 relearn + keeps c2 unlocked
    t = c.get(f"/api/topic/{tid}", headers=H).json()
    by_id = {x["id"]: x for x in t["concepts"]}
    assert by_id[c1]["relearn"] is True and by_id[c1]["mastered"] is False
    assert by_id[c2]["unlocked"] is True


def test_remastery_clears_relearn(client):
    c, dbmod = client
    H = _auth(c)
    tid, concepts = _first_topic_concepts(c, H)
    c1 = concepts[0]["id"]
    uid = dbmod.user_for_token(H["Authorization"].split()[-1])["id"]
    dbmod.upsert_state(uid, tid, c1, p_mastery=0.94, streak=2, unlocked=1,
                       relearn=1, mastered_at=None)
    pack = c.get(f"/api/topic/{tid}/concept/{c1}", headers=H).json()
    q = pack["questions"][0]
    right = _answer_key(tid, c1, q["id"])
    r = c.post(f"/api/topic/{tid}/concept/{c1}/answer", headers=H,
               json={"question_id": q["id"], "choice": right, "latency_ms": 1})
    out = r.json()
    assert out["mastered"] is True and out["newly_mastered"] is True
    st = dbmod.get_states(uid, tid)[c1]
    assert st["relearn"] == 0 and st["mastered_at"] is not None
