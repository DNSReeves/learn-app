"""Category grouping and within-category ordering (operator, 2026-08-08).

WHY. Physiology is becoming a FAMILY of packs — a survey plus specialist depth modules
(Exercise, and Neuro/Cardiac planned). The grouping level for that already existed: the pack
`category`. What did not exist was ordering WITHIN a category — packs rendered in filename
order, which would have put the survey last among its own depth modules:

    cardiac-physiology, exercise-physiology, neurophysiology, physiology-biophysics
                                                              ^ the one to read first

`order` is an optional integer. Packs without one sort after ordered packs, alphabetically by
title, so every pre-existing category renders exactly as it did.
"""
import json
import pathlib

import pytest

TOPICS = pathlib.Path(__file__).resolve().parent.parent / "topics"


def _packs():
    return {p.stem: json.loads(p.read_text())
            for p in sorted(TOPICS.glob("*.json")) if not p.name.startswith("_")}


def _index():
    return (pathlib.Path(__file__).resolve().parent.parent
            / "static" / "index.html").read_text(encoding="utf-8")


def test_physiology_is_its_own_category():
    packs = _packs()
    fam = {k: v for k, v in packs.items() if v.get("category") == "Physiology"}
    assert "physiology-biophysics" in fam, "the survey is not in the Physiology category"
    assert "exercise-physiology" in fam, "the depth module is not in the Physiology category"


def test_the_survey_sorts_first_in_its_category():
    """The specific bug this ordering exists to prevent: filename order puts the foundation
    course after every specialist module that builds on it."""
    packs = _packs()
    fam = [v for v in packs.values() if v.get("category") == "Physiology"]
    ordered = sorted(fam, key=lambda p: (p.get("order", float("inf")), p.get("title", "")))
    assert ordered[0]["id"] == "physiology-biophysics", (
        f"the survey does not sort first: {[p['id'] for p in ordered]}")


def test_depth_modules_declare_the_survey_as_a_prerequisite():
    """The dashboard renders '· builds on <prereq>' from this, which is what makes the
    survey/depth relationship visible without any new UI."""
    packs = _packs()
    for pid, p in packs.items():
        if p.get("category") == "Physiology" and pid != "physiology-biophysics":
            assert "physiology-biophysics" in p.get("prereqs", []), (
                f"{pid} does not declare the foundation course as a prerequisite")


def test_category_is_registered_in_the_dashboard_order():
    """An unregistered category still renders, but falls to the end — so a new track would
    appear below Physics & Engineering rather than beside its siblings."""
    h = _index()
    order = h[h.index("const ORDER=["):h.index("const ORDER=[") + 260]
    assert '"Physiology"' in order, "Physiology is missing from the dashboard category order"
    assert order.index('"Health & Medicine"') < order.index('"Physiology"')


def test_ordering_is_applied_client_side():
    h = _index()
    assert "a.order??Infinity" in h.replace(" ", ""), "no within-category sort is applied"


def test_packs_without_an_order_keep_their_existing_position():
    """REGRESSION GUARD for every OTHER category, and it already earned its keep: the first
    implementation broke ties alphabetically by title, which moved "Trading Options & Implied
    Volatility" from 4th to 6th in Investing & Finance — a cosmetic change to a category nobody
    asked me to touch. Unordered packs must keep arrival order, so the comparator has to return
    0 for them and rely on Array.sort being stable."""
    h = _index()
    line = next(l for l in h.splitlines() if "a.order??Infinity" in l.replace(" ", ""))
    assert "localeCompare" not in line, (
        "unordered packs are being re-sorted by title — that reshuffles existing categories")
    packs = [p for p in _packs().values() if p.get("category") == "Investing & Finance"]
    assert packs and all("order" not in p for p in packs)


def test_survey_is_framed_as_the_foundation():
    p = _packs()["physiology-biophysics"]
    text = f"{p['title']} {p['tagline']} {p['description']}".lower()
    assert any(w in text for w in ("foundation", "survey")), (
        "the survey pack does not say it is the survey — which is the whole point of the split")


def test_the_life_science_categories_follow_the_academic_split():
    """OPERATOR, 2026-08-08: "in school at UAB Genetics was in the sub area of molecular biology."

    Genetics is not physiology and physiology does not contain it — they are siblings under the
    life sciences. The app has ONE grouping level, so that hierarchy is expressed as adjacent
    categories rather than nesting, with Health & Medicine left for the clinical/consumer packs.
    """
    packs = _packs()
    assert packs["genetics"]["category"] == "Molecular Biology"
    assert packs["physiology-biophysics"]["category"] == "Physiology"
    clinical = {k for k, v in packs.items() if v.get("category") == "Health & Medicine"}
    assert "genetics" not in clinical, "a basic-science pack is back in the clinical category"
    assert {"diabetes", "glp1-medicines"} <= clinical, "clinical packs moved unexpectedly"


def test_the_biology_categories_are_adjacent_in_the_dashboard():
    """Adjacency is what communicates the shared parent when there is no nesting level."""
    h = _index()
    order = h[h.index("const ORDER=["):h.index("const ORDER=[") + 300]
    i_phys, i_mol = order.index('"Physiology"'), order.index('"Molecular Biology"')
    i_fin = order.index('"Investing & Finance"')
    assert i_phys < i_mol < i_fin, "the life-science categories are not adjacent"
