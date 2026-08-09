"""Learn — SQLite persistence layer.

All user data stays local. Passwords are PBKDF2-hashed; no plaintext,
no telemetry, no external calls from this module.
"""
import hashlib
import hmac
import re
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("LEARN_DB", os.path.join(os.path.dirname(__file__), "learn.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    pw_salt     BLOB NOT NULL,
    pw_hash     BLOB NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
-- One row per (user, topic, concept): BKT state + spaced-review schedule.
CREATE TABLE IF NOT EXISTS concept_state (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    topic_id    TEXT NOT NULL,
    concept_id  TEXT NOT NULL,
    p_mastery   REAL NOT NULL DEFAULT 0.15,   -- BKT P(known)
    attempts    INTEGER NOT NULL DEFAULT 0,
    correct     INTEGER NOT NULL DEFAULT 0,
    streak      INTEGER NOT NULL DEFAULT 0,
    unlocked    INTEGER NOT NULL DEFAULT 0,
    mastered_at REAL,
    -- SM-2-lite review scheduling
    interval_d  REAL NOT NULL DEFAULT 0,
    ease        REAL NOT NULL DEFAULT 2.5,
    due_at      REAL,
    -- P2.6 (iss_0824d5ad): review-failure relearn — concept fell out of
    -- mastered (cards re-served, gate re-run) but successors stay unlocked
    relearn     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, topic_id, concept_id)
);
-- Full answer log: powers analytics and AI remediation context.
CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    topic_id    TEXT NOT NULL,
    concept_id  TEXT NOT NULL,
    question_id TEXT NOT NULL,
    given       TEXT,
    is_correct  INTEGER NOT NULL,
    latency_ms  INTEGER,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answers_user ON answers(user_id, topic_id, concept_id);
-- Placement results (also marks that placement was offered/completed or skipped)
CREATE TABLE IF NOT EXISTS placements (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    topic_id    TEXT NOT NULL,
    level       INTEGER,           -- NULL = skipped
    at          REAL NOT NULL,
    PRIMARY KEY (user_id, topic_id)
);
-- P5.17 pack versioning (iss_8d424360): one row per registered pack content
-- version; manifest_json holds the concept/question id surface for diffing.
CREATE TABLE IF NOT EXISTS pack_versions (
    topic_id      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    pack_hash     TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    seen_at       REAL NOT NULL,
    PRIMARY KEY (topic_id, version)
);
-- P5.17: progress rows displaced by a pack edit land here (never silently lost).
CREATE TABLE IF NOT EXISTS concept_state_archive (
    user_id     INTEGER NOT NULL,
    topic_id    TEXT NOT NULL,
    concept_id  TEXT NOT NULL,
    p_mastery   REAL, attempts INTEGER, correct INTEGER, streak INTEGER,
    unlocked    INTEGER, mastered_at REAL, interval_d REAL, ease REAL, due_at REAL,
    stability   REAL, difficulty REAL, relearn INTEGER,
    archived_at REAL NOT NULL,
    reason      TEXT NOT NULL,     -- concept_removed | rename_collision:<old>-><new>
    from_hash   TEXT,
    to_hash     TEXT
);
-- Email-verified self-registration (2026-08-01). A registration in flight: the
-- account does NOT exist until the emailed code is verified. Password is hashed
-- HERE, at start — plaintext never lands in a row. The token is stored only as
-- a PBKDF2 hash with its own salt (same scheme as passwords).
-- Where the student left off (2026-08-03, operator ask): one row per user,
-- overwritten on every concept open. Server-side (not localStorage) so Resume
-- works across shared devices — the same reason concept_state lives here.
CREATE TABLE IF NOT EXISTS last_location (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),
    topic_id    TEXT NOT NULL,
    concept_id  TEXT NOT NULL,
    at          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_registrations (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL,
    email       TEXT NOT NULL,          -- normalized: stripped + lowercased
    pw_salt     BLOB NOT NULL,
    pw_hash     BLOB NOT NULL,
    token_salt  BLOB NOT NULL,
    token_hash  BLOB NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    ip          TEXT,
    consumed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_email ON pending_registrations(email);
CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_registrations(expires_at);
-- Throttle/cap counters. In the DB, not memory: the app restarts, and a rate
-- limit that a restart clears is not a rate limit.
CREATE TABLE IF NOT EXISTS reg_events (
    id   INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,     -- start_email | start_ip | verify_ip | mail_send
    key  TEXT NOT NULL,     -- normalized email / client ip / '' for global
    at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reg_events ON reg_events(kind, key, at);
"""

_ITER = 240_000


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
    # SESSION TOKENS AT REST (2026-08-08): convert any raw token to sha256(token). Runs on
    # every init, costs one scan, and is a no-op once converted. Live sessions survive because
    # the client's raw token hashes to the stored value on its next request.
    with conn() as c:
        n = _migrate_session_tokens(c)
    if n:
        try:
            import logging
            logging.getLogger("learn").info("hashed %d session token(s) at rest", n)
        except Exception:
            pass
    # P2.6 additive migration (existing DBs predate the relearn column)
    with conn() as c:
        try:
            c.execute("ALTER TABLE concept_state ADD COLUMN relearn INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass          # column already there
    # P5.20 (iss_e8b3bfb8) additive migration — author-vs-learner roles.
    with conn() as c:
        try:
            c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'learner'")
        except sqlite3.OperationalError:
            pass
    # P2.7 (iss_d13d1882) additive migration — FSRS memory state. Legacy rows
    # keep NULL stability; the first FSRS review migrates them (interval_d →
    # stability, exact at the 0.90 retention target).
    with conn() as c:
        for table in ("concept_state", "concept_state_archive"):
            for col in ("stability REAL", "difficulty REAL"):
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
    # BE-15: the archive predates the relearn flag; BE-22: answers snapshot the
    # scheduled interval AT ANSWER TIME (retention buckets were re-bucketing
    # history by the CURRENT interval).
    with conn() as c:
        for stmt in ("ALTER TABLE concept_state_archive ADD COLUMN relearn INTEGER",
                     "ALTER TABLE answers ADD COLUMN interval_at REAL"):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass
    # Email-verified registration (2026-08-01): users gain a verified address.
    # Nullable — every pre-existing account (LAN bootstrap, invite era) has none,
    # and the partial unique index tolerates that while still pinning one
    # account per verified address.
    with conn() as c:
        try:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT")
        except sqlite3.OperationalError:
            pass
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
                  "ON users(email) WHERE email IS NOT NULL")
    # Certificates (2026-08-04): the name that appears on a completion
    # certificate. Separate from `username` on purpose — a login handle
    # ("kate") is not the name you frame ("Kate Reeves"). NULL until the
    # student sets it; the certificate falls back to the username.
    # `app_meta` holds the per-install HMAC key that signs verification
    # codes: without a secret the code would be a public recipe anyone
    # could re-derive, which is decoration, not verification.
    with conn() as c:
        try:
            c.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        except sqlite3.OperationalError:
            pass
        c.execute("CREATE TABLE IF NOT EXISTS app_meta ("
                  "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    # FIRST + LAST NAME (2026-08-08, operator request): the certificate now prints the
    # recipient's name, so the name is captured as two fields rather than one free-text
    # line. Separate columns because "the name on the diploma" has parts a single string
    # cannot address — sorting, "Last, First", and a middle name typed into a one-line box
    # all become guesswork the moment you need them.
    #
    # `display_name` is KEPT, not dropped: existing students already set one, and silently
    # discarding a name someone chose for their own certificate would be a data loss with a
    # face on it. It is the fallback when first/last are empty.
    with conn() as c:
        for col in ("first_name", "last_name"):
            try:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass


def _hash(pw: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)


def validate_credentials(username: str, password: str) -> str:
    """The single definition of 'is this a usable username/password'. Returns
    the normalized username. create_user() and the email-registration path both
    go through here so they can never drift apart."""
    username = (username or "").strip().lower()
    if not username or len(password or "") < 6:
        raise ValueError("Username required; password must be at least 6 characters.")
    return username


def create_user(username: str, password: str, email: str | None = None):
    username = validate_credentials(username, password)
    salt = secrets.token_bytes(16)
    with conn() as c:
        try:
            c.execute(
                "INSERT INTO users (username, pw_salt, pw_hash, created_at, email) "
                "VALUES (?,?,?,?,?)",
                (username, salt, _hash(password, salt), time.time(), email or None),
            )
        except sqlite3.IntegrityError:
            raise ValueError("That username is taken.")


def create_user_hashed(username: str, pw_salt: bytes, pw_hash: bytes,
                       email: str | None = None):
    """Same row as create_user(), but from an ALREADY-hashed password — the
    email-verification path hashes at /register/start and never holds the
    plaintext long enough to re-hash it here."""
    username = (username or "").strip().lower()
    if not username:
        raise ValueError("Username required; password must be at least 6 characters.")
    with conn() as c:
        try:
            c.execute(
                "INSERT INTO users (username, pw_salt, pw_hash, created_at, email) "
                "VALUES (?,?,?,?,?)",
                (username, pw_salt, pw_hash, time.time(), email or None),
            )
        except sqlite3.IntegrityError:
            raise ValueError("That username is taken.")


def authenticate(username: str, password: str) -> str | None:
    """Return a session token on success, else None."""
    with conn() as c:
        row = c.execute(
            "SELECT id, pw_salt, pw_hash FROM users WHERE username = ?",
            (username.strip().lower(),),
        ).fetchone()
        if not row:
            # BE-24: burn the same PBKDF2 cost as the real branch so response
            # timing cannot enumerate which usernames exist.
            _hash(password, b"\x00" * 16)
            return None
        if not hmac.compare_digest(_hash(password, row["pw_salt"]), row["pw_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_tok_hash(token), row["id"], now, now + SESSION_TTL_S),
        )
        return token


# Phase A shared-device hardening (2026-07-31, operator-directed): sessions were
# 30 DAYS — whoever picked up a shared iPad silently continued as the last user
# for a month. Now 12h SLIDING: active use never interrupts (each authenticated
# request renews), an idle session dies the same day. The always-login-at-launch
# behavior itself is client-side (sessionStorage token); this is defense in depth.
SESSION_TTL_S = 12 * 3600


def user_for_token(token: str):
    if not token:
        return None
    now = time.time()
    with conn() as c:
        row = c.execute(
            """SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (_tok_hash(token), now),
        ).fetchone()
        if row:
            c.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                      (now + SESSION_TTL_S, _tok_hash(token)))   # sliding renewal
        return dict(row) if row else None


_TEST_NAME = re.compile(r"^(smoke[_-]|deploy-|e2e[_-]|test[_-])|_\d{9,}$")


def list_usernames() -> list[str]:
    """Phase A profile chooser — usernames ONLY (no ids, no roles).
    Filters test-pattern accounts (smoke_*/deploy-*/e2e_*/…_<epoch>) so a
    stray smoke run can never re-pollute the family chooser (2026-07-31:
    four junk profiles reached the operator's login screen). Convention
    stays: smokes should tear their users down — this is the backstop."""
    with conn() as c:
        return [r[0] for r in c.execute("SELECT username FROM users ORDER BY username")
                if not _TEST_NAME.search(r[0])]


def user_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ---------- session tokens at rest (2026-08-08) ----------
# Sessions stored the RAW token as the primary key, so anything that could read the database
# file held 50 working credentials — no password needed. That is not only a hosting concern:
# learn.db is picked up by Time Machine and the nightly off-site backup, so raw tokens were
# already leaving the machine on removable media every night.
#
# The fix is the standard one: store sha256(token) and look up by hash. The token itself is
# high-entropy (secrets.token_urlsafe(32)), so a plain SHA-256 is correct here — there is
# nothing to brute-force and no salt/stretching needed, unlike a password.
#
# MIGRATION IS IN-PLACE AND NON-DISRUPTIVE: existing rows are rewritten as sha256(stored
# value), so a client presenting its raw token still matches on the next request and nobody
# is logged out. It runs once; a second pass would hash the hashes, so it is guarded by a
# marker column check.
def _tok_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _migrate_session_tokens(c) -> int:
    """Rewrite raw tokens as their hashes. Idempotent: a 64-char lowercase hex value is
    already hashed and is left alone. Returns how many rows were converted."""
    rows = c.execute("SELECT token FROM sessions").fetchall()
    n = 0
    for r in rows:
        t = r["token"]
        if isinstance(t, str) and len(t) == 64 and all(ch in "0123456789abcdef" for ch in t):
            continue                       # already a hash
        try:
            c.execute("UPDATE sessions SET token = ? WHERE token = ?", (_tok_hash(t), t))
            n += 1
        except Exception:
            c.execute("DELETE FROM sessions WHERE token = ?", (t,))   # collision → drop it
    return n


def set_role(username: str, role: str) -> bool:
    """P5.20: grant/revoke authoring. Roles: 'learner' (default) | 'author'."""
    if role not in ("learner", "author"):
        raise ValueError("role must be 'learner' or 'author'")
    with conn() as c:
        cur = c.execute("UPDATE users SET role = ? WHERE username = ?",
                        (role, username.strip().lower()))
        return cur.rowcount == 1


def logout(token: str):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (_tok_hash(token),))


def issue_session(username: str) -> str | None:
    """Mint a session for an already-authenticated identity. Used by the
    email-verification path, which proves identity with the emailed code and
    has no plaintext password to hand to authenticate()."""
    with conn() as c:
        row = c.execute("SELECT id FROM users WHERE username = ?",
                        (username.strip().lower(),)).fetchone()
        if not row:
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_tok_hash(token), row["id"], now, now + SESSION_TTL_S),
        )
        return token


# ---------- email-verified self-registration ----------

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def email_taken(email: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM users WHERE email = ? LIMIT 1",
                         (normalize_email(email),)).fetchone() is not None


def username_taken(username: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM users WHERE username = ? LIMIT 1",
                         ((username or "").strip().lower(),)).fetchone() is not None


def put_pending(username: str, email: str, password: str, token: str,
                ttl_s: float, ip: str | None = None) -> None:
    """Create-or-replace the in-flight registration for this address.

    Both secrets are hashed here and the plaintexts are dropped: the row can
    reconstruct neither the password nor the code. Replacing resets attempts —
    a fresh code deserves a fresh budget."""
    email = normalize_email(email)
    username = (username or "").strip().lower()
    pw_salt, tk_salt = secrets.token_bytes(16), secrets.token_bytes(16)
    now = time.time()
    with conn() as c:
        c.execute(
            """INSERT INTO pending_registrations
                 (username, email, pw_salt, pw_hash, token_salt, token_hash,
                  created_at, expires_at, attempts, ip, consumed_at)
               VALUES (?,?,?,?,?,?,?,?,0,?,NULL)
               ON CONFLICT(email) DO UPDATE SET
                 username=excluded.username, pw_salt=excluded.pw_salt,
                 pw_hash=excluded.pw_hash, token_salt=excluded.token_salt,
                 token_hash=excluded.token_hash, created_at=excluded.created_at,
                 expires_at=excluded.expires_at, attempts=0, ip=excluded.ip,
                 consumed_at=NULL""",
            (username, email, pw_salt, _hash(password, pw_salt),
             tk_salt, _hash(token, tk_salt), now, now + ttl_s, ip),
        )


def get_pending(email: str):
    with conn() as c:
        row = c.execute("SELECT * FROM pending_registrations WHERE email = ?",
                        (normalize_email(email),)).fetchone()
        return dict(row) if row else None


def check_pending_token(row: dict, token: str) -> bool:
    """Constant-time compare of a candidate code against the stored hash."""
    return hmac.compare_digest(_hash(token or "", row["token_salt"]), row["token_hash"])


def bump_pending_attempts(email: str) -> int:
    with conn() as c:
        c.execute("UPDATE pending_registrations SET attempts = attempts + 1 "
                  "WHERE email = ?", (normalize_email(email),))
        r = c.execute("SELECT attempts FROM pending_registrations WHERE email = ?",
                      (normalize_email(email),)).fetchone()
        return r["attempts"] if r else 0


def consume_pending(email: str) -> bool:
    """Mark consumed. Returns False if it was ALREADY consumed — the atomic
    single-use guard (a replayed code must never mint a second account)."""
    with conn() as c:
        cur = c.execute("UPDATE pending_registrations SET consumed_at = ? "
                        "WHERE email = ? AND consumed_at IS NULL",
                        (time.time(), normalize_email(email)))
        return cur.rowcount == 1


def purge_pending(older_than_s: float = 86400) -> int:
    """Housekeeping on the cheap: drop spent/expired rows and stale counters on
    each /register/start. No cron, no unbounded growth."""
    cut = time.time() - older_than_s
    with conn() as c:
        n = c.execute(
            "DELETE FROM pending_registrations WHERE (consumed_at IS NOT NULL "
            "AND consumed_at < ?) OR expires_at < ?", (cut, cut)).rowcount
        c.execute("DELETE FROM reg_events WHERE at < ?", (cut,))
        return n


def reg_event(kind: str, key: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO reg_events (kind, key, at) VALUES (?,?,?)",
                  (kind, key or "", time.time()))


def reg_count(kind: str, key: str, window_s: float) -> int:
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM reg_events WHERE kind=? AND key=? AND at > ?",
            (kind, key or "", time.time() - window_s)).fetchone()[0]


# ---------- last location (Resume) ----------

def set_last_location(user_id: int, topic_id: str, concept_id: str):
    with conn() as c:
        c.execute("INSERT INTO last_location (user_id, topic_id, concept_id, at) "
                  "VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                  "topic_id=excluded.topic_id, concept_id=excluded.concept_id, at=excluded.at",
                  (user_id, topic_id, concept_id, time.time()))


def get_last_location(user_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT topic_id, concept_id, at FROM last_location "
                      "WHERE user_id = ?", (user_id,)).fetchone()
        return dict(r) if r else None


# ---------- concept state ----------

def get_states(user_id: int, topic_id: str) -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM concept_state WHERE user_id = ? AND topic_id = ?",
            (user_id, topic_id),
        ).fetchall()
        return {r["concept_id"]: dict(r) for r in rows}


def upsert_state(user_id: int, topic_id: str, concept_id: str, c=None, **fields):
    keys = ["p_mastery", "attempts", "correct", "streak", "unlocked",
            "mastered_at", "interval_d", "ease", "due_at", "relearn",
            "stability", "difficulty"]
    vals = {k: fields[k] for k in keys if k in fields}
    if c is not None:                      # BE-16: join the caller's transaction
        _upsert_state_in(c, user_id, topic_id, concept_id, vals)
        return
    with conn() as c:
        _upsert_state_in(c, user_id, topic_id, concept_id, vals)


def _upsert_state_in(c, user_id, topic_id, concept_id, vals):
    c.execute(
        """INSERT INTO concept_state (user_id, topic_id, concept_id)
           VALUES (?,?,?) ON CONFLICT(user_id, topic_id, concept_id) DO NOTHING""",
        (user_id, topic_id, concept_id),
    )
    if vals:
        sets = ", ".join(f"{k} = ?" for k in vals)
        c.execute(
            f"UPDATE concept_state SET {sets} WHERE user_id=? AND topic_id=? AND concept_id=?",
            (*vals.values(), user_id, topic_id, concept_id),
        )


def log_answer(user_id, topic_id, concept_id, question_id, given, is_correct,
               latency_ms, interval_at=None, c=None):
    """interval_at (BE-22): the concept's scheduled interval when this answer
    happened — retention analytics bucket on it, not on today's value.
    c (BE-16): join an already-open transaction instead of opening one."""
    if c is not None:
        _log_answer_in(c, user_id, topic_id, concept_id, question_id, given,
                       is_correct, latency_ms, interval_at)
        return
    with conn() as cc:
        _log_answer_in(cc, user_id, topic_id, concept_id, question_id, given,
                       is_correct, latency_ms, interval_at)


def _log_answer_in(c, user_id, topic_id, concept_id, question_id, given,
                   is_correct, latency_ms, interval_at):
    c.execute(
        """INSERT INTO answers (user_id, topic_id, concept_id, question_id, given,
           is_correct, latency_ms, at, interval_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (user_id, topic_id, concept_id, question_id,
         json.dumps(given), int(is_correct), latency_ms, time.time(), interval_at),
    )


def recent_misses(user_id, topic_id, concept_id, limit=6):
    with conn() as c:
        rows = c.execute(
            """SELECT question_id, given FROM answers
               WHERE user_id=? AND topic_id=? AND concept_id=? AND is_correct=0
               ORDER BY at DESC LIMIT ?""",
            (user_id, topic_id, concept_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def placement_status(user_id: int, topic_id: str):
    """None if never offered/decided; else dict with level (None = skipped)."""
    with conn() as c:
        row = c.execute(
            "SELECT level, at FROM placements WHERE user_id=? AND topic_id=?",
            (user_id, topic_id),
        ).fetchone()
        return dict(row) if row else None


def set_placement(user_id: int, topic_id: str, level):
    with conn() as c:
        c.execute(
            """INSERT INTO placements (user_id, topic_id, level, at) VALUES (?,?,?,?)
               ON CONFLICT(user_id, topic_id) DO UPDATE SET level=excluded.level, at=excluded.at""",
            (user_id, topic_id, level, time.time()),
        )


def has_answers(user_id: int, topic_id: str) -> bool:
    with conn() as c:
        return c.execute(
            "SELECT 1 FROM answers WHERE user_id=? AND topic_id=? LIMIT 1",
            (user_id, topic_id),
        ).fetchone() is not None


# ---------- certificates (2026-08-04) ----------

def install_key() -> bytes:
    """Per-install secret that signs certificate verification codes. Generated
    once on first use and stored in app_meta — so a code can be RE-DERIVED by
    this server (real verification) and not by whoever holds the recipe."""
    with conn() as c:
        row = c.execute("SELECT value FROM app_meta WHERE key='cert_key'").fetchone()
        if row:
            return bytes.fromhex(row["value"])
        key = secrets.token_bytes(32)
        c.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('cert_key', ?)",
                  (key.hex(),))
        return key


def get_display_name(user_id: int) -> str | None:
    with conn() as c:
        row = c.execute("SELECT display_name FROM users WHERE id=?", (user_id,)).fetchone()
        return (row["display_name"] or None) if row else None


def set_display_name(user_id: int, name: str) -> str:
    """Set the name that appears on certificates. Trimmed and length-capped;
    stored verbatim otherwise (people's names are not ours to normalize)."""
    clean = " ".join((name or "").split())[:60]
    with conn() as c:
        c.execute("UPDATE users SET display_name=? WHERE id=?", (clean or None, user_id))
    return clean


def get_name(user_id: int) -> dict:
    """The certificate name, as parts and as the line to print.

    Resolution order: first/last if either is set → the legacy `display_name` → None.
    Returning the parts AND the composed line keeps the caller from re-deriving the
    fallback rule in three places and getting it slightly different in one of them.
    """
    with conn() as c:
        row = c.execute("SELECT first_name, last_name, display_name FROM users WHERE id=?",
                        (user_id,)).fetchone()
    if not row:
        return {"first_name": None, "last_name": None, "full_name": None}
    first = (row["first_name"] or "").strip() or None
    last = (row["last_name"] or "").strip() or None
    full = " ".join(p for p in (first, last) if p) or (row["display_name"] or None)
    return {"first_name": first, "last_name": last, "full_name": full}


def set_name(user_id: int, first: str, last: str) -> dict:
    """Set the certificate name as two fields.

    Names are trimmed and length-capped and otherwise stored VERBATIM — no title-casing,
    no ASCII folding, no splitting on spaces. "van der Berg", "O'Neill", "de la Cruz" and
    every name with a particle or an apostrophe are correct as typed and wrong after
    normalisation, and a certificate is the last place to be clever about someone's name.

    `display_name` is kept in sync so anything still reading it sees the current name.
    """
    f = " ".join((first or "").split())[:40]
    l = " ".join((last or "").split())[:40]
    full = " ".join(p for p in (f, l) if p)
    with conn() as c:
        c.execute("UPDATE users SET first_name=?, last_name=?, display_name=? WHERE id=?",
                  (f or None, l or None, full or None, user_id))
    return {"first_name": f or None, "last_name": l or None, "full_name": full or None}


def topic_stats(user_id: int, topic_id: str) -> dict:
    """Honest completion statistics for a certificate: what the student
    actually did, straight from the answer log — never estimated."""
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(is_correct),0) ok, MIN(at) first_at, MAX(at) last_at "
            "FROM answers WHERE user_id=? AND topic_id=?", (user_id, topic_id)).fetchone()
        mrow = c.execute(
            "SELECT COUNT(*) n, MAX(mastered_at) last_mastered FROM concept_state "
            "WHERE user_id=? AND topic_id=? AND mastered_at IS NOT NULL",
            (user_id, topic_id)).fetchone()
    return {"answers": row["n"] or 0, "correct": row["ok"] or 0,
            "first_at": row["first_at"], "last_at": row["last_at"],
            "mastered_concepts": mrow["n"] or 0,
            "completed_at": mrow["last_mastered"]}


if __name__ == "__main__":
    # P5.20 role CLI:  .venv/bin/python db.py grant-author <username>
    #                  .venv/bin/python db.py revoke-author <username>
    import sys
    if len(sys.argv) == 3 and sys.argv[1] in ("grant-author", "revoke-author"):
        init()
        role = "author" if sys.argv[1] == "grant-author" else "learner"
        ok = set_role(sys.argv[2], role)
        print(f"{sys.argv[2]} → {role}" if ok else f"no such user: {sys.argv[2]}")
        sys.exit(0 if ok else 1)
    print(__doc__ or "usage: db.py grant-author|revoke-author <username>")
