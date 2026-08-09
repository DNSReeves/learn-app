"""Session tokens must never be stored raw (2026-08-08).

WHY, and why it was not merely a hosting concern: the sessions table used the RAW token as its
primary key, so anything able to read learn.db held working credentials — no password required.
learn.db is collected by Time Machine and the nightly off-site backup, so 82 live tokens across
the live file and three stale copies were already leaving the machine on removable media.

The token is high-entropy (secrets.token_urlsafe(32)), so plain SHA-256 is the right primitive:
there is nothing to brute-force, unlike a password.
"""
import hashlib
import importlib
import sqlite3

import pytest


@pytest.fixture()
def dbmod(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    import db as m
    importlib.reload(m)
    m.init()
    return m


def _tokens(dbmod):
    with dbmod.conn() as c:
        return [r["token"] for r in c.execute("SELECT token FROM sessions")]


def test_the_raw_token_is_never_stored(dbmod):
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.authenticate("u", "pw-secret-1")
    stored = _tokens(dbmod)
    assert tok not in stored, "the raw session token is in the database"
    assert hashlib.sha256(tok.encode()).hexdigest() in stored


def test_the_session_still_works(dbmod):
    """Hashing at rest must be invisible to the client."""
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.authenticate("u", "pw-secret-1")
    assert (dbmod.user_for_token(tok) or {}).get("username") == "u"
    dbmod.logout(tok)
    assert dbmod.user_for_token(tok) is None


def test_issue_session_hashes_too(dbmod):
    """The email-verification path mints tokens by a different route — it must not bypass this."""
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.issue_session("u")
    assert tok not in _tokens(dbmod)
    assert (dbmod.user_for_token(tok) or {}).get("username") == "u"


def test_a_stolen_database_row_is_not_a_credential(dbmod):
    """THE POINT. Reading the table must not yield anything that authenticates."""
    dbmod.create_user("u", "pw-secret-1")
    dbmod.authenticate("u", "pw-secret-1")
    for stored in _tokens(dbmod):
        assert dbmod.user_for_token(stored) is None, (
            "a value read straight from the sessions table still authenticates")


def test_migration_converts_legacy_rows_without_logging_anyone_out(dbmod):
    """Existing installs must not have their users kicked out — the raw token a client already
    holds has to keep working after the upgrade."""
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.authenticate("u", "pw-secret-1")
    with dbmod.conn() as c:                      # regress the row to the legacy raw form
        c.execute("UPDATE sessions SET token = ?", (tok,))
    assert tok in _tokens(dbmod)
    dbmod.init()                                  # migration runs here
    assert tok not in _tokens(dbmod)
    assert (dbmod.user_for_token(tok) or {}).get("username") == "u", "the migration logged them out"


def test_migration_is_idempotent(dbmod):
    """Running twice must not hash the hash, which would silently invalidate every session."""
    dbmod.create_user("u", "pw-secret-1")
    tok = dbmod.authenticate("u", "pw-secret-1")
    dbmod.init(); dbmod.init()
    assert (dbmod.user_for_token(tok) or {}).get("username") == "u"
