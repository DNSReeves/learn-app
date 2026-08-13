#!/usr/bin/env python3
"""contact_sheet.py — one page of every preface animation's final frame, for skimming.

Composed as HTML and screenshotted, so the captions are real text rather than drawn pixels.
"""
import base64
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

SHOTS = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
LEARN = pathlib.Path("/Users/david/agentic_software_from_scratch/learn_app")

# anim -> topic title, so each tile is labelled with the subject rather than a function name
titles = {}
for f in sorted((LEARN / "topics").glob("*.json")):
    if f.name.startswith("_"):
        continue
    d = json.loads(f.read_text())
    a = (d.get("preface") or {}).get("anim")
    if a:
        titles[a] = d["title"]

tiles = []
for anim, title in sorted(titles.items(), key=lambda kv: kv[1]):
    png = SHOTS / f"{anim}.png"
    if not png.exists():
        continue
    b64 = base64.b64encode(png.read_bytes()).decode()
    tiles.append(f'''<figure>
      <img src="data:image/png;base64,{b64}">
      <figcaption>{title}</figcaption>
    </figure>''')

html = f"""<!doctype html><meta charset="utf-8">
<style>
  body {{ margin:0; padding:26px; background:#0B1117;
         font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif; }}
  h1 {{ color:#D7DFEA; font-size:21px; margin:0 0 4px; }}
  p.sub {{ color:#8496AB; font-size:13.5px; margin:0 0 22px; }}
  .grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:20px 18px; }}
  figure {{ margin:0; background:#0F1720; border:1px solid #22303C; border-radius:8px;
            overflow:hidden; }}
  img {{ display:block; width:100%; }}
  figcaption {{ color:#D7DFEA; font-size:13.5px; font-weight:600;
                padding:9px 12px; border-top:1px solid #22303C; }}
</style>
<body>
<h1>Preface animations — final frame of each</h1>
<p class="sub">{len(tiles)} topics. These are the closing frames; each also animates through
2&ndash;4 stages with narration. Looking for: anything unreadable, overlapping, or that does not
match what the words claim.</p>
<div class="grid">{''.join(tiles)}</div>
</body>"""

tmp = OUT.parent / "_sheet.html"
OUT.parent.mkdir(parents=True, exist_ok=True)
tmp.write_text(html, encoding="utf-8")

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 1000}, device_scale_factor=1)
    pg.goto("file://" + str(tmp))
    pg.screenshot(path=str(OUT), full_page=True)
    b.close()
print(f"  {len(tiles)} tiles → {OUT}  ({OUT.stat().st_size/1e6:.1f} MB)")
