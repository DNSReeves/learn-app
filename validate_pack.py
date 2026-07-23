"""validate_pack.py — gate 1 for topic packs.

Usage:
  python3 validate_pack.py topics/calculus.json            # schema check
  python3 validate_pack.py topics/calculus.json --links    # + live HTTP check on resources

Exit 0 = pass. Nonzero = list of violations. Nothing goes live without a pass.
"""
import json
import re
import urllib.error
import sys
import urllib.request

errs = []


def err(m):
    errs.append(m)


def check(pack):
    for k in ("id", "title", "concepts"):
        if k not in pack:
            err(f"pack missing '{k}'")
    if not re.fullmatch(r"[a-z0-9-]+", pack.get("id", "")):
        err("pack id must be a lowercase slug")
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
            if r.get("type") not in ("video", "interactive", "reading"):
                err(f"{tag} resource {r.get('label')}: type must be video|interactive|reading")
        if c.get("anim") and c["anim"] not in ("square", "grid", "chain", "beta"):
            err(f"{tag}: unknown anim '{c['anim']}' (register it in static/index.html ANIMS first)")
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
