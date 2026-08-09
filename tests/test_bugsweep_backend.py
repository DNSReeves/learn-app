"""Backend bug-sweep pins (BUGSWEEP_FINDINGS.md, 2026-07-31) — one test per fix.

Hermetic: temp LEARN_DB, no network, AI stubbed where relevant.
"""
import importlib
import json
import os
import sys
import threading
import time

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import grading
import validate_pack as vp


# ── fixtures ─────────────────────────────────────────────────────────────────

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
    return tc, amod, dbmod, tmp_path, {"Authorization": f"Bearer {tok}"}


def _first_topic(amod):
    tid, pack = next(iter(amod.load_topics().items()))
    return tid, pack


# ── BE-1 / BE-4 / BE-5: import merge ─────────────────────────────────────────

def test_be1_more_attempts_weaker_p_cannot_regress_mastery(client):
    tc, amod, dbmod, tmp, h = client
    now = time.time()
    dbmod.upsert_state(1, "t", "c1", p_mastery=0.92, attempts=4, mastered_at=now - 10)
    r = tc.post("/api/progress/import", headers=h, json={
        "format": "learn-progress/1",
        "concept_state": [{"topic_id": "t", "concept_id": "c1",
                           "attempts": 20, "p_mastery": 0.40}]}).json()
    assert r["imported"] == 0 and r["skipped"] == 1
    s = dbmod.get_states(1, "t")["c1"]
    assert s["p_mastery"] == pytest.approx(0.92) and s["mastered_at"]


def test_be1_stronger_on_both_dims_still_imports(client):
    tc, amod, dbmod, tmp, h = client
    dbmod.upsert_state(1, "t", "c1", p_mastery=0.5, attempts=3)
    r = tc.post("/api/progress/import", headers=h, json={
        "format": "learn-progress/1",
        "concept_state": [{"topic_id": "t", "concept_id": "c1",
                           "attempts": 9, "p_mastery": 0.8}]}).json()
    assert r["imported"] == 1
    assert dbmod.get_states(1, "t")["c1"]["p_mastery"] == pytest.approx(0.8)


def test_be4_forged_values_are_clamped(client):
    tc, amod, dbmod, tmp, h = client
    r = tc.post("/api/progress/import", headers=h, json={
        "format": "learn-progress/1",
        "concept_state": [{"topic_id": "t", "concept_id": "cx",
                           "p_mastery": 5.0, "attempts": 10, "correct": 999,
                           "mastered_at": time.time() + 9e6,
                           "difficulty": 99, "stability": -5}]}).json()
    assert r["imported"] == 1
    s = dbmod.get_states(1, "t")["cx"]
    assert s["p_mastery"] == 1.0                    # clamped
    assert s["correct"] <= s["attempts"]            # coherent
    assert s["mastered_at"] <= time.time() + 1      # future timestamp clamped to now
    assert s["difficulty"] == 10.0 and s["stability"] == 0.0


def test_be5_malformed_rows_skip_not_500(client):
    tc, amod, dbmod, tmp, h = client
    r = tc.post("/api/progress/import", headers=h, json={
        "format": "learn-progress/1",
        "concept_state": [None, 1, [], {"topic_id": "t"},
                          {"topic_id": "t", "concept_id": "ok", "p_mastery": "x"}],
        "placements": [None, {"level": 2}]})
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == 4 and body["imported"] == 1   # the coercible row lands
    assert dbmod.get_states(1, "t")["ok"]["p_mastery"] == 0.0  # bad value → default


# ── BE-2 / BE-6: the answer route ────────────────────────────────────────────

def test_be2_locked_concept_answers_are_403(client):
    tc, amod, dbmod, tmp, h = client
    tid, pack = _first_topic(amod)
    locked = pack["concepts"][1]
    q = locked["questions"][0]
    r = tc.post(f"/api/topic/{tid}/concept/{locked['id']}/answer", headers=h,
                json={"question_id": q["id"], "choice": 0})
    assert r.status_code == 403


def test_be6_bad_choice_is_400_not_500(client):
    tc, amod, dbmod, tmp, h = client
    tid, pack = _first_topic(amod)
    c0 = pack["concepts"][0]
    q = next(qq for qq in c0["questions"] if qq.get("type") != "free")
    for bad in ({"question_id": q["id"]},                       # missing
                {"question_id": q["id"], "choice": 99}):        # out of range
        r = tc.post(f"/api/topic/{tid}/concept/{c0['id']}/answer", headers=h, json=bad)
        assert r.status_code == 400, bad


# ── BE-12: one malformed pack must not kill the app ──────────────────────────

def test_be12_bad_pack_file_is_skipped(client, monkeypatch):
    tc, amod, dbmod, tmp, h = client
    tdir = tmp / "topics"
    tdir.mkdir()
    (tdir / "good.json").write_text(json.dumps(
        {"id": "good", "title": "G", "concepts": []}))
    (tdir / "bad.json").write_text("{ half written")
    (tdir / "notapack.json").write_text('{"foo": 1}')
    monkeypatch.setattr(amod, "TOPIC_DIR", str(tdir))
    packs = amod.load_topics()
    assert list(packs) == ["good"]


# ── BE-13 / BE-14: placement seeds ───────────────────────────────────────────

def test_be13_be14_placement_seeds_survive_one_miss(client):
    tc, amod, dbmod, tmp, h = client
    import mastery
    tid, pack = _first_topic(amod)
    # drive the v2 placement to completion, answering everything correctly
    hist = []
    while True:
        r = tc.post(f"/api/topic/{tid}/placement", headers=h,
                    json={"history": hist}).json()
        if r.get("done"):
            break
        qq = r["question"]
        concept = next(c for c in pack["concepts"] if c["id"] == qq["concept_id"])
        real_q = next(x for x in concept["questions"] if x["id"] == qq["id"])
        hist.append({"concept_id": qq["concept_id"], "question_id": qq["id"],
                     "choice": real_q["answer"]})
    assert r["level"] == len(pack["concepts"])
    s = dbmod.get_states(1, tid)[pack["concepts"][0]["id"]]
    assert s["p_mastery"] == pytest.approx(mastery.MASTERY_P)     # BE-13
    assert s["stability"] == pytest.approx(mastery.FSRS_W[2])     # BE-14
    # one review miss must NOT demote a placement-trusted concept
    assert mastery.bkt_update(s["p_mastery"], False) >= mastery.RELEARN_P


# ── BE-7: validator re-entrancy + upload containment ─────────────────────────

def test_be7_concurrent_validations_do_not_clobber():
    good = {"id": "ok-pack", "title": "T", "concepts": []}
    bad = {"id": "BAD SLUG", "concepts": []}
    results = {}
    def run(name, pack):
        for _ in range(30):
            results.setdefault(name, []).append(len(vp.check(pack)))
    t1 = threading.Thread(target=run, args=("good", good))
    t2 = threading.Thread(target=run, args=("bad", bad))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert all(n == 0 for n in results["good"])
    assert all(n >= 1 for n in results["bad"])


def test_be7_upload_rejects_non_slug_even_if_validator_said_ok(client, monkeypatch):
    tc, amod, dbmod, tmp, h = client
    dbmod.set_role("u", "author")
    monkeypatch.setattr(amod, "STAGED_DIR", str(tmp / "staged"))
    monkeypatch.setattr(amod, "_validate_pack_dict", lambda p: [])   # simulate the race
    r = tc.post("/api/author/pack/upload", headers=h,
                json={"pack": {"id": "../../evil", "title": "x", "concepts": []}})
    assert r.status_code == 400
    assert not (tmp / "evil.json").exists()


def test_be23_oversized_draft_is_413(client, monkeypatch):
    tc, amod, dbmod, tmp, h = client
    dbmod.set_role("u", "author")
    big = {"id": "big", "title": "b", "concepts": [{"id": f"c{i}"} for i in range(101)]}
    r = tc.post("/api/author/pack/upload", headers=h, json={"pack": big})
    assert r.status_code == 413


# ── BE-3 / 8 / 9 / 10 / 18 / 19 / 20: grading + validator schema ─────────────

def test_be20_scientific_notation():
    assert grading._first_number("about 3.0e8 m/s") == pytest.approx(3.0e8)


def test_be19_default_relative_tolerance():
    q = {"grading": {"kind": "numeric", "answer": 1 / 3},
         "feedback_correct": "yes!", "feedback_incorrect": "no"}
    ok, _ = grading.grade(q, "0.333")
    assert ok is True


def test_be9_string_answer_is_authoring_error_not_500():
    q = {"grading": {"kind": "numeric", "answer": "5"}}
    ok, fb = grading.grade(q, "5")
    assert ok is True or "authoring error" in fb    # "5" coerces cleanly
    q2 = {"grading": {"kind": "numeric", "answer": "five"}}
    ok2, fb2 = grading.grade(q2, "5")
    assert ok2 is False and "authoring error" in fb2


def test_be18_regex_is_anchored():
    q = {"grading": {"kind": "regex", "patterns": [r"no"]},
         "feedback_correct": "ok", "feedback_incorrect": "nope"}
    ok, _ = grading.grade(q, "I do not know")
    assert ok is False                               # unanchored search matched this
    ok2, _ = grading.grade(q, "No")
    assert ok2 is True


def test_be8_invalid_pattern_never_500s():
    q = {"grading": {"kind": "regex", "patterns": ["(", r"yes"]},
         "feedback_correct": "ok", "feedback_incorrect": "nope"}
    ok, _ = grading.grade(q, "yes")
    assert ok is True                                # bad pattern skipped, good one hit


def test_be3_learner_text_is_capped():
    q = {"grading": {"kind": "regex", "patterns": [r"(a+)+$"]},
         "feedback_correct": "ok", "feedback_incorrect": "nope"}
    t0 = time.time()
    grading.grade(q, "a" * 100000 + "!")
    assert time.time() - t0 < 2.0                    # capped input, no blowup


def test_be10_validator_requires_patterns_for_regex():
    pack = {"id": "p", "title": "t", "concepts": [{
        "id": "c1", "title": "c", "summary": "s",
        "cards": [{"h": "h", "md": "m"}],
        "questions": [
            {"id": "q1", "type": "free", "prompt": "p?",
             "grading": {"kind": "regex", "answer": "yes"},
             "feedback_correct": "long enough feedback here",
             "feedback_incorrect": "long enough feedback here"},
            {"id": "q2", "type": "free", "prompt": "p?",
             "grading": {"kind": "regex", "patterns": ["("]},
             "feedback_correct": "long enough feedback here",
             "feedback_incorrect": "long enough feedback here"},
            {"id": "q3", "type": "free", "prompt": "p?",
             "grading": {"kind": "numeric", "answer": "not-a-number"},
             "feedback_correct": "long enough feedback here",
             "feedback_incorrect": "long enough feedback here"}]}]}
    errors = vp.check(pack)
    text = "\n".join(errors)
    assert "non-empty 'patterns'" in text
    assert "does not compile" in text
    assert "numeric 'answer'" in text


# ── BE-11 / BE-15 / BE-17: packver archival ──────────────────────────────────

def _mkpack(tid, cids):
    return {"id": tid, "title": "t",
            "concepts": [{"id": c, "questions": []} for c in cids]}


@pytest.fixture()
def pv(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import packver as pvmod
    importlib.reload(pvmod)
    dbmod.create_user("pvuser", "pw-secret-1")      # FK target for user_id=1
    return dbmod, pvmod


def test_be11_dangling_rename_archives_instead_of_stranding(pv):
    dbmod, pvmod = pv
    pvmod.sync_pack(_mkpack("top", ["a", "b"]))
    dbmod.upsert_state(1, "top", "a", p_mastery=0.9, relearn=1)
    newpack = _mkpack("top", ["b"])
    newpack["migrations"] = {"renamed_concepts": {"a": "GHOST"}}   # target absent
    rep = pvmod.sync_pack(newpack)
    assert rep["archived"].get("a") == 1              # fell through to archival
    assert "a" not in dbmod.get_states(1, "top")      # nothing stranded live
    with dbmod.conn() as c:
        row = c.execute("SELECT relearn, reason FROM concept_state_archive").fetchone()
    assert row["relearn"] == 1                        # BE-15: flag survives archival


def test_be17_vanished_topic_archives_all_state(pv):
    dbmod, pvmod = pv
    pvmod.maybe_sync({"gone-topic": _mkpack("gone-topic", ["x"])})
    dbmod.upsert_state(1, "gone-topic", "x", p_mastery=0.8)
    reports = pvmod.maybe_sync({})                    # the file vanished
    assert any(r.get("topic_removed") for r in reports)
    assert dbmod.get_states(1, "gone-topic") == {}
    with dbmod.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM concept_state_archive "
                      "WHERE reason='topic_removed'").fetchone()[0]
    assert n == 1


# ── BE-24: constant-time login ───────────────────────────────────────────────

def test_be24_missing_user_still_burns_a_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    calls = []
    real = dbmod._hash
    monkeypatch.setattr(dbmod, "_hash", lambda pw, salt: calls.append(1) or real(pw, salt))
    assert dbmod.authenticate("ghost-user", "whatever-pass") is None
    assert len(calls) == 1                            # dummy hash on the missing branch


# ── BE-16 / BE-22: atomic answer write + interval snapshot ───────────────────

def test_be16_be22_answer_writes_in_one_txn_with_snapshot(client):
    tc, amod, dbmod, tmp, h = client
    tid, pack = _first_topic(amod)
    c0 = pack["concepts"][0]
    q = next(qq for qq in c0["questions"] if qq.get("type") != "free")
    dbmod.upsert_state(1, tid, c0["id"], interval_d=7.5)   # pretend a schedule exists
    r = tc.post(f"/api/topic/{tid}/concept/{c0['id']}/answer", headers=h,
                json={"question_id": q["id"], "choice": q["answer"]})
    assert r.status_code == 200 and r.json()["correct"] is True
    with dbmod.conn() as c:
        row = c.execute("SELECT interval_at FROM answers ORDER BY id DESC LIMIT 1").fetchone()
    assert row["interval_at"] == pytest.approx(7.5)        # BE-22 snapshot


# ── Phase A (2026-07-31): shared-device auth hardening ───────────────────────

def test_phase_a_sessions_are_12h_sliding(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.authenticate("u", "pw-secret-1")
    with dbmod.conn() as c:
        exp0 = c.execute("SELECT expires_at FROM sessions WHERE token=?", (dbmod._tok_hash(tok),)).fetchone()[0]
    assert exp0 - time.time() < 12.5 * 3600            # 12h, not 30d
    with dbmod.conn() as c:                            # simulate an old session
        # Sessions are stored as sha256(token) since 2026-08-08; a test reaching into
        # the table must address the STORED key, not the one the client holds.
        c.execute("UPDATE sessions SET expires_at=? WHERE token=?",
                  (time.time() + 60, dbmod._tok_hash(tok)))
    assert dbmod.user_for_token(tok)                   # still valid → renews
    with dbmod.conn() as c:
        exp1 = c.execute("SELECT expires_at FROM sessions WHERE token=?", (dbmod._tok_hash(tok),)).fetchone()[0]
    assert exp1 - time.time() > 11 * 3600              # slid forward


def test_phase_a_register_closed_without_invite(client, monkeypatch):
    tc, amod, dbmod, tmp, h = client                   # a user already exists
    monkeypatch.delenv("LEARN_INVITE_CODE", raising=False)
    r = tc.post("/api/register", json={"username": "kate", "password": "secret1"})
    assert r.status_code == 403                        # closed: users exist, no code


def test_phase_a_register_with_invite_code(client, monkeypatch):
    tc, amod, dbmod, tmp, h = client
    monkeypatch.setenv("LEARN_INVITE_CODE", "family-code-1")
    r = tc.post("/api/register", json={"username": "kate", "password": "secret1",
                                       "invite": "wrong"})
    assert r.status_code == 403
    r2 = tc.post("/api/register", json={"username": "kate", "password": "secret1",
                                        "invite": "family-code-1"})
    assert r2.status_code == 200 and r2.json()["token"]
    assert "kate" in dbmod.list_usernames()


def test_phase_a_bootstrap_first_user_from_lan(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    monkeypatch.delenv("LEARN_INVITE_CODE", raising=False)
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import app as amod
    importlib.reload(amod)
    from fastapi.testclient import TestClient
    tc = TestClient(amod.app)
    monkeypatch.setattr(amod, "_lan_client", lambda r: True)   # TestClient host is "testclient"
    r = tc.post("/api/register", json={"username": "first", "password": "secret1"})
    assert r.status_code == 200                        # zero-user bootstrap allowed
    r2 = tc.post("/api/register", json={"username": "second", "password": "secret1"})
    assert r2.status_code == 403                       # and only once


def test_phase_a_users_chooser_lan_only(client, monkeypatch):
    tc, amod, dbmod, tmp, h = client
    monkeypatch.setattr(amod, "_lan_client", lambda r: getattr(getattr(r, "client", None), "host", "") != "203.0.113.9")
    r = tc.get("/api/users")
    assert r.status_code == 200 and r.json()["users"] == ["u"]
    assert list(r.json().keys()) == ["users"]          # names only, nothing else
    # a public client sees nothing
    class FakeClient:  # request.client stand-in
        host = "203.0.113.9"
    class FakeReq:
        client = FakeClient()
    with pytest.raises(Exception):
        amod.users_chooser(FakeReq())


def test_chooser_filters_test_pattern_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    for u in ("bubba", "kate", "smoke_1784986989", "deploy-smoke",
              "e2e_run", "fr_1784987196"):
        dbmod.create_user(u, "pw-secret-1")
    assert dbmod.list_usernames() == ["bubba", "kate"]
