/**
 * tests/preface_anims.test.js — every preface animation, verified as far as it can be (2026-08-13).
 *
 * 23 of the 24 prefaces are driven by a shared kit (bars, paths, dot grids, arrows, panels) rather
 * than being bespoke canvas scenes. That was a deliberate trade: fewer novel drawing paths means
 * fewer ways to be silently wrong, and it lets one harness check all of them identically.
 *
 * WHAT THIS CAN CHECK: that every animation and its static frame exist and are registered; that
 * every coordinate reaching the context is FINITE (a NaN in a canvas call draws nothing, silently,
 * which is the failure nobody would ever see); that each ends on a drawn frame carrying text
 * rather than a blank; and that none puts jargon on screen — the wrong-audience failure.
 *
 * WHAT IT CANNOT CHECK: whether any of them LOOKS right. Layout, spacing, contrast and whether the
 * picture reads at a glance all still need a human with a browser. This test exists so that the
 * things which can be checked mechanically are, not to imply the rest has been.
 */
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const verify = require("./verify_pref_anims.js");

const TOPICS = path.join(__dirname, "..", "topics");
const names = fs.readdirSync(TOPICS)
  .filter((f) => f.endsWith(".json") && !f.startsWith("_"))
  .map((f) => JSON.parse(fs.readFileSync(path.join(TOPICS, f), "utf8")))
  .map((d) => (d.preface || {}).anim)
  .filter((a) => a && a.startsWith("pref_"))
  .sort();

(async () => {
  assert.ok(names.length >= 23, `expected >=23 kit-driven preface animations, found ${names.length}`);
  const results = await verify(null, names);
  console.log(`ok — ${results.length} preface animations: registered, finite, non-blank, no jargon`);

  // A frame that draws almost nothing renders as an empty box next to the text. The ETF scene
  // failed exactly this during authoring: its closing frame took a branch that drew two bars and
  // dropped everything else.
  const thin = results.filter((r) => r.ops < 15);
  assert.deepStrictEqual(thin.map((r) => r.name), [],
    `these end on a near-empty frame: ${thin.map((r) => r.name + "(" + r.ops + " ops)").join(", ")}`);
  console.log(`ok — no animation ends on a near-empty frame (thinnest: ${Math.min(...results.map((r) => r.ops))} ops)`);

  // Every preface with an animation must name one that is actually registered, and vice versa —
  // the same both-directions check the help topics get.
  const page = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");
  for (const n of names) {
    assert.ok(page.includes(`ANIMS.${n}=`), `${n} is wired in a pack but not registered in the page`);
    assert.ok(page.includes(`ANIMS.${n}_static=`), `${n}_static is missing`);
  }
  const registered = [...page.matchAll(/ANIMS\.(pref_[a-z0-9_]+)\s*=/g)]
    .map((m) => m[1]).filter((n) => !n.endsWith("_static"));
  const orphans = [...new Set(registered)].filter((n) => !names.includes(n));
  assert.deepStrictEqual(orphans, [], `registered but no pack uses them: ${orphans.join(", ")}`);
  console.log(`ok — ${names.length} animations wired and registered, none orphaned`);

  console.log("preface_anims: all assertions passed");
})().catch((e) => { console.error(e.message); process.exit(1); });
