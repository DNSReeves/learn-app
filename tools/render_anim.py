#!/usr/bin/env python3
"""render_anim.py — screenshot a preface animation's frames, so a human (or I) can actually LOOK.

Every check written so far verifies geometry: coordinates finite, frames non-blank, no jargon on
screen. None of it can say whether the picture READS. This renders the real canvas in real Chromium
at the real size and writes PNGs.

    python3 render_anim.py pref_bayesian_inference [more names…]

Writes <name>.png (final frame) and <name>_mid.png (halfway) into the output directory.
"""
import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

LEARN = pathlib.Path("/Users/david/agentic_software_from_scratch/learn_app")
OUT = pathlib.Path(sys.argv[-1]) if sys.argv[-1].startswith("/") else pathlib.Path("/tmp/anim_shots")
NAMES = [a for a in sys.argv[1:] if not a.startswith("/")]
OUT.mkdir(parents=True, exist_ok=True)

page_src = (LEARN / "static" / "index.html").read_text(encoding="utf-8")

# The pieces the animations need, lifted out of the real page so what is rendered is what ships.
kit_start = page_src.index("/* ── PREFACE ANIMATION KIT")
kit_open = page_src.index("{", page_src.index("(function()", kit_start))
KIT = page_src[kit_open + 1:page_src.index("})();", kit_open)]
ANIM_SRC = page_src[page_src.index("/* preface animations — batch 1"):
                    page_src.index("/* quantum / qm_preface")]
QUANTUM = page_src[page_src.index("/* quantum / qm_preface"):
                   page_src.index("/* newtonian-physics / phys_slides */")]

HTML = """<!doctype html><meta charset="utf-8">
<body style="margin:0;background:#0F1720">
<canvas id="cv" width="900" height="380"></canvas>
<script>
const ANIMS = {};
const cv = document.getElementById("cv"), ctx = cv.getContext("2d");
function clear(c,v){ c.clearRect(0,0,v.width,v.height); }
function _t(c,x,y,s,o){ o=o||{};
  c.fillStyle=o.col||"#D7DFEA"; c.textAlign=o.align||"left"; c.textBaseline="alphabetic";
  c.font=(o.bold?"600 ":"")+(o.size||14)+"px -apple-system,system-ui,sans-serif";
  c.fillText(s,x,y); }
const _lerp=(a,b,k)=>a+(b-a)*k;
let FRAC = 1;                       // how far through each tween to stop
async function narrate(){ }
async function tween(ms, fn){ fn(FRAC); }
__KIT__
__ANIMS__
window.__run = async (name, frac) => {
  FRAC = frac;
  ctx.fillStyle = "#0F1720"; ctx.fillRect(0,0,cv.width,cv.height);
  await ANIMS[name](cv, ctx);
  return true;
};
</script></body>"""

html = (HTML.replace("__KIT__", KIT)
            .replace("__ANIMS__", ANIM_SRC + "\n" + QUANTUM))
tmp = OUT / "_harness.html"
tmp.write_text(html, encoding="utf-8")

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 940, "height": 420},
                            device_scale_factor=2)
    page.goto("file://" + str(tmp))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    for name in NAMES:
        for frac, suffix in ((1.0, ""), (0.5, "_mid")):
            page.evaluate(f"window.__run({json.dumps(name)}, {frac})")
            page.locator("#cv").screenshot(path=str(OUT / f"{name}{suffix}.png"))
        print(f"  rendered {name}")
    if errors:
        print("  PAGE ERRORS:", errors[:3])
    browser.close()
print(f"  → {OUT}")
