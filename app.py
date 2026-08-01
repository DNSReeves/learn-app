"""Learn — mastery-based learning dashboard.

Run:  uvicorn app:app --host 0.0.0.0 --port 8090
"""
import json
import logging
import os
import re
import secrets
import time

import hmac

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai
import db
import mailer
import mastery
import packver

BASE = os.path.dirname(os.path.abspath(__file__))
TOPIC_DIR = os.path.join(BASE, "topics")

app = FastAPI(title="Learn")
db.init()

# Operator visibility. uvicorn configures ONLY its own loggers and leaves root
# bare, so everything this app logs under "learn" (the registration audit line,
# mail-transport warnings) silently died below the last-resort WARNING level.
# Give the tree one stderr handler at INFO; propagation stays on, so pytest's
# caplog still sees every record.
_applog = logging.getLogger("learn")
if not _applog.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _applog.addHandler(_h)
    _applog.setLevel(logging.INFO)

# ---------- topic packs (the extensible part: drop a JSON in topics/) ----------

def load_topics() -> dict:
    packs = {}
    for fn in sorted(os.listdir(TOPIC_DIR)):
        if fn.endswith(".json") and not fn.startswith("_"):
            # BE-12: one malformed file (a half-written edit→refresh save, a bad
            # drop) must not 500 the WHOLE app — log, skip, serve the rest.
            try:
                with open(os.path.join(TOPIC_DIR, fn)) as f:
                    p = json.load(f)
                if not isinstance(p, dict) or not p.get("id") or "concepts" not in p:
                    raise ValueError("not a topic pack (id/concepts missing)")
            except Exception as e:
                logging.getLogger("learn").error("topic %s skipped: %s", fn, e)
                continue
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
    invite: str | None = None


def _lan_client(request) -> bool:
    """True when the request came from loopback / RFC1918 / the tailnet CGNAT
    range. Coarse by design — the point is that internet exposure never gets
    the open behaviors (registration bootstrap, the username chooser)."""
    host = (request.client.host if request and request.client else "") or ""
    return (host == "::1" or host.startswith(("127.", "10.", "192.168.", "100."))
            or host.startswith("172.") and host.split(".")[1].isdigit()
            and 16 <= int(host.split(".")[1]) <= 31)


def _reg_mode() -> str:
    """LEARN_REGISTRATION: open | invite | closed. Default `open` — the
    operator's 2026-08-01 decision: anyone with a working email address may
    register, the emailed code is the gate. `invite` additionally requires
    LEARN_INVITE_CODE; `closed` refuses every path."""
    m = (os.environ.get("LEARN_REGISTRATION") or "open").strip().lower()
    return m if m in ("open", "invite", "closed") else "open"


@app.post("/api/register")
def register(c: Creds, request: Request = None):
    # Phase A: registration is INVITE-GATED (shared devices today, possibly the
    # internet tomorrow — an open /api/register is an account-creation hole).
    # LEARN_INVITE_CODE enables invites; without it registration is closed,
    # except the bootstrap case: a fresh install with zero users, from the LAN.
    # NOTE this is the LEGACY password-only path. It stays invite-gated even
    # when LEARN_REGISTRATION=open — `open` means "open via a verified EMAIL"
    # (/api/register/start), never "open with no verification at all".
    if _reg_mode() == "closed":
        raise HTTPException(403, "Registration is closed.")
    code = os.environ.get("LEARN_INVITE_CODE", "")
    if code:
        if not c.invite or not hmac.compare_digest(c.invite, code):
            raise HTTPException(403, "Registration needs a valid invite code.")
    elif not (db.user_count() == 0 and _lan_client(request)):
        raise HTTPException(403, "Registration is closed — ask the operator "
                                 "for an invite code.")
    try:
        db.create_user(c.username, c.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = db.authenticate(c.username, c.password)
    return {"token": token, "role": (db.user_for_token(token) or {}).get("role", "learner")}


@app.get("/api/users")
def users_chooser(request: Request = None):
    """Phase A profile chooser: usernames only, and only for LAN/tailnet
    clients — an internet-exposed deployment reveals nothing."""
    if not _lan_client(request):
        raise HTTPException(404, "Not available.")
    return {"users": db.list_usernames()}


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


# ---------- email-verified self-registration (2026-08-01) ----------
# Open registration for an internet-exposed install: anyone with a working
# address may register, a 6-digit emailed code completes it. The account does
# not exist until the code is verified.
#
# THE ENUMERATION RULE: /api/register/start returns ONE body — byte-identical —
# whether the address is fresh, already registered, malformed, throttled, or
# the send failed. Anything that varies with ACCOUNT STATE is an oracle that
# tells a stranger who has an account here. The only non-neutral replies are
# determined purely by the request or by operator configuration: a bad
# username/password shape (400), registration closed / bad invite (403), and
# an unconfigured mail transport (503, identical for every input).
#
# Mail never goes through the dnsr-agent's Gmail identity — see mailer.py.

REG_TTL_MIN = 15
REG_TTL_S = REG_TTL_MIN * 60
REG_MAX_ATTEMPTS = 5              # 6th verify on a row is dead
_HOUR = 3600
_START_PER_EMAIL_HR = 3
_START_PER_IP_HR = 10
_VERIFY_PER_IP_HR = 20

# The one and only /register/start body. A frozen constant on purpose: if a
# future edit makes this vary by input, test_registration's byte-identity pin
# fails loudly instead of quietly re-opening account enumeration.
NEUTRAL_START = {
    "ok": True,
    "ttl_minutes": REG_TTL_MIN,
    "detail": ("If that address can receive mail, a 6-digit code is on its way. "
               "It expires in 15 minutes."),
}

# Deliberately permissive: shape only. Deliverability is decided by the code
# actually arriving, not by a regex arguing with RFC 5322.
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")


class RegStart(BaseModel):
    username: str
    email: str
    password: str
    invite: str | None = None


class RegVerify(BaseModel):
    email: str
    token: str


def _client_ip(request) -> str:
    return (request.client.host if request and request.client else "") or "unknown"


def _reg_gate(invite: str | None):
    """Kill switch + invite gate, shared by the email path."""
    mode = _reg_mode()
    if mode == "closed":
        raise HTTPException(403, "Registration is closed.")
    if mode == "invite":
        code = os.environ.get("LEARN_INVITE_CODE", "")
        if not code:
            raise HTTPException(403, "Registration is closed.")
        if not invite or not hmac.compare_digest(invite, code):
            raise HTTPException(403, "Registration needs a valid invite code.")


@app.post("/api/register/start")
def register_start(body: RegStart, request: Request = None):
    _reg_gate(body.invite)

    # Operator-facing, not account-facing: identical for every input, so it
    # leaks nothing. Better a loud 503 than a silent "check your inbox" for
    # mail that will never be sent.
    if mailer.transport() == "off":
        raise HTTPException(503, "Email delivery is not configured on this "
                                 "server, so accounts cannot be created yet. "
                                 "(Operator: set LEARN_MAIL_TRANSPORT.)")

    # Input shape — same rules create_user enforces, checked before anything
    # touches account state. Client-determined, so rejecting is not a leak.
    try:
        username = db.validate_credentials(body.username, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.purge_pending()                       # cheap housekeeping, no cron
    email = db.normalize_email(body.email)
    ip = _client_ip(request)
    log = logging.getLogger("learn")

    # Everything below this line returns NEUTRAL_START, whatever happens.
    def neutral():
        return NEUTRAL_START

    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        return neutral()
    if db.reg_count("start_ip", ip, _HOUR) >= _START_PER_IP_HR:
        log.warning("registration start throttled: ip=%s", ip)
        return neutral()
    if db.reg_count("start_email", email, _HOUR) >= _START_PER_EMAIL_HR:
        log.warning("registration start throttled: email=%s", mailer.mask(email))
        return neutral()

    # Count the attempt BEFORE the state checks, so a throttle can never be
    # dodged by aiming at addresses that short-circuit.
    db.reg_event("start_ip", ip)
    db.reg_event("start_email", email)

    if db.email_taken(email):
        # Already has an account. Say nothing different; send nothing.
        return neutral()

    token = f"{secrets.randbelow(1_000_000):06d}"     # 6 digits: phone-typable
    db.put_pending(username, email, body.password, token, REG_TTL_S, ip)
    mailer.send_registration_token(email, token, ttl_minutes=REG_TTL_MIN)
    return neutral()


@app.post("/api/register/verify")
def register_verify(body: RegVerify, request: Request = None):
    if _reg_mode() == "closed":
        raise HTTPException(403, "Registration is closed.")
    ip = _client_ip(request)
    if db.reg_count("verify_ip", ip, _HOUR) >= _VERIFY_PER_IP_HR:
        raise HTTPException(429, "Too many attempts. Try again later.")
    db.reg_event("verify_ip", ip)

    email = db.normalize_email(body.email)
    bad = HTTPException(400, "That code is not valid, or it has expired. "
                             "Request a new one.")
    row = db.get_pending(email)
    # One message for no-row / expired / already-consumed / wrong code: a
    # distinct "expired" vs "no such registration" is an enumeration oracle.
    if not row or row["consumed_at"] is not None or row["expires_at"] <= time.time():
        raise bad
    if row["attempts"] >= REG_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many incorrect codes. Start again to get "
                                 "a new one.")
    if not db.check_pending_token(row, (body.token or "").strip()):
        db.bump_pending_attempts(email)
        raise bad

    # Correct code. consume_pending is the atomic single-use guard: if a
    # concurrent request already consumed the row, this one gets nothing.
    if not db.consume_pending(email):
        raise bad
    try:
        db.create_user_hashed(row["username"], row["pw_salt"], row["pw_hash"], email)
    except ValueError as e:
        # Username got taken between start and verify. The address is verified,
        # so this is safe to say plainly — it is about the username the caller
        # just typed, not about anyone else's account.
        raise HTTPException(409, str(e))
    token = db.issue_session(row["username"])
    logging.getLogger("learn").info(
        "registration complete: username=%s email=%s ip=%s",
        row["username"], mailer.mask(email), _client_ip(request))
    return {"token": token,
            "role": (db.user_for_token(token) or {}).get("role", "learner")}


# ---------- ANIM-TTS (operator, 2026-07-31: "the new voice system did not make it to the
# animation panels") ----------
# Card narration is PRE-rendered (kokoro_render.py) because card text is static. Animation
# narration is DYNAMIC — the lines interpolate numbers from the running scene — so it needs
# live synthesis. This proxies the shared Kokoro engine (127.0.0.1:8807, loopback-only by
# design) for signed-in users, in the ACTIVE house voice (static/audio/active_voice.json,
# the same one the card renders use), with a content-addressed disk cache so a replayed
# anim never re-synthesizes. On ANY failure the endpoint 503s and the client falls back to
# the paced browser voice — narration must never go silent because the engine is down.

_ANIM_TTS_URL = os.environ.get("KOKORO_TTS_URL", "http://127.0.0.1:8807/tts")
_ANIM_TTS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "static", "audio", "_anim")
_ANIM_TTS_MAX_CHARS = 800          # anim lines are 1-3 sentences; a novel is a bug


def _active_voice() -> str:
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "static", "audio", "active_voice.json")
        return (json.load(open(p)).get("voice") or "af_heart").strip()
    except Exception:
        return "af_heart"


class AnimTTS(BaseModel):
    text: str


@app.post("/api/anim_tts")
def anim_tts(body: AnimTTS, authorization: str | None = Header(default=None)):
    require_user(authorization)
    text = " ".join((body.text or "").split()).strip()
    if not text:
        raise HTTPException(400, "empty text")
    if len(text) > _ANIM_TTS_MAX_CHARS:
        text = text[:_ANIM_TTS_MAX_CHARS]
    voice = _active_voice()
    import hashlib as _hl
    key = _hl.sha256(f"{voice}|{text}".encode()).hexdigest()[:16]
    cdir = os.path.join(_ANIM_TTS_CACHE, voice)
    cpath = os.path.join(cdir, f"{key}.m4a")
    if os.path.isfile(cpath) and os.path.getsize(cpath) > 200:
        return FileResponse(cpath, media_type="audio/mp4")
    try:
        import urllib.request
        req = urllib.request.Request(
            _ANIM_TTS_URL,
            data=json.dumps({"text": text, "voice": voice, "format": "m4a"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read()
        # the engine duration-verifies (media-verification rule) — but never cache a stub
        if len(data) < 200:
            raise ValueError(f"suspiciously small render ({len(data)} bytes)")
        os.makedirs(cdir, exist_ok=True)
        tmp = cpath + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cpath)
        return FileResponse(cpath, media_type="audio/mp4")
    except HTTPException:
        raise
    except Exception as e:
        logging.getLogger("learn").warning("anim_tts: engine unavailable (%s)", e)
        raise HTTPException(503, "tts engine unavailable — client should fall back")


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
    def _num(v, lo, hi, default=None):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return min(hi, max(lo, f))

    now_ts = time.time()
    with db.conn() as c:
        for row in body.concept_state:
            # BE-5: malformed rows (non-dicts, missing keys) skip, never 500
            if (not isinstance(row, dict) or not STATE_COLS.issuperset(row)
                    or not isinstance(row.get("topic_id"), str)
                    or not isinstance(row.get("concept_id"), str)):
                skipped += 1; continue
            # BE-4: clamp/validate every client value — imports are forgeable
            # self-harm otherwise (p=5.0 breaks decay math; mastered_at in the
            # future corrupts review; correct > attempts corrupts analytics)
            row = dict(row)
            row["p_mastery"] = _num(row.get("p_mastery"), 0.0, 1.0, 0.0)
            row["attempts"] = int(_num(row.get("attempts"), 0, 1e9, 0))
            row["correct"] = int(min(_num(row.get("correct"), 0, 1e9, 0), row["attempts"]))
            row["streak"] = int(_num(row.get("streak"), 0, 1e6, 0))
            if row.get("mastered_at") is not None:
                row["mastered_at"] = _num(row["mastered_at"], 0, now_ts, None)
            for k, lo, hi in (("interval_d", 0, 36500), ("ease", 1.0, 5.0),
                              ("stability", 0, 36500), ("difficulty", 1.0, 10.0)):
                if row.get(k) is not None:
                    row[k] = _num(row[k], lo, hi, None)
            cur = c.execute("SELECT attempts, p_mastery, mastered_at FROM concept_state "
                            "WHERE user_id=? AND topic_id=? AND concept_id=?",
                            (user["id"], row["topic_id"], row["concept_id"])).fetchone()
            # BE-1: the old lexicographic tuple compare let a weaker row with
            # MORE attempts overwrite mastered progress. Never-regress now means:
            # BOTH dimensions must be >= to overwrite, AND an import may never
            # un-master or lower p on a locally-mastered row.
            if cur is not None:
                cur_att, cur_p, cur_mast = cur[0] or 0, cur[1] or 0.0, cur[2]
                stronger = (row["attempts"] >= cur_att and row["p_mastery"] >= cur_p)
                unmasters = bool(cur_mast) and not row.get("mastered_at")
                if not stronger or unmasters:
                    skipped += 1; continue    # local evidence is stronger — never regress
            fields = {k: row[k] for k in row if k in STATE_COLS and k not in ("topic_id", "concept_id")}
            cols = ", ".join(fields)
            c.execute(f"INSERT INTO concept_state (user_id, topic_id, concept_id, {cols}) "
                      f"VALUES ({','.join('?' * (3 + len(fields)))}) "
                      f"ON CONFLICT(user_id, topic_id, concept_id) DO UPDATE SET "
                      + ", ".join(f"{k}=excluded.{k}" for k in fields),
                      (user["id"], row["topic_id"], row["concept_id"], *fields.values()))
            imported += 1
        for pl in body.placements:
            if not isinstance(pl, dict) or "topic_id" not in pl: continue
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
              AND a.question_id NOT LIKE 'gen:%'
            GROUP BY s.topic_id, s.concept_id ORDER BY hours""", (user["id"],))]
        # retention: review-mode outcomes bucketed by scheduled interval
        ret = [dict(r) for r in c.execute("""
            SELECT CASE WHEN COALESCE(a.interval_at, s.interval_d) < 2 THEN '1d'
                        WHEN COALESCE(a.interval_at, s.interval_d) < 5 THEN '2-4d'
                        WHEN COALESCE(a.interval_at, s.interval_d) < 10 THEN '5-9d'
                        ELSE '10d+' END AS bucket,
                   COUNT(*) AS n, ROUND(AVG(a.is_correct), 3) AS recall
            FROM answers a JOIN concept_state s
              ON s.user_id = a.user_id AND s.topic_id = a.topic_id AND s.concept_id = a.concept_id
            WHERE a.user_id = ? AND s.mastered_at IS NOT NULL AND a.at > s.mastered_at
              AND a.question_id NOT LIKE 'gen:%'
            GROUP BY bucket""", (user["id"],))]
        weakest = [dict(r) for r in c.execute("""
            SELECT topic_id, concept_id, ROUND(p_mastery, 3) AS p, attempts, correct
            FROM concept_state WHERE user_id = ? AND attempts > 0 AND mastered_at IS NULL
            ORDER BY p_mastery ASC LIMIT 8""", (user["id"],))]
        n_review = c.execute("""SELECT COUNT(*) FROM answers a JOIN concept_state s
            ON s.user_id=a.user_id AND s.topic_id=a.topic_id AND s.concept_id=a.concept_id
            WHERE a.user_id=? AND s.mastered_at IS NOT NULL AND a.at > s.mastered_at
            AND a.question_id NOT LIKE 'gen:%'""",
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
                    "category": pack.get("category", "More Topics"),
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
            # trust but verify: mastered, but due for review immediately.
            # BE-13: seed at MASTERY_P, not 0.90 — one miss from 0.90 lands at
            # 0.55 < RELEARN_P and instantly demoted a placement-trusted
            # concept; from 0.95 a single slip shortens the review, as designed.
            # BE-14: seed FSRS stability at S0(Good), not the 0.5d floor, so
            # the first review reschedules from real memory params.
            db.upsert_state(user["id"], tid, c["id"], p_mastery=mastery.MASTERY_P,
                            mastered_at=now, interval_d=0.5, due_at=now,
                            stability=mastery.FSRS_W[2],
                            difficulty=mastery._fsrs_d0(3))
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
    # BE-2: enforce the ladder gate HERE, not only on concept GET — placement
    # leaks every concept's question ids, and the server reveals `correct`, so
    # an ungated answer route lets a client brute-force a locked concept to
    # mastered and walk the whole ladder around the gate.
    idx = next(i for i, cc in enumerate(pack["concepts"]) if cc["id"] == cid)
    if idx > 0:
        states_gate = db.get_states(user["id"], tid)
        prev = states_gate.get(pack["concepts"][idx - 1]["id"], {})
        if not (prev.get("mastered_at") or prev.get("relearn")):
            raise HTTPException(403, "That concept is still locked — master its "
                                     "predecessor first.")
    q = next((q for q in c["questions"] if q["id"] == a.question_id), None)
    if not q:
        raise HTTPException(404, "No such question.")
    # BE-6: validate the choice BEFORE any state moves — a missing or
    # out-of-range index used to 500 at the feedback lookup.
    if q.get("type") != "free":
        if a.choice is None or not isinstance(a.choice, int) \
                or not 0 <= a.choice < len(q.get("options", [])):
            raise HTTPException(400, "choice must be an option index for this question")
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

    # BE-16: the read → compute → write below runs inside ONE immediate
    # transaction — two concurrent answers for the same user (double-submit,
    # two tabs) used to interleave get_states/upsert and lose an update.
    # Everything in between is pure computation (grading already happened
    # above), so the write lock is held for microseconds.
    with db.conn() as dbc:
        dbc.execute("BEGIN IMMEDIATE")
        row = dbc.execute("SELECT * FROM concept_state WHERE user_id=? AND "
                          "topic_id=? AND concept_id=?",
                          (user["id"], tid, cid)).fetchone()
        s = dict(row) if row else {}
        p_before = s.get("p_mastery") if s.get("p_mastery") is not None else mastery.P_INIT
        p_after = mastery.bkt_update(p_before, correct)
        streak = ((s.get("streak") or 0) + 1) if correct else 0
        was_mastered = bool(s.get("mastered_at"))
        now_mastered = was_mastered or mastery.is_mastered(p_after, streak)

        fields = dict(
            p_mastery=p_after,
            attempts=(s.get("attempts") or 0) + 1,
            correct=(s.get("correct") or 0) + int(correct),
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

        db.upsert_state(user["id"], tid, cid, c=dbc, **fields)
        # BE-22: snapshot the scheduled interval AT ANSWER TIME for retention
        db.log_answer(user["id"], tid, cid, a.question_id,
                      (a.text or "")[:400] if q.get("type") == "free" else a.choice,
                      correct, a.latency_ms,
                      interval_at=s.get("interval_d"), c=dbc)

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
    (BE-7: check() now collects thread-locally and RETURNS the list, so
    concurrent validations can no longer clobber each other's results.)"""
    import validate_pack as vp
    try:
        return list(vp.check(pack))
    except Exception as e:  # a malformed draft must report, not 500
        return [f"validator crashed on this draft: {type(e).__name__}: {e}"]


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
    # BE-23: bound the draft before any processing — a pack has no business
    # being tens of MB or hundreds of concepts.
    if len(json.dumps(body.pack)) > 2_000_000 or len(body.pack.get("concepts") or []) > 100:
        raise HTTPException(413, "Draft too large (2MB / 100 concepts max).")
    errors = _validate_pack_dict(body.pack)
    if errors:
        return {"ok": False, "staged": None, "errors": errors}
    # BE-7 (endpoint half): do NOT trust the validator alone for the filename —
    # its module-global error list is shared across threads, so a race could
    # blank the errors for a non-slug id. Re-validate the slug HERE and prove
    # the resolved path stays inside STAGED_DIR before opening anything.
    pid = body.pack["id"]
    if not re.fullmatch(r"[a-z0-9-]{1,64}", pid or ""):
        raise HTTPException(400, "pack id must be a lowercase slug")
    os.makedirs(STAGED_DIR, exist_ok=True)
    path = os.path.realpath(os.path.join(STAGED_DIR, f"{pid}.json"))
    if os.path.dirname(path) != os.path.realpath(STAGED_DIR):
        raise HTTPException(400, "resolved path escapes topics_staged/")
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

# .m4a MIME fix (audio "Listen" bug, 2026-07-30): Python's mimetypes maps .m4a to
# 'audio/mp4a-latm' (a macOS quirk) — the LATM-streaming type, which browsers refuse to
# decode in <audio>, so pre-rendered narration silently fell back to speechSynthesis. These
# are standard AAC-in-MP4 files; register the browser-playable type BEFORE StaticFiles mounts.
import mimetypes
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/mp4", ".m4b")

app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
