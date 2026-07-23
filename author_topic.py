"""author_topic.py — draft a new topic pack with Claude, to the exact Learn schema.

Two-gate flow:
  1. python3 author_topic.py llms                 # writes topics/_draft-llms.json (underscore = NOT live)
  2. python3 validate_pack.py topics/_draft-llms.json --links
  3. Human vet: read every card, question, and feedback line. Fix what's wrong.
  4. mv topics/_draft-llms.json topics/llms.json  # goes live on next request

Requires ANTHROPIC_API_KEY. Roadmap topics are predefined in topics/_roadmap.json;
arbitrary topics: python3 author_topic.py --custom "Topic Title" --slug topic-slug
"""
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("LEARN_AI_MODEL", "claude-sonnet-4-6")

PROMPT = """You are authoring a topic pack for a mastery-based learning system. Output ONLY valid JSON, no markdown fences, no preamble.

TOPIC: {title}
SLUG (pack id): {slug}
PREREQUISITE TOPIC IDS: {prereqs}
CONCEPT COUNT: {n} concepts, ordered from the most basic idea to the most advanced; the order is the unlock sequence and each concept may assume mastery of all earlier ones.

SCHEMA (follow exactly):
{{
 "id": "{slug}", "title": "...", "tagline": "one evocative line",
 "description": "1-2 sentences", "prereqs": {prereqs},
 "concepts": [
  {{"id": "c01-slug", "title": "...", "summary": "one-line gist used as fallback hint",
    "cards": [{{"h": "chunk title", "md": "markdown body"}}],
    "questions": [{{"id": "q1", "type": "mcq", "prompt": "...",
                    "options": ["...","...","...","..."], "answer": <correct index>,
                    "feedback": ["per-option explanation", "...", "...", "..."]}}],
    "resources": [{{"label": "...", "url": "https://...", "type": "video|interactive|reading",
                    "note": "why it's worth their time"}}]
  }}
 ]
}}

QUALITY RULES (non-negotiable):
- 2-3 cards per concept, each under 200 words. One idea per card. Working-memory discipline.
- Markdown in cards: **bold**, *italics*, "## " prefix for one centered key formula/statement, "- " lists.
- Exactly 3+ questions per concept. Questions test understanding and transfer, not recall of card phrasing. At least one question per concept applies the idea to a fresh scenario.
- feedback array length MUST equal options length. Each wrong-option feedback names the specific misconception that answer represents and corrects it. The correct option's feedback confirms and adds one insight.
- No trick questions; distractors are plausible errors a real learner makes.
- resources: 1-2 per concept, only canonical, stable, high-reputation sources (university OCW, 3blue1brown.com written lessons, well-known free textbooks, institutional pages). Prefer stable domains over raw YouTube IDs. Never invent URLs — if unsure a URL is real, omit the resource.
- End the final concept's last card with a short pointer to what topics build on this one.
- Where a concept connects to Bayesian inference, calculus, or neural networks concepts, say so explicitly — cross-topic links aid transfer.
"""


def call_claude(prompt):
    body = json.dumps({"model": MODEL, "max_tokens": 30000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json",
                 "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")


def main():
    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("Set ANTHROPIC_API_KEY first.")
    roadmap = json.load(open(os.path.join(BASE, "topics", "_roadmap.json")))
    if sys.argv[1] == "--custom":
        title, slug = sys.argv[2], sys.argv[sys.argv.index("--slug") + 1]
        spec = {"title": title, "prereqs": [], "concepts": 8}
    else:
        slug = sys.argv[1]
        spec = roadmap.get(slug) or sys.exit(f"'{slug}' not in roadmap. Known: {', '.join(roadmap)}")
        title = spec["title"]
    prompt = PROMPT.format(title=title, slug=slug,
                           prereqs=json.dumps(spec.get("prereqs", [])),
                           n=spec.get("concepts", 8))
    print(f"Drafting '{title}' ({spec.get('concepts', 8)} concepts) with {MODEL} …")
    text = call_claude(prompt).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    pack = json.loads(text)  # hard fail here if the model returned non-JSON
    out = os.path.join(BASE, "topics", f"_draft-{slug}.json")
    json.dump(pack, open(out, "w"), indent=1, ensure_ascii=False)
    print(f"Draft written: {out}")
    print(f"Next: python3 validate_pack.py {out} --links   → vet by hand → rename to go live.")


if __name__ == "__main__":
    main()
