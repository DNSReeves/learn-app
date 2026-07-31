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


# ── P3.11 (iss_b98c7ee2): AI-generated supplementary practice ────────────────
# Extra formative reps for a concept the learner is grinding on. Generated
# items are EPHEMERAL and NEVER touch BKT/streak/gate — an unvalidated item
# must not be able to open (or hold shut) a mastery gate; the authored
# checkpoint stays the only gate evidence. Answer keys stay server-side.

def _validate_practice(items) -> list | None:
    """Strict shape gate on model output — reject anything malformed rather
    than serve a broken item. Returns the cleaned list or None."""
    if not isinstance(items, list) or not items:
        return None
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return None
        opts, fb = it.get("options"), it.get("feedback")
        ans = it.get("answer")
        if (not isinstance(it.get("prompt"), str) or not it["prompt"].strip()
                or not isinstance(opts, list) or len(opts) != 4
                or len({str(o) for o in opts}) != 4
                or not isinstance(fb, list) or len(fb) != 4
                or not isinstance(ans, int) or not 0 <= ans <= 3):
            return None
        out.append({"id": f"gen{i+1}", "type": "mcq", "prompt": it["prompt"].strip(),
                    "options": [str(o) for o in opts], "answer": ans,
                    "feedback": [str(f) for f in fb], "generated": True})
    return out


def generate_practice(concept: dict, misses: list, existing_prompts: list,
                      n: int = 3) -> list | None:
    """Return n validated practice MCQs targeting the learner's revealed
    misconceptions, or None when AI is unavailable / output fails the gate."""
    if not available():
        return None
    miss_lines = [f"- question {m.get('question_id')}: answered {m.get('given')}"
                  for m in (misses or [])[-6:]]
    prompt = (
        "You write practice questions inside a mastery-learning app. Generate "
        f"{n} NEW multiple-choice questions on the concept "
        f"'{concept['title']}'.\n\n"
        f"Concept summary:\n{concept.get('summary', '')}\n\n"
        + ("The learner's recent wrong answers (target these misconceptions):\n"
           + "\n".join(miss_lines) + "\n\n" if miss_lines else "")
        + "Do NOT duplicate these existing questions:\n"
        + "\n".join(f"- {p}" for p in existing_prompts[:12]) + "\n\n"
        + "Rules: each question has exactly 4 options, one correct; every "
        "distractor embodies a distinct plausible misconception; 'feedback' has "
        "one entry per option explaining why it is right or what misconception "
        "it reflects. Reply with ONLY a JSON array of objects: "
        '{"prompt": str, "options": [4 strings], "answer": 0-3, '
        '"feedback": [4 strings]}.'
    )
    body = json.dumps({"model": MODEL, "max_tokens": 2000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Content-Type": "application/json",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        raw = "".join(b.get("text", "") for b in data.get("content", [])
                      if b.get("type") == "text")
        raw = raw[raw.index("["): raw.rindex("]") + 1]
        return _validate_practice(json.loads(raw))
    except Exception:
        return None


def grade_free(question: dict, text: str) -> dict | None:
    """P2.9: grade a free-response answer against the authored rubric.
    Returns {"correct": bool, "category": str|None, "feedback": str} or None when
    AI is unavailable/fails (caller then returns an honest UNGRADED verdict —
    never a guessed one)."""
    if not available():
        return None
    g = question.get("grading", {})
    rubric = g.get("rubric", [])
    prompt = (
        "You are grading ONE short-answer response in a mastery-learning app. Be fair and "
        "strict: credit understanding, not keyword overlap.\n\n"
        f"QUESTION: {question.get('prompt','')}\n\n"
        f"MODEL ANSWER (author's): {g.get('model_answer','')}\n\n"
        "RUBRIC CATEGORIES (misconception buckets for WRONG answers):\n"
        + "\n".join(f"- {r['category']}: {r['expects']}" for r in rubric) + "\n\n"
        f"LEARNER'S ANSWER: {text[:800]}\n\n"
        'Reply with ONLY a JSON object: {"correct": true|false, "category": "<rubric '
        'category that best matches, or null if correct/none>", "feedback": "<2-3 '
        "sentences: what they got right/wrong and the key idea they missed>\"}"
    )
    body = json.dumps({"model": MODEL, "max_tokens": 300,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Content-Type": "application/json",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        raw = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        raw = raw[raw.index("{"): raw.rindex("}") + 1]
        out = json.loads(raw)
        if not isinstance(out.get("correct"), bool):
            return None
        # per-misconception feedback: the rubric's authored line leads, the model's detail follows
        cat = out.get("category")
        authored = next((r["feedback"] for r in rubric if r["category"] == cat), None)
        if authored and not out["correct"]:
            out["feedback"] = f"{authored} {out.get('feedback','')}".strip()
        return {"correct": out["correct"], "category": cat, "feedback": out.get("feedback", "")}
    except Exception:
        return None
