"""Email-verified self-registration (2026-08-01) — hermetic pins.

Temp LEARN_DB, no network, mail transport stubbed. The load-bearing test in
here is test_response_is_byte_identical_fresh_vs_registered: /api/register/start
must never let a stranger discover who has an account on this install.
"""
import importlib
import os
import sqlite3
import sys
import time

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def reg(tmp_path, monkeypatch):
    """(client, app, db, sent) with the mailer stubbed to capture codes."""
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "learn.db"))
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "console")
    for k in ("LEARN_INVITE_CODE", "LEARN_REGISTRATION", "LEARN_PUBLIC",
              "LEARN_MAIL_DAILY_CAP"):
        monkeypatch.delenv(k, raising=False)
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import mailer as mmod
    importlib.reload(mmod)
    import app as amod
    importlib.reload(amod)

    sent = []

    def fake_send(to_email, token, *, ttl_minutes):
        sent.append({"to": to_email, "token": token, "ttl": ttl_minutes})
        return True

    monkeypatch.setattr(amod.mailer, "send_registration_token", fake_send)
    from fastapi.testclient import TestClient
    return TestClient(amod.app), amod, dbmod, sent


def _start(tc, email="new@example.com", username="alice", password="pw-secret-1", **kw):
    body = {"username": username, "email": email, "password": password}
    body.update(kw)
    return tc.post("/api/register/start", json=body)


def _rows(dbmod):
    with dbmod.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM pending_registrations")]


# ── the happy path ───────────────────────────────────────────────────────────

def test_start_creates_pending_row_and_no_user(reg):
    tc, amod, dbmod, sent = reg
    r = _start(tc)
    assert r.status_code == 200
    assert r.json() == amod.NEUTRAL_START
    rows = _rows(dbmod)
    assert len(rows) == 1 and rows[0]["email"] == "new@example.com"
    assert rows[0]["consumed_at"] is None and rows[0]["attempts"] == 0
    assert dbmod.user_count() == 0            # the account does NOT exist yet
    assert len(sent) == 1 and sent[0]["ttl"] == 15
    assert sent[0]["token"].isdigit() and len(sent[0]["token"]) == 6


def test_verify_creates_the_user_and_signs_in(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    r = tc.post("/api/register/verify",
                json={"email": "NEW@Example.com ", "token": sent[0]["token"]})
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"token", "role"} and d["role"] == "learner"
    # same shape as /api/login → the client logs straight in
    assert set(tc.post("/api/login", json={"username": "alice",
                                           "password": "pw-secret-1"}).json()) == set(d)
    assert dbmod.user_for_token(d["token"])["username"] == "alice"
    assert dbmod.user_count() == 1
    with dbmod.conn() as c:
        assert c.execute("SELECT email FROM users").fetchone()[0] == "new@example.com"


def test_email_is_normalized_on_start(reg):
    tc, amod, dbmod, sent = reg
    _start(tc, email="  MiXeD@Example.COM  ")
    assert _rows(dbmod)[0]["email"] == "mixed@example.com"


# ── code handling ────────────────────────────────────────────────────────────

def test_wrong_token_fails_and_increments_attempts(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    r = tc.post("/api/register/verify",
                json={"email": "new@example.com", "token": "000000"})
    assert r.status_code == 400
    assert _rows(dbmod)[0]["attempts"] == 1
    assert dbmod.user_count() == 0


def test_sixth_attempt_hard_fails(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    wrong = "999999" if sent[0]["token"] != "999999" else "111111"
    for i in range(5):
        assert tc.post("/api/register/verify",
                       json={"email": "new@example.com", "token": wrong}).status_code == 400
    assert _rows(dbmod)[0]["attempts"] == 5
    r = tc.post("/api/register/verify",
                json={"email": "new@example.com", "token": wrong})
    assert r.status_code == 429
    # and the row is dead even for the RIGHT code
    assert tc.post("/api/register/verify",
                   json={"email": "new@example.com",
                         "token": sent[0]["token"]}).status_code == 429
    assert dbmod.user_count() == 0


def test_expired_token_fails(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    with dbmod.conn() as c:
        c.execute("UPDATE pending_registrations SET expires_at = ?", (time.time() - 1,))
    r = tc.post("/api/register/verify",
                json={"email": "new@example.com", "token": sent[0]["token"]})
    assert r.status_code == 400
    assert dbmod.user_count() == 0


def test_consumed_token_cannot_be_reused(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    tok = sent[0]["token"]
    assert tc.post("/api/register/verify",
                   json={"email": "new@example.com", "token": tok}).status_code == 200
    r = tc.post("/api/register/verify",
                json={"email": "new@example.com", "token": tok})
    assert r.status_code == 400
    assert dbmod.user_count() == 1            # no second account


def test_resend_replaces_the_code_and_resets_attempts(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    tc.post("/api/register/verify", json={"email": "new@example.com", "token": "000000"})
    assert _rows(dbmod)[0]["attempts"] == 1
    _start(tc)                                 # resend
    assert len(sent) == 2 and _rows(dbmod)[0]["attempts"] == 0
    assert len(_rows(dbmod)) == 1              # create-or-REPLACE, not a pile-up
    # the superseded code is dead, the new one works
    assert tc.post("/api/register/verify",
                   json={"email": "new@example.com",
                         "token": sent[0]["token"]}).status_code == 400
    assert tc.post("/api/register/verify",
                   json={"email": "new@example.com",
                         "token": sent[1]["token"]}).status_code == 200


# ── the enumeration pin ──────────────────────────────────────────────────────

def test_response_is_byte_identical_fresh_vs_registered(reg):
    tc, amod, dbmod, sent = reg
    _start(tc, email="taken@example.com", username="alice")
    tc.post("/api/register/verify",
            json={"email": "taken@example.com", "token": sent[-1]["token"]})
    assert dbmod.user_count() == 1

    fresh = _start(tc, email="fresh@example.com", username="bob")
    known = _start(tc, email="taken@example.com", username="carol")
    assert fresh.status_code == known.status_code == 200
    assert fresh.content == known.content              # byte-for-byte
    assert fresh.json() == amod.NEUTRAL_START
    # …and nothing was sent to the address that already has an account
    assert [s["to"] for s in sent[1:]] == ["fresh@example.com"]


def test_malformed_email_is_also_neutral(reg):
    tc, amod, dbmod, sent = reg
    good = _start(tc, email="fine@example.com")
    for bad in ("nope", "a@b", "@example.com", "a b@example.com", ""):
        r = _start(tc, email=bad, username="x")
        assert r.status_code == 200 and r.content == good.content
    assert len(sent) == 1 and not _rows(dbmod)[0]["email"].startswith("nope")


def test_bad_username_or_password_is_rejected_by_the_same_rules(reg):
    tc, amod, dbmod, sent = reg
    assert _start(tc, username="  ").status_code == 400
    assert _start(tc, password="short").status_code == 400
    assert sent == [] and _rows(dbmod) == []


# ── throttles ────────────────────────────────────────────────────────────────

def test_per_email_throttle_trips_and_stays_neutral(reg):
    tc, amod, dbmod, sent = reg
    base = None
    for i in range(3):
        r = _start(tc, email="loop@example.com")
        base = base or r.content
        assert r.content == base
    assert len(sent) == 3
    r = _start(tc, email="loop@example.com")           # 4th within the hour
    assert r.status_code == 200 and r.content == base
    assert len(sent) == 3                              # no send


def test_per_ip_throttle_trips_and_stays_neutral(reg):
    tc, amod, dbmod, sent = reg
    base = _start(tc, email="ip0@example.com").content
    for i in range(1, 10):
        assert _start(tc, email=f"ip{i}@example.com").content == base
    assert len(sent) == 10
    r = _start(tc, email="ip99@example.com")           # 11th from this IP
    assert r.status_code == 200 and r.content == base
    assert len(sent) == 10


def test_verify_ip_throttle_returns_429(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    for i in range(20):
        tc.post("/api/register/verify",
                json={"email": f"nobody{i}@example.com", "token": "000000"})
    r = tc.post("/api/register/verify",
                json={"email": "new@example.com", "token": sent[0]["token"]})
    assert r.status_code == 429


# ── secret hygiene ───────────────────────────────────────────────────────────

def test_password_is_never_stored_in_plaintext(reg):
    tc, amod, dbmod, sent = reg
    pw = "correct-horse-battery"
    _start(tc, password=pw)
    row = _rows(dbmod)[0]
    assert "pw_hash" in row and isinstance(row["pw_hash"], bytes)
    assert all(pw not in str(v) for v in row.values())
    assert pw.encode() not in row["pw_hash"] + row["pw_salt"]
    # nowhere in the file either
    with open(os.environ["LEARN_DB"], "rb") as f:
        assert pw.encode() not in f.read()


def test_token_is_stored_only_as_a_hash(reg):
    tc, amod, dbmod, sent = reg
    _start(tc)
    tok = sent[0]["token"]
    row = _rows(dbmod)[0]
    assert "token" not in [k for k, v in row.items() if v == tok]
    assert all(str(v) != tok for v in row.values())
    assert dbmod.check_pending_token(row, tok)
    assert not dbmod.check_pending_token(row, "000000")


def test_mailer_never_touches_the_agent_identity():
    """The design constraint, pinned in the AST (not the prose): registration
    mail must not ride the dnsr-agent's Gmail — that identity's policy layer
    gates external recipients, and this sends to strangers."""
    import ast
    tree = ast.parse(open(os.path.join(BASE, "mailer.py")).read())

    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    assert imported <= {"logging", "os", "smtplib", "ssl", "email", "db"}, imported

    # every env var this module actually reads, from the call sites
    env = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("get", "__getitem__") and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            env.add(n.args[0].value)
    # its whole configuration surface is LEARN_* — no borrowed agent credential
    assert env and all(k.startswith("LEARN_") for k in env), env


# ── housekeeping ─────────────────────────────────────────────────────────────

def test_start_purges_stale_rows(reg):
    tc, amod, dbmod, sent = reg
    _start(tc, email="old@example.com")
    with dbmod.conn() as c:
        c.execute("UPDATE pending_registrations SET expires_at = ?",
                  (time.time() - 86400 * 2,))
    _start(tc, email="new2@example.com")
    assert [r["email"] for r in _rows(dbmod)] == ["new2@example.com"]


# ── kill switch ──────────────────────────────────────────────────────────────

def test_registration_closed_refuses_everything(reg, monkeypatch):
    tc, amod, dbmod, sent = reg
    monkeypatch.setenv("LEARN_REGISTRATION", "closed")
    assert _start(tc).status_code == 403
    assert tc.post("/api/register/verify",
                   json={"email": "a@b.com", "token": "000000"}).status_code == 403
    assert tc.post("/api/register",
                   json={"username": "z", "password": "pw-secret-1"}).status_code == 403


def test_invite_mode_gates_the_email_path(reg, monkeypatch):
    tc, amod, dbmod, sent = reg
    monkeypatch.setenv("LEARN_REGISTRATION", "invite")
    monkeypatch.setenv("LEARN_INVITE_CODE", "s3cret")
    assert _start(tc).status_code == 403
    assert _start(tc, invite="wrong").status_code == 403
    assert _start(tc, invite="s3cret").status_code == 200
    assert len(sent) == 1


def test_zero_user_lan_bootstrap_still_works(reg, monkeypatch):
    """The legacy path a fresh install depends on must survive this feature."""
    tc, amod, dbmod, sent = reg
    monkeypatch.setattr(amod, "_lan_client", lambda request: True)
    r = tc.post("/api/register", json={"username": "first", "password": "pw-secret-1"})
    assert r.status_code == 200 and r.json()["token"]
    assert dbmod.user_count() == 1
    # …and only for the FIRST user
    assert tc.post("/api/register",
                   json={"username": "second", "password": "pw-secret-1"}).status_code == 403


# ── transport ────────────────────────────────────────────────────────────────

def test_transport_off_yields_the_operator_facing_503(reg, monkeypatch):
    tc, amod, dbmod, sent = reg
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "off")
    r = _start(tc)
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]
    assert _rows(dbmod) == [] and sent == []
    # identical for every input — not an enumeration channel
    assert _start(tc, email="other@example.com").content == r.content


def test_unknown_transport_is_treated_as_off(reg, monkeypatch):
    tc, amod, dbmod, sent = reg
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "carrier-pigeon")
    assert _start(tc).status_code == 503


# ── the mailer itself (unstubbed) ────────────────────────────────────────────

@pytest.fixture()
def mail(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "mail.db"))
    for k in ("LEARN_MAIL_TRANSPORT", "LEARN_MAIL_DAILY_CAP", "LEARN_PUBLIC"):
        monkeypatch.delenv(k, raising=False)
    import db as dbmod
    importlib.reload(dbmod)
    dbmod.init()
    import mailer as mmod
    importlib.reload(mmod)
    return mmod, dbmod


def test_transport_off_is_the_default(mail):
    mmod, dbmod = mail
    assert mmod.transport() == "off"
    assert mmod.send_registration_token("a@b.com", "123456", ttl_minutes=15) is False


def test_console_refuses_when_public(mail, monkeypatch):
    mmod, dbmod = mail
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "console")
    assert mmod.send_registration_token("a@b.com", "123456", ttl_minutes=15) is True
    monkeypatch.setenv("LEARN_PUBLIC", "1")
    assert mmod.send_registration_token("a@b.com", "123456", ttl_minutes=15) is False


def test_daily_cap_blocks_sends(mail, monkeypatch):
    mmod, dbmod = mail
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "console")
    monkeypatch.setenv("LEARN_MAIL_DAILY_CAP", "2")
    assert mmod.send_registration_token("a@b.com", "111111", ttl_minutes=15) is True
    assert mmod.send_registration_token("c@d.com", "222222", ttl_minutes=15) is True
    assert mmod.sends_today() == 2
    assert mmod.send_registration_token("e@f.com", "333333", ttl_minutes=15) is False
    assert mmod.send_registration_token("a@b.com", "444444", ttl_minutes=15) is False


def test_cap_is_global_across_addresses_and_survives_restart(mail, monkeypatch):
    mmod, dbmod = mail
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "console")
    monkeypatch.setenv("LEARN_MAIL_DAILY_CAP", "1")
    assert mmod.send_registration_token("a@b.com", "111111", ttl_minutes=15) is True
    importlib.reload(mmod)                      # "restart": counter is in the DB
    assert mmod.sends_today() == 1
    assert mmod.send_registration_token("z@z.com", "222222", ttl_minutes=15) is False


def test_smtp_without_config_fails_closed(mail, monkeypatch):
    mmod, dbmod = mail
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "smtp")
    monkeypatch.delenv("LEARN_SMTP_HOST", raising=False)
    monkeypatch.delenv("LEARN_SMTP_FROM", raising=False)
    assert mmod.send_registration_token("a@b.com", "123456", ttl_minutes=15) is False
    assert mmod.sends_today() == 0


def test_masking(mail):
    mmod, dbmod = mail
    assert mmod.mask("alice@example.com") == "a***@example.com"
    assert mmod.mask("bogus") == "***"


def test_token_never_reaches_the_log_except_on_console(mail, monkeypatch, caplog):
    mmod, dbmod = mail
    monkeypatch.setenv("LEARN_MAIL_TRANSPORT", "smtp")
    monkeypatch.setenv("LEARN_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("LEARN_SMTP_PORT", "1")          # nothing listening → fails
    monkeypatch.setenv("LEARN_SMTP_FROM", "learn@example.com")
    with caplog.at_level(0):
        assert mmod.send_registration_token("alice@example.com", "424242",
                                            ttl_minutes=15) is False
    text = caplog.text
    assert "424242" not in text
    assert "alice@example.com" not in text and "a***@example.com" in text
