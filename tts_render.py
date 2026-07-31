"""P3.12 (iss_ff647ebc): pre-rendered TTS narration — build-time pipeline.

Renders each concept card's narration to a static m4a via macOS `say`
(no runtime TTS dependency, one consistent voice) and writes a manifest the
frontend player consumes. Content-addressed filenames make re-runs cheap
(unchanged text → file already exists → skip) and let stale audio be pruned.

Audio is an enhancement, never a gate: any per-card failure leaves a null
manifest slot (the player falls back to browser speechSynthesis) and the
pipeline never raises out of render_all().

CLI:  .venv/bin/python tts_render.py [--packs id,id] [--voice Samantha]
Manifest:  static/audio/manifest.json  →  {pack_id: {concept_id: [url|null per card]}}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
TOPICS = BASE / "topics"
AUDIO = BASE / "static" / "audio"
SAY = "/usr/bin/say"


def narration_text(card: dict) -> str:
    """Card heading + markdown body flattened to speakable plain text."""
    md = card.get("md", "")
    md = re.sub(r"```.*?```", " ", md, flags=re.S)          # code blocks: skip
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)           # images
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)        # links → text
    md = re.sub(r"[`*_#>|]", "", md)                          # md decoration
    md = re.sub(r"\s+", " ", md).strip()
    h = (card.get("h") or "").strip()
    return f"{h}. {md}".strip(". ") + "."


def _card_path(pack_id: str, concept_id: str, idx: int, text: str) -> Path:
    h8 = hashlib.sha1(text.encode()).hexdigest()[:8]
    return AUDIO / pack_id / f"{concept_id}_c{idx}_{h8}.m4a"


def _synthesize(text: str, out: Path, voice: str | None) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [SAY, "--file-format=m4af", "--data-format=aac", "-o", str(out)]
    if voice:
        cmd += ["-v", voice]
    cmd.append(text)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        return r.returncode == 0 and out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def render_pack(pack: dict, voice: str | None = None) -> dict:
    """→ {concept_id: [url|None per card]}. Skips unchanged cards by hash."""
    out = {}
    for c in pack.get("concepts", []):
        urls = []
        for i, card in enumerate(c.get("cards", [])):
            text = narration_text(card)
            if not text.strip("."):
                urls.append(None)
                continue
            p = _card_path(pack["id"], c["id"], i, text)
            ok = p.exists() or _synthesize(text, p, voice)
            urls.append(f"/static/audio/{pack['id']}/{p.name}" if ok else None)
        out[c["id"]] = urls
    return out


def render_all(pack_ids: list[str] | None = None,
               voice: str | None = None) -> dict:
    """Render every (or the named) topic pack; write + return the manifest.
    Prunes audio files no longer referenced (content-addressing makes stale
    files from edited cards otherwise accumulate). Never raises."""
    voice = voice or os.environ.get("LEARN_TTS_VOICE") or None
    manifest = {}
    if AUDIO.exists() and (AUDIO / "manifest.json").exists():
        try:
            manifest = json.loads((AUDIO / "manifest.json").read_text())
        except Exception:
            manifest = {}
    for f in sorted(TOPICS.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            pack = json.loads(f.read_text())
            pid = pack.get("id")
            if not pid or not pack.get("concepts"):
                continue
            if pack_ids and pid not in pack_ids:
                continue
            manifest[pid] = render_pack(pack, voice)
        except Exception as e:  # a bad pack must not kill the rest
            print(f"⚠ tts: {f.name} skipped ({type(e).__name__}: {e})", file=sys.stderr)
    AUDIO.mkdir(parents=True, exist_ok=True)
    (AUDIO / "manifest.json").write_text(json.dumps(manifest, indent=1))
    _prune(manifest)
    return manifest


def _prune(manifest: dict) -> None:
    keep = {Path(u).name for per in manifest.values()
            for urls in per.values() for u in urls if u}
    for pid_dir in AUDIO.iterdir() if AUDIO.exists() else []:
        if pid_dir.is_dir():
            for f in pid_dir.glob("*.m4a"):
                if f.name not in keep:
                    f.unlink(missing_ok=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs", help="comma-separated pack ids (default: all)")
    ap.add_argument("--voice", help="say voice (default: system / $LEARN_TTS_VOICE)")
    a = ap.parse_args()
    m = render_all(a.packs.split(",") if a.packs else None, a.voice)
    n = sum(1 for per in m.values() for urls in per.values() for u in urls if u)
    misses = sum(1 for per in m.values() for urls in per.values() for u in urls if u is None)
    print(f"tts manifest: {len(m)} packs, {n} rendered cards, {misses} null slots")
