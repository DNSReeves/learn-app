"""Departments and the visible roadmap (operator, 2026-08-08: "create all departments and fill
out taxonomies now, and if there is no material now, add some kind of visual work in progress …
I want the student learning system to grow in breadth and depth over time").

The contract:
  * every department in the registry is registered in the dashboard order;
  * a department may exist with NO built courses (Biochemistry, Computer Science today);
  * planned entries are not packs — unopenable, uncountable, no effect on certificates;
  * the roadmap cannot advertise work that already shipped;
  * a work-in-progress card is not clickable.
"""
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLANNED = ROOT / "topics" / "_planned.json"


def _index():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _registry():
    return json.loads(PLANNED.read_text())["departments"]


def _packs():
    return {p.stem: json.loads(p.read_text())
            for p in sorted((ROOT / "topics").glob("*.json")) if not p.name.startswith("_")}


def test_every_department_is_registered_in_the_dashboard_order():
    """An unregistered category still renders, but falls to the end after 'More Topics' —
    so a new department would appear detached from the taxonomy it belongs to."""
    h = _index()
    order = h[h.index("const ORDER=["):h.index("];", h.index("const ORDER=["))]
    for dept in _registry():
        assert f'"{dept["category"]}"' in order, f'{dept["category"]} missing from ORDER'


def test_planned_files_are_not_loadable_as_packs():
    """The underscore prefix is the whole safety mechanism: nothing here can be opened,
    mastered, or counted toward a certificate."""
    import app
    packs = app.load_topics()
    assert "_planned" not in packs
    titles = {p["title"] for p in packs.values()}
    for dept in _registry():
        for item in dept["planned"]:
            assert item["title"] not in titles


def test_the_roadmap_cannot_advertise_shipped_work():
    """THE FAILURE MODE OF THE LAST ROADMAP. `_roadmap.json` still listed newtonian-physics,
    llms, ai-agents and the rest long after every one of them shipped. Filtering is mechanical
    here rather than a manual step someone remembers."""
    import app
    live = {p["title"].strip().lower() for p in app.load_topics().values()}
    for dept in app.load_planned():
        for item in dept["planned"]:
            assert item["title"].strip().lower() not in live, (
                f'{item["title"]} already exists as a pack but is still advertised as planned')


def test_a_department_may_exist_with_no_courses():
    """Biochemistry and Computer Science have nothing built today, and must still render —
    that is the point of declaring the taxonomy before the material."""
    import app
    built = {p.get("category") for p in app.load_topics().values()}
    declared = {d["category"] for d in _registry()}
    empty = declared - built
    assert empty, "expected at least one department with no built courses"
    h = _index()
    assert "g[x.category]=g[x.category]||[]" in h.replace(" ", ""), (
        "empty departments are not seeded into the grouping, so they cannot render")


def test_work_in_progress_cards_are_not_clickable():
    """WIP cards reuse .tcard for layout but have no topic id. A bare `.tcard` selector wires
    viewTopic(undefined) and sends the student to a course that does not exist."""
    h = _index()
    assert 'querySelectorAll(".tcard[data-t]")' in h, (
        "the topic click wiring is not scoped to cards with a target")
    pcard = h[h.index("const pcard="):h.index("const pcard=") + 400]
    assert "data-t" not in pcard, "a WIP card carries a topic id it cannot honour"
    assert "meterline" not in pcard, "a WIP card shows a progress meter for 0 concepts"


def test_planned_entries_are_honest_about_status():
    """This surface is intended to be public. 'In development' states an intention; 'coming
    soon' implies a date nobody has committed to."""
    h = _index()
    pcard = h[h.index("const pcard="):h.index("const pcard=") + 400]
    assert "In development" in pcard
    # Read the RENDERED card, not the file. The first version scanned the whole page and failed
    # on the comment that explains why we do not say "coming soon" — the fifth prose false
    # positive in this project, and the same lesson each time: assert on output, not on source.
    assert "coming soon" not in pcard.lower()


def test_every_planned_entry_says_what_it_will_cover():
    """A roadmap of bare titles is a wish list. The hint is what makes it informative."""
    for dept in _registry():
        for item in dept["planned"]:
            assert item.get("hint", "").strip(), f'{item["title"]} has no hint'
            assert len(item["hint"]) > 25, f'{item["title"]} hint is too thin to be useful'


def test_registry_survives_a_broken_file():
    """A malformed roadmap must never take down the dashboard."""
    import app
    orig = PLANNED.read_text()
    try:
        PLANNED.write_text("{ this is not json")
        assert app.load_planned() == []
    finally:
        PLANNED.write_text(orig)
