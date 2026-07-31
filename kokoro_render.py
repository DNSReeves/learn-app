#!/usr/bin/env python3
"""Pre-render every Learn card's narration to .m4a with the shared Kokoro engine, in
the VOICE currently selected in the agent settings (or an explicit --voice), and write
the manifest the player reads.

Voice-aware layout (2026-07-31):
  static/audio/<voice>/<topic>/<concept>_c<idx>_<hash8>.m4a   (namespaced by voice)
  static/audio/manifest.<voice>.json    per-voice manifest ({topic:{concept:[url|null]}})
  static/audio/manifest.json            the ACTIVE manifest (a copy of the selected voice)
  static/audio/active_voice.json        {"voice": "<voice>"} for display

Because each voice has its own dir + manifest, switching to a voice already rendered is
instant (just re-activate its manifest — no re-render). Content-addressed by text hash,
so re-runs skip unchanged cards. Every file is duration-verified by the core
(kokoro_tts.to_m4a asserts > 0), so an empty render can never ship. The active manifest
is only swapped in at the END of a render, so the app keeps serving the previous voice
until the new one is complete (no half-rendered / robotic-fallback window).

  python kokoro_render.py                      # all packs, in the CURRENT setting's voice
  python kokoro_render.py --voice am_liam      # all packs, in am_liam
  python kokoro_render.py fundamental-analysis --voice bf_emma   # one pack, one voice
  python kokoro_render.py --activate af_heart  # instant switch to an already-rendered voice
"""
import os
import re
import sys
import json
import glob
import shutil
import hashlib

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/agentic_software_from_scratch/kokoro-tts"))
import kokoro_tts  # noqa: E402

AUDIO = os.path.join(APP, "static", "audio")
ACTIVE_MANIFEST = os.path.join(AUDIO, "manifest.json")


def voice_manifest_path(voice):
    return os.path.join(AUDIO, f"manifest.{voice}.json")


def resolve_voice(explicit=None):
    """The voice to render in. Defaults to the Kokoro house default (af_heart) — Learn
    is deliberately pinned to af_heart (operator, 2026-07-31: live surfaces follow the
    selected voice, but Learn stays baked in af_heart so a voice switch never triggers a
    tens-of-minutes re-render). Pass --voice <id> to bake a different voice on demand."""
    return explicit or kokoro_tts.DEFAULT_VOICE


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


def load_json(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def activate(voice):
    """Make <voice> the voice the player serves (copy its manifest into place)."""
    src = voice_manifest_path(voice)
    if not os.path.exists(src):
        print(f"!! no manifest for {voice} — render it first", flush=True)
        return False
    shutil.copyfile(src, ACTIVE_MANIFEST)
    json.dump({"voice": voice}, open(os.path.join(AUDIO, "active_voice.json"), "w"))
    print(f"[active] Learn now speaks: {voice}", flush=True)
    return True


def render_all(only=None, voice=None):
    voice = resolve_voice(voice)
    vroot = os.path.join(AUDIO, voice)
    os.makedirs(vroot, exist_ok=True)
    manifest = load_json(voice_manifest_path(voice))
    rendered = skipped = 0
    total_dur = 0.0
    print(f"[render] voice={voice}  →  static/audio/{voice}/", flush=True)
    for pf in sorted(glob.glob(os.path.join(APP, "topics", "*.json"))):
        if os.path.basename(pf).startswith("_"):
            continue
        pack = json.load(open(pf))
        pid = pack.get("id")
        if not pid or "concepts" not in pack:
            continue
        if only and pid != only:
            continue
        pdir = os.path.join(vroot, pid)
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
                url = f"/static/audio/{voice}/{pid}/{name}"
                if os.path.exists(out) and os.path.getsize(out) > 1000:
                    skipped += 1
                    urls.append(url)
                    continue
                try:
                    dur = kokoro_tts.to_m4a(text, out, voice=voice)  # asserts duration > 0
                    total_dur += dur
                    rendered += 1
                    print(f"  {pid}/{cid} c{i}: {dur:.0f}s", flush=True)
                    urls.append(url)
                except Exception as e:  # one bad card must not kill the batch → null → browser fallback
                    print(f"  !! {pid}/{cid} c{i} FAILED: {e}", flush=True)
                    urls.append(None)
            if any(urls):
                manifest.setdefault(pid, {})[cid] = urls
        json.dump(manifest, open(voice_manifest_path(voice), "w"))  # incremental per pack
        print(f"[{pid}] done", flush=True)
    # Activate only at the END — the app serves the prior voice until this one is whole.
    activate(voice)
    print(f"\nDONE ({voice}): rendered {rendered}, skipped {skipped}, "
          f"{total_dur/60:.1f} min new audio; manifest = {len(manifest)} packs", flush=True)


def _parse_argv(argv):
    only, voice, act = None, None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--voice" and i + 1 < len(argv):
            voice = argv[i + 1]; i += 2; continue
        if a == "--activate" and i + 1 < len(argv):
            act = argv[i + 1]; i += 2; continue
        if not a.startswith("--"):
            only = a
        i += 1
    return only, voice, act


if __name__ == "__main__":
    only, voice, act = _parse_argv(sys.argv[1:])
    if act:
        activate(act)
    else:
        render_all(only=only, voice=voice)
