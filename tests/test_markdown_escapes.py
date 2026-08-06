"""Backslash escapes in card markdown.

Operator, 2026-08-05: "some cards in the investment area show /$ instead of $".

The card renderer's inline() ran esc(), then code spans, then emphasis — and never
handled backslash escapes at all. So an authored `\\$50.10` printed its backslash, and
the reported "/$" was that stray backslash. It was NOT confined to the investment
pack: genetics escapes its pharmacogenomic star alleles (`CYP2C9\\*2`, `HLA-B\\*57:01`)
so they do not turn into italics, and those printed backslashes too.

Two fixes, because the content had two different problems:

* THE RENDERER now resolves escapes. It lifts them out BEFORE emphasis and restores
  them after, because the packs legitimately wrap an escaped star in italics —
  `*CYP2C9\\*2*` must render as an italic CYP2C9*2, which a naive strip-the-backslash
  pass would turn into a mangled emphasis run.
* THE CONTENT dropped its `\\$` escapes. `$` is not special here (no math pass
  anywhere), and 18 of the 31 sat in prompt/feedback/options — fields rendered with
  esc() alone, which NO renderer change could ever have unescaped.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _run_md(text: str) -> str:
    """Execute the page's REAL esc()+md() under node against `text`."""
    if subprocess.run(["which", "node"], capture_output=True).returncode != 0:
        pytest.skip("node not available")
    esc = next(l for l in SRC.split("\n") if l.startswith("const esc ="))
    md = re.search(r"function md\(src\)\{.*?\n\}", SRC, re.S)
    assert md, "md() is gone"
    js = f"{esc}\n{md.group(0)}\nconsole.log(md({json.dumps(text)}));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.parametrize("src,want", [
    (r"an ETF quotes \$50.10 against", "quotes $50.10 against"),
    (r"CYP2C9\*2 acts", "CYP2C9*2 acts"),
    (r"*CYP2C9\*2* and *\*3*", "<em>CYP2C9*2</em>"),      # italics AROUND an escaped star
    ("this is *emphasis* here", "<em>emphasis</em>"),      # emphasis still works
    ("this is **strong** here", "<strong>strong</strong>"),
    ("use `x` now", "<code"),
])
def test_escape_rendering(src, want):
    assert want in _run_md(src)


def test_no_stray_backslash_survives():
    assert "\\" not in _run_md(r"NAV \$20.00 and \$18.00")


def test_escaped_star_does_not_open_emphasis():
    assert "<em>" not in _run_md(r"\*not italic\* here")


def test_placeholder_never_leaks():
    """The escape shuttle uses a NUL sentinel; one leaking into the page would be a
    visible control character in a lesson."""
    assert "\u0000" not in _run_md(r"a \$5 and \*b")


def test_escapes_cannot_reintroduce_raw_html():
    """esc() runs first, so restoring a literal must never hand back raw markup —
    the escapable set excludes & < > \" for exactly this reason."""
    assert "<script>" not in _run_md(r"\<script\>")
    assert "&lt;script&gt;" in _run_md("<script>alert(1)</script>")


def test_no_pack_carries_a_dollar_escape():
    """`$` is not special in this renderer, and in esc()-only fields (prompt,
    feedback, options) an escape can never be undone — it just prints."""
    offenders = []
    for p in sorted((ROOT / "topics").glob("*.json")):
        if p.name.startswith("_"):
            continue
        if r"\$" in p.read_text(encoding="utf-8").replace("\\\\$", ""):
            offenders.append(p.name)
    assert not offenders, f"packs re-introduced \\$ escapes: {offenders}"


def test_escapes_outside_markdown_fields_are_rejected():
    """prompt / feedback / options render through esc() with no markdown pass, so a
    backslash escape there is always a visible bug. Cards may legitimately use them."""
    bad = []
    for p in sorted((ROOT / "topics").glob("*.json")):
        if p.name.startswith("_"):
            continue
        pack = json.loads(p.read_text(encoding="utf-8"))

        def walk(o, path=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{path}[{i}]")
            elif isinstance(o, str):
                field = re.sub(r"\[\d+\]", "", path).split(".")[-1]
                if field in ("prompt", "feedback", "options") and re.search(r"\\[^n]", o):
                    bad.append(f"{p.name}:{path}")
        walk(pack)
    assert not bad, f"backslash escapes in non-markdown fields (they will print): {bad}"
