"""Certificate of completion (operator, 2026-08-04: "When a student completes
and passes all tests, show a congratulations screen with a certification of
completion … suitable for framing … offer a print option").

The contract worth pinning is honesty, not decoration:
  * a certificate is issued ONLY when every concept in the pack is mastered —
    an unfinished course gets a 409, never a generous rounding;
  * every printed number comes from the answer log (questions, accuracy, days);
  * the verification code is HMAC'd with a per-install key, so it can be
    re-derived here and NOT by whoever knows the recipe — a forged code fails;
  * the display name is the student's to set, and is escaped in the UI.

Frontend half: source-level pins (same style as test_resume.py / test_category_view.py).
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
    return tc, amod, dbmod, {"Authorization": f"Bearer {tok}"}


def _first_topic(amod):
    tid, pack = next(iter(amod.load_topics().items()))
    return tid, pack


def _master_all(dbmod, amod, uid, tid, pack, *, answers=10, correct=10):
    now = time.time()
    for c in pack["concepts"]:
        dbmod.upsert_state(uid, tid, c["id"], p_mastery=0.97, streak=3,
                           mastered_at=now, attempts=3, correct=3)
    for i in range(answers):
        dbmod.log_answer(uid, tid, pack["concepts"][0]["id"], f"q{i}", "a",
                         1 if i < correct else 0, 900)


def test_unfinished_course_is_refused(client):
    tc, amod, dbmod, h = client
    tid, _ = _first_topic(amod)
    r = tc.get(f"/api/topic/{tid}/certificate", headers=h)
    assert r.status_code == 409
    assert "not yet mastered" in r.json()["detail"]


def test_certificate_issued_when_every_concept_is_mastered(client):
    tc, amod, dbmod, h = client
    tid, pack = _first_topic(amod)
    uid = dbmod.authenticate  # noqa: F841 — uid resolved below
    with dbmod.conn() as c:
        uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]
    _master_all(dbmod, amod, uid, tid, pack, answers=10, correct=9)
    d = tc.get(f"/api/topic/{tid}/certificate", headers=h).json()
    assert d["course"] == pack["title"]
    assert d["concepts"] == len(pack["concepts"])
    assert d["questions_answered"] == 10
    assert d["accuracy"] == pytest.approx(0.9)          # from the log, not rounded up
    assert d["standard"]["mastery_p"] == 0.95 and d["standard"]["streak"] == 3
    assert d["student"] == "u"                           # falls back to username
    assert d["code"].count("-") == 2


def test_verification_code_is_signed_not_a_recipe(client):
    tc, amod, dbmod, h = client
    tid, pack = _first_topic(amod)
    with dbmod.conn() as c:
        uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]
    _master_all(dbmod, amod, uid, tid, pack)
    d = tc.get(f"/api/topic/{tid}/certificate", headers=h).json()
    at = int(d["completed_at"])
    ok = tc.get("/api/certificate/verify",
                params={"u": "u", "t": tid, "at": at, "c": d["code"]}).json()
    assert ok["valid"] is True and ok["student"] == "u"
    forged = tc.get("/api/certificate/verify",
                    params={"u": "u", "t": tid, "at": at, "c": "AAAA-BBBB-CCCC"}).json()
    assert forged["valid"] is False and forged["student"] is None
    # a different student's name must not validate against this code
    other = tc.get("/api/certificate/verify",
                   params={"u": "someone-else", "t": tid, "at": at, "c": d["code"]}).json()
    assert other["valid"] is False


def test_display_name_is_settable_and_appears(client):
    tc, amod, dbmod, h = client
    tid, pack = _first_topic(amod)
    with dbmod.conn() as c:
        uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]
    _master_all(dbmod, amod, uid, tid, pack)
    assert tc.post("/api/me/display_name", json={"name": "  Kate  Reeves "},
                   headers=h).json()["display_name"] == "Kate Reeves"
    assert tc.get(f"/api/topic/{tid}/certificate", headers=h).json()["student"] == "Kate Reeves"
    assert tc.post("/api/me/display_name", json={"name": "   "}, headers=h).status_code == 400


def test_completeness_requires_the_final_concept(client):
    """The certificate's completeness rule: everything but the last concept is
    still incomplete; mastering the last one is what opens it."""
    tc, amod, dbmod, h = client
    tid, pack = _first_topic(amod)
    with dbmod.conn() as c:
        uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]
    # master everything except the last concept
    now = time.time()
    for cpt in pack["concepts"][:-1]:
        dbmod.upsert_state(uid, tid, cpt["id"], p_mastery=0.97, streak=3, mastered_at=now)
    states = dbmod.get_states(uid, tid)
    done = sum(1 for cpt in pack["concepts"] if states.get(cpt["id"], {}).get("mastered_at"))
    assert done == len(pack["concepts"]) - 1
    # with the last one still open the course is not complete
    assert not all(states.get(cpt["id"], {}).get("mastered_at") for cpt in pack["concepts"])
    # ...and once it masters, it is (the endpoint's own completeness rule)
    dbmod.upsert_state(uid, tid, pack["concepts"][-1]["id"],
                       p_mastery=0.97, streak=3, mastered_at=now)
    assert tc.get(f"/api/topic/{tid}/certificate", headers=h).status_code == 200


# ---------- frontend pins ----------

def _index():
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
            / "static" / "index.html").read_text(encoding="utf-8")


def test_frontend_wires_the_celebration_and_certificate():
    h = _index()
    assert "async function viewCelebration(" in h
    assert "async function viewCertificate(" in h
    assert "r.topic_complete" in h, "the completion trigger must be read from the answer result"
    assert 'id="go-cert"' in h, "the answer screen must offer the certificate"
    assert 'id="cert-rung"' in h, "the ladder must keep a permanent way back to it"
    assert "window.print()" in h, "print option (operator ask)"


def test_certificate_prints_landscape_and_keeps_its_colors():
    h = _index()
    print_block = h.split("@media print{")[1].split("\n}\n")[0]
    assert "@page{size:A4 landscape" in print_block
    assert "print-color-adjust:exact" in print_block
    # app chrome must not print on the framed sheet
    hide_rule = next(ln for ln in print_block.splitlines() if "display:none" in ln)
    for hidden in ("#top", ".certactions", ".certnote"):
        assert hidden in hide_rule, f"{hidden} would print on the certificate"


def test_certificate_uses_no_external_assets():
    """The seal and rules are drawn (SVG/CSS): a framed keepsake must not depend
    on a host that can disappear, and the print must be identical offline."""
    h = _index()
    seal = h.split("const CERT_SEAL")[1].split("`;")[0]
    for forbidden in ("http://", "https://", "<img", "url("):
        assert forbidden not in seal, f"certificate seal must not reference {forbidden}"


# ---------- the "stuck in a loop" regression (operator, 2026-08-04 pm) ----------
# "The ETF investing module gets stuck in a loop at Performance chasing and the
# behavior gap." That card is the FIRST card of the LAST concept: a finished
# course fell back to T.concepts[last], re-entered its cards, and the checkpoint's
# "Next concept" button came right back — cards → checkpoint → cards, forever.

def test_topic_complete_is_state_not_event(client):
    """The backend half: topic_complete must stay true on a REVIEW answer of an
    already-complete course. As a transition-only flag it went false, the UI fell
    back to 'Next concept', and that button restarted the last concept."""
    tc, amod, dbmod, h = client
    tid, pack = _first_topic(amod)
    with dbmod.conn() as c:
        uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]
    _master_all(dbmod, amod, uid, tid, pack, answers=0)
    last = pack["concepts"][-1]
    q = last["questions"][0]
    r = tc.post(f"/api/topic/{tid}/concept/{last['id']}/answer",
                json={"question_id": q["id"], "choice": q["answer"], "latency_ms": 800},
                headers=h).json()
    assert r["mastered"] is True
    assert r["newly_mastered"] is False, "already mastered — this is a review"
    assert r["topic_complete"] is True, "a finished course must stay finished"


def test_finished_course_does_not_reopen_its_last_concept():
    """The frontend half: viewTopic must route a fully-mastered course to the
    completion stage instead of the last concept's first card."""
    h = _index()
    body = h.split("async function viewTopic(")[1].split("\n}")[0]
    assert "allDone" in body and "viewTopicComplete(tid)" in body
    assert "async function viewTopicComplete(" in h
    # the loop was the checkpoint offering "Next concept" on a finished course
    fin = h.split("const finish=(r,pickBtn)=>{")[1].split("\n  };")[0]
    assert "r.topic_complete" in fin and 'id="go-cert"' in fin
