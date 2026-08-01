"""Text-overlap detector — the check that catches an UNREADABLE animation.

Usage:  <a venv with playwright>/bin/python tools/check_text_overlap.py
        (the Learn service must be running on :8801)

Exit status is informational; read the report. Run it before shipping an
animation batch.


Four label collisions shipped today past every gate we had — render check,
registry contract, pixel counts, full suite. All of them pass because a canvas
with two labels stacked on one line is, to those checks, identical to a canvas
with two labels placed correctly. This wraps ctx.fillText during a static render,
measures every drawn box with the real font metrics, and reports intersecting
pairs.
"""
import sys, json
from playwright.sync_api import sync_playwright
sys.path.insert(0,'/Users/david/agentic_software_from_scratch/learn_app')
import db; db.init()
try: db.create_user('overlap_tmp','tmp-pw-77')
except Exception: pass
TOK=db.authenticate('overlap_tmp','tmp-pw-77')

JS = r"""(() => {
  const out = [];
  const cv = document.createElement('canvas'); cv.width=900; cv.height=380;
  const ctx = cv.getContext('2d');
  const names = Object.keys(ANIMS).filter(k => k.endsWith('_static'));
  const realFill = CanvasRenderingContext2D.prototype.fillText;
  for (const n of names) {
    const boxes = [];
    CanvasRenderingContext2D.prototype.fillText = function(t, x, y, mw) {
      try {
        const s = String(t);
        if (s.trim()) {
          const m = this.measureText(s);
          const w = m.width;
          const asc = (m.actualBoundingBoxAscent  || parseInt(this.font)||12);
          const desc = (m.actualBoundingBoxDescent || 3);
          let x0 = x;
          if (this.textAlign === 'center') x0 = x - w/2;
          else if (this.textAlign === 'right' || this.textAlign === 'end') x0 = x - w;
          boxes.push({t: s, x0, y0: y - asc, x1: x0 + w, y1: y + desc});
        }
      } catch (e) {}
      return realFill.apply(this, arguments);
    };
    try { ctx.clearRect(0,0,900,380); ANIMS[n](cv, ctx); } catch (e) {}
    CanvasRenderingContext2D.prototype.fillText = realFill;
    // pairwise intersection, ignoring trivial slivers
    const hits = [];
    for (let i=0;i<boxes.length;i++) for (let j=i+1;j<boxes.length;j++){
      const a=boxes[i], b=boxes[j];
      const ox = Math.min(a.x1,b.x1) - Math.max(a.x0,b.x0);
      const oy = Math.min(a.y1,b.y1) - Math.max(a.y0,b.y0);
      if (ox > 4 && oy > 4) {
        const area = ox*oy;
        const small = Math.min((a.x1-a.x0)*(a.y1-a.y0), (b.x1-b.x0)*(b.y1-b.y0));
        if (area / small > 0.18) hits.push({a:a.t.slice(0,42), b:b.t.slice(0,42),
                                            frac: +(area/small).toFixed(2)});
      }
    }
    if (hits.length) out.push({anim: n.replace('_static',''), hits});
  }
  return out;
})()"""

with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    pg.add_init_script(f"sessionStorage.setItem('learn_token', {TOK!r});")
    pg.goto("http://127.0.0.1:8801/", wait_until="domcontentloaded")
    pg.wait_for_selector("#cats .cat", timeout=20000)
    res = pg.evaluate(JS)
    b.close()

print(f"animations with overlapping text: {len(res)}")
for r in sorted(res, key=lambda r: -len(r["hits"])):
    print(f"\n  {r['anim']}  ({len(r['hits'])} pair(s))")
    for h in r["hits"][:3]:
        print(f"      {h['frac']:.0%} overlap:  {h['a']!r}")
        print(f"                     vs  {h['b']!r}")
json.dump(res, open('/private/tmp/claude-501/-Users-david-agentic-software-from-scratch-dnsr-agent/76e4b1fc-6409-4cbe-adda-d4849f3dd11a/scratchpad/overlaps.json','w'), indent=1)
with db.conn() as c:
    r=c.execute('SELECT id FROM users WHERE username=?',('overlap_tmp',)).fetchone()
    if r: c.execute('DELETE FROM sessions WHERE user_id=?',(r[0],)); c.execute('DELETE FROM users WHERE id=?',(r[0],))
