/* Generic verification for preface animations: runs each against a recording context, sweeping
   every tween, and asserts what can be asserted without eyes. */
const fs = require("fs"), assert = require("assert");
function recorder() {
  const ops = [], texts = [];
  const num = (v) => { if (typeof v === "number") assert.ok(Number.isFinite(v), `NON-FINITE: ${v}`); return v; };
  const rec = (n) => (...a) => { a.forEach(num); ops.push(n); };
  const ctx = { ops, texts,
    clearRect: (...a) => { a.forEach(num); ops.length = 0; texts.length = 0; },
    beginPath: rec("beginPath"), moveTo: rec("moveTo"), lineTo: rec("lineTo"), stroke: rec("stroke"),
    fill: rec("fill"), closePath: rec("closePath"), arc: rec("arc"), ellipse: rec("ellipse"),
    fillRect: rec("fillRect"), strokeRect: rec("strokeRect"),
    fillText(s, x, y) { num(x); num(y); texts.push(String(s)); ops.push("fillText"); } };
  for (const p of ["fillStyle","strokeStyle","lineWidth","font","textAlign","textBaseline"])
    Object.defineProperty(ctx, p, { set() {}, get() { return ""; } });
  return ctx;
}
const JARGON = ["amplitude","superposition","unitary","qubit","eigen","derivative","integral",
                "stochastic","heteroske","covariance","logarithm","coefficient"];
module.exports = async function verify(src, names) {
  // Default to the animations read out of the INSTALLED page. Passing src explicitly is
  // only for checking a batch before it is installed.
  src = src || SRC;
  const cv = { width: 900, height: 380 };
  const clear = (ctx) => ctx.clearRect(0, 0, cv.width, cv.height);
  const _t = (ctx, x, y, s) => ctx.fillText(s, x, y);
  const win = {};
  const results = [];
  for (const name of names) {
    const stages = [];
    const ctx = recorder();
    const tween = async (ms, fn) => { for (const k of [0, 0.17, 0.5, 0.83, 1]) fn(k);
                                      stages.push(ctx.texts.join(" | ")); };
    const ANIMS = {};
    // The kit IIFE wrapper is stripped so its declarations land in the SAME scope as the batch.
    // They must NOT also be parameters: a parameter named PK shadows the const the kit declares,
    // which is a redeclaration error rather than a shadow.
    const body = KIT + "\n" + src;
    new Function("ANIMS","clear","_t","narrate","tween","window","requestAnimationFrame","performance", body)
      (ANIMS, clear, _t, async () => {}, tween, win, () => {}, { now: () => 0 });
    assert.ok(typeof ANIMS[name] === "function", `${name} is not registered`);
    assert.ok(typeof ANIMS[name + "_static"] === "function", `${name}_static is missing`);
    const s = recorder(); ANIMS[name + "_static"](cv, s);
    assert.ok(s.ops.length > 15, `${name}: static frame drew almost nothing (${s.ops.length} ops)`);
    await ANIMS[name](cv, ctx);
    assert.ok(stages.length >= 1, `${name}: no tweened stages`);
    assert.ok(ctx.ops.length > 15, `${name}: ends on a near-empty frame`);
    assert.ok(ctx.texts.length > 0, `${name}: final frame has no text at all`);
    const all = (stages.join(" ") + " " + ctx.texts.join(" ")).toLowerCase();
    for (const j of JARGON)
      assert.ok(!all.includes(j), `${name}: the word "${j}" is on screen — wrong audience`);
    results.push({ name, stages: stages.length, ops: ctx.ops.length, texts: ctx.texts.length });
  }
  return results;
};
const path = require("path");
const PAGE = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");
const kitStart = PAGE.indexOf("/* ── PREFACE ANIMATION KIT");
if (kitStart < 0) throw new Error("the preface animation kit is not in static/index.html");
const kitOpen = PAGE.indexOf("{", PAGE.indexOf("(function()", kitStart));
const KIT = PAGE.slice(kitOpen + 1, PAGE.indexOf("})();", kitOpen));
// every preface batch, in one slice: they are installed contiguously above the quantum block
const SRC = PAGE.slice(PAGE.indexOf("/* preface animations — batch 1"),
                       PAGE.indexOf("/* quantum / qm_preface"));
