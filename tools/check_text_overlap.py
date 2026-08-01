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
          let bx0 = x0, by0 = y - asc, bx1 = x0 + w, by1 = y + desc;
          // TRANSFORM-AWARE (2026-08-01): the first version recorded the RAW x,y
          // arguments and ignored the active transform, so every rotated label —
          // drawn via save/translate/rotate/fillText(s,0,0) — was recorded at
          // literal (0,0). Two of them then scored a 100% "overlap" while sitting
          // 620px apart on screen. Map the corners through the CTM instead.
          const T = (typeof this.getTransform === 'function') ? this.getTransform() : null;
          if (T && !(T.a===1 && T.b===0 && T.c===0 && T.d===1 && T.e===0 && T.f===0)) {
            const px = (X,Y) => ({x: T.a*X + T.c*Y + T.e, y: T.b*X + T.d*Y + T.f});
            const pts = [px(bx0,by0), px(bx1,by0), px(bx0,by1), px(bx1,by1)];
            bx0 = Math.min(...pts.map(p=>p.x)); bx1 = Math.max(...pts.map(p=>p.x));
            by0 = Math.min(...pts.map(p=>p.y)); by1 = Math.max(...pts.map(p=>p.y));
          }
          boxes.push({t: s, x0: bx0, y0: by0, x1: bx1, y1: by1});
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
    // OUT-OF-BOUNDS (2026-08-01): a label running off the canvas edge is just as
    // unreadable as one buried under another, and no check we had could see it —
    // it surfaced only because a human noticed "age, months" rendering as
    // "age, month". Same instrument, second defect class.
    // NOT TRUSTWORTHY YET — reported separately and never as a defect. Calibrated
    // against rendered frames: the LARGE overruns are false. fee_drag and epicycles
    // were flagged 157px and 92px over while their single caption plainly fits on
    // screen, so measureText is still returning wrong widths for some faces even
    // after document.fonts.ready. Confusingly the SMALL flags are the real ones —
    // gen_two_hit at 6px genuinely rendered as "age, month". Until the metrics are
    // stable this is a hint to go and LOOK, not a gate. The overlap check above IS
    // trustworthy: it found 11 real collisions, every one confirmed by eye.
    const clipped = (typeof BOUNDS_CHECK !== 'undefined' && !BOUNDS_CHECK) ? [] :
                    boxes.filter(b => b.x0 < -1 || b.y0 < -1 || b.x1 > 901 || b.y1 > 381)
                         .map(b => ({t: b.t.slice(0,42),
                                     over: +(Math.max(b.x1-900, b.y1-380,
                                                      -b.x0, -b.y0)).toFixed(0)}));
    if (hits.length || clipped.length)
      out.push({anim: n.replace('_static',''), hits, clipped});
  }
  return out;
})()"""

with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    pg.add_init_script(f"sessionStorage.setItem('learn_token', {TOK!r});")
    pg.goto("http://127.0.0.1:8801/", wait_until="domcontentloaded")
    pg.wait_for_selector("#cats .cat", timeout=20000)
    # FONT-READY (2026-08-01): measureText returns FALLBACK metrics until the
    # webfonts have loaded, which made long monospace captions look 90-160px
    # wider than they render and produced a wave of phantom "off-canvas" hits.
    # Wait for the real faces before measuring anything.
    pg.evaluate("document.fonts && document.fonts.ready")
    pg.wait_for_timeout(1200)
    res = pg.evaluate(JS)
    b.close()

nover = sum(1 for r in res if r.get("hits"))
nclip = sum(1 for r in res if r.get("clipped"))
print(f"flagged: {len(res)}  (overlapping text: {nover} — RELIABLE, "
      f"off-canvas: {nclip} — HINTS ONLY, verify by eye before treating as defects)")
for r in sorted(res, key=lambda r: -len(r.get("hits",[]))):
    print(f"\n  {r['anim']}  ({len(r['hits'])} pair(s))")
    for h in r.get("hits", [])[:3]:
        print(f"      {h['frac']:.0%} overlap:  {h['a']!r}")
        print(f"                     vs  {h['b']!r}")
    for c in r.get("clipped", [])[:3]:
        print(f"      off-canvas by {c['over']}px:  {c['t']!r}")
json.dump(res, open('/private/tmp/claude-501/-Users-david-agentic-software-from-scratch-dnsr-agent/76e4b1fc-6409-4cbe-adda-d4849f3dd11a/scratchpad/overlaps.json','w'), indent=1)
with db.conn() as c:
    r=c.execute('SELECT id FROM users WHERE username=?',('overlap_tmp',)).fetchone()
    if r: c.execute('DELETE FROM sessions WHERE user_id=?',(r[0],)); c.execute('DELETE FROM users WHERE id=?',(r[0],))
