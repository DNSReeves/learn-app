"""P2.9 free-response grading (iss_8c6eb4ee). Local checks FIRST — a numeric or
regex question never costs an API call; only rubric-kind grading calls the LLM.

grade(q, text) -> (correct: bool, feedback: str) | None
None means "cannot grade honestly right now" (rubric kind, AI unavailable) —
the caller must treat that as UNGRADED, never as wrong.
"""
from __future__ import annotations

import re


def _first_number(text: str) -> float | None:
    m = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text.replace("−", "-"))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def grade(q: dict, text: str) -> tuple[bool, str] | None:
    g = q.get("grading") or {}
    kind = g.get("kind")
    text = (text or "").strip()
    ok_fb = q.get("feedback_correct") or "Correct."
    bad_fb = q.get("feedback_incorrect") or ""

    if kind == "numeric":
        val = _first_number(text)
        if val is None:
            return False, (f"I couldn't find a number in your answer. {bad_fb}").strip()
        correct = abs(val - float(g["answer"])) <= float(g.get("tolerance", 0))
        if correct:
            return True, ok_fb
        return False, (f"Your value {val:g} is outside the accepted range "
                       f"({g['answer']:g} ± {g.get('tolerance', 0):g}"
                       f"{(' ' + g['unit']) if g.get('unit') else ''}). {bad_fb}").strip()

    if kind == "regex":
        for pat in g.get("patterns", []):
            if re.search(pat, text, re.IGNORECASE):
                return True, ok_fb
        return False, (bad_fb or "Not quite — compare with the model answer: "
                       + g.get("model_answer", ""))

    if kind == "rubric":
        import ai
        out = ai.grade_free(q, text)
        if out is None:
            return None                      # honest UNGRADED — caller handles
        return bool(out["correct"]), out.get("feedback", "")

    # unknown kind = authoring error; fail loudly, not silently wrong
    return False, f"(authoring error: unknown grading kind {kind!r})"
