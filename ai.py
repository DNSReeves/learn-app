"""AI adaptation layer (the "hybrid" half of the content model).

The authored topic pack is the spine; when a learner misses questions on a
concept, this module asks Claude for a re-explanation targeted at the
specific misconception their wrong answers reveal.

Requires ANTHROPIC_API_KEY in the environment. Without it, endpoints
degrade gracefully to the pack's authored hints — the app never breaks.
"""
import json
import os
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("LEARN_AI_MODEL", "claude-sonnet-4-6")


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def remediate(concept: dict, misses: list, question: dict | None) -> str | None:
    """Return a short tailored re-explanation, or None if AI unavailable/fails."""
    if not available():
        return None
    miss_lines = []
    for m in misses:
        miss_lines.append(f"- question {m['question_id']}: answered {m['given']}")
    prompt = (
        "You are a tutor inside a mastery-learning app. A learner is stuck on the "
        f"concept '{concept['title']}'.\n\n"
        f"Concept summary (authored material they already read):\n{concept.get('summary','')}\n\n"
        f"Their recent wrong answers:\n" + "\n".join(miss_lines) + "\n\n"
        + (f"The question they just missed:\n{json.dumps(question)}\n\n" if question else "")
        + "In under 150 words: identify the most likely misconception, re-explain the "
        "idea a different way than the summary (new analogy or worked micro-example), "
        "and end with one sentence telling them what to focus on. Plain text only."
    )
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text") or None
    except Exception:
        return None
