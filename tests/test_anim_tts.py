"""ANIM-TTS pins (2026-07-31, operator: "the new voice system did not make it to the
animation panels").

/api/anim_tts proxies the shared Kokoro engine for DYNAMIC anim narration (card audio is
pre-rendered; anim lines interpolate scene numbers, so they can't be). Invariants pinned:
auth required · engine bytes cached content-addressed per voice (second call never hits
the engine) · engine-down → 503 (the client's cue to fall back to the paced browser
voice, never silence) · stub-sized renders are never cached · text is capped. The engine
call is stubbed — no network, no Kokoro.
"""
import importlib
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

FAKE_M4A = b"\x00" * 300 + b"fake-m4a-payload" * 20      # > the 200-byte stub floor


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import app as amod
    importlib.reload(amod)
    # isolate the disk cache
    amod._ANIM_TTS_CACHE = str(tmp_path / "anim_cache")
    from fastapi.testclient import TestClient
    tc = TestClient(amod.app)
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.authenticate("u", "pw-secret-1")
    return tc, amod, {"Authorization": f"Bearer {tok}"}


def _stub_engine(monkeypatch, payload=FAKE_M4A, fail=False, calls=None):
    import urllib.request

    class _Resp:
        def __init__(self, data): self._d = data
        def read(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        if calls is not None:
            calls.append(req)
        if fail:
            raise OSError("connection refused")
        return _Resp(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_auth_required(client):
    tc, _amod, _h = client
    r = tc.post("/api/anim_tts", json={"text": "hello"})
    assert r.status_code == 401


def test_synthesizes_and_caches(client, monkeypatch):
    tc, amod, h = client
    calls = []
    _stub_engine(monkeypatch, calls=calls)
    r1 = tc.post("/api/anim_tts", json={"text": "Ten trades, minus 21 percent."}, headers=h)
    assert r1.status_code == 200 and r1.content == FAKE_M4A
    assert r1.headers["content-type"].startswith("audio/mp4")
    assert len(calls) == 1
    # second call: served from the disk cache, engine NOT hit again
    r2 = tc.post("/api/anim_tts", json={"text": "Ten trades, minus 21 percent."}, headers=h)
    assert r2.status_code == 200 and r2.content == FAKE_M4A
    assert len(calls) == 1, "cache miss — the engine was re-hit for identical text"


def test_engine_down_is_503_not_silence(client, monkeypatch):
    tc, _amod, h = client
    _stub_engine(monkeypatch, fail=True)
    r = tc.post("/api/anim_tts", json={"text": "narrate me"}, headers=h)
    assert r.status_code == 503                      # the client's fall-back cue


def test_stub_render_never_cached(client, monkeypatch):
    tc, amod, h = client
    _stub_engine(monkeypatch, payload=b"tiny")       # < 200 bytes = a stub, not audio
    r = tc.post("/api/anim_tts", json={"text": "x y z"}, headers=h)
    assert r.status_code == 503
    voice_dir = os.path.join(amod._ANIM_TTS_CACHE, amod._active_voice())
    assert not os.path.isdir(voice_dir) or not os.listdir(voice_dir)


def test_empty_text_400_and_long_text_capped(client, monkeypatch):
    tc, amod, h = client
    assert tc.post("/api/anim_tts", json={"text": "   "}, headers=h).status_code == 400
    seen = {}
    import urllib.request

    class _Resp:
        def read(self): return FAKE_M4A
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def spy(req, timeout=0):
        import json as _j
        seen["text"] = _j.loads(req.data.decode())["text"]
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    tc.post("/api/anim_tts", json={"text": "word " * 500}, headers=h)
    assert len(seen["text"]) <= amod._ANIM_TTS_MAX_CHARS


def test_frontend_wiring_pins():
    """Source pins: narrate() tries Kokoro FIRST, falls back to speakPaced; the Play click
    unlocks the shared element in-gesture; cancelVoice stops the Kokoro element too."""
    src = open(os.path.join(BASE, "static", "index.html")).read()
    i = src.index("async function narrate")
    body = src[i:i + 700]
    assert "_animTTSFetch" in body and "speakPaced" in body
    assert body.index("_animTTSFetch") < body.index("speakPaced"), "Kokoro must be tried first"
    assert "unlockAnimAudio();" in src.split("playBtn.onclick")[1][:300], \
        "the Play click must unlock the audio element in-gesture (Safari)"
    cv = src[src.index("function cancelVoice"):src.index("function cancelVoice") + 300]
    assert "_animCancel" in cv, "cancelVoice must also stop the anim audio element"
    assert '"/api/anim_tts"' in src
