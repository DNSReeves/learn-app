# Learn

Mastery-based learning dashboard. Multi-user, extensible to any topic, all data local.
A growing library: **24 topics across 8 categories** (Investing & Finance · AI & Machine Learning · Health & Medicine · Mathematics · Physics & Engineering · Physiology · Molecular Biology · Statistics) — **216 gated concepts and 730 checkpoint questions**, with **203 of 216 concepts carrying an animated, voice-narrated canvas** (196 distinct animations in use, 393 registered; Kokoro TTS). Every topic opens with a **preface** written for a curious twelve-year-old, then an intro for the adult reader. Started as a single Bayesian-Inference topic and generalized to any subject — drop a JSON pack in `topics/` and it appears on the dashboard. (The managed service runs on `:8801`; the `--port 8090` below is the manual-run example.)

## Run

```bash
pip install -r requirements.txt --break-system-packages
uvicorn app:app --host 0.0.0.0 --port 8090
# optional AI tutoring layer:
export ANTHROPIC_API_KEY=sk-ant-...      # enables tailored remediation; omit and the app falls back to authored hints
export LEARN_DB=/path/to/learn.db        # optional; defaults to ./learn.db
```

Open http://localhost:8090, create a profile, start learning.

## Architecture

```
app.py        FastAPI — auth, lesson delivery, server-side grading, gating, review queue
db.py         SQLite — users (PBKDF2 passwords), sessions, concept_state, full answer log
mastery.py    Bayesian Knowledge Tracing + SM-2-lite spaced review scheduling
ai.py         Optional Claude remediation (hybrid layer); graceful fallback without a key
topics/*.json Topic packs — drop a file in, it appears on the dashboard
static/       Single-file SPA (vanilla JS, no build step)
```

Answers and per-option feedback never leave the server; the client receives questions
without answer keys and grading happens in `app.py`. Progression gates are enforced
server-side too — the API refuses to serve a locked concept.

## Pedagogy (why it works this way)

- **Placement first** — prior knowledge is the strongest single predictor of learning
  (Ausubel), so a new topic opens with an optional adaptive placement: binary search
  over the concept ladder, ~log₂(n)+1 questions, no feedback shown. The result sets
  each concept's BKT prior instead of assuming flat ignorance — placed-out concepts
  are marked mastered but fall due for review immediately (trust, then verify).
- **Chunking** — each concept is 2–4 short cards shown one at a time. Working-memory
  capacity is ~4 items (Cowan 2001); cognitive load theory (Sweller) says exceed it
  and retention collapses.
- **Retrieval practice** — every concept ends in a checkpoint. Testing beats re-reading
  for retention (Roediger & Karpicke 2006).
- **Immediate elaborated feedback** — every distractor has its own explanation of the
  specific misconception it represents.
- **Mastery gates** — Bloom's mastery learning: no advancement until the evidence says
  you know it. The gate is a Bayesian Knowledge Tracing posterior (Corbett & Anderson
  1995) ≥ 0.95 *plus* a 3-correct streak, so guessing can't open it.
- **Spacing + interleaving** — mastered concepts fall due for review at expanding
  intervals (SM-2 family; Cepeda et al. 2006), served as a mixed queue.
- **The meter is the lesson** — the mastery bar is a live Bayes update on P(you know
  this), shown as `prior → posterior` after every answer. For the Bayesian Inference
  topic, the interface demonstrates the subject on the learner themselves.

## Adding topic #2 … #100

### The preface (2026-08-12)

Every pack opens with a `preface` before the `intro`. They are different jobs and should not be
merged: the **intro** argues why a subject deserves an adult's time; the **preface** makes the
central idea land at all, for a curious and inquisitive twelve-year-old, using only what that
reader already has. One concrete image, no jargon, no formula.

```json
"preface": {
  "hook":   "one line that has to earn the next thirty seconds (< 200 chars)",
  "anim":   "optional; must already be registered in static/index.html",
  "body":   ["up to 6 short paragraphs"],
  "aha":    [{"title": "...", "text": "..."}],   // 1-3 takeaway cards
  "closer": "one line"
}
```

`validate_pack.py` gates it the same way it gates the intro, plus two checks specific to this
audience: the average sentence must stay at or under **26 words** and no single sentence may
exceed **48**, because sentence length is where an explanation stops reading as plain speech.
Jargon cannot be checked automatically — that part is on the author.

Measured across the 24 shipped prefaces: average sentence **14.2 words**, longest **39**.

Copy `topics/bayesian-inference.json` as a template. Schema per concept:

```jsonc
{
  "id": "c01-slug",
  "title": "...",
  "summary": "one-line gist (also used as the fallback hint)",
  "cards":     [{ "h": "chunk title", "md": "markdown body" }],   // 2–4 chunks
  "questions": [{ "id": "q1", "type": "mcq", "prompt": "...",
                  "options": ["...","...","...","..."],
                  "answer": 1,                       // index of correct option
                  "feedback": ["why each option is right/wrong", ...] }],
  "resources": [{ "label": "...", "url": "...", "type": "video|interactive|reading",
                  "note": "why it's worth their time" }],
  "anim": "square"        // optional; omit if no built-in animation applies
}
```

Concept order in the file **is** the unlock order. No code changes, no restart
required — packs are read per request.

### Scaling with the authoring pipeline

`author_topic.py` drafts new packs with Claude to this exact schema. The workflow is
two-gated by design:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 author_topic.py llms                        # → topics/_draft-llms.json (not live)
python3 validate_pack.py topics/_draft-llms.json --links   # gate 1: schema + link check
# gate 2: read every card, question, and feedback line yourself; fix what's wrong
mv topics/_draft-llms.json topics/llms.json          # goes live
```

`topics/_roadmap.json` predefines the next seven topics with their prerequisite graph
and suggested concept ladders (Newtonian physics, LLMs, AI agents, AGI, ODEs, Fourier
series, PDEs). Arbitrary topics: `author_topic.py --custom "Title" --slug slug`.
Underscore-prefixed files are never loaded by the app, so drafts can't leak to
learners. A pack's optional `prereqs` list displays as "builds on …" on the dashboard.
Note: the link checker reports 403s as warnings — several reputable sites
(e.g. Smarthistory) bot-block scripted requests; confirm those in a browser.

Animations live in `static/index.html` under `ANIMS` (canvas draw + narration script
via the Web Speech API — audio works offline, mute toggle included, respects
`prefers-reduced-motion`). Add a new `ANIMS.yourkey` and reference it from a pack.

## Data & privacy

Everything lives in one SQLite file you control. Passwords are PBKDF2-SHA256
(240k iterations). No telemetry, no third-party calls except the optional
Anthropic remediation endpoint (which sends concept summary + wrong-answer IDs,
never credentials). Per-user isolation is enforced in every query.

## Notes / failure conditions

- Voice narration uses the browser's built-in speech synthesis; on a machine with no
  voices installed, captions still advance on timers.
- The AI "Explain it differently" button requires `ANTHROPIC_API_KEY` on the server;
  without it you get the authored fallback, clearly labeled.
- Review scheduling is per-concept, not per-question; a due concept re-serves its
  full question set interleaved with other due concepts.

## Completion certificates & spaced-review sessions (2026-08-04)

**Certificate of completion.** When the answer that masters the *last* concept in a pack clears the
gate, the checkpoint offers a certificate. It is issued **server-side** (`GET /api/topic/{tid}/certificate`)
and **refuses unfinished work** with a 409 naming how many concepts are short — there is no path to a
certificate for a course you did not finish. Every number on it is read from the answer log (concepts,
questions answered, accuracy, elapsed days); the wall-worthiness is meant to come from stating the real
standard — *95% posterior confidence with three consecutive correct answers at every gate*, which is
stricter than most graded courses — not from imitating an accreditation.

* **Verification code** is HMAC'd with a per-install key (`app_meta.cert_key`), so this server can
  re-derive it and someone holding the recipe cannot. `GET /api/certificate/verify` is public on
  purpose: a check that needs the holder's login proves nothing to anyone else.
* **The name** on it is the student's to set (`POST /api/me/display_name` → `users.display_name`) —
  a login handle is not the name you frame.
* **Design**: A4 landscape, engraved double rule, drawn SVG seal, **no images and no external assets**,
  so it prints identically offline. Print CSS hides all app chrome; enable "Background graphics".
* **Issuer/signer are env-overridable**: `LEARN_CERT_ISSUER`, `LEARN_CERT_SIGNER`, `LEARN_CERT_SIGNER_TITLE`.
* A 🎓 rung stays on the ladder afterwards — a certificate you can only see once is a screenshot, not a record.

**Spaced-review sessions.** FSRS has scheduled reviews since P2.7, but nothing in the UI ever opened the
queue, so "N reviews due" was a badge with no door. Now: `GET /api/review` (cross-topic) and
`GET /api/topic/{tid}/review` serve **one retrieval per due concept** (not every question it owns — that
is a re-test, not a review), **rotating by attempt count** so successive cycles ask something different,
**most overdue first**. Answers post through the *normal* answer route, so FSRS rescheduling, relearn
demotion and the ungraded-free-response rule behave exactly as they do in a checkpoint. Entry points:
dashboard button (all topics), a 🔁 ladder rung, and the course-complete stage.

**Performance.** `load_topics()` is cached behind a directory signature (name, mtime_ns, size): 7.44 ms
→ 0.04 ms per call. The house *edit-file → refresh* convention is preserved — saving a pack moves its
mtime and the next request reloads (and `packver.maybe_sync` re-runs).

## Pack versioning & migrations (P5.17, 2026-07-22)

Every topic pack is content-hashed on load; versions live in `pack_versions`
(one row per registered edit, with the concept/question id manifest). Editing
a live pack is now safe:

- **Renaming a concept id**: declare it in the pack —
  `"migrations": {"renamed_concepts": {"old-id": "new-id"}}` — and every
  user's `concept_state` row moves to the new id on the first request after
  the edit. If a user already has state under the new id, the old row is
  archived, never clobbered.
- **Removing a concept**: its state rows are archived to
  `concept_state_archive` (with from/to pack hashes and a reason), never
  silently orphaned.
- **Question-id churn**: registers a new pack version but touches no state
  (state is per-concept; `answers` is a historical log and keeps old ids).

`validate_pack.py` checks migration declarations (target must exist, source
must not). Sync runs inside `load_topics()` behind an in-memory hash cache —
the edit-file → refresh convention holds, with one cheap sync on the first
request after a real change. Tests: `tests/test_packver.py`.
