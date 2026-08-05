"""Spaced-review session (operator, 2026-08-04: "yes, wire a real review session").

Context: FSRS has scheduled reviews since P2.7, and `review_due` badges have been
rendered all along — but nothing in the UI ever called /api/topic/{tid}/review, so
the queue was a door with no handle. This wires it, and rewrites the queue itself:

  * ONE retrieval per due concept, not every question it owns (the old queue
    emitted all 3-5 — that is a re-test, not a review);
  * the served question ROTATES by attempt count, so successive cycles don't
    train recall of one item;
  * most-overdue first;
  * a cross-topic /api/review, because a per-topic queue is a chore you have to
    remember to go find;
  * answers post through the NORMAL answer route, so FSRS rescheduling and
    relearn-demotion behave exactly as they do in a checkpoint.

The correct answer is never in the payload.
"""
import importlib
import time

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import app as amod
    importlib.reload(amod)
    from fastapi.testclient import TestClient
    tc = TestClient(amod.app)
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.authenticate("u", "pw-secret-1")
    with dbmod.conn() as c:
        uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]
    return tc, amod, dbmod, {"Authorization": f"Bearer {tok}"}, uid


def _first_topic(amod):
    tid, pack = next(iter(amod.load_topics().items()))
    return tid, pack


def _make_due(dbmod, uid, tid, concept, *, overdue_days=2.0, attempts=0):
    dbmod.upsert_state(uid, tid, concept["id"], p_mastery=0.97, streak=3,
                       mastered_at=time.time() - 86400 * 30, attempts=attempts,
                       interval_d=5.0, due_at=time.time() - 86400 * overdue_days)


def test_nothing_due_returns_an_empty_queue(client):
    tc, amod, dbmod, h, uid = client
    tid, _ = _first_topic(amod)
    d = tc.get(f"/api/topic/{tid}/review", headers=h).json()
    assert d["due"] == 0 and d["queue"] == []


def test_one_retrieval_per_due_concept(client):
    """The rewrite: a due concept contributes ONE question, not all of them."""
    tc, amod, dbmod, h, uid = client
    tid, pack = _first_topic(amod)
    c0 = pack["concepts"][0]
    assert len(c0["questions"]) > 1, "fixture needs a multi-question concept"
    _make_due(dbmod, uid, tid, c0)
    d = tc.get(f"/api/topic/{tid}/review", headers=h).json()
    assert d["due"] == 1 and len(d["queue"]) == 1
    assert d["queue"][0]["concept_id"] == c0["id"]


def test_served_question_rotates_with_attempts(client):
    """Successive review cycles must not drill the same item."""
    tc, amod, dbmod, h, uid = client
    tid, pack = _first_topic(amod)
    c0 = pack["concepts"][0]
    n = len(c0["questions"])
    seen = []
    for a in range(n):
        _make_due(dbmod, uid, tid, c0, attempts=a)
        seen.append(tc.get(f"/api/topic/{tid}/review",
                           headers=h).json()["queue"][0]["question"]["id"])
    assert len(set(seen)) == n, f"expected {n} distinct questions across cycles, got {seen}"


def test_queue_is_most_overdue_first(client):
    tc, amod, dbmod, h, uid = client
    tid, pack = _first_topic(amod)
    a, b = pack["concepts"][0], pack["concepts"][1]
    _make_due(dbmod, uid, tid, a, overdue_days=1)
    _make_due(dbmod, uid, tid, b, overdue_days=9)
    q = tc.get(f"/api/topic/{tid}/review", headers=h).json()["queue"]
    assert [x["concept_id"] for x in q] == [b["id"], a["id"]]
    assert q[0]["overdue_days"] > q[1]["overdue_days"]


def test_queue_never_leaks_the_answer(client):
    tc, amod, dbmod, h, uid = client
    tid, pack = _first_topic(amod)
    _make_due(dbmod, uid, tid, pack["concepts"][0])
    item = tc.get(f"/api/topic/{tid}/review", headers=h).json()["queue"][0]
    assert set(item["question"]) == {"id", "type", "prompt", "options"}
    assert "answer" not in item["question"] and "feedback" not in item["question"]


def test_cross_topic_review_spans_topics(client):
    tc, amod, dbmod, h, uid = client
    packs = amod.load_topics()
    (t1, p1), (t2, p2) = list(packs.items())[:2]
    _make_due(dbmod, uid, t1, p1["concepts"][0])
    _make_due(dbmod, uid, t2, p2["concepts"][0])
    d = tc.get("/api/review", headers=h).json()
    assert d["due"] == 2
    assert {x["topic_id"] for x in d["queue"]} == {t1, t2}
    # ...while the per-topic queue stays scoped
    assert tc.get(f"/api/topic/{t1}/review", headers=h).json()["due"] == 1


def test_answering_a_review_reschedules_and_clears_the_queue(client):
    """The round trip: answering through the normal route pushes the next due
    date out, so the item leaves the queue."""
    tc, amod, dbmod, h, uid = client
    tid, pack = _first_topic(amod)
    c0 = pack["concepts"][0]
    _make_due(dbmod, uid, tid, c0)
    item = tc.get(f"/api/topic/{tid}/review", headers=h).json()["queue"][0]
    q = next(x for x in c0["questions"] if x["id"] == item["question"]["id"])
    before = dbmod.get_states(uid, tid)[c0["id"]]["due_at"]
    r = tc.post(f"/api/topic/{tid}/concept/{c0['id']}/answer",
                json={"question_id": q["id"], "choice": q["answer"], "latency_ms": 700},
                headers=h).json()
    assert r["correct"] is True and r["mastered"] is True
    after = dbmod.get_states(uid, tid)[c0["id"]]["due_at"]
    assert after > before, "a passed review must schedule the next one further out"
    assert tc.get(f"/api/topic/{tid}/review", headers=h).json()["due"] == 0


def test_concept_with_no_questions_is_skipped(client, monkeypatch):
    """A due concept the author left question-less must not produce an
    unanswerable card (it would dead-end the session). load_topics() re-reads
    from disk on every call, so the pack has to be patched at the seam."""
    tc, amod, dbmod, h, uid = client
    tid, pack = _first_topic(amod)
    c0 = pack["concepts"][0]
    _make_due(dbmod, uid, tid, c0)
    assert tc.get(f"/api/topic/{tid}/review", headers=h).json()["due"] == 1
    stripped = dict(pack, concepts=[dict(c0, questions=[])] + pack["concepts"][1:])
    monkeypatch.setattr(amod, "load_topics", lambda: {tid: stripped})
    assert tc.get(f"/api/topic/{tid}/review", headers=h).json()["due"] == 0


# ---------- frontend pins ----------

def _index():
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
            / "static" / "index.html").read_text(encoding="utf-8")


def test_frontend_has_a_review_session_with_entry_points():
    h = _index()
    for fn in ("async function viewReview(", "function renderReviewQ(", "function renderReviewDone("):
        assert fn in h, f"missing {fn}"
    assert 'id="review-btn"' in h, "dashboard needs a review entry"
    assert 'id="review-rung"' in h, "the ladder needs a review entry"
    assert "viewReview(null)" in h, "the dashboard session spans all topics"


def test_review_posts_through_the_normal_answer_route():
    """Not a private grading path: FSRS rescheduling and relearn demotion live in
    the answer endpoint, and a review must go through them."""
    h = _index()
    body = h.split("function renderReviewQ(")[1].split("\nfunction renderReviewDone")[0]
    assert "/concept/${it.concept_id}/answer" in body
    assert "r.demoted" in body, "a demotion must be shown, not silently swallowed"
    assert "r.ungraded" in body, "an ungraded free response must not count either way"
