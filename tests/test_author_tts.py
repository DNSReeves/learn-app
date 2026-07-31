"""P5.20 author backend + P3.12 TTS pre-render pins.

Author invariants: role-gated, uploads land in topics_staged/ only, invalid
drafts report instead of staging. TTS invariants: speakable text extraction,
content-addressed skip, null-slot degradation, manifest shape. No network,
no real `say` (subprocess stubbed).
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import tts_render


# ── P3.12: narration text ────────────────────────────────────────────────────

def test_narration_flattens_markdown():
    t = tts_render.narration_text({
        "h": "Heads up",
        "md": "A **bold** [link](http://x) and `code`.\n\n```py\nskip=1\n```\n> quote"})
    assert t.startswith("Heads up. ")
    assert "**" not in t and "http" not in t and "skip=1" not in t
    assert "link" in t and "quote" in t


def test_narration_empty_card_yields_bare():
    assert tts_render.narration_text({"h": "", "md": ""}) == "."


# ── P3.12: render pipeline (say stubbed) ─────────────────────────────────────

@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_render, "AUDIO", tmp_path / "audio")
    monkeypatch.setattr(tts_render, "TOPICS", tmp_path / "topics")
    (tmp_path / "topics").mkdir()
    calls = []

    def fake_say(text, out, voice):
        calls.append(str(out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"m4a")
        return True
    monkeypatch.setattr(tts_render, "_synthesize", fake_say)
    return tmp_path, calls


PACK = {"id": "demo", "concepts": [
    {"id": "c1", "cards": [{"h": "One", "md": "first"}, {"h": "Two", "md": "second"}]}]}


def test_render_pack_content_addressed_skip(sandbox):
    tmp, calls = sandbox
    m1 = tts_render.render_pack(PACK)
    assert len(calls) == 2 and all(u for u in m1["c1"])
    calls.clear()
    m2 = tts_render.render_pack(PACK)                # unchanged → no synthesis
    assert calls == [] and m2 == m1
    # edited text → new hash → re-render of that card only
    edited = json.loads(json.dumps(PACK))
    edited["concepts"][0]["cards"][1]["md"] = "changed"
    tts_render.render_pack(edited)
    assert len(calls) == 1


def test_render_failure_degrades_to_null_slot(sandbox, monkeypatch):
    tmp, calls = sandbox
    monkeypatch.setattr(tts_render, "_synthesize", lambda *a: False)
    m = tts_render.render_pack(PACK)
    assert m["c1"] == [None, None]                   # player falls back, no raise


def test_render_all_manifest_and_prune(sandbox):
    tmp, calls = sandbox
    (tmp / "topics" / "demo.json").write_text(json.dumps(PACK))
    (tmp / "topics" / "_roadmap.json").write_text("{}")   # underscore files skipped
    m = tts_render.render_all()
    assert "demo" in m and len(m["demo"]["c1"]) == 2
    mf = json.loads((tmp / "audio" / "manifest.json").read_text())
    assert mf == m
    # an orphan from an edited card gets pruned
    orphan = tmp / "audio" / "demo" / "c1_c0_deadbeef.m4a"
    orphan.write_bytes(b"x")
    tts_render.render_all()
    assert not orphan.exists()


# ── P5.20: author role backend ───────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import app as amod
    importlib.reload(amod)
    monkeypatch.setattr(amod, "STAGED_DIR", str(tmp_path / "staged"))
    from fastapi.testclient import TestClient
    tc = TestClient(amod.app)
    dbmod.create_user("writer", "pw-secret-1")
    dbmod.create_user("reader", "pw-secret-2")
    dbmod.set_role("writer", "author")
    return (tc, amod, dbmod, tmp_path,
            {"Authorization": f"Bearer {dbmod.authenticate('writer', 'pw-secret-1')}"},
            {"Authorization": f"Bearer {dbmod.authenticate('reader', 'pw-secret-2')}"})


def _q(i):
    return {"id": f"q{i}", "type": "mcq", "prompt": f"Question {i}: which option is correct here?",
            "options": ["the right one", "a plausible miss", "another miss", "a third miss"],
            "answer": 0,
            "feedback": ["Correct — this is the defining property the card explained.",
                         "This confuses the mechanism with its side effect; re-read card one.",
                         "This reverses cause and effect — the card's example shows why.",
                         "This is true of the neighboring concept, not this one."]}


GOOD_PACK = {"id": "draft-pack", "title": "T", "concepts": [
    {"id": "c1", "title": "C", "summary": "s",
     "cards": [{"h": "h", "md": "m"}],
     "questions": [_q(1), _q(2), _q(3)]}]}


def test_role_in_login_payload(client):
    tc, *_ = client
    r = tc.post("/api/login", json={"username": "writer", "password": "pw-secret-1"}).json()
    assert r["role"] == "author"
    r2 = tc.post("/api/login", json={"username": "reader", "password": "pw-secret-2"}).json()
    assert r2["role"] == "learner"


def test_learner_is_403_from_author_endpoints(client):
    tc, amod, dbmod, tmp, _, reader = client
    for ep in ("validate", "upload"):
        r = tc.post(f"/api/author/pack/{ep}", headers=reader, json={"pack": GOOD_PACK})
        assert r.status_code == 403


def test_validate_reports_errors_without_staging(client):
    tc, amod, dbmod, tmp, writer, _ = client
    bad = {"id": "BAD SLUG!", "concepts": []}
    r = tc.post("/api/author/pack/validate", headers=writer, json={"pack": bad}).json()
    assert r["ok"] is False and r["errors"]
    r2 = tc.post("/api/author/pack/upload", headers=writer, json={"pack": bad}).json()
    assert r2["ok"] is False and r2["staged"] is None
    assert not (tmp / "staged").exists() or not list((tmp / "staged").iterdir())


def test_upload_stages_never_live(client):
    tc, amod, dbmod, tmp, writer, _ = client
    r = tc.post("/api/author/pack/upload", headers=writer, json={"pack": GOOD_PACK}).json()
    assert r["ok"] is True and r["staged"] == "topics_staged/draft-pack.json"
    staged = tmp / "staged" / "draft-pack.json"
    assert staged.exists()
    assert json.loads(staged.read_text())["id"] == "draft-pack"
    # the LIVE topics dir gained nothing
    live = Path(BASE) / "topics" / "draft-pack.json"
    assert not live.exists()
    assert r["overwrites_live"] is False


def test_grant_revoke_cli_roundtrip(client):
    tc, amod, dbmod, tmp, writer, reader = client
    assert dbmod.set_role("reader", "author") is True
    assert (dbmod.user_for_token(reader["Authorization"].split()[-1]) or {})["role"] == "author"
    assert dbmod.set_role("reader", "learner") is True
    assert dbmod.set_role("ghost", "author") is False
    with pytest.raises(ValueError):
        dbmod.set_role("reader", "admin")
