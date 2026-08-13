const fs = require("fs"), assert = require("assert");

function recorder() {
  const ops = [], texts = [];
  const num = (v) => { if (typeof v === "number") assert.ok(Number.isFinite(v), `NON-FINITE coordinate: ${v}`); return v; };
  const rec = (name) => (...a) => { a.forEach(num); ops.push({ name, a }); };
  const ctx = {
    ops, texts,
    clearRect: (...a) => { a.forEach(num); ops.length = 0; texts.length = 0; ops.push({ name: "clearRect", a }); },
    beginPath: rec("beginPath"), moveTo: rec("moveTo"), lineTo: rec("lineTo"),
    stroke: rec("stroke"), fill: rec("fill"), closePath: rec("closePath"),
    arc: rec("arc"), fillRect: rec("fillRect"), strokeRect: rec("strokeRect"),
    fillText(s, x, y) { num(x); num(y); texts.push({ s: String(s), x, y }); ops.push({ name: "fillText", a: [s, x, y] }); },
  };
  for (const p of ["fillStyle", "strokeStyle", "lineWidth", "font", "textAlign", "textBaseline"])
    Object.defineProperty(ctx, p, { set() {}, get() { return ""; } });
  return ctx;
}

const ANIMS = {};
const cv = { width: 900, height: 380 };
const clear = (ctx) => ctx.clearRect(0, 0, cv.width, cv.height);
const _t = (ctx, x, y, s) => ctx.fillText(s, x, y);

// Read the animation out of the INSTALLED page, not a copy — a harness that verifies a file
// nobody ships is worse than no harness.
const PAGE = require("path").join(__dirname, "..", "static", "index.html");
const page = fs.readFileSync(PAGE, "utf8");
function block(marker, endMarker) {
  const a = page.indexOf(marker);
  assert.ok(a >= 0, `${marker} is not in static/index.html`);
  const b = page.indexOf(endMarker, a);
  assert.ok(b > a, `could not find the end of ${marker}`);
  return page.slice(a, b);
}
// Deliberately spans BOTH quantum blocks: qm_preface is installed immediately above
// qm_interference, so one slice defines both. If they are ever reordered this still fails safe —
// the missing ANIMS entry throws rather than silently verifying nothing.
const src = block("/* quantum / qm_preface", "/* newtonian-physics / phys_slides */");
assert.ok(src.includes("qm_interference") && src.includes("qm_preface"),
  "the slice no longer covers both quantum animations");
new Function("ANIMS", "clear", "_t", "narrate", "tween", "requestAnimationFrame", "performance", src)
  (ANIMS, clear, _t, async () => {}, null, () => {}, { now: () => 0 });

(async () => {
  // ── 1. the static frame draws, with finite coordinates throughout ──────────────
  const s = recorder();
  ANIMS.qm_interference_static(cv, s);
  assert.ok(s.ops.length > 20, "the static frame drew almost nothing");
  console.log(`ok — static frame: ${s.ops.length} draw ops, all coordinates finite`);

  // ── 2. every stage of the animation, snapshotted ──────────────────────────────
  const stages = [];
  const ctx = recorder();
  // tween(ms, fn) is swept across k so intermediate frames are checked too, not just the end
  const tween = async (ms, fn) => {
    for (const k of [0, 0.13, 0.5, 0.87, 1]) fn(k);
    stages.push({ texts: ctx.texts.map((t) => t.s), ops: ctx.ops.length });
  };
  new Function("ANIMS", "clear", "_t", "narrate", "tween", "requestAnimationFrame", "performance", src)
    (ANIMS, clear, _t, async () => {}, tween, () => {}, { now: () => 0 });
  await ANIMS.qm_interference(cv, ctx);
  assert.ok(stages.length >= 5, `expected >=5 tweened stages, got ${stages.length}`);
  console.log(`ok — ${stages.length} stages ran, every intermediate frame finite`);

  const all = stages.map((st) => st.texts.join(" | "));

  // ── 3. the cancellation is actually drawn ─────────────────────────────────────
  const cancel = all.find((t) => t.includes("|0|² = 0"));
  assert.ok(cancel, "no stage showed the probability collapsing to zero");
  assert.ok(cancel.includes("+½") && cancel.includes("−½"),
    "the cancelling stage does not show both signed amplitudes");
  assert.ok(cancel.includes("the outcome never happens"), "the payoff line is missing");
  console.log("ok — a stage shows +½ and −½ summing to |0|² = 0");

  // ── 4. and the classical contrast ─────────────────────────────────────────────
  assert.ok(all.some((t) => t.includes("|+½|² + |−½|² = ½")),
    "the classical comparison never appears");
  console.log("ok — the classical rule is drawn alongside for contrast");

  // ── 5. the reinforcing case reaches probability 1 ─────────────────────────────
  const reinforce = all.find((t) => t.includes("|1|² = 1"));
  assert.ok(reinforce, "the same-sign case never reaches probability 1");
  assert.ok(!reinforce.includes("−½"), "the reinforcing stage still shows a negative amplitude");
  console.log("ok — flipping the sign reinforces to |1|² = 1");

  // ── 6. the final frame is the reinforcing one, not a blank ────────────────────
  const fin = recorder();
  await ANIMS.qm_interference(cv, fin);
  assert.ok(fin.texts.some((t) => t.s.includes("reinforce")), "the animation ends on a blank or wrong frame");
  console.log("ok — ends on a drawn frame with the closing caption");


  // ── 7. the PREFACE animation: the pond, then the punchline ────────────────────
  {
    const stagesP = [];
    const ctxP = recorder();
    const tweenP = async (ms, fn) => {
      for (const k of [0, 0.2, 0.6, 1]) fn(k);
      stagesP.push(ctxP.texts.map((t) => t.s).join(" | "));
    };
    const A2 = {};
    new Function("ANIMS", "clear", "_t", "narrate", "tween", "requestAnimationFrame", "performance", src)
      (A2, clear, _t, async () => {}, tweenP, () => {}, { now: () => 0 });
    const sP = recorder();
    A2.qm_preface_static(cv, sP);
    assert.ok(sP.ops.length > 20, "the preface static frame drew almost nothing");
    await A2.qm_preface(cv, ctxP);
    assert.ok(stagesP.length >= 3, `expected >=3 preface stages, got ${stagesP.length}`);
    assert.ok(stagesP.some((t) => t.includes("twice as big")),
      "the constructive case (two crests) is never shown");
    assert.ok(stagesP.some((t) => t.includes("flat — the water is still")),
      "the cancelling case never reaches a flat pond — the whole point of the preface");
    assert.ok(stagesP.some((t) => t.includes("routes can cancel")),
      "the pond is never connected back to particles");
    const words = stagesP.join(" ");
    for (const jargon of ["amplitude", "superposition", "unitary", "qubit", "eigen"])
      assert.ok(!words.toLowerCase().includes(jargon),
        `the preface animation uses the word "${jargon}" — this audience does not have it yet`);
    console.log(`ok — preface: ${stagesP.length} stages, pond cancels to flat, no jargon on screen`);
  }

  console.log("\nqm_interference + qm_preface: geometry and stages verified (pixels still unseen)");
})();
