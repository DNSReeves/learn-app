"""Learn — mastery-based learning dashboard.

Run:  uvicorn app:app --host 0.0.0.0 --port 8090
"""
import json
import logging
import os
import time

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai
import db
import mastery
import packver

BASE = os.path.dirname(os.path.abspath(__file__))
TOPIC_DIR = os.path.join(BASE, "topics")

app = FastAPI(title="Learn")
db.init()

# ---------- topic packs (the extensible part: drop a JSON in topics/) ----------

def load_topics() -> dict:
    packs = {}
    for fn in sorted(os.listdir(TOPIC_DIR)):
        if fn.endswith(".json") and not fn.startswith("_"):
            with open(os.path.join(TOPIC_DIR, fn)) as f:
                p = json.load(f)
            packs[p["id"]] = p
    # P5.17 (iss_8d424360): register/diff pack versions; migrate or archive
    # concept_state on a live edit instead of silently orphaning. In-memory
    # hash cache makes this a no-op except on the first request after a pack
    # file actually changes — the edit-file → refresh convention holds.
    try:
        packver.maybe_sync(packs)
    except Exception:
        logging.getLogger("learn").exception("pack version sync failed (serving anyway)")
    return packs


def require_user(authorization: str | None):
    token = (authorization or "").removeprefix("Bearer ").strip()
    user = db.user_for_token(token)
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    return user


# ---------- auth ----------

class Creds(BaseModel):
    username: str
    password: str


@app.post("/api/register")
def register(c: Creds):
    try:
        db.create_user(c.username, c.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = db.authenticate(c.username, c.password)
    return {"token": token, "role": (db.user_for_token(token) or {}).get("role", "learner")}


@app.post("/api/login")
def login(c: Creds):
    token = db.authenticate(c.username, c.password)
    if not token:
        raise HTTPException(401, "Username and password don't match.")
    return {"token": token, "role": (db.user_for_token(token) or {}).get("role", "learner")}


@app.post("/api/logout")
def do_logout(authorization: str | None = Header(default=None)):
    db.logout((authorization or "").removeprefix("Bearer ").strip())
    return {"ok": True}


# ---------- dashboard ----------

# ---------- P4.16 progress export/import (iss_bbc24580) ----------
# Move YOUR progress between deployments: a self-describing JSON of concept_state +
# placements (+ answer count for honesty — raw answers stay put: they are analytics
# bulk, not state, and importing them could collide ids). Import is per-user MERGE:
# a row wins only if its (attempts, p_mastery) evidence is >= the local row's — a
# stale export can never regress live progress silently.
@app.get("/api/progress/export")
def progress_export(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with db.conn() as c:
        states = [dict(r) for r in c.execute(
            "SELECT topic_id, concept_id, p_mastery, attempts, correct, streak, unlocked, "
            "mastered_at, interval_d, ease, due_at, relearn, stability, difficulty "
            "FROM concept_state WHERE user_id=?",
            (user["id"],))]
        placements = [dict(r) for r in c.execute(
            "SELECT topic_id, level, at FROM placements WHERE user_id=?", (user["id"],))]
        n_answers = c.execute("SELECT COUNT(*) FROM answers WHERE user_id=?", (user["id"],)).fetchone()[0]
    return {"format": "learn-progress/1", "exported_at": time.time(),
            "username": user["username"], "concept_state": states,
            "placements": placements, "answers_recorded": n_answers,
            "note": "answers stay on the source deployment (analytics, not state)"}


class ProgressImport(BaseModel):
    format: str
    concept_state: list = []
    placements: list = []


@app.post("/api/progress/import")
def progress_import(body: ProgressImport, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    if body.format != "learn-progress/1":
        raise HTTPException(400, "unknown format (want learn-progress/1)")
    STATE_COLS = {"topic_id", "concept_id", "p_mastery", "attempts", "correct", "streak",
                  "unlocked", "mastered_at", "interval_d", "ease", "due_at", "relearn",
                  "stability", "difficulty"}
    imported = skipped = 0
    with db.conn() as c:
        for row in body.concept_state:
            if not STATE_COLS.issuperset(row) or "topic_id" not in row or "concept_id" not in row:
                skipped += 1; continue
            cur = c.execute("SELECT attempts, p_mastery FROM concept_state WHERE user_id=? AND "
                            "topic_id=? AND concept_id=?",
                            (user["id"], row["topic_id"], row["concept_id"])).fetchone()
            if cur and (row.get("attempts", 0), row.get("p_mastery", 0)) < (cur[0], cur[1]):
                skipped += 1; continue        # local evidence is stronger — never regress
            fields = {k: row[k] for k in row if k in STATE_COLS and k not in ("topic_id", "concept_id")}
            cols = ", ".join(fields)
            c.execute(f"INSERT INTO concept_state (user_id, topic_id, concept_id, {cols}) "
                      f"VALUES ({','.join('?' * (3 + len(fields)))}) "
                      f"ON CONFLICT(user_id, topic_id, concept_id) DO UPDATE SET "
                      + ", ".join(f"{k}=excluded.{k}" for k in fields),
                      (user["id"], row["topic_id"], row["concept_id"], *fields.values()))
            imported += 1
        for pl in body.placements:
            if "topic_id" not in pl: continue
            c.execute("INSERT OR IGNORE INTO placements (user_id, topic_id, level, at) VALUES (?,?,?,?)",
                      (user["id"], pl["topic_id"], pl.get("level"), pl.get("at") or time.time()))
        c.commit()
    return {"imported": imported, "skipped": skipped,
            "note": "skipped = malformed OR local progress was already ahead (never regressed)"}


# ---------- P4.13 per-user analytics + P4.14 session summary (iss_c5379270 / iss_16ad79eb) ----------
@app.get("/api/analytics")
def analytics(authorization: str | None = Header(default=None)):
    """Per-user learning analytics — every number from the answers/state tables, and the
    sparse-data cases say so instead of charting noise."""
    user = require_user(authorization)
    with db.conn() as c:
        # time-to-mastery: first answer -> mastered_at, per mastered concept
        ttm = [dict(r) for r in c.execute("""
            SELECT s.topic_id, s.concept_id,
                   ROUND((s.mastered_at - MIN(a.at)) / 3600.0, 2) AS hours,
                   COUNT(a.id) AS attempts
            FROM concept_state s JOIN answers a
              ON a.user_id = s.user_id AND a.topic_id = s.topic_id AND a.concept_id = s.concept_id
            WHERE s.user_id = ? AND s.mastered_at IS NOT NULL AND a.at <= s.mastered_at
            GROUP BY s.topic_id, s.concept_id ORDER BY hours""", (user["id"],))]
        # retention: review-mode outcomes bucketed by scheduled interval
        ret = [dict(r) for r in c.execute("""
            SELECT CASE WHEN s.interval_d < 2 THEN '1d' WHEN s.interval_d < 5 THEN '2-4d'
                        WHEN s.interval_d < 10 THEN '5-9d' ELSE '10d+' END AS bucket,
                   COUNT(*) AS n, ROUND(AVG(a.is_correct), 3) AS recall
            FROM answers a JOIN concept_state s
              ON s.user_id = a.user_id AND s.topic_id = a.topic_id AND s.concept_id = a.concept_id
            WHERE a.user_id = ? AND s.mastered_at IS NOT NULL AND a.at > s.mastered_at
            GROUP BY bucket""", (user["id"],))]
        weakest = [dict(r) for r in c.execute("""
            SELECT topic_id, concept_id, ROUND(p_mastery, 3) AS p, attempts, correct
            FROM concept_state WHERE user_id = ? AND attempts > 0 AND mastered_at IS NULL
            ORDER BY p_mastery ASC LIMIT 8""", (user["id"],))]
        n_review = c.execute("""SELECT COUNT(*) FROM answers a JOIN concept_state s
            ON s.user_id=a.user_id AND s.topic_id=a.topic_id AND s.concept_id=a.concept_id
            WHERE a.user_id=? AND s.mastered_at IS NOT NULL AND a.at > s.mastered_at""",
            (user["id"],)).fetchone()[0]
    return {"time_to_mastery": ttm, "retention": ret, "weakest": weakest,
            "review_answers": n_review,
            "retention_note": (None if n_review >= 30 else
                               f"only {n_review} review answers so far — the retention curve "
                               "becomes meaningful as spaced reviews accumulate")}


@app.get("/api/topics")
def topics(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    out = []
    packs = load_topics()
    for tid, pack in packs.items():
        states = db.get_states(user["id"], tid)
        n = len(pack["concepts"])
        mastered = sum(1 for c in pack["concepts"]
                       if states.get(c["id"], {}).get("mastered_at"))
        due = sum(1 for c in pack["concepts"]
                  if (s := states.get(c["id"])) and s.get("mastered_at")
                  and s.get("due_at") and s["due_at"] < time.time())
        out.append({"id": tid, "title": pack["title"], "tagline": pack.get("tagline", ""),
                    "description": pack.get("description", ""),
                    "prereqs": [packs[p]["title"] for p in pack.get("prereqs", []) if p in packs],
                    "concepts": n, "mastered": mastered, "review_due": due})
    return {"user": user["username"], "topics": out, "ai": ai.available()}


@app.get("/api/topic/{tid}")
def topic(tid: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    pack = load_topics().get(tid)
    if not pack:
        raise HTTPException(404, "No such topic.")
    states = db.get_states(user["id"], tid)
    needs_placement = (db.placement_status(user["id"], tid) is None
                       and not db.has_answers(user["id"], tid)
                       and not states)
    concepts, prev_mastered = [], True
    for c in pack["concepts"]:
        s = states.get(c["id"], {})
        p = s.get("p_mastery", mastery.P_INIT)
        mastered = bool(s.get("mastered_at"))
        relearn = bool(s.get("relearn")) and not mastered
        unlocked = prev_mastered  # a concept opens when its predecessor is mastered
        due = bool(mastered and s.get("due_at") and s["due_at"] < time.time())
        concepts.append({
            "id": c["id"], "title": c["title"], "summary": c["summary"],
            "p_mastery": mastery.decay_for_review(p, s.get("due_at")) if mastered else p,
            "streak": s.get("streak", 0), "attempts": s.get("attempts", 0),
            "mastered": mastered, "unlocked": unlocked, "review_due": due,
            "relearn": relearn,
            "has_anim": bool(c.get("anim")), "n_resources": len(c.get("resources", [])),
        })
        # P2.6: a concept in RELEARN was once mastered — successors stay open
        prev_mastered = mastered or relearn
    return {"id": tid, "title": pack["title"], "tagline": pack.get("tagline", ""),
            "concepts": concepts, "needs_placement": needs_placement,
            "mastery_p": mastery.MASTERY_P, "streak_gate": mastery.STREAK_GATE}


@app.get("/api/topic/{tid}/concept/{cid}")
def concept(tid: str, cid: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    pack = load_topics().get(tid)
    if not pack:
        raise HTTPException(404, "No such topic.")
    idx = next((i for i, c in enumerate(pack["concepts"]) if c["id"] == cid), None)
    if idx is None:
        raise HTTPException(404, "No such concept.")
    # enforce the gate server-side
    states = db.get_states(user["id"], tid)
    if idx > 0:
        prev = pack["concepts"][idx - 1]["id"]
        ps = states.get(prev, {})
        # P2.6: a prev in RELEARN was once mastered — the ladder stays traversable
        if not (ps.get("mastered_at") or ps.get("relearn")):
            raise HTTPException(403, "Master the previous concept to unlock this one.")
    c = pack["concepts"][idx]
    s = states.get(cid, {})
    # strip answers/feedback — grading happens server-side only
    qs = [{"id": q["id"], "type": q["type"], "prompt": q["prompt"], "options": q["options"]}
          for q in c["questions"]]
    # P2.5 (2026-07-23): serve the item most informative at the learner's current
    # P(known) instead of authored order. Fresh concepts (no calibrated items) come
    # back in authored order byte-identically; failures fall back the same way —
    # question ORDER must never 500 a checkpoint.
    try:
        import calibration
        qs = calibration.order_questions(qs, tid, cid,
                                         s.get("p_mastery", mastery.P_INIT))
    except Exception:
        pass
    return {"id": cid, "title": c["title"], "summary": c["summary"], "cards": c["cards"],
            "anim": c.get("anim"), "resources": c.get("resources", []),
            "questions": qs,
            "p_mastery": s.get("p_mastery", mastery.P_INIT),
            "streak": s.get("streak", 0), "mastered": bool(s.get("mastered_at")),
            "relearn": bool(s.get("relearn"))}


# ---------- placement (adaptive prior-setting) ----------

class PlacementBody(BaseModel):
    # history of answers so far in this placement run; server re-grades everything
    history: list[dict] = []   # [{"concept_id":..., "question_id":..., "choice":...}]
    skip: bool = False


def _graded_map(pack, history):
    graded = {}
    for h in history:
        c = next((c for c in pack["concepts"] if c["id"] == h.get("concept_id")), None)
        q = next((q for q in (c or {}).get("questions", []) if q["id"] == h.get("question_id")), None)
        if c and q:
            graded[c["id"]] = (h.get("choice") == q["answer"])
    return graded


def _placement_search(pack, history):
    """Binary search over the ladder. Returns (next_concept_index | None, level).
    level = index of the first concept the learner should study."""
    lo, hi = 0, len(pack["concepts"]) - 1
    graded = _graded_map(pack, history)
    while lo <= hi:
        mid = (lo + hi) // 2
        cid = pack["concepts"][mid]["id"]
        if cid not in graded:
            return mid, None          # next probe needed
        if graded[cid]:
            lo = mid + 1
        else:
            hi = mid - 1
    return None, lo                   # search complete; study from index lo


# P2.8 (iss_a617455d): placement v2 — gappy-knowledge detection. The binary
# search assumes monotone ladder knowledge, so everything below the frontier
# was blanket-trusted; real prior knowledge has holes. Phase 2 sweeps the
# below-frontier concepts the search never probed (front-to-back, bounded by
# PLACEMENT_GAP_CAP extra questions). A wrong sweep answer marks a GAP —
# served via the P2.6 relearn state (cards re-serve, gate re-runs, successors
# stay open), so a gap never re-locks the ladder the learner placed past.
# Note the search itself can't produce below-frontier wrongs (a wrong probe
# drags the frontier below it), so gaps come only from the sweep.
PLACEMENT_GAP_CAP = 6


def _placement_v2(pack, history):
    """Returns (next_probe_index | None, level | None, gap_indices)."""
    nxt, level = _placement_search(pack, history)
    if nxt is not None:
        return nxt, None, []
    graded = _graded_map(pack, history)
    max_q = _placement_max_q(pack)
    for i in range(level):
        cid = pack["concepts"][i]["id"]
        if cid not in graded and len(history) < max_q:
            return i, None, []        # next sweep probe (budget remaining)
    gaps = [i for i in range(level)
            if graded.get(pack["concepts"][i]["id"]) is False]
    return None, level, gaps


def _placement_max_q(pack) -> int:
    return len(pack["concepts"]).bit_length() + 1 + PLACEMENT_GAP_CAP


@app.post("/api/topic/{tid}/placement")
def placement(tid: str, body: PlacementBody,
              authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    pack = load_topics().get(tid)
    if not pack:
        raise HTTPException(404, "No such topic.")
    if body.skip:
        db.set_placement(user["id"], tid, None)
        return {"done": True, "level": 0, "skipped": True}

    nxt, level, gaps = _placement_v2(pack, body.history)
    if nxt is not None:
        c = pack["concepts"][nxt]
        q = c["questions"][0]
        return {"done": False,
                "asked": len(body.history) + 1,
                "max_questions": _placement_max_q(pack),
                "question": {"concept_id": c["id"], "concept_title": c["title"],
                             "id": q["id"], "prompt": q["prompt"], "options": q["options"]}}

    # apply the result (P2.8: below-frontier gaps → relearn, not blanket trust)
    now = time.time()
    gap_set = set(gaps)
    for i, c in enumerate(pack["concepts"]):
        if i in gap_set:
            db.upsert_state(user["id"], tid, c["id"], p_mastery=0.30, relearn=1)
        elif i < level:
            # trust but verify: mastered, but due for review immediately
            db.upsert_state(user["id"], tid, c["id"], p_mastery=0.90,
                            mastered_at=now, interval_d=0.5, due_at=now)
        elif i == level:
            db.upsert_state(user["id"], tid, c["id"], p_mastery=0.50)
    db.set_placement(user["id"], tid, level)
    return {"done": True, "level": level, "skipped": False,
            "start_title": pack["concepts"][level]["title"] if level < len(pack["concepts"]) else None,
            "verified_later": max(0, level - len(gaps)),
            "gaps": [{"index": i, "id": pack["concepts"][i]["id"],
                      "title": pack["concepts"][i]["title"]} for i in gaps]}


# ---------- answering ----------

class Answer(BaseModel):
    question_id: str
    choice: int | None = None          # mcq
    text: str | None = None           # P2.9 free-response
    latency_ms: int | None = None


@app.post("/api/topic/{tid}/concept/{cid}/answer")
def answer(tid: str, cid: str, a: Answer,
           authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    pack = load_topics().get(tid)
    c = next((c for c in (pack or {}).get("concepts", []) if c["id"] == cid), None)
    if not c:
        raise HTTPException(404, "No such concept.")
    q = next((q for q in c["questions"] if q["id"] == a.question_id), None)
    if not q:
        raise HTTPException(404, "No such question.")
    # P2.9 (iss_8c6eb4ee): free-response grading — LOCAL first (numeric tolerance /
    # regex need no API call); the LLM grades only rubric-kind questions. When the
    # LLM is unavailable the answer is honestly UNGRADED (mastery untouched, model
    # answer shown) — never silently marked wrong, never guessed right.
    fr_feedback = None
    if q.get("type") == "free":
        import grading
        verdict = grading.grade(q, a.text or "")
        if verdict is None:                       # rubric question, AI down → ungraded
            db.log_answer(user["id"], tid, cid, a.question_id,
                          (a.text or "")[:400], 0, a.latency_ms)
            return {"correct": None, "ungraded": True,
                    "feedback": "AI grading is unavailable right now — compare your answer "
                                "with the model answer below; your mastery was not changed.",
                    "model_answer": (q.get("grading") or {}).get("model_answer", "")}
        correct, fr_feedback = verdict
    else:
        correct = a.choice == q["answer"]

    s = db.get_states(user["id"], tid).get(cid, {})
    p_before = s.get("p_mastery", mastery.P_INIT)
    p_after = mastery.bkt_update(p_before, correct)
    streak = (s.get("streak", 0) + 1) if correct else 0
    was_mastered = bool(s.get("mastered_at"))
    now_mastered = was_mastered or mastery.is_mastered(p_after, streak)

    fields = dict(
        p_mastery=p_after,
        attempts=s.get("attempts", 0) + 1,
        correct=s.get("correct", 0) + int(correct),
        streak=streak,
        mastered_at=s.get("mastered_at") or (time.time() if now_mastered else None),
    )
    demoted = False
    if was_mastered and not correct and p_after < mastery.RELEARN_P:
        # P2.6 (iss_0824d5ad): the review failed hard — the concept drops out
        # of mastered into RELEARN: cards re-serve, the 0.95+streak gate re-runs.
        # Successors stay unlocked (relearn is not un-learning the ladder).
        demoted = True
        now_mastered = False
        fields.update(mastered_at=None, relearn=1, streak=0,
                      interval_d=0.0, due_at=None)
    elif was_mastered:  # review mode: reschedule (P2.7: FSRS replaced SM-2-lite)
        # Legacy rows (stability NULL) migrate by passing interval_d as
        # stability — exact at the 0.90 retention target where interval == S.
        prev_s = s.get("stability") or s.get("interval_d") or 0
        # elapsed since the last review, reconstructed from the old schedule
        last = (s["due_at"] - (s.get("interval_d") or 0) * 86400) if s.get("due_at") else None
        elapsed_d = max(0.0, (time.time() - last) / 86400) if last else 0.0
        s2, d2, iv, due = mastery.fsrs_review(prev_s, s.get("difficulty"), elapsed_d, correct)
        fields.update(stability=s2, difficulty=d2, interval_d=iv, due_at=due)
    elif now_mastered:  # (re-)mastery: first review lands tomorrow; relearn clears
        # Pedagogy unchanged (trust-then-verify: quick first check); FSRS state
        # initialized so the SECOND review schedules from real memory params.
        # A relearned concept keeps its surviving stability.
        fields.update(interval_d=1.0, due_at=time.time() + 86400, relearn=0,
                      mastered_at=time.time(),
                      stability=s.get("stability") or mastery.FSRS_W[2],
                      difficulty=s.get("difficulty") or mastery._fsrs_d0(3))

    db.upsert_state(user["id"], tid, cid, **fields)
    db.log_answer(user["id"], tid, cid, a.question_id,
                  (a.text or "")[:400] if q.get("type") == "free" else a.choice,
                  correct, a.latency_ms)

    return {"correct": correct,
            "feedback": fr_feedback if q.get("type") == "free" else q["feedback"][a.choice],
            "p_before": round(p_before, 4), "p_after": round(p_after, 4),
            "streak": streak, "mastered": now_mastered,
            "newly_mastered": now_mastered and not was_mastered,
            "demoted": demoted}


# ---------- review queue (interleaved, spaced) ----------

@app.get("/api/topic/{tid}/review")
def review(tid: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    pack = load_topics().get(tid)
    if not pack:
        raise HTTPException(404, "No such topic.")
    states = db.get_states(user["id"], tid)
    queue = []
    for c in pack["concepts"]:
        s = states.get(c["id"])
        if s and s.get("mastered_at") and s.get("due_at") and s["due_at"] < time.time():
            for q in c["questions"]:
                queue.append({"concept_id": c["id"], "concept_title": c["title"],
                              "id": q["id"], "type": q["type"],
                              "prompt": q["prompt"], "options": q["options"]})
    # interleave across concepts: round-robin by original order
    queue.sort(key=lambda q: q["id"])
    return {"queue": queue}


# ---------- AI remediation (hybrid layer) ----------

class Stuck(BaseModel):
    question_id: str | None = None


@app.post("/api/topic/{tid}/concept/{cid}/help")
def help_me(tid: str, cid: str, body: Stuck,
            authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    pack = load_topics().get(tid)
    c = next((c for c in (pack or {}).get("concepts", []) if c["id"] == cid), None)
    if not c:
        raise HTTPException(404, "No such concept.")
    misses = db.recent_misses(user["id"], tid, cid)
    q = next((q for q in c["questions"] if q["id"] == body.question_id), None) \
        if body.question_id else None
    q_safe = {"prompt": q["prompt"], "options": q["options"]} if q else None
    text = ai.remediate(c, misses, q_safe)
    if text:
        return {"source": "ai", "text": text}
    # graceful fallback: re-serve the authored summary as the hint
    return {"source": "authored",
            "text": "Re-read the cards with this in mind: " + c["summary"]}


# ---------- P5.20 (iss_e8b3bfb8): author role — backend ----------
# Authoring is a ROLE (users.role = 'author'; grant via `db.py grant-author`).
# Uploads land in topics_staged/ ONLY — never the live topics/ dir; promotion
# to live remains the operator's deploy step (predeploy gate + restart), so a
# bad draft can never break the serving app. Validation reuses validate_pack.

STAGED_DIR = os.path.join(os.path.dirname(__file__), "topics_staged")


def require_author(authorization: str | None):
    user = require_user(authorization)
    if user.get("role") != "author":
        raise HTTPException(403, "Authoring requires the author role "
                                 "(grant: db.py grant-author <username>).")
    return user


def _validate_pack_dict(pack: dict) -> list[str]:
    """Run validate_pack.check on an in-memory pack; returns the error list.
    (validate_pack collects into a module-global — reset around each run.)"""
    import validate_pack as vp
    vp.errs = []
    try:
        vp.check(pack)
    except Exception as e:  # a malformed draft must report, not 500
        vp.errs.append(f"validator crashed on this draft: {type(e).__name__}: {e}")
    return list(vp.errs)


class PackDraft(BaseModel):
    pack: dict


@app.post("/api/author/pack/validate")
def author_validate(body: PackDraft,
                    authorization: str | None = Header(default=None)):
    require_author(authorization)
    errors = _validate_pack_dict(body.pack)
    return {"ok": not errors, "errors": errors,
            "concepts": len(body.pack.get("concepts", []) or [])}


@app.post("/api/author/pack/upload")
def author_upload(body: PackDraft,
                  authorization: str | None = Header(default=None)):
    user = require_author(authorization)
    errors = _validate_pack_dict(body.pack)
    if errors:
        return {"ok": False, "staged": None, "errors": errors}
    pid = body.pack["id"]                      # slug-validated by the checker
    os.makedirs(STAGED_DIR, exist_ok=True)
    path = os.path.join(STAGED_DIR, f"{pid}.json")
    with open(path, "w") as f:
        json.dump(body.pack, f, indent=1)
    live = pid in load_topics()
    return {"ok": True, "staged": f"topics_staged/{pid}.json", "errors": [],
            "overwrites_live": live,
            "note": ("Draft staged. Promotion to live topics/ is the deploy "
                     "step (predeploy gate + restart) — staged packs never "
                     "serve directly."),
            "by": user["username"]}


# ---------- P3.11 (iss_b98c7ee2): AI-generated supplementary practice ----------
# Extra formative reps, ephemeral by design: generated items live in a
# process-local cache (a restart honestly expires them), answer keys never
# reach the client, and grading NEVER touches BKT/streak/gate — an
# unvalidated AI item must not move mastery evidence in either direction.

_practice_cache: dict[tuple, dict] = {}   # (user_id, tid, cid) -> {qid: item}


class PracticeAnswer(BaseModel):
    question_id: str
    choice: int
    latency_ms: int | None = None


@app.post("/api/topic/{tid}/concept/{cid}/practice")
def gen_practice(tid: str, cid: str,
                 authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    pack = load_topics().get(tid)
    c = next((c for c in (pack or {}).get("concepts", []) if c["id"] == cid), None)
    if not c:
        raise HTTPException(404, "No such concept.")
    misses = db.recent_misses(user["id"], tid, cid)
    items = ai.generate_practice(c, misses, [q["prompt"] for q in c["questions"]])
    if not items:
        return {"available": False,
                "text": "AI practice isn't available right now — the authored "
                        "checkpoint questions are the gate either way."}
    _practice_cache[(user["id"], tid, cid)] = {it["id"]: it for it in items}
    return {"available": True, "formative_only": True,
            "questions": [{"id": it["id"], "type": "mcq", "prompt": it["prompt"],
                           "options": it["options"]} for it in items]}


@app.post("/api/topic/{tid}/concept/{cid}/practice/answer")
def practice_answer(tid: str, cid: str, body: PracticeAnswer,
                    authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    it = _practice_cache.get((user["id"], tid, cid), {}).get(body.question_id)
    if not it:
        return {"expired": True,
                "text": "That practice set expired — generate a fresh one."}
    if not 0 <= body.choice <= 3:
        raise HTTPException(400, "choice out of range")
    correct = body.choice == it["answer"]
    # analytics only — a "gen:" id keeps generated items out of calibration
    # (calibration.py keys on authored question ids) and no state is upserted
    db.log_answer(user["id"], tid, cid, f"gen:{body.question_id}",
                  body.choice, correct, body.latency_ms)
    return {"correct": correct, "answer": it["answer"],
            "feedback": it["feedback"][body.choice], "formative_only": True}


# ---------- static ----------

@app.get("/")
def index():
    # FLEET PATCH (deploy 2026-07-21, dnsr convention): no-cache on the app shell —
    # Safari served week-old pages on two sibling surfaces this week without it.
    # Re-apply on every rebuild from learn.zip (recorded in iss_1ffb8e2b).
    return FileResponse(os.path.join(BASE, "static", "index.html"),
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # P4.15 (iss_a0cef670): serve the service worker from ROOT so it may claim
    # scope '/' (a /static/-served SW could only scope /static/ and could never
    # cache the app-shell navigation or /api). Service-Worker-Allowed makes the
    # broad scope explicit; no-cache so SW updates deploy on refresh.
    return FileResponse(os.path.join(BASE, "static", "sw.js"),
                        media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/",
                                 "Cache-Control": "no-cache"})

app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
