"""P2.9 free-response grading (iss_8c6eb4ee). Local checks FIRST — a numeric or
regex question never costs an API call; only rubric-kind grading calls the LLM.

grade(q, text) -> (correct: bool, feedback: str) | None
None means "cannot grade honestly right now" (rubric kind, AI unavailable) —
the caller must treat that as UNGRADED, never as wrong.

Bug-sweep hardening (2026-07-31): learner text capped (ReDoS surface, BE-3);
patterns compile under try/except (BE-8) and match with fullmatch anchoring —
an unanchored IGNORECASE search let "no" grade "I do not know" correct (BE-18);
numeric answers coerce safely (BE-9), default tolerance is a small relative
epsilon instead of exact float equality (BE-19), and the number parser accepts
scientific notation (BE-20). Pattern QUALITY is enforced upstream in
validate_pack (compilable, non-pathological) — this module stays fail-safe.
"""
from __future__ import annotations

import re

TEXT_MAX = 600            # BE-3: bound the learner-controlled regex input


def _first_number(text: str) -> float | None:
    # BE-20: scientific notation supported ("3.0e8" is 3e8, not 3)
    m = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?", text.replace("−", "-"))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _numeric_target(g: dict) -> tuple[float, float] | None:
    """(answer, tolerance) with safe coercion; None = authoring error."""
    try:
        ans = float(g["answer"])
    except (KeyError, TypeError, ValueError):
        return None
    tol = g.get("tolerance")
    if tol is None:
        # BE-19: exact float compare rejects 0.333 for 1/3 — default to a
        # 0.5% relative epsilon (floor 1e-9 so answer=0 still works)
        tol = max(abs(ans) * 0.005, 1e-9)
    else:
        try:
            tol = abs(float(tol))
        except (TypeError, ValueError):
            return None
    return ans, tol


def grade(q: dict, text: str) -> tuple[bool, str] | None:
    g = q.get("grading") or {}
    kind = g.get("kind")
    text = (text or "").strip()[:TEXT_MAX]
    ok_fb = q.get("feedback_correct") or "Correct."
    bad_fb = q.get("feedback_incorrect") or ""

    if kind == "numeric":
        target = _numeric_target(g)
        if target is None:               # BE-9: malformed answer → loud, not 500
            return False, "(authoring error: numeric grading needs a numeric 'answer')"
        ans, tol = target
        val = _first_number(text)
        if val is None:
            return False, (f"I couldn't find a number in your answer. {bad_fb}").strip()
        if abs(val - ans) <= tol:
            return True, ok_fb
        return False, (f"Your value {val:g} is outside the accepted range "
                       f"({ans:g} ± {tol:g}"
                       f"{(' ' + g['unit']) if g.get('unit') else ''}). {bad_fb}").strip()

    if kind == "regex":
        for pat in g.get("patterns", []):
            try:
                # BE-18: fullmatch anchors the pattern — authors write the
                # looseness they mean (".*\bno\b.*"), it never happens by accident
                if re.fullmatch(pat, text, re.IGNORECASE):
                    return True, ok_fb
            except re.error:             # BE-8: bad pattern → skip, not 500
                continue
        return False, (bad_fb or "Not quite — compare with the model answer: "
                       + g.get("model_answer", ""))

    if kind == "rubric":
        import ai
        out = ai.grade_free(q, text)
        if out is None:
            return None                  # honest UNGRADED — caller handles
        return bool(out["correct"]), out.get("feedback", "")

    # unknown kind = authoring error; fail loudly, not silently wrong
    return False, f"(authoring error: unknown grading kind {kind!r})"
