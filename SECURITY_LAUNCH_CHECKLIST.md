# Learn — security checklist BEFORE public internet exposure

Learn is currently **LAN/tailnet-only and bearer-gated** — the items below are
deployment/abuse concerns that only bite when it moves to the open internet. From
the 2026-08-01 security review. The app's core is well-hardened (PBKDF2, 256-bit
session tokens, constant-time compare, parameterized SQL, server-side gating,
XSS-escaped SPA, enumeration-neutral registration). Do these before flipping it public:

## Must-fix (blockers)
- [ ] **TLS termination in front of the app** (Caddy/nginx/Cloudflare Tunnel); never
      serve the app port raw. Passwords, session tokens, and 6-digit codes are
      cleartext over plain HTTP. Add HSTS at the proxy (the app already emits it).
- [ ] **`--proxy-headers --forwarded-allow-ips="127.0.0.1"`** on uvicorn once behind
      a proxy. Otherwise `request.client.host` becomes `127.0.0.1` for everyone, which
      (a) makes `/api/users` (LAN-only username chooser) return usernames to the whole
      internet and (b) collapses every per-IP registration throttle into one bucket.
      Verify `/api/users` 404s from an external client and two IPs get separate buckets.
- [ ] **Per-user + global LLM/TTS rate caps** on `/help`, `/practice`, free-response
      grading, and `/api/anim_tts`. Open registration + no cap = unbounded Anthropic
      spend on the operator key and a DoS on the shared Kokoro engine. Mirror the
      `LEARN_MAIL_DAILY_CAP` pattern with a per-user hourly ceiling.

## Should-fix
- [ ] **Login throttle** (per-IP + per-username) on `/api/login` — none today.
- [ ] **Password floor** to ~10 chars (currently ≥6); optional HIBP k-anonymity check.
- [ ] **Hash session tokens at rest** — store `sha256(token)`, look up by hash
      (passwords + reg codes are hashed; session tokens are the raw PRIMARY KEY today).
      This invalidates live sessions on deploy (everyone re-logs-in), so do it at a
      maintenance moment. **Then purge the `learn.db.bak-*` copies on disk.**
- [ ] **PBKDF2 iterations 240k → 600k** (OWASP). Needs a hash-format migration that
      carries the per-hash iteration count (verify old hashes at 240k, write new at
      600k, re-hash on next login) — a bare constant bump would lock out existing users.

## Done 2026-08-01 (already shipped)
- [x] `docs_url`/`redoc_url`/`openapi_url` disabled when `LEARN_PUBLIC=1`.
- [x] Security-headers middleware (CSP / X-Frame-Options DENY / nosniff / Referrer /
      HSTS) — SPA verified working under the CSP.
- [x] Registration is enumeration-neutral, rate-limited, single-use-token, kill-switched.
- [x] Answer keys never sent to the client; mastery gate enforced on the answer route.

## Note
Set **both** `LEARN_PUBLIC=1` *and* the proxy config together — they are independent
switches and it's easy to flip one without the other.
