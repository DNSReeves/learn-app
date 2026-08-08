"""Category collapse/expand pins (operator, 2026-08-01: "as more and more topics are
added students will find it easier to navigate with less scrolling").

Source-level pins for the browser behavior verified live on 2026-08-01 (19 topics /
5 categories: collapse hid all 19 cards leaving 5 headers; opening one showed 2;
the mode + open set survived a reload; a header click from expanded jumped straight
to collapsed-with-that-one-open; card clicks still navigated; and with a due review
seeded, the roll-up rendered on the collapsed header).

The load-bearing one is the LAST: a student with reviews due must still see that
when the category is collapsed, or a tidier list quietly hides the work.
"""
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(BASE, "static", "index.html")).read()


def test_toggle_and_container_exist():
    assert 'id="cat-toggle"' in SRC and 'class="cats" id="cats"' in SRC
    assert "⊞ Expand all" in SRC and "⊟ Collapse to categories" in SRC


def test_headers_are_real_buttons_for_keyboard_and_screen_readers():
    assert '<button class="cat-head" type="button" aria-expanded="true">' in SRC
    assert 'h.setAttribute("aria-expanded"' in SRC
    assert ".cat-head:focus-visible" in SRC


def test_collapse_is_css_only_so_cards_stay_wired():
    """Cards are always RENDERED and only hidden — the existing .tcard click wiring
    (and any future per-card feature) must not depend on the view mode."""
    assert '.cats[data-mode="collapsed"] .cat:not([data-open="1"]) .cat-body{display:none}' in SRC
    i = SRC.index("wireCategoryView(d.user);")
    # `[data-t]` added 2026-08-08: work-in-progress cards reuse .tcard for layout but carry no
    # topic id, so the wiring must skip them or it calls viewTopic(undefined).
    j = SRC.index('main.querySelectorAll(".tcard[data-t]")')
    assert j > i, "card wiring must run after the view is painted"


def test_due_reviews_survive_collapsing():
    """The one signal that must not hide behind a collapsed header."""
    assert "cat-due" in SRC
    assert re.search(r"const due=g\[cat\]\.reduce\(\(n,t\)=>n\+\(t\.review_due\|\|0\),0\)", SRC)


def test_prefs_are_per_user_and_corruption_falls_back_to_old_behavior():
    """Shared device (Phase A): David and Kate keep their own view. A corrupt or
    unreadable value must degrade to the always-expanded original layout."""
    assert 'const catKey = u => `learn_cats:${u || "anon"}`' in SRC
    assert 'return {mode:"expanded", open:[]};' in SRC          # the catch branch
    assert 'localStorage.setItem(catKey(user)' in SRC
    # the token must NOT have moved to localStorage as a side effect
    assert 'sessionStorage.getItem("learn_token")' in SRC


def test_header_click_from_expanded_collapses_to_that_category():
    """The operator's flow: one click from anywhere gets the short list with your
    section open."""
    assert 'if(p.mode!=="collapsed"){ p.mode="collapsed"; p.open=[sec.dataset.cat]; paint(); return; }' in SRC
