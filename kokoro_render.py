#!/usr/bin/env python3
"""Pre-render every Learn card's narration to .m4a with the shared Kokoro engine and
write static/audio/manifest.json ({topic:{concept:[url|null per card idx]}}).

Content-addressed by text hash -> re-runs skip unchanged cards (cheap updates when
content changes). Every file is duration-verified by the core (kokoro_tts.to_m4a asserts
> 0), so an empty render can never ship. The manifest is written after each pack, so a
crash mid-run leaves usable partial progress and a re-run resumes.

  python kokoro_render.py            # all packs
  python kokoro_render.py <topic_id> # one pack (e.g. fundamental-analysis)
"""
import os
import re
import sys
import json
import glob
import hashlib

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/agentic_software_from_scratch/kokoro-tts"))
import kokoro_tts  # noqa: E402

AUDIO = os.path.join(APP, "static", "audio")
MANIFEST = os.path.join(AUDIO, "manifest.json")


def plain(md):
    """Strip the app's minimal markdown to the spoken plain text (mirrors the client's
    cardPlainText: render markdown, take the text)."""
    t = re.sub(r"`([^`]+)`", r"\1", md)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    t = re.sub(r"^#{1,3}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.M)
    t = re.sub(r"\n{2,}", ". ", t)
    return re.sub(r"\s+", " ", t).strip()


def card_text(card):
    return (card.get("h", "") + ". " + plain(card.get("md", ""))).strip()


def load_manifest():
    try:
        return json.load(open(MANIFEST))
    except Exception:
        return {}


def render_all(only=None):
    os.makedirs(AUDIO, exist_ok=True)
    manifest = load_manifest()
    rendered = skipped = 0
    total_dur = 0.0
    for pf in sorted(glob.glob(os.path.join(APP, "topics", "*.json"))):
        if os.path.basename(pf).startswith("_"):
            continue
        pack = json.load(open(pf))
        pid = pack.get("id")
        if not pid or "concepts" not in pack:
            continue
        if only and pid != only:
            continue
        pdir = os.path.join(AUDIO, pid)
        os.makedirs(pdir, exist_ok=True)
        for c in pack["concepts"]:
            cid = c["id"]
            urls = []
            for i, card in enumerate(c.get("cards", [])):
                text = card_text(card)
                if not text:
                    urls.append(None)
                    continue
                h8 = hashlib.sha256(text.encode()).hexdigest()[:8]
                name = f"{cid}_c{i}_{h8}.m4a"
                out = os.path.join(pdir, name)
                if os.path.exists(out) and os.path.getsize(out) > 1000:
                    skipped += 1
                    urls.append(f"/static/audio/{pid}/{name}")
                    continue
                try:
                    dur = kokoro_tts.to_m4a(text, out)  # asserts duration > 0
                    total_dur += dur
                    rendered += 1
                    print(f"  {pid}/{cid} c{i}: {dur:.0f}s", flush=True)
                    urls.append(f"/static/audio/{pid}/{name}")
                except Exception as e:  # one bad card must not kill the batch → null → browser fallback
                    print(f"  !! {pid}/{cid} c{i} FAILED: {e}", flush=True)
                    urls.append(None)
            if any(urls):
                manifest.setdefault(pid, {})[cid] = urls
        json.dump(manifest, open(MANIFEST, "w"))  # incremental: usable after each pack
        print(f"[{pid}] done", flush=True)
    print(f"\nDONE: rendered {rendered}, skipped {skipped}, "
          f"{total_dur/60:.1f} min new audio; manifest = {len(manifest)} packs", flush=True)


if __name__ == "__main__":
    render_all(sys.argv[1] if len(sys.argv) > 1 else None)
