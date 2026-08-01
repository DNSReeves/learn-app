"""Learn — pluggable outbound mail for email-verified self-registration.

DELIBERATELY ISOLATED, and that isolation is the point.

The sibling dnsr-agent runtime restricts its production Gmail identity to three
recipient addresses behind an operator-confirmation gate (security/policies.py).
Registration mail goes to arbitrary strangers, so routing it through that identity
— or side-stepping the gate with the Gmail app password — would defeat an
operator-set control. Therefore this module:

  * NEVER imports anything from the dnsr-agent repo,
  * NEVER reads GMAIL_APP_PASSWORD or any agent credential,
  * is OFF by default and refuses to send until an operator has deliberately
    configured a DEDICATED sending identity of its own.

Transports (env LEARN_MAIL_TRANSPORT):
  off      (default) — log + return False. Registration then reports, to the
                       OPERATOR, that delivery is unconfigured.
  smtp                — generic SMTP. Works with any dedicated mailbox or
                        transactional provider (Postmark/SES/Mailgun/Fastmail…).
  console             — write the code to the log. DEV / LAN ONLY: refuses to
                        run when LEARN_PUBLIC=1.

Operator env for `smtp`:
  LEARN_SMTP_HOST      required, e.g. smtp.postmarkapp.com
  LEARN_SMTP_PORT      default 587
  LEARN_SMTP_FROM      required, the dedicated From: address
  LEARN_SMTP_USER      optional (omit for an unauthenticated relay)
  LEARN_SMTP_PASS      optional
  LEARN_SMTP_SECURITY  starttls (default) | ssl | none
  LEARN_MAIL_DAILY_CAP default 50 — hard ceiling across ALL addresses per day

Logging discipline: the token is NEVER logged except by the `console` transport
(that is that transport's whole job). Recipient addresses are masked (a***@dom)
at info level and everywhere else in this module.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import db

log = logging.getLogger("learn.mail")

DEFAULT_DAILY_CAP = 50
_DAY_S = 86400
# db.reg_events bucket for the global send counter (key is '' — the cap is a
# ceiling on the CONFIGURED IDENTITY, not a per-address budget).
MAIL_EVENT = "mail_send"


def transport() -> str:
    """Normalized transport name. Anything unrecognized is treated as 'off'."""
    t = (os.environ.get("LEARN_MAIL_TRANSPORT") or "off").strip().lower()
    return t if t in ("off", "smtp", "console") else "off"


def is_public() -> bool:
    return (os.environ.get("LEARN_PUBLIC") or "").strip() in ("1", "true", "yes", "on")


def daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("LEARN_MAIL_DAILY_CAP") or DEFAULT_DAILY_CAP))
    except ValueError:
        return DEFAULT_DAILY_CAP


def mask(email: str) -> str:
    """a***@example.com — enough to recognize your own address, not enough to
    hand an attacker someone else's out of a log file."""
    e = (email or "").strip()
    if "@" not in e:
        return "***"
    local, _, domain = e.partition("@")
    return f"{local[:1] or '*'}***@{domain}"


def sends_today() -> int:
    return db.reg_count(MAIL_EVENT, "", _DAY_S)


def _subject() -> str:
    return os.environ.get("LEARN_MAIL_SUBJECT") or "Your Learn verification code"


def _body(token: str, ttl_minutes: int) -> str:
    return (
        f"Your Learn verification code is {token}\n\n"
        f"It expires in {ttl_minutes} minutes. Enter it in the app to finish\n"
        "creating your account.\n\n"
        "If you did not ask to create a Learn account, ignore this message —\n"
        "nothing was created and no further mail will be sent.\n"
    )


def send_registration_token(to_email: str, token: str, *, ttl_minutes: int) -> bool:
    """Best-effort delivery of a registration code. True only on an actual send.

    Callers MUST treat False as 'nothing was delivered' and MUST NOT vary their
    response to the client because of it (that is an enumeration oracle) — the
    one caller-visible exception is transport 'off', which is identical for
    every input and is an operator-configuration signal, not account state.
    """
    t = transport()
    if t == "off":
        log.warning("mail transport is OFF — no registration code sent to %s "
                    "(set LEARN_MAIL_TRANSPORT)", mask(to_email))
        return False

    if t == "console" and is_public():
        # The console transport prints the code to the log. On a public install
        # that is a code-disclosure channel to anyone who can read logs, and it
        # silently 'works', which is worse than failing.
        log.error("console mail transport refused: LEARN_PUBLIC=1 "
                  "(configure LEARN_MAIL_TRANSPORT=smtp for a public install)")
        return False

    cap = daily_cap()
    used = sends_today()
    if used >= cap:
        log.error("daily mail cap reached (%d/%d) — no code sent to %s",
                  used, cap, mask(to_email))
        return False

    ok = _console(to_email, token, ttl_minutes) if t == "console" \
        else _smtp(to_email, token, ttl_minutes)
    if ok:
        db.reg_event(MAIL_EVENT, "")
        log.info("registration code sent to %s via %s (%d/%d today)",
                 mask(to_email), t, used + 1, cap)
    return ok


def _console(to_email: str, token: str, ttl_minutes: int) -> bool:
    """DEV/LAN ONLY. The single place in this codebase allowed to log a token."""
    log.warning("[console mail] to=%s code=%s ttl=%dm", to_email, token, ttl_minutes)
    return True


def _smtp(to_email: str, token: str, ttl_minutes: int) -> bool:
    host = (os.environ.get("LEARN_SMTP_HOST") or "").strip()
    frm = (os.environ.get("LEARN_SMTP_FROM") or "").strip()
    if not host or not frm:
        log.error("smtp transport misconfigured: LEARN_SMTP_HOST and "
                  "LEARN_SMTP_FROM are both required")
        return False
    try:
        port = int(os.environ.get("LEARN_SMTP_PORT") or 587)
    except ValueError:
        log.error("smtp transport misconfigured: LEARN_SMTP_PORT is not a number")
        return False
    sec = (os.environ.get("LEARN_SMTP_SECURITY") or "starttls").strip().lower()
    user = os.environ.get("LEARN_SMTP_USER") or ""
    pw = os.environ.get("LEARN_SMTP_PASS") or ""

    msg = EmailMessage()
    msg["Subject"] = _subject()
    msg["From"] = frm
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(_body(token, ttl_minutes))

    try:
        ctx = ssl.create_default_context()
        if sec == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                if user:
                    s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                if sec != "none":
                    s.starttls(context=ctx)
                    s.ehlo()
                if user:
                    s.login(user, pw)
                s.send_message(msg)
        return True
    except Exception as e:
        # Masked recipient, exception type + message only. No token, ever.
        log.error("smtp send to %s failed: %s: %s", mask(to_email),
                  type(e).__name__, e)
        return False
