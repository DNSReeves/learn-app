"""P3.11 (iss_b98c7ee2): AI-generated supplementary practice pins.

Formative-only invariants: strict shape gate on model output, answer keys stay
server-side, grading never touches BKT/streak/gate, calibration excludes gen:
rows. AI calls are stubbed — no network.
"""
import importlib
import os
import sys
import time

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import ai


GOOD = [{"prompt": f"P{i}?", "options": ["a", "b", "c", "d"],
         "answer": 1, "feedback": ["w", "r", "w", "w"]} for i in range(3)]


def test_validate_accepts_good_and_stamps_ids():
    out = ai._validate_practice([dict(g) for g in GOOD])
    assert [o["id"] for o in out] == ["gen1", "gen2", "gen3"]
    assert all(o["generated"] and o["type"] == "mcq" for o in out)


@pytest.mark.parametrize("mutate", [
    lambda it: it.update(options=["a", "b", "c"]),          # 3 options
    lambda it: it.update(options=["a", "a", "b", "c"]),     # duplicate options
    lambda it: it.update(answer=4),                          # answer out of range
    lambda it: it.update(answer="1"),                        # non-int answer
    lambda it: it.update(feedback=["only-one"]),             # feedback arity
    lambda it: it.update(prompt="  "),                       # blank prompt
])
def test_validate_rejects_malformed(mutate):
    items = [dict(g) for g in GOOD]
    mutate(items[1])
    assert ai._validate_practice(items) is None


def test_validate_rejects_non_lists():
    assert ai._validate_practice(None) is None
    assert ai._validate_practice([]) is None
    assert ai._validate_practice("[]") is None


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
    dbmod.create_user("t", "pw-secret-1")
    tok = dbmod.authenticate("t", "pw-secret-1")
    return tc, amod, dbmod, {"Authorization": f"Bearer {tok}"}


def _first_topic_concept(amod):
    tid, pack = next(iter(amod.load_topics().items()))
    return tid, pack["concepts"][0]["id"]


def test_practice_unavailable_degrades_honestly(client, monkeypatch):
    tc, amod, dbmod, hdrs = client
    monkeypatch.setattr(amod.ai, "generate_practice", lambda *a, **k: None)
    tid, cid = _first_topic_concept(amod)
    r = tc.post(f"/api/topic/{tid}/concept/{cid}/practice", headers=hdrs)
    assert r.status_code == 200 and r.json()["available"] is False


def test_practice_flow_hides_answers_and_never_moves_state(client, monkeypatch):
    tc, amod, dbmod, hdrs = client
    monkeypatch.setattr(amod.ai, "generate_practice",
                        lambda *a, **k: ai._validate_practice([dict(g) for g in GOOD]))
    tid, cid = _first_topic_concept(amod)
    r = tc.post(f"/api/topic/{tid}/concept/{cid}/practice", headers=hdrs).json()
    assert r["available"] and r["formative_only"]
    assert all("answer" not in q and "feedback" not in q for q in r["questions"])

    before = dict(dbmod.get_states(1, tid).get(cid) or {})
    a = tc.post(f"/api/topic/{tid}/concept/{cid}/practice/answer", headers=hdrs,
                json={"question_id": "gen1", "choice": 1}).json()
    assert a["correct"] is True and a["formative_only"]
    a2 = tc.post(f"/api/topic/{tid}/concept/{cid}/practice/answer", headers=hdrs,
                 json={"question_id": "gen2", "choice": 0}).json()
    assert a2["correct"] is False and a2["feedback"] == "w"
    after = dict(dbmod.get_states(1, tid).get(cid) or {})
    assert before == after                      # BKT/streak/gate untouched

    # logged for analytics with the gen: prefix
    with dbmod.conn() as c:
        ids = [r[0] for r in c.execute("SELECT question_id FROM answers")]
    assert ids and all(i.startswith("gen:") for i in ids)


def test_practice_expiry_is_honest(client, monkeypatch):
    tc, amod, dbmod, hdrs = client
    tid, cid = _first_topic_concept(amod)
    r = tc.post(f"/api/topic/{tid}/concept/{cid}/practice/answer", headers=hdrs,
                json={"question_id": "gen9", "choice": 0}).json()
    assert r.get("expired") is True


def test_calibration_excludes_gen_rows(client):
    tc, amod, dbmod, hdrs = client
    import calibration
    tid, cid = _first_topic_concept(amod)
    dbmod.log_answer(1, tid, cid, "gen:gen1", 1, True, None)
    dbmod.log_answer(1, tid, cid, "q1", 1, True, None)
    stats = calibration.item_stats(os.environ["LEARN_DB"])
    keys = list(stats.keys()) if isinstance(stats, dict) else [s for s in stats]
    flat = str(keys)
    assert "gen:" not in flat and "q1" in flat
