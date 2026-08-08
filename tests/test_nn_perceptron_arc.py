"""The perceptron arc in Neural Networks (operator, 2026-08-08).

"start with the analogue of a biologic action potential with dendrites, excitatory and repressor
impulses … but the analogy is mostly mathematic and not at all the same as the biological …
show a sketch of a real nerve and a perceptron next to each other … show what a perceptron can
do and what it cannot … add another perceptron … in the intro give some brief history."

The pack previously opened with "Strip the biology away" — it dismissed the analogy instead of
teaching it, and never mentioned that one unit provably cannot compute XOR, which is the reason
layers exist at all.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACK = json.loads((ROOT / "topics" / "neural-networks.json").read_text())


def _c(cid):
    return next(c for c in PACK["concepts"] if c["id"] == cid)


def _index():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_the_pack_opens_with_the_biological_comparison():
    assert PACK["concepts"][0]["id"] == "c00-biological-neuron"


def test_the_biology_concept_covers_the_borrowed_mechanism():
    md = " ".join(c["md"] for c in _c("c00-biological-neuron")["cards"]).lower()
    for term in ("dendrite", "excitatory", "inhibitory", "axon hillock", "threshold",
                 "all-or-none", "weighted summation"):
        assert term in md, f"missing {term!r}"


def test_the_biology_concept_states_the_limits_of_the_analogy():
    """The operator's actual point: the analogy is mathematical, NOT biological. A concept that
    only drew the resemblance would be the same overselling the field is famous for."""
    md = " ".join(c["md"] for c in _c("c00-biological-neuron")["cards"]).lower()
    for term in ("rate", "refractory", "backpropagation", "biologically implausible"):
        assert term in md, f"missing the limit: {term!r}"
    assert "mathematical, not biological" in md


def test_xor_concept_exists_and_precedes_layers():
    ids = [c["id"] for c in PACK["concepts"]]
    assert "c01b-perceptron-limits" in ids
    assert ids.index("c01b-perceptron-limits") < ids.index("c02-layers"), (
        "the limit must motivate layers, so it has to come first")


def test_xor_is_presented_as_impossible_not_merely_hard():
    md = " ".join(c["md"] for c in _c("c01b-perceptron-limits")["cards"]).lower()
    assert "linearly separable" in md
    assert "no straight line separates them" in md
    assert "minsky" in md and "1969" in md
    assert "not a training problem" in md, (
        "XOR must be framed as a representational impossibility, not a tuning failure")


def test_the_fix_is_another_unit_and_the_nonlinearity_caveat_is_kept():
    md = " ".join(c["md"] for c in _c("c01b-perceptron-limits")["cards"]).lower()
    assert "use more than one unit" in md
    assert "universal approximation" in md
    assert "still just a weighted sum" in md, (
        "stacking only helps because of the nonlinearity — omitting that teaches the wrong lesson")


def test_both_animations_are_registered_and_wired():
    import validate_pack
    for a in ("nn_bio_vs_unit", "nn_perceptron_xor"):
        assert a in validate_pack._ANIMS, f"{a} is not registered"
    assert _c("c00-biological-neuron")["anim"] == "nn_bio_vs_unit"
    assert _c("c01b-perceptron-limits")["anim"] == "nn_perceptron_xor"


def test_the_side_by_side_sketch_draws_both_machines():
    """The ask was specifically a sketch of a real nerve NEXT TO a perceptron."""
    h = _index()
    block = h[h.index("/* neural-networks / nn_bio_vs_unit"):
              h.index("/* neural-networks / nn_loss_xent */")]
    assert "function bioNeuron(" in block and "function unit(" in block
    assert "BIOLOGICAL" in block and "ARTIFICIAL" in block
    for part in ("dendrites", "soma", "axon hillock"):
        assert part in block, f"the biological sketch is missing {part!r}"


def test_the_intro_carries_the_history():
    body = " ".join(PACK["intro"]["body"])
    for year in ("1943", "1958", "1969", "1986", "2012"):
        assert year in body, f"history is missing {year}"
    for name in ("McCulloch", "Rosenblatt", "Minsky", "LeCun", "Hinton"):
        assert name in body, f"history is missing {name}"
