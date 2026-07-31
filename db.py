"""Learn — SQLite persistence layer.

All user data stays local. Passwords are PBKDF2-hashed; no plaintext,
no telemetry, no external calls from this module.
"""
import hashlib
import hmac
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


def _hash(pw: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)


def create_user(username: str, password: str):
    username = username.strip().lower()
    if not username or len(password) < 6:
        raise ValueError("Username required; password must be at least 6 characters.")
    salt = secrets.token_bytes(16)
    with conn() as c:
        try:
            c.execute(
                "INSERT INTO users (username, pw_salt, pw_hash, created_at) VALUES (?,?,?,?)",
                (username, salt, _hash(password, salt), time.time()),
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
            (token, row["id"], now, now + 30 * 86400),
        )
        return token


def user_for_token(token: str):
    if not token:
        return None
    with conn() as c:
        row = c.execute(
            """SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, time.time()),
        ).fetchone()
        return dict(row) if row else None


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
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


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
