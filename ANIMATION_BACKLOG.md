# Animation backlog — P1.3 coverage pass (2026-07-25, iss_eb4f851e)

> ## ▶ REOPENED (2026-08-12) — coverage is no longer 100%, and the block below is now historical
> Two additions since that block was written, and the honest numbers have moved:
>
> - **Concepts: 203 of 216 animated (94%).** The library grew from 195 to 216 concepts. The new
>   `quantum` topic ships 1 of 9 animated (`qm_interference` — two amplitudes cancelling); the
>   other 8 have none. `exercise-physiology` still carries 5 without, unchanged from before.
> - **Prefaces: 1 of 24 animated.** Every topic now opens with a preface for a twelve-year-old
>   reader. Only `quantum` has a bespoke visual (`qm_preface` — two pond ripples cancelling to
>   flat). The other 23 are text-only.
>
> **Do not reuse an existing concept animation for a preface.** They are narrated for an adult
> reader and break the register in the first sentence. A preface visual needs its own scene and
> its own script.
>
> **Verification note.** `tests/verify_anim.js` drives the quantum animations against a recording
> 2-D context and asserts geometry, stage sequence, finite coordinates and — for the preface —
> that no jargon reaches the screen. It cannot check that anything *looks* right; that still needs
> a human with a browser. Any new animation should get the same treatment plus an actual look.
>
> Remaining, in the order worth doing: `quantum` c04 (Bloch sphere), c05 (entanglement
> correlations), c08 (Grover rotation); then preface visuals for the highest-traffic topics.

> ## ▶▶ COMPLETE (2026-08-01) — 100% VISUAL COVERAGE
> The batch-2 visual-aids programme closed this backlog out. **Every one of the 195 concepts now carries a bespoke animated, voice-narrated canvas (192 distinct animations) — coverage 100%.** The final push added **73 animations** across Mathematics, AI/ML, Investing & Finance, Health & Medicine, Physics, and Genetics, built by parallel authoring agents (fragment-author → merge-verify) and browser-verified (static frames paint + runners play, 0 page errors) before each commit. The declarative-vs-imperative decision record below stays the standing build guidance. The status block that follows is the earlier (64%) snapshot, kept for history.

> ## ▶ STATUS (2026-07-26) — backlog largely DELIVERED
> The list below was the *plan*; most of it has shipped. Current state:
> - **Coverage ~18% → 64%** (72 of 112 concepts carry a bespoke animation) — the
>   52-animation expansion across 13 topics landed in `f6729f6`.
> - **IV visuals** added to `options-and-iv`: `c05` `iv_inversion` + `c06` `vol_smile`
>   (`c5299c2`).
> - **Validator modernized** — understands free-response questions, real resource
>   types, and a *dynamic* anim registry (no more hardcoded allowlist) (`36e8085`).
> - **Review sweep** — an adversarial audit of the 52 new animations found + fixed
>   **8 real bugs** (envelope σ, term-structure skew, off-canvas labels, a tangent
>   overshoot, a narration rounding, a bars/narration contradiction, a wrong
>   Wilbraham–Gibbs constant); gate green (`e9d6710`).
> - **DELIVERED — ETF-module expansion** (`62d4ed6` content, `71cb100` animations):
>   5 new concepts, each animated — `c01b` open-vs-closed (create/redeem tanks),
>   `c01c` ETNs (issuer-default wipeout), `c04b` CAGR (jagged→smooth), `c04c` IRR
>   (discount-rate dial → NPV=0), `c04d` TWR-vs-MWR (good/bad timer). Authored via
>   fan-out, finance-verified (all 5 clean), each animation node-checked + bounds-checked.
>   ETF module: 7 → 12 concepts.
>
> This doc is retained for the **P3.10 declarative-vs-imperative decision record** below,
> which is still the standing guidance for how new animations get built.


> **P3.10 DECISION (2026-07-25, iss_ef1e0e3c — one built each way, as specced):**
> **Declarative wins**, as the operator expected — now with evidence. Arm (a):
> `phase_portrait` is a ~20-line spec (matrices, starts, narration prose) over a 5-op
> vetted vocabulary (`axes/field/flows/label/clear` + RK4) — vetting a new in-vocabulary
> anim means reading numbers and English. Arm (b): `attention_heatmap` is ~55 lines of
> arbitrary canvas code — full code review every time, reuse only by copy-paste. The
> engine cost (~70 lines) amortizes from the second spec onward. **Path forward:**
> declarative by default; grow `DECL_OPS` by vetted increments (a `heatgrid` op would
> retro-absorb arm b's pattern); imperative stays permitted for genuinely bespoke
> one-offs under normal code review. Both experiment anims are LIVE (odes
> c06-systems-phase, llms c03-transformer).

**Scope:** LIST ONLY, per the issue — 1–2 concepts per topic where a moving picture teaches
what static prose cannot. **Build happens in P3.10** (iss_ef1e0e3c), which also decides the
authoring path. Ranked so P3.10 can work top-down.

**Selection rule applied:** an animation earns a slot only when the concept's core is a
*process unfolding in time* or a *quantity flowing through space* — things a paragraph
genuinely fumbles. Concepts that are taxonomies, debates, or checklists got NO slot on
purpose (most of `agi-and-beyond`, by design — it's an essayistic pack).

**Player contract** (static/index.html): each key needs `ANIMS[key](cv,ctx)` (narrated
beats via `narrate()` + `tween()`) and `ANIMS[key+"_static"]` (first frame). Canvas
900×380, reduced-motion honored, palette in `F`.

## Tier 1 — the four the issue named (highest payoff, canonical visuals)

| topic · concept | key | what it shows, beat by beat |
|---|---|---|
| `odes` · `c06-systems-phase` | `phase_portrait` | A 2-D vector field fades in as arrows → drop a point, watch it flow along a trajectory → drop 5 more from different starts → the portrait emerges as the *family* of flows; narrate spiral vs saddle by switching the matrix mid-animation. |
| `fourier-series` · `c03-coefficients` (tie-in `c01-periodic`) | `epicycles` | One rotating circle traces a sine → stack a 3rd-harmonic circle on its rim → 5th, 7th → the trace visibly squares up; end with partial-sum count slider narrated ("more circles, sharper corners — and the wiggle at the jump that never dies" → Gibbs, which q-checks reference). |
| `pdes` · `c02-the-big-three` (heat leg) | `heat_diffusion` | A 1-D bar with a hot spike → time-step the profile flattening (explicit finite differences live) → narrate "curvature drives flow: peaks fall, valleys fill" → contrast beat: same initial condition under the WAVE equation splits and travels instead of smoothing. One animation, the parabolic-vs-hyperbolic distinction made visible. |
| `llms` · `c03-transformer` | `attention_heatmap` | A short sentence's tokens in a row → for one query token, attention weights light up as bars over every other token → sweep the query token left-to-right, heatmap rows filling in → narrate "each token asks: who matters to me?" → final beat: the pronoun row concentrating on its referent. |

## Tier 2 — strong candidates (clear process, cheap to build)

| topic · concept | key | what it shows |
|---|---|---|
| `neural-networks` · `c04-gradient-descent` | `loss_descent` | A 2-D loss bowl (contours) + a ball stepping downhill; learning-rate slider beats: too small (crawl), right (converge), too large (diverge). The single most-requested visual in every ML course for a reason. |
| `neural-networks` · `c05-backprop` | `backprop_flow` | A 3-layer net drawn as nodes; forward pass lights activations left→right, then the error signal flows right→left with edge widths scaling to gradient magnitude. Pairs with `loss_descent`; shares layout code. |
| `calculus` · `c02-derivative-def` | `secant_to_tangent` | Secant line through two points; drag h→0 with the slope readout live → the tangent emerges; narrate the limit as a *process*, not a substitution. |
| `options-and-iv` · `c04-greeks` | `delta_gamma_surface` | Payoff curve at expiry vs the smooth pre-expiry value curve; time ticks down and the curve melts onto the kink — delta as slope shown as a rolling tangent, gamma as the curvature concentrating at the strike. |
| `newtonian-physics` · `c07-oscillation` | `spring_phase` | Mass-on-spring bouncing beside its position/velocity circle in phase space — the same motion drawn twice; narrate why SHM is "a circle viewed edge-on." Reuses `phase_portrait` plumbing. |
| `quant-finance` · `c07-regime-tail` | `drawdown_paths` | 30 Monte-Carlo equity curves fan out; same mean, two vol regimes toggled — narrate why the ARITHMETIC mean survives while paths die (volatility drag, sequence risk); ends on the max-drawdown distribution histogram. The course's own doctrine, animated. |
| `ai-agents` · `the-agentic-loop` | `react_loop` | Four boxes (Goal → Model → Tool → Observation) with a token pulsing around the cycle; a failure beat where the observation is an ERROR that redirects the next action; a termination beat where the budget counter hits zero. Text genuinely cannot show the *circulation*. |

## Tier 3 — nice-to-have (only after Tiers 1–2)

| topic · concept | key | what it shows |
|---|---|---|
| `roman-engineering` · `c02-arch` | `arch_thrust` | Voussoirs assemble over falsework; load applied at the crown, force arrows flow down the ring into the abutments; remove the keystone → collapse beat. |
| `investing-with-etfs` · `c06-costs-and-taxes` | `fee_drag` | Two identical growth curves, 0.05% vs 1.0% fees, 30 years; the gap opens slowly then yawns — narrate the end-wealth difference in dollars. |
| `agi-and-beyond` · `measuring-progress` | `jagged_frontier` | A radar/bar profile of one model across task types — spiky, not round; overlay a second generation: peaks jump, valleys crawl. The ONE agi-pack concept that is a picture rather than an argument. |

## Explicitly skipped (and why)

- `bayesian-inference` — already covered (4 anims, the house exemplars).
- `agi-and-beyond` (rest) — debates and decision frameworks; motion adds nothing prose lacks.
- `investing-with-etfs` taxonomy/mechanics, `llms` tokens/prompting, `calculus` rules — table/step content; static is honest.
- `pdes` `c04-separation-of-variables` — considered; it's algebraic choreography, and the payoff is in `heat_diffusion` already.

**Count: 14 entries across 11 topics (4 + 7 + 3).** P3.10's first decision (declarative spec
vs LLM-generated code) should prototype on `phase_portrait` — it exercises fields, particles,
narration beats, and a mid-animation parameter switch, which spans the whole feature surface.
