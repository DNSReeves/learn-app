"""Back navigation — the operator's 2026-08-05 report: "the back button does not
work in at least some modules", reported from card 1 of "Sugar in, insulin out".

TWO SEPARATE DEFECTS were behind it, and the tests below pin both.

1. THE ONE THAT WAS ACTUALLY BEING HIT. On the first card of a concept the pager's
   Back was rendered `disabled` — defensible, there is no previous card — except
   that `.btn.ghost` and `.btn:disabled` have EQUAL CSS specificity (0,2,0), so
   whichever is written last wins. With the disabled rule written first, a disabled
   ghost button kept its enabled appearance, and the only remaining cue was
   `cursor:default` — which does not exist on an iPad. A button that looks live and
   does nothing reads as broken. It is on the FIRST CARD OF EVERY CONCEPT, which is
   exactly why it looked like "some modules" and not others.

2. NO HISTORY MANAGEMENT AT ALL. The app had no pushState/popstate/hashchange
   anywhere, so the browser and iPad Back buttons could only leave the app. The
   server-side resume then dropped you at your last concept on reload *sometimes*,
   which is the other half of why the behaviour looked inconsistent.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "static" / "index.html"
SRC = APP.read_text(encoding="utf-8")


# ── defect 1: the first-card Back ────────────────────────────────────────────

def test_first_card_back_is_not_a_dead_disabled_button():
    m = re.search(r'<button[^>]*id="prev"[^>]*>', SRC)
    assert m, "the card pager's Back button is gone"
    assert "disabled" not in m.group(0), (
        "Back is disabled on card 0 again. It renders as a live-looking button on "
        "touch devices (see the specificity note below) and reads as broken.")


def test_first_card_back_navigates_out_of_the_concept():
    """It must DO something on card 0 — leaving the concept — rather than no-op."""
    h = re.search(r'\$\("#prev"\)\.onclick\s*=\s*\(\)\s*=>\s*\{(.+?)\};', SRC, re.S)
    assert h, "the #prev handler is gone"
    body = h.group(1)
    assert "cardIx===0" in body.replace(" ", ""), "card 0 must be handled explicitly"
    assert "viewDash()" in body, "card 0 Back must leave the concept"
    assert "cardIx--" in body, "later cards must still step back one card"


def test_disabled_buttons_look_disabled_whatever_variant_they_carry():
    """The specificity trap itself. `.btn:disabled` must come AFTER `.btn.ghost` /
    `.btn.warm`, or equal specificity silently restores the enabled look."""
    # anchor on the RULE, not on prose that mentions it (the page documents this
    # trap in a comment, and the first .index() hit was that comment)
    m = re.search(r'^\.btn:disabled[^{\n]*\{[^}]*\}', SRC, re.M)
    assert m, "the .btn:disabled rule is gone"
    dis = m.start()
    ghost = SRC.index(".btn.ghost{")
    warm = SRC.index(".btn.warm{")
    assert dis > ghost and dis > warm, (
        "`.btn:disabled` is written BEFORE the variant classes again — equal "
        "specificity means the variant wins and disabled buttons look enabled.")
    rule = m.group(0)
    assert "opacity" in rule, "needs a cue that does not depend on a cursor (iPad)"
    assert ".btn.ghost:disabled" in rule, "the ghost variant must be covered explicitly"


# ── defect 2: browser / device Back ──────────────────────────────────────────

def test_the_app_manages_browser_history():
    assert "pushState" in SRC, "no history entries -> browser Back leaves the app"
    assert 'addEventListener("popstate"' in SRC, "nothing responds to Back"


def test_navigation_records_history_for_every_view_that_matters():
    for fn in ("viewDash", "viewTopic", "renderCard", "startCheckpoint",
               "viewReview", "viewCertificate"):
        assert re.search(rf'\b{fn}\s*=\s*(?:async\s*)?function', SRC), (
            f"{fn} is no longer wrapped, so navigating to it leaves no history entry")


def test_view_concept_does_not_double_push():
    """viewConcept calls renderCard immediately; if BOTH pushed, one Back press
    would appear to do nothing — the exact complaint being fixed."""
    assert re.search(r'viewConcept\s*=\s*(?:async\s*)?function', SRC) is None, (
        "viewConcept must NOT be wrapped — renderCard owns the concept route")


def test_replaying_history_does_not_rewrite_it():
    mark = re.search(r'function _mark\(hash\)\{(.+?)\n\}', SRC, re.S)
    assert mark, "_mark is gone"
    body = mark.group(1)
    assert "_replaying" in body, "replaying history must not push new entries"
    assert "location.hash === hash" in body, (
        "a duplicate entry makes one Back press look like it did nothing")


def test_routing_never_breaks_a_lesson():
    """Routing is a convenience. It must never throw into a lesson."""
    mark = re.search(r'function _mark\(hash\)\{(.+?)\n\}', SRC, re.S).group(1)
    assert "try{" in mark and "catch" in mark


@pytest.mark.parametrize("hash_,want", [
    ("#/topics", {"view": "topics"}),
    ("#/t/diabetes", {"view": "topic", "tid": "diabetes"}),
    ("#/t/diabetes/c/betacell-insulin/0",
     {"view": "card", "tid": "diabetes", "cid": "betacell-insulin", "card": 0}),
    ("#/t/diabetes/c/betacell-insulin/3",
     {"view": "card", "tid": "diabetes", "cid": "betacell-insulin", "card": 3}),
    ("#/t/diabetes/c/betacell-insulin/check",
     {"view": "check", "tid": "diabetes", "cid": "betacell-insulin"}),
    ("#/t/diabetes/review", {"view": "review", "tid": "diabetes"}),
    ("#/t/diabetes/cert", {"view": "cert", "tid": "diabetes"}),
    ("", None),
    ("#/nonsense", None),
    ("#/t/x/c/y/abc", {"view": "card", "tid": "x", "cid": "y", "card": 0}),
    ("#/t/x/c/y/-5", {"view": "card", "tid": "x", "cid": "y", "card": 0}),
])
def test_parse_hash_cases(hash_, want):
    """Run the REAL parseHash out of the page under node — a routing table that is
    only eyeballed is a routing table that drops someone on the wrong screen."""
    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode != 0:
        pytest.skip("node not available")
    fn = re.search(r'function parseHash\(h\)\{.+?\n\}', SRC, re.S)
    assert fn, "parseHash is gone"
    js = (fn.group(0) + "\nconst location={hash:''};\n"
          f"console.log(JSON.stringify(parseHash({hash_!r})));")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json
    assert json.loads(out.stdout.strip()) == want


def test_the_page_javascript_parses():
    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode != 0:
        pytest.skip("node not available")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", SRC, re.S)
    js = "\n;\n".join(blocks)
    out = subprocess.run(["node", "--input-type=module", "--check"],
                         input=js, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:      # module mode is stricter; fall back to script mode
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js); path = f.name
        out = subprocess.run(["node", "--check", path],
                             capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"page JS does not parse:\n{out.stderr[:800]}"
