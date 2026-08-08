"""Course introductions (operator, 2026-08-08: "a broad introductory section for each module …
motivational and compelling … draw the student in").

The contract worth pinning is not the prose — it is that the doorway behaves:
  * every shipped pack HAS one, and it validates;
  * it is served by the topic API, and shown on a first visit;
  * it comes BEFORE placement, because asking "what do you already know?" is a reasonable
    question only once someone has decided they care;
  * it does not trap the student in a loop, which is the specific way this app has broken
    before (2026-08-04, "stuck at Performance chasing");
  * it stays reachable from the ladder forever, like the certificate;
  * its visual reuses the registered animation registry — no new asset path.
"""
import importlib
import json
import pathlib

import pytest

TOPICS = pathlib.Path(__file__).resolve().parent.parent / "topics"


def _packs():
    return [p for p in sorted(TOPICS.glob("*.json")) if not p.name.startswith("_")]


def _index():
    return (pathlib.Path(__file__).resolve().parent.parent
            / "static" / "index.html").read_text(encoding="utf-8")


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


# ---------- content ----------

@pytest.mark.parametrize("path", _packs(), ids=lambda p: p.stem)
def test_every_pack_has_a_well_formed_intro(path):
    """Coverage is the point of the ask — "each module", not "some modules"."""
    import validate_pack
    pack = json.loads(path.read_text())
    intro = pack.get("intro")
    assert intro, f"{path.stem} has no intro"
    validate_pack._tl.errs = []
    validate_pack._check_intro(pack)
    assert validate_pack._tl.errs == [], f"{path.stem}: {validate_pack._tl.errs}"


@pytest.mark.parametrize("path", _packs(), ids=lambda p: p.stem)
def test_intro_animation_is_registered(path):
    """A visual that does not exist renders as a blank box on the first screen a student sees."""
    import validate_pack
    anim = (json.loads(path.read_text()).get("intro") or {}).get("anim")
    if anim:
        assert anim in validate_pack._ANIMS, f"{path.stem}: '{anim}' is not registered"


@pytest.mark.parametrize("path", _packs(), ids=lambda p: p.stem)
def test_intro_is_not_a_restatement_of_the_tagline(path):
    """Cheap guard against a placeholder intro that technically validates. The hook has to be
    doing its own work, not echoing the one-line tagline already shown on the dashboard."""
    pack = json.loads(path.read_text())
    hook = pack["intro"]["hook"].strip().lower().rstrip(".")
    tag = (pack.get("tagline") or "").strip().lower().rstrip(".")
    assert hook != tag, f"{path.stem}: the hook just repeats the tagline"
    assert len(pack["intro"]["hook"]) > 40, f"{path.stem}: hook is too short to draw anyone in"


# ---------- API ----------

def test_topic_api_serves_the_intro_and_a_fresh_flag(client):
    tc, amod, dbmod, h = client
    tid = next(iter(amod.load_topics()))
    d = tc.get(f"/api/topic/{tid}", headers=h).json()
    assert d["intro"], "the topic payload carries no intro"
    assert d["fresh"] is True, "a brand-new student should be flagged fresh"
    assert {"hook", "body", "why"} <= set(d["intro"])


def test_fresh_turns_off_once_the_student_actually_starts(client):
    """`fresh` is derived from real progress, not a 'seen' flag — so it cannot drift out of
    sync with what the student has done."""
    tc, amod, dbmod, h = client
    tid, pack = next(iter(amod.load_topics().items()))
    with dbmod.conn() as c:
        uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()["id"]
    dbmod.log_answer(uid, tid, pack["concepts"][0]["id"], "q1", "a", 1, 900)
    assert tc.get(f"/api/topic/{tid}", headers=h).json()["fresh"] is False


# ---------- frontend wiring ----------

def test_frontend_renders_the_intro_stage():
    h = _index()
    assert "function viewIntro(" in h
    assert 'class="introhook"' in h and 'class="whygrid"' in h
    assert 'id="intro-rung"' in h, "the intro must stay reachable from the ladder"


def test_intro_precedes_placement():
    """Order is a deliberate product decision: a reason before a test."""
    h = _index()
    body = h[h.index("async function viewTopic("):]
    intro_at = body.index("viewIntro(tid)")
    place_at = body.index("viewPlacement(tid)")
    assert intro_at < place_at, "placement is being shown before the introduction"


def test_intro_shows_for_students_who_already_have_progress():
    """Operator, 2026-08-08: "i don't see an intro for Physiology and Biophysics" — they had 25
    answers logged there. Gating the auto-show on `fresh` hid the introduction on exactly the
    modules a student actually uses. The auto-show must NOT consult `fresh`."""
    h = _index()
    guard = h[h.index("if (T.intro"):h.index("if (T.intro") + 120]
    assert "T.fresh" not in guard, (
        "the intro is gated on first-visit-ever again — students with progress will never see it")


def test_intro_cannot_loop():
    """THE FAILURE THIS APP HAS ALREADY HAD ONCE. `fresh` stays true until the first answer is
    logged, so gating only on `fresh` sends a student who clicks through to placement straight
    back to the intro. Dismissal must be state, not a re-derived condition."""
    h = _index()
    assert "INTRO_SEEN" in h, "no dismissal state — the intro can re-trigger in a loop"
    assert "INTRO_SEEN.add(tid)" in h, "dismissal is never recorded"
    assert "!INTRO_SEEN.has(tid)" in h, "the auto-show does not consult the dismissal state"


def test_intro_text_is_escaped():
    """Pack-authored content rendered into a template string."""
    h = _index()
    stage = h[h.index("function viewIntro("):h.index("function viewIntro(") + 2200]
    for field in ("I.hook", "w.title", "w.text"):
        assert f"esc({field})" in stage, f"{field} is interpolated unescaped"


# ---------- Listen, visual, zoom (operator asks, 2026-08-08) ----------

def test_intro_listen_uses_the_house_voice_not_the_browser():
    """OPERATOR, 2026-08-08: "the listen button sounds robotic. it should the same voice system
    used in the other components."

    The first version called speakPaced — the browser's speechSynthesis, which is this app's
    FALLBACK, not its voice. Cards are pre-rendered Kokoro; animation lines go live through
    /api/anim_tts in the active house voice. The intro must use the same engine.
    """
    h = _index()
    fn = h[h.index("async function speakIntro("):h.index("async function speakIntro(") + 900]
    assert "_animTTSFetch(" in fn, "Listen does not call the Kokoro proxy"
    assert "_animPlay(" in fn, "Listen never plays the synthesized audio"
    stage = h[h.index("function viewIntro("):h.index("function viewIntro(") + 2600]
    assert "speakIntro(I, ip)" in stage, "the button is not wired to the house-voice path"
    assert "speakPaced(introPlainText" not in h, "the browser-voice version is still wired up"


def test_listen_falls_back_but_only_after_trying_the_engine():
    """Narration must never go silent because a service is down — but the fallback has to be
    second, or the robotic voice is what everyone hears."""
    h = _index()
    fn = h[h.index("async function speakIntro("):h.index("async function speakIntro(") + 900]
    assert fn.index("_animTTSFetch(") < fn.index("speakPaced("), "the fallback runs first"


def test_listen_reads_the_whole_doorway():
    """Hook, body, why-cards and closer — not just the hook."""
    h = _index()
    fn = h[h.index("function _introChunks("):h.index("function _introChunks(") + 700]
    for piece in ("I.hook", "I.body", "I.why", "I.closer"):
        assert piece in fn, f"Listen skips {piece}"


def test_listen_chunks_under_the_endpoint_limit():
    """/api/anim_tts truncates above _ANIM_TTS_MAX_CHARS (800). An intro is 1500-2500 chars, so
    an unchunked request would cut the student off mid-sentence, silently."""
    import app
    h = _index()
    fn = h[h.index("function _introChunks("):h.index("function _introChunks(") + 700]
    import re as _re
    limit = int(_re.search(r"limit=(\d+)", fn).group(1))
    assert limit < app._ANIM_TTS_MAX_CHARS, (
        f"chunk limit {limit} is not below the endpoint cap {app._ANIM_TTS_MAX_CHARS}")


def test_listen_unlocks_audio_inside_the_gesture():
    """Safari blocks play() if an await intervenes between the click and the call."""
    h = _index()
    stage = h[h.index("function viewIntro("):h.index("function viewIntro(") + 2600]
    click = stage[stage.index("ip.onclick"):stage.index("ip.onclick") + 420]
    assert "unlockAnimAudio()" in click
    assert click.index("unlockAnimAudio()") < click.index("speakIntro(")


def test_intro_reuses_the_real_player_markup():
    """THE BUG THIS PINS. The first version hand-rolled the canvas and omitted one CSS line,
    `.player canvas{background:#0F1A2C}`. clear() is a clearRect, so the canvas is TRANSPARENT
    and every animation drew into a see-through box — reported as "the intro visual aid in
    Physiology is broken". Reusing animHTML() is what stops that drifting apart again."""
    h = _index()
    stage = h[h.index("function viewIntro("):h.index("function viewIntro(") + 2600]
    assert "animHTML()" in stage, "the intro hand-rolls its player markup again"
    assert "<canvas" not in stage, "the intro declares its own canvas instead of reusing the player"


def test_the_player_canvas_still_has_an_opaque_background():
    """Every animation clears with clearRect and assumes the CSS supplies the dark field."""
    h = _index()
    rule = h[h.index(".player canvas{"):h.index(".player canvas{") + 160]
    assert "background:" in rule, "the canvas background is gone — animations will render transparent"


def test_play_zooms_the_intro_visual_and_restores_afterwards():
    h = _index()
    assert ".player.zoomed{" in h, "no zoomed state defined"
    mount = h[h.index("function mountAnim("):h.index("function mountAnim(") + 1800]
    assert 'classList.add("zoomed")' in mount, "play does not zoom"
    assert "unzoom()" in mount and "finally{" in mount, "zoom is never released"
    assert 'e.key==="Escape"' in mount, "no keyboard exit from the zoomed view"


def test_zoom_is_opt_in_so_concept_cards_are_unchanged():
    """Scope discipline: the ask was about the introduction. Existing card animations must
    behave exactly as before."""
    h = _index()
    assert "mountAnim(C.anim)" in h, "the concept-card call signature changed"
    assert "mountAnim(I.anim, {zoom:true})" in h, "the intro does not opt in to zoom"


# ---------- contrast (operator, 2026-08-08) ----------

def _lum(h):
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [(x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4) for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(fg, bg):
    a, b = sorted([_lum(fg), _lum(bg)], reverse=True)
    return (a + 0.05) / (b + 0.05)


def _css_rule(selector):
    h = _index()
    i = h.index(selector + "{")
    return h[i:h.index("}", i)]


def _theme():
    import re
    h = _index()
    root = h[h.index(":root{"):h.index(":root{") + 400]
    return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9A-Fa-f]{6})", root))


def _resolve(value, theme):
    import re
    m = re.search(r"var\((--[a-z-]+)", value)
    if m:
        return theme[m.group(1)]
    return re.search(r"(#[0-9A-Fa-f]{6})", value).group(1)


def test_why_card_text_meets_wcag_aa():
    """OPERATOR-REPORTED. The first version set `background:var(--card,#141B26)` — a token this
    app does not define — so the cards fell back to a DARK navy while the theme is light.
    Measured on what actually rendered: title 1.11:1 (invisible), body 3.18:1 (below AA).

    Computed, not asserted as a hex string: a colour swap that keeps the palette but breaks
    legibility should still fail this test.
    """
    theme = _theme()
    card = _css_rule(".whycard")
    bg = _resolve(card[card.index("background:"):card.index("background:") + 40], theme)
    title = _css_rule(".whycard h3")
    body = _css_rule(".whycard p")
    t_fg = _resolve(title[title.index("color:"):title.index("color:") + 40], theme)
    b_fg = _resolve(body[body.index("color:"):body.index("color:") + 40], theme)

    assert _ratio(t_fg, bg) >= 4.5, f"why-card title {t_fg} on {bg} = {_ratio(t_fg,bg):.2f}:1"
    assert _ratio(b_fg, bg) >= 4.5, f"why-card body {b_fg} on {bg} = {_ratio(b_fg,bg):.2f}:1"


def test_drop_cap_is_readable():
    """It is the first letter of the body copy, not decoration. Large text → 3:1 floor."""
    theme = _theme()
    rule = _css_rule(".introbody p:first-child::first-letter")
    fg = _resolve(rule[rule.index("color:"):rule.index("color:") + 40], theme)
    assert _ratio(fg, theme["--paper"]) >= 3.0, f"drop cap {fg} = {_ratio(fg, theme['--paper']):.2f}:1"


def test_intro_css_references_no_undefined_tokens():
    """THE ROOT CAUSE CLASS. `var(--card,#141B26)` looks deliberate in source and renders as a
    colour from a different palette. Any token the intro uses must actually exist."""
    import re
    h = _index()
    start = h.index("/* ---------- course introduction")
    block = h[start:h.index("/* ---------- completion + certificate")]
    # Strip CSS comments first: this file DOCUMENTS the bad tokens (--card, --warm) in prose
    # explaining the bug, and matching those would be a false positive on the explanation —
    # the same trap that has produced four bogus guards in this project.
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    root = h[h.index(":root{"):h.index(":root{") + 400]
    defined = set(re.findall(r"(--[a-z-]+)\s*:", root))     # ALL tokens, not only #hex ones
    used = set(re.findall(r"var\((--[a-z-]+)", block))
    missing = sorted(used - defined)
    assert not missing, f"intro CSS uses undefined tokens: {missing}"
