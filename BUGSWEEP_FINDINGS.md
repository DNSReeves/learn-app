# Learn-app bug sweep — 2026-07-31

Source: 8 read-only auditor agents (David-directed), findings verified against code. Owners:
**FE** = frontend `static/index.html` (Local's lane) · **BE** = backend (Remote's lane:
`app.py` / `db.py` / `mastery.py` / `packver.py` / `grading.py` / `validate_pack.py`).

Severity: P0 = data-loss/security/gate-bypass/DoS · P1 = wrong behavior a user hits ·
P2 = edge/robustness · P3 = cosmetic. **Latent** = real but not reachable via current live content.

---

## P0

- **BE-1 · import merge regresses mastery** — `app.py:133`. `(row.attempts, row.p_mastery) < (cur.attempts, cur.p_mastery)` compares lexicographically; a weaker row with MORE attempts overwrites mastered progress and wipes `mastered_at`. *Repro:* local `attempts=4,p=0.92,mastered`; import `attempts=20,p=0.40` → `(20,0.40)<(4,0.92)` False → not skipped → weaker wins. *Fix:* never let an import lower `p_mastery` or clear `mastered_at` on an already-mastered local row; require BOTH `attempts>=cur` AND `p_mastery>=cur` (or a real strength score) to overwrite. **Couple with FE-1.**
- **BE-2 · ladder gate not enforced on `/answer`** — the gate lives only in concept GET (`app.py:256-261`); the answer route runs no `idx>0 → prev mastered/relearn` check. *Repro:* placement returns `question.id` for all concepts; grab a locked concept's qid, brute-force `choice` (server reveals `correct`), repeat correct 3× → `mastered_at` set → next concept's GET gate passes. Ladder bypassed. *Fix:* resolve the concept index in `answer()` and run the same predecessor-mastered/relearn check; 403 if locked. (Also consider not letting one repeated `question_id` satisfy the streak.)
- **BE-3 · regex grading ReDoS** *(latent — no live regex questions)* — `grading.py:43` `re.search(author_pat, learner_text, IGNORECASE)`, uncompiled, no timeout, learner controls text length. Measured `(a+)+$` + 30-char answer = 54.9s CPU; sync route has no try/except → threadpool exhaustion. *Fix:* precompile patterns at pack-load, cap `len(text)`, run under a hard timeout (or step budget); reject vulnerable patterns in `validate_pack`.

## P1 — frontend (Local)

- **FE-1 · import double-encodes body** — `static/index.html:384` passes `body:JSON.stringify(body)` but `api()` also stringifies → server gets a JSON string literal → import always fails. *Fix:* `body: body`. **Hold until BE-1/BE-4/BE-5 land** so a working import can't run the regressing/unvalidated merge.
- **FE-2 · answer-submit no try/catch → deadlock** — `static/index.html:~683` (and free `~674`): options disabled then `await /answer`; any throw leaves them permanently disabled, `#after` empty. **[FIXED, uncommitted — try/catch + re-enable]**
- **FE-3 · ungraded branch has no exit** — `static/index.html:636-643`: free-response + AI grader down renders only "Next question"; `qIx%len` cycles, mastery frozen → loop, no "Next concept". *Fix:* give it the `r.mastered`-gated exit + a "Back to ladder" affordance.

## P1 — backend (Remote)

- **BE-4 · import writes client values unvalidated → forgeable mastery** — `app.py:135-141`. No clamp of `p_mastery∈[0,1]`, `correct<=attempts`, `mastered_at<=now`, `interval_d>=0`. *Repro:* POST `p_mastery:1.0, mastered_at:now, attempts:999999` for every concept → instantly masters the whole course (self-scoped, but corrupts SR + analytics; `p_mastery:5.0`/`-3` breaks decay math). *Fix:* validate/clamp on import.
- **BE-5 · import 500s on malformed JSON** — `app.py:128/133/144`. `STATE_COLS.issuperset(row)` on a non-iterable (`[null]`,`[1]`) → TypeError; equal-attempts with non-numeric `p_mastery` → `str<float` TypeError; `placements:[null]` → TypeError. *Fix:* `isinstance(row, dict)` guard + numeric coercion per loop.
- **BE-6 · `/answer` 500 on bad `choice`** — `app.py:~479` `q["feedback"][a.choice]` with `choice` None (missing) → TypeError, or out-of-range int → IndexError → 500. Practice route validates `0<=choice<=3`; this one doesn't. *Fix:* require `choice is not None and 0<=choice<len(options)` → 400 else.
- **BE-7 · `validate_pack` global-`errs` race** — `validate_pack.py:16` module-global `errs`; author endpoints are sync → shared threadpool; concurrent validations clobber each other's list. Worst interleaving returns `[]` for a bad pack → `author_upload` proceeds with a non-slug `pid` → `open(join(STAGED_DIR, f"{pid}.json"))` escapes `topics_staged/` (path traversal). Author-gated. *Fix:* make `check()` return a local list; re-validate `pid` as `^[a-z0-9-]+$` and `realpath` inside `STAGED_DIR` before `open()`.
- **BE-8 · invalid regex → `re.error` 500** — `grading.py:43`. Pattern `"("` → uncaught `re.error`. *Fix:* try/except; compile-validate in `validate_pack`.
- **BE-9 · numeric wrong-answer crash on string `answer`** — `grading.py:37-39`. `{g['answer']:g}` when `answer:"5"` → `ValueError: Unknown format code 'g' for str`. Validator only checks `"answer" in g`. *Fix:* coerce `float(g["answer"])` once; require numeric type in validator.
- **BE-10 · regex grader/validator schema mismatch** — grader reads `g["patterns"]` (`grading.py:42`); validator requires `"answer"` for regex (`validate_pack.py:81`) and never checks `patterns`. A validator-passing regex question always grades False (unpassable); a working one fails predeploy. *Fix:* make grader+validator agree on `patterns` (non-empty, compilable).
- **BE-11 · packver silent-orphans state** — `packver.py:107-124,127`. A `renamed_concepts` entry whose target id is absent from the new pack is skipped AND excluded from the removal/archive set → old `concept_state` persists live under a dead id, unarchived. *Fix:* when rename target ∉ new_ids, fall through to archival.
- **BE-12 · one malformed pack file 500s the whole app** — `app.py:30-34`. `load_topics()` `json.load` per file has no per-file guard; the try/except wraps only `packver.maybe_sync`. A half-written file (edit→refresh) or bad drop makes every endpoint 500. *Fix:* per-file try/except, log-and-skip, serve the rest.

## P2

**Frontend (Local):**
- **FE-4** unescaped `r.type`/`r.url` in resources (`static/index.html:604-605`) → XSS/attr-injection via authored content. *Fix:* `esc()` + reject non-http(s) schemes.
- **FE-5** empty `questions` array crashes "Start checkpoint" (`:612` `[0%0]`→NaN→`q.type` throws). *Fix:* guard in `startCheckpoint`.
- **FE-6** "Explain it differently" sticks on failure (`:670-674`, no catch → button frozen "Thinking…"). *Fix:* try/catch restores label.
- **FE-7** empty `concepts` array throws (`:394` `T.concepts[-1].id`). *Fix:* guard.
- **FE-8** ladder-nav-mid-answer writes to wrong concept (`:632/645` read global `C` after await). **[FIXED with FE-2 — capture `myC`, ignore if `C!==myC`]**
- **FE-9** `md()` renders code fences/inline backticks literally (`:193-206`, ~12 cards incl. `ai-agents` JSON block). *Fix:* add inline-code + fenced-code branches to `md()`.

**Backend (Remote):**
- **BE-13** placement seeds mastery at p=0.90 but `RELEARN_P=0.70` is tuned for gate-entry ~0.95 → a placement-trusted concept demotes on ONE miss (`app.py:381`, `bkt_update(0.90,F)=0.55`). *Fix:* seed at `MASTERY_P`, or gate demotion on `p_before>=MASTERY_P`.
- **BE-14** placement mastery mis-seeds FSRS stability (0.5) → churns at the 12h floor (`app.py:382/458` vs gate path `470`). *Fix:* seed `stability=FSRS_W[2]`.
- **BE-15** archive drops `relearn` flag — `concept_state_archive` has no `relearn` column (`db.py:83-94`, `packver._archive_rows`). *Fix:* additive `ALTER` + copy it.
- **BE-16** non-atomic mastery RMW — `db.get_states` (`app.py:432`) then `db.upsert_state` (`:473`) are separate txns; concurrent same-user answers lose updates (double-submit/two tabs). *Fix:* SELECT+UPDATE in one `conn()`, or relative SQL update.
- **BE-17** whole-topic orphan on `topic_id` change / file removal — versioning diffs only within a topic_id; a vanished topic_id strands all its state (`packver`/`load_topics`). *Fix:* archive state for registered topic_ids no longer present.
- **BE-18** regex match unanchored + IGNORECASE → wrong answers grade correct (`grading.py:43`, `"no"` matches "I do not know"). *Fix:* `fullmatch`/anchor.
- **BE-19** numeric default tolerance 0 (exact float compare) — `grading.py:34`; `1/3` rejects `0.333`. *Fix:* small epsilon default.
- **BE-20** numeric parser grabs first number, no scientific notation — `grading.py:14`; `3.0e8`→`3`. *Fix:* add `[eE][+-]?\d+`.
- **BE-21** practice `gen:` answers pollute analytics counts — `app.py:160-189` (three queries) lack the `gen:%` exclusion calibration uses. *Fix:* `AND a.question_id NOT LIKE 'gen:%'`.
- **BE-22** retention buckets by CURRENT `interval_d`, not answer-time — `app.py:170-176`. *Fix:* snapshot interval on each answer, bucket on that.
- **BE-23** unbounded author upload size (`app.py:576`, `PackDraft.pack: dict` no cap). *Fix:* body-size + concept-count ceiling.
- **BE-24** login timing side-channel enables username enumeration — `db.py:158-166` skips PBKDF2 on missing user. *Fix:* dummy-hash the missing branch (constant time).

## P3
- **BE-25** `recent_misses` returns `gen:` practice misses into AI remediation context (`db.py:243`). *Fix:* exclude `gen:%`.
- **BE-26** `_practice_cache` unbounded (process memory, cosmetic).

---

## Verified clean (ruled-out hypotheses)
- BKT + FSRS math sound (numerically stress-tested — no div0/NaN, bounds hold, gate reachable at 3 correct, no relearn strand/loop).
- **All 14 packs' content clean** — no mis-keyed/unpassable MCQs (math recomputed on ~380), no missing/broken anims (all 55 resolve), no malformed URLs, no dupe ids.
- Auth solid — PBKDF2 (240k) + per-user salt + constant-time compare; `secrets` tokens; expiry + logout invalidation; role gate no self-grant; SQL fully parameterized.
- **Practice never leaks into mastery** — `practice_answer` never touches `concept_state`; `gen:` id excluded from calibration.
- **Benign content edits do NOT reset progress** — reproduced across a hash-changing card edit (the terminology pass was safe).
- `calibration.order_questions` identity-safe; rubric grading crash-safe (AI-down → honest UNGRADED).
