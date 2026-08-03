"""Resume + collapsed-on-login (operator, 2026-08-03: "Always default to the
collapsed view on login with a resume button to resume where the student left
off" + "show the resume button inactive if nothing has been started").

Backend half: /api/topic/{tid}/concept/{cid} records the user's last location
(server-side, so Resume works across shared devices); /api/topics returns it as
`resume` — validated against the LIVE packs (removed topic → null, never a dead
link) and null for a user who has not started anything (the button's disabled
state). Frontend half: source-level pins, same style as test_category_view.py.
"""
import importlib
import os

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


def _first(amod):
    tid, pack = next(iter(amod.load_topics().items()))
    return tid, pack


# ── backend ──────────────────────────────────────────────────────────────────

def test_resume_is_null_until_something_is_started(client):
    """A fresh account has no resume target — the button renders disabled on this."""
    tc, amod, dbmod, h = client
    assert tc.get("/api/topics", headers=h).json()["resume"] is None


def test_opening_a_concept_records_the_resume_target(client):
    tc, amod, dbmod, h = client
    tid, pack = _first(amod)
    cid = pack["concepts"][0]["id"]                      # idx 0 — never unlock-gated
    assert tc.get(f"/api/topic/{tid}/concept/{cid}", headers=h).status_code == 200
    r = tc.get("/api/topics", headers=h).json()["resume"]
    assert r == {"tid": tid, "cid": cid,
                 "topic_title": pack["title"],
                 "concept_title": pack["concepts"][0]["title"]}


def test_latest_open_wins(client):
    """One row per user, overwritten — resume points at the LAST place they were."""
    tc, amod, dbmod, h = client
    tid, pack = _first(amod)
    c0 = pack["concepts"][0]["id"]
    tc.get(f"/api/topic/{tid}/concept/{c0}", headers=h)
    # master c0 so c1 is reachable through the real gate, then open c1
    import time
    dbmod.upsert_state(1, tid, c0, mastered_at=time.time())
    c1 = pack["concepts"][1]["id"]
    assert tc.get(f"/api/topic/{tid}/concept/{c1}", headers=h).status_code == 200
    assert tc.get("/api/topics", headers=h).json()["resume"]["cid"] == c1


def test_forbidden_fetch_never_becomes_the_resume_target(client):
    """The recording sits AFTER the unlock gate — a 403'd deep link must not move
    (or create) the resume point."""
    tc, amod, dbmod, h = client
    tid, pack = _first(amod)
    locked = pack["concepts"][2]["id"]                   # c1 unmastered → c2 is locked
    assert tc.get(f"/api/topic/{tid}/concept/{locked}", headers=h).status_code == 403
    assert tc.get("/api/topics", headers=h).json()["resume"] is None


def test_resume_of_a_removed_topic_is_null_not_a_dead_link(client):
    tc, amod, dbmod, h = client
    dbmod.set_last_location(1, "ghost-topic", "ghost-concept")
    assert tc.get("/api/topics", headers=h).json()["resume"] is None


# ── frontend source pins (style of test_category_view.py) ────────────────────

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(BASE, "static", "index.html")).read()


def test_login_always_lands_collapsed_but_reloads_do_not_reset():
    src = _src()
    # the flag is set ONLY in acceptSession (login/register), consumed in viewDash
    assert src.count('learn_fresh_login') == 3   # set + get + remove — nowhere else
    assert 'sessionStorage.setItem("learn_fresh_login","1")' in src
    assert 'saveCatPrefs(d.user, {mode:"collapsed", open:[]})' in src
    assert 'sessionStorage.removeItem("learn_fresh_login")' in src


def test_resume_button_disabled_with_honest_title_when_nothing_started():
    src = _src()
    assert 'id="resume-btn"' in src
    assert '${d.resume?"":"disabled"}' in src
    assert "Nothing started yet" in src
    assert '.catbar button[disabled]' in src              # visibly inactive


def test_resume_button_wired_only_when_a_target_exists():
    src = _src()
    assert 'if(rb && d.resume) rb.onclick=()=>viewTopic(d.resume.tid, d.resume.cid);' in src
    # wired after the dash is painted, alongside the card wiring
    assert src.index('id="resume-btn"') < src.index("if(rb && d.resume)")
