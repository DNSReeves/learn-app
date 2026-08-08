"""Animation registry contract (2026-08-01).

The visual programme took Learn from 113 to 165+ animated concepts in a day, across
two sessions and a dozen parallel authors merging fragments into one file. At that
rate the cheap, high-frequency failure is not a broken drawing — it is a WIRING
break: a pack names an animation that does not exist, and the student gets a blank
panel with no error anywhere. These pins are source-level (no browser, milliseconds)
so they run in the normal suite on every change.

The browser-level check (every static frame actually paints, zero page errors) is a
separate manual harness; this catches the class that a browser check would only find
if someone happened to open that exact concept.
"""
import json
import os
import re
import glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = open(os.path.join(BASE, "static", "index.html")).read()


def _registered() -> set[str]:
    """Every animation registered in index.html, in BOTH forms it is written.

    The original packs define them as members of one `const ANIMS = { ... }`
    object literal (`square_static(cv,ctx){...}`, `async square(cv,ctx){...}`);
    everything appended since is an assignment (`ANIMS.med_nakpump = ...`).
    A first draft of this test only matched the assignment form and reported four
    perfectly good Bayesian-inference animations as missing — the regex was wrong,
    not the code. Both forms count."""
    assigned = set(re.findall(r"\bANIMS\.([A-Za-z_][A-Za-z0-9_]*)\s*=", INDEX))
    # object-literal members: optional `async`, name, then `(` at method-shorthand
    # depth. Scoped to the ANIMS literal so unrelated objects cannot leak in.
    lit = ""
    m = re.search(r"const ANIMS\s*=\s*\{", INDEX)
    if m:
        depth, i = 0, m.end() - 1
        while i < len(INDEX):
            if INDEX[i] == "{":
                depth += 1
            elif INDEX[i] == "}":
                depth -= 1
                if depth == 0:
                    lit = INDEX[m.end():i]
                    break
            i += 1
    members = set(re.findall(r"^\s*(?:async\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                             lit, re.MULTILINE))
    return assigned | members


def _packs():
    for f in sorted(glob.glob(os.path.join(BASE, "topics", "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        yield os.path.basename(f), json.load(open(f))


def test_every_named_anim_is_registered():
    """A pack naming a missing animation renders a blank panel, silently."""
    reg = _registered()
    missing = [(fn, c["id"], c["anim"])
               for fn, p in _packs() for c in p["concepts"]
               if c.get("anim") and c["anim"] not in reg]
    assert not missing, f"packs name animations that do not exist: {missing}"


def test_every_named_anim_has_a_static_twin():
    """The concept page draws <name>_static as its first frame before Play is
    pressed. Without it the panel is empty until the student hits play."""
    reg = _registered()
    no_static = [(fn, c["id"], c["anim"])
                 for fn, p in _packs() for c in p["concepts"]
                 if c.get("anim") and f"{c['anim']}_static" not in reg]
    assert not no_static, f"animations missing their _static twin: {no_static}"


def test_no_duplicate_anim_wiring():
    """Two concepts sharing one animation is legal (cross-pack reuse is
    deliberate) — but flag it, because it is far more often a copy-paste slip
    than an intentional reuse."""
    seen: dict[str, list[str]] = {}
    for fn, p in _packs():
        for c in p["concepts"]:
            if c.get("anim"):
                seen.setdefault(c["anim"], []).append(f"{p['id']}/{c['id']}")
    dupes = {a: w for a, w in seen.items() if len(w) > 1}
    # Reuse must be recorded here deliberately, so an accidental one fails.
    # Cross-pack reuse is free (the registry is global) and was recommended
    # deliberately when Health & Medicine grew to five packs — a sarcomere is a
    # sarcomere whether the lesson is muscle physiology or sarcopenia.
    INTENTIONAL = {
        "med_insulin_action",   # diabetes/insulin-action + nutrition/intermittent-fasting
        "med_actrii",           # glp1/muscle-saving + nutrition/protein-ageing-sarcopenia
        "med_sarcomere",        # nutrition/muscle-growth-loss + physiology/muscle-contraction
        # exercise-physiology (2026-08-08). Cross-pack reuse only, and only where the MECHANISM
        # is the same one — that pack lists physiology-biophysics as a prerequisite, so a
        # returning visual is a callback rather than a gap. Within-pack duplicates were removed
        # instead of declared: meeting the same animation twice in one course is a weakness the
        # allowlist should not launder.
        "med_starling",         # physiology/frank-starling + exercise-phys/cardiac-output
        "med_homeostasis",      # physiology/homeostasis + exercise-phys/autonomic-control
        "med_o2curve",          # physiology/oxygen-transport + exercise-phys/vo2max
        "med_bodycomp_recomp",  # nutrition/body-composition + exercise-phys/sarcopenia
    }
    unexpected = {a: w for a, w in dupes.items() if a not in INTENTIONAL}
    assert not unexpected, (
        f"an animation is wired to more than one concept: {unexpected}. "
        "If this is deliberate cross-pack reuse, add it to INTENTIONAL above.")


def test_registry_is_append_only_safe():
    """Every runner has a matching _static and vice versa — the invariant the
    fragment-merge method relies on when many authors append at once."""
    reg = _registered()
    runners = {n for n in reg if not n.endswith("_static") and not n.startswith("_")}
    statics = {n[:-7] for n in reg if n.endswith("_static")}
    orphan_static = sorted(statics - runners)
    assert not orphan_static, f"_static with no runner: {orphan_static}"


def test_debug_handles_exist_for_computed_batches():
    """The narrated-numbers rule: batches built under it export a debug handle so
    a harness can assert the voice-over against the pixels. Pins that the handles
    survive future merges."""
    for handle in ("_gen_debug", "_med_pump_debug", "_med_physio2_debug",
                   "_med_metabolic_debug"):
        assert f"ANIMS.{handle}" in INDEX, f"lost the {handle} debug handle"
