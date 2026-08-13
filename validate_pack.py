"""validate_pack.py — gate 1 for topic packs.

Usage:
  python3 validate_pack.py topics/calculus.json            # schema check
  python3 validate_pack.py topics/calculus.json --links    # + live HTTP check on resources

Exit 0 = pass. Nonzero = list of violations. Nothing goes live without a pass.
"""
import json
import pathlib
import re
import urllib.error
import sys
import urllib.request

errs = []


def _registered_anims():
    """Anim keys actually registered in static/index.html — read dynamically so new
    animations (declarative ANIMS.key= specs AND the ANIMS{} method shorthand) don't
    require editing this validator. Falls back to the original set if the file is
    unreadable, so the gate never crashes on an env issue."""
    try:
        html = (pathlib.Path(__file__).parent / "static" / "index.html").read_text()
    except OSError:
        return {"square", "grid", "chain", "beta"}
    keys = set(re.findall(r"ANIMS\.([a-z0-9_]+)\s*=", html))            # ANIMS.key = ...
    block = re.search(r"const ANIMS\s*=\s*\{(.*?)\n\};", html, re.S)     # the ANIMS{} literal
    if block:
        keys |= set(re.findall(r"(?:async\s+)?([a-z0-9_]+)\s*\(cv,ctx\)", block.group(1)))
    return {k[:-7] if k.endswith("_static") else k for k in keys}


_ANIMS = _registered_anims()


import concurrent.futures as _cf
import threading as _threading

_tl = _threading.local()


def err(m):
    # BE-7: collect into a thread-local list — the old module-global raced
    # across the server threadpool (concurrent validations clobbered each
    # other; worst case a bad pack validated clean and its non-slug id walked
    # into a filesystem path). The global `errs` stays for CLI back-compat.
    getattr(_tl, "errs", errs).append(m)


def _regex_issue(pat) -> str | None:
    """Compile + pathological-input probe for an authored grading pattern.
    Returns a description of the problem, or None if the pattern is usable."""
    if not isinstance(pat, str) or not pat.strip():
        return "empty or non-string pattern"
    try:
        cp = re.compile(pat, re.IGNORECASE)
    except re.error as e:
        return f"does not compile ({e})"
    probes = ["a" * 300, "ab" * 150, ("a" * 40 + "!") * 6, "no " * 100]
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(lambda: [cp.fullmatch(p) for p in probes])
        fut.result(timeout=0.25)
    except _cf.TimeoutError:
        return "pathological backtracking (probe exceeded 250ms) — rewrite without nested quantifiers"
    except Exception as e:  # noqa: BLE001
        return f"probe failed ({type(e).__name__})"
    finally:
        ex.shutdown(wait=False)
    return None


def _check_preface(pack):
    """The optional `preface` — the subject explained to a curious twelve-year-old, shown BEFORE
    the intro (operator, 2026-08-12).

    It is not a shorter intro. The intro argues why a subject deserves an adult's attention; the
    preface makes the central idea land at all, using only what a twelve-year-old already has.
    That difference is what these checks defend:

      · no jargon gate can be automated, but LENGTH can — long sentences are where explanations
        stop being readable at that age, so the average is capped;
      · `aha` cards are the memorable takeaways and are capped at 3, because four is a lecture;
      · `anim`, as with the intro, must already be registered in index.html.

    Optional, strict once present — same rule as the intro. A half-built preface is the first
    thing a student ever sees.
    """
    pref = pack.get("preface")
    if pref is None:
        return
    if not isinstance(pref, dict):
        err("preface must be an object")
        return
    hook = pref.get("hook")
    if not isinstance(hook, str) or not hook.strip():
        err("preface: missing 'hook' — the one line that has to make a twelve-year-old curious")
    elif len(hook) > 200:
        err(f"preface: hook is {len(hook)} chars — keep it under 200; it is a hook, not a summary")
    body = pref.get("body")
    if not isinstance(body, list) or not body or not all(isinstance(x, str) and x.strip() for x in body):
        err("preface: 'body' must be a non-empty list of non-empty paragraphs")
    elif len(body) > 6:
        err(f"preface: {len(body)} body paragraphs — 6 is the ceiling for this audience")
    else:
        import re as _re
        sents = [t for para in body for t in _re.split(r"(?<=[.!?])\s+", para) if t.strip()]
        if sents:
            avg = sum(len(t.split()) for t in sents) / len(sents)
            if avg > 26:
                err(f"preface: average sentence is {avg:.0f} words — over 26 stops reading as "
                    f"plain speech. Split the long ones.")
            longest = max(sents, key=lambda t: len(t.split()))
            if len(longest.split()) > 48:
                err(f"preface: a {len(longest.split())}-word sentence — no twelve-year-old parses "
                    f"that: {longest[:70]}…")
    aha = pref.get("aha")
    if aha is not None:
        if not isinstance(aha, list) or not 1 <= len(aha) <= 3:
            err("preface: 'aha' must be a list of 1-3 {title, text} cards — four is a lecture")
        else:
            for i, a in enumerate(aha):
                if not isinstance(a, dict) or not a.get("title") or not a.get("text"):
                    err(f"preface: aha[{i}] needs a non-empty 'title' and 'text'")
    anim = pref.get("anim")
    if anim is not None and anim not in _ANIMS:
        err(f"preface: anim '{anim}' is not registered in static/index.html "
            f"(known: {', '.join(sorted(_ANIMS))})")
    if pref.get("closer") is not None and not str(pref.get("closer")).strip():
        err("preface: 'closer' must be a non-empty string when present")


def _check_intro(pack):
    """The optional `intro` block — a motivating opening shown before concept 1.

    OPTIONAL, but strict once present. A half-built intro is worse than none: it is the first
    thing a student sees, so a missing hook or an unregistered animation is a broken first
    impression rather than a degraded later screen.

    `anim` must name an animation ALREADY registered in index.html. The intro reuses the
    existing animation registry deliberately — no new asset pipeline, no external files, same
    edit-file-then-refresh property the rest of the app has.
    """
    intro = pack.get("intro")
    if intro is None:
        return
    if not isinstance(intro, dict):
        err("intro must be an object")
        return
    hook = intro.get("hook")
    if not isinstance(hook, str) or not hook.strip():
        err("intro: missing 'hook' (the opening line that has to earn the next 30 seconds)")
    elif len(hook) > 240:
        err(f"intro: hook is {len(hook)} chars — keep it under 240, it is a hook not a paragraph")

    body = intro.get("body")
    if not isinstance(body, list) or not body or not all(
            isinstance(p, str) and p.strip() for p in body):
        err("intro: 'body' must be a non-empty list of non-empty paragraphs")
    elif len(body) > 4:
        err(f"intro: {len(body)} body paragraphs — 4 is the ceiling; this is a doorway, not a lecture")

    why = intro.get("why")
    if not isinstance(why, list) or not (2 <= len(why) <= 4):
        err("intro: 'why' must be a list of 2-4 {title, text} cards")
    else:
        for i, w in enumerate(why):
            if not isinstance(w, dict) or not str(w.get("title", "")).strip() \
                    or not str(w.get("text", "")).strip():
                err(f"intro: why[{i}] needs a non-empty 'title' and 'text'")

    anim = intro.get("anim")
    if anim is not None:
        if not isinstance(anim, str) or anim not in _ANIMS:
            err(f"intro: anim '{anim}' is not registered in static/index.html "
                f"(a visual that does not exist is a blank box on the first screen)")

    closer = intro.get("closer")
    if closer is not None and (not isinstance(closer, str) or not closer.strip()):
        err("intro: 'closer' must be a non-empty string when present")


def check(pack):
    _tl.errs = []
    for k in ("id", "title", "concepts"):
        if k not in pack:
            err(f"pack missing '{k}'")
    if not re.fullmatch(r"[a-z0-9-]+", pack.get("id", "")):
        err("pack id must be a lowercase slug")
    _check_preface(pack)
    _check_intro(pack)
    ids = set()
    for i, c in enumerate(pack.get("concepts", [])):
        tag = f"concept[{i}] ({c.get('id','?')})"
        for k in ("id", "title", "summary", "cards", "questions"):
            if k not in c:
                err(f"{tag}: missing '{k}'")
        if c.get("id") in ids:
            err(f"{tag}: duplicate concept id")
        ids.add(c.get("id"))
        cards = c.get("cards", [])
        if not (1 <= len(cards) <= 4):
            err(f"{tag}: {len(cards)} cards (chunking rule: 1-4)")
        for j, card in enumerate(cards):
            if not card.get("h") or not card.get("md"):
                err(f"{tag} card[{j}]: needs 'h' and 'md'")
            if len(card.get("md", "")) > 2200:
                err(f"{tag} card[{j}]: card too long ({len(card['md'])} chars) — split the chunk")
        qs = c.get("questions", [])
        if len(qs) < 3:
            err(f"{tag}: {len(qs)} questions (need ≥3 for the mastery gate to mean anything)")
        qids = set()
        for q in qs:
            qt = f"{tag} q({q.get('id','?')})"
            if q.get("id") in qids:
                err(f"{qt}: duplicate question id")
            qids.add(q.get("id"))
            if q.get("type") == "free":
                # P2.9 free-response (graded by grading.py: numeric | regex | rubric) — no options.
                if not str(q.get("prompt", "")).strip():
                    err(f"{qt}: free-response needs a 'prompt'")
                g = q.get("grading") or {}
                if g.get("kind") not in ("numeric", "regex", "rubric"):
                    err(f"{qt}: free grading.kind must be numeric|regex|rubric (got {g.get('kind')!r})")
                if g.get("kind") == "numeric":
                    # BE-9: the grader formats answer with :g — it must BE numeric
                    if not isinstance(g.get("answer"), (int, float)) or isinstance(g.get("answer"), bool):
                        err(f"{qt}: numeric grading needs a numeric 'answer'")
                    if "tolerance" in g and (not isinstance(g["tolerance"], (int, float))
                                             or isinstance(g["tolerance"], bool)):
                        err(f"{qt}: numeric 'tolerance' must be a number")
                if g.get("kind") == "regex":
                    # BE-10: the grader matches g['patterns'] — requiring 'answer'
                    # here meant a validator-passing regex question could never
                    # grade correct, and a working one failed predeploy.
                    pats = g.get("patterns")
                    if not isinstance(pats, list) or not pats:
                        err(f"{qt}: regex grading needs a non-empty 'patterns' list")
                    else:
                        for p in pats:
                            issue = _regex_issue(p)
                            if issue:
                                err(f"{qt}: pattern {p!r}: {issue}")
                if g.get("kind") == "rubric":
                    # rubric-graded: per-category feedback lives in grading.rubric, so a single
                    # feedback_incorrect is not required (the rubric supplies it).
                    if not (g.get("rubric") and all(r.get("feedback") for r in g["rubric"])):
                        err(f"{qt}: rubric grading needs a 'rubric' with per-category feedback")
                    if len(str(q.get("feedback_correct", "")).strip()) < 15:
                        err(f"{qt}: feedback_correct too thin")
                else:  # numeric | regex — single-shot correct/incorrect feedback
                    for key in ("feedback_correct", "feedback_incorrect"):
                        if len(str(q.get(key, "")).strip()) < 15:
                            err(f"{qt}: {key} too thin — explain the answer/misconception")
            else:
                opts = q.get("options", [])
                if not (2 <= len(opts) <= 6):
                    err(f"{qt}: {len(opts)} options")
                a = q.get("answer")
                if not isinstance(a, int) or not (0 <= a < len(opts)):
                    err(f"{qt}: answer index out of range")
                fb = q.get("feedback", [])
                if len(fb) != len(opts):
                    err(f"{qt}: feedback count {len(fb)} != options {len(opts)} — every distractor needs its own explanation")
                for f in fb:
                    if len(f.strip()) < 15:
                        err(f"{qt}: feedback too thin ('{f[:30]}…') — explain the misconception")
        for r in c.get("resources", []):
            if not str(r.get("url", "")).startswith("https://") and not str(r.get("url","")).startswith("http://"):
                err(f"{tag} resource: bad url {r.get('url')}")
            if r.get("type") not in ("video", "interactive", "reading", "paper", "article", "essay", "book", "docs", "data"):
                err(f"{tag} resource {r.get('label')}: unknown type {r.get('type')!r} "
                    "(video|interactive|reading|paper|article|essay|book|docs|data)")
        if c.get("anim") and c["anim"] not in _ANIMS:
            err(f"{tag}: unknown anim '{c['anim']}' (register it in static/index.html ANIMS first; "
                f"known: {', '.join(sorted(_ANIMS))})")
    # P5.17 (iss_8d424360): migration declarations. renamed_concepts maps
    # old-id -> new-id; the TARGET must exist in this pack, the SOURCE must not
    # (it was renamed away). packver.sync_pack migrates concept_state on load.
    mig = pack.get("migrations", {})
    if mig:
        if not isinstance(mig, dict):
            err("migrations: must be an object")
        ren = mig.get("renamed_concepts", {})
        if not isinstance(ren, dict):
            err("migrations.renamed_concepts: must be an object of old-id -> new-id")
        else:
            for old, new in ren.items():
                if not isinstance(old, str) or not isinstance(new, str):
                    err(f"migrations.renamed_concepts: {old!r} -> {new!r} must be strings")
                    continue
                if new not in ids:
                    err(f"migrations.renamed_concepts: target '{new}' is not a concept id in this pack")
                if old in ids:
                    err(f"migrations.renamed_concepts: source '{old}' still exists in this pack — not a rename")
        unknown = set(mig) - {"renamed_concepts"}
        if unknown:
            err(f"migrations: unknown key(s) {sorted(unknown)}")
    # BE-7: return the thread-local collection; mirror into the module global
    # so the CLI path (`check(pack); if errs:`) keeps working single-threaded.
    collected = _tl.errs
    del _tl.errs
    errs.extend(collected)
    return collected


def check_links(pack):
    seen = set()
    for c in pack.get("concepts", []):
        for r in c.get("resources", []):
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                req = urllib.request.Request(url, method="GET",
                                             headers={"User-Agent": "Mozilla/5.0 (LearnLinkCheck)"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status >= 400:
                        err(f"link {resp.status}: {url}")
                    else:
                        print(f"  ok  {url}")
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    # Many legitimate sites bot-block scripted requests; verify by hand.
                    print(f"  WARN 403 (likely bot-block — open in a browser to confirm): {url}")
                else:
                    err(f"link failed: {url} (HTTP {e.code})")
            except Exception as e:
                err(f"link failed: {url} ({e})")


if __name__ == "__main__":
    path = sys.argv[1]
    pack = json.load(open(path))
    check(pack)
    if "--links" in sys.argv:
        check_links(pack)
    if errs:
        print(f"FAIL — {len(errs)} issue(s) in {path}:")
        for e in errs:
            print("  •", e)
        sys.exit(1)
    print(f"PASS — {path}: {len(pack['concepts'])} concepts, "
          f"{sum(len(c['questions']) for c in pack['concepts'])} questions, "
          f"{sum(len(c.get('resources', [])) for c in pack['concepts'])} resources")
