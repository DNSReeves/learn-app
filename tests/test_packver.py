"""P5.17 pack versioning + migrations (iss_8d424360) — hermetic tests.

Run from learn_app/:  <python-with-pytest> -m pytest tests/test_packver.py
Uses a temp LEARN_DB; never touches the live learn.db.
"""
import importlib
import os
import sys
import tempfile

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh db module bound to a temp LEARN_DB + fresh packver cache."""
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn_test.db"))
    import db
    importlib.reload(db)
    db.init()
    import packver
    importlib.reload(packver)
    with db.conn() as c:   # concept_state FKs users(id) — seed two test users
        for uid in (1, 2):
            c.execute("INSERT INTO users (id, username, pw_salt, pw_hash, created_at)"
                      " VALUES (?, ?, x'00', x'00', 0)", (uid, f"u{uid}"))
    return db, packver


def _pack(tid="calc", concepts=("c01", "c02"), migrations=None, salt=""):
    p = {
        "id": tid, "title": "T", "concepts": [
            {"id": cid, "title": cid, "summary": "s" + salt,
             "cards": [{"h": "h", "md": "m"}],
             "questions": [{"id": f"q{n}", "type": "mc", "prompt": "p",
                            "options": ["a", "b"], "answer": 0,
                            "feedback": ["because reasons here", "because reasons here"]}
                           for n in (1, 2, 3)]}
            for cid in concepts]}
    if migrations:
        p["migrations"] = migrations
    return p


def _seed_state(db, user_id, tid, cid, p_mastery=0.9):
    db.upsert_state(user_id, tid, cid, p_mastery=p_mastery, attempts=7)


def _states(db, tid):
    with db.conn() as c:
        return {(r["user_id"], r["concept_id"]): dict(r) for r in
                c.execute("SELECT * FROM concept_state WHERE topic_id=?", (tid,))}


def _archived(db, tid):
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM concept_state_archive WHERE topic_id=?", (tid,))]


def _versions(db, tid):
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pack_versions WHERE topic_id=? ORDER BY version", (tid,))]


def test_first_sync_registers_v1_no_state_touch(env):
    db, pv = env
    r = pv.sync_pack(_pack())
    assert r["version"] == 1 and not r["migrated"] and not r["archived"]
    assert len(_versions(db, "calc")) == 1


def test_unchanged_pack_is_noop(env):
    db, pv = env
    p = _pack()
    assert pv.sync_pack(p)["version"] == 1
    assert pv.sync_pack(p) is None
    assert len(_versions(db, "calc")) == 1


def test_maybe_sync_in_memory_throttle(env):
    db, pv = env
    p = _pack()
    assert len(pv.maybe_sync({"calc": p})) == 1
    assert pv.maybe_sync({"calc": p}) == []          # cached — no DB round trip
    p2 = _pack(salt="x")                             # content change → resync
    assert len(pv.maybe_sync({"calc": p2})) == 1
    assert _versions(db, "calc")[-1]["version"] == 2


def test_removed_concept_archives_state(env):
    db, pv = env
    pv.sync_pack(_pack(concepts=("c01", "c02")))
    _seed_state(db, 1, "calc", "c01")
    _seed_state(db, 1, "calc", "c02")
    r = pv.sync_pack(_pack(concepts=("c01",)))
    assert r["archived"] == {"c02": 1}
    live = _states(db, "calc")
    assert (1, "c02") not in live and (1, "c01") in live
    arch = _archived(db, "calc")
    assert len(arch) == 1 and arch[0]["reason"] == "concept_removed"
    assert arch[0]["p_mastery"] == pytest.approx(0.9)


def test_declared_rename_migrates_state(env):
    db, pv = env
    pv.sync_pack(_pack(concepts=("c01", "c02")))
    _seed_state(db, 1, "calc", "c01", p_mastery=0.77)
    _seed_state(db, 2, "calc", "c01", p_mastery=0.55)
    r = pv.sync_pack(_pack(concepts=("c01-limits", "c02"),
                           migrations={"renamed_concepts": {"c01": "c01-limits"}}))
    assert r["migrated"] == {"c01": {"to": "c01-limits", "rows": 2}}
    assert not r["archived"] and not r["collisions"]
    live = _states(db, "calc")
    assert live[(1, "c01-limits")]["p_mastery"] == pytest.approx(0.77)
    assert live[(2, "c01-limits")]["p_mastery"] == pytest.approx(0.55)
    assert (1, "c01") not in live


def test_rename_collision_archives_not_clobbers(env):
    db, pv = env
    pv.sync_pack(_pack(concepts=("c01", "c02")))
    _seed_state(db, 1, "calc", "c01", p_mastery=0.4)   # old id
    _seed_state(db, 1, "calc", "c02", p_mastery=0.9)   # user already has target
    r = pv.sync_pack(_pack(concepts=("c02",),
                           migrations={"renamed_concepts": {"c01": "c02"}}))
    live = _states(db, "calc")
    assert live[(1, "c02")]["p_mastery"] == pytest.approx(0.9)   # untouched
    assert r["collisions"] == {"c01": {"to": "c02", "archived": 1}}
    arch = _archived(db, "calc")
    assert len(arch) == 1 and arch[0]["reason"].startswith("rename_collision")


def test_question_id_churn_registers_version_but_keeps_state(env):
    db, pv = env
    p = _pack()
    pv.sync_pack(p)
    _seed_state(db, 1, "calc", "c01")
    p2 = _pack()
    p2["concepts"][0]["questions"][0]["id"] = "q1-renamed"
    r = pv.sync_pack(p2)
    assert r["version"] == 2 and not r["archived"] and not r["migrated"]
    assert (1, "c01") in _states(db, "calc")


def test_validator_accepts_and_rejects_migrations():
    sys.path.insert(0, BASE)
    import validate_pack as vp
    vp.errs.clear()
    good = _pack(concepts=("c02",), migrations={"renamed_concepts": {"c01": "c02"}})
    vp.check(good)
    assert vp.errs == []
    vp.errs.clear()
    bad = _pack(concepts=("c02",), migrations={
        "renamed_concepts": {"c01": "nope"},          # target missing
        "typo_key": {}})
    bad["migrations"]["renamed_concepts"]["c02"] = "c02"  # source still live
    vp.check(bad)
    text = "\n".join(vp.errs)
    assert "target 'nope'" in text and "still exists" in text and "unknown key" in text
