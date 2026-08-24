"""
EVALUATION & MONITORING — metrics the mentor review asked for.

Non-negotiable part of the L2 review. Produces a per-query evaluation:
    - groundedness / faithfulness  (deterministic: facts used == facts fetched)
    - similarity score              (cosine similarity, itinerary vs research)
    - response accuracy             (deterministic numeric/date checks)
    - retrieval precision / allowlist_utilization / precision@k (IR-style,
      attractions — see retrieval_metrics() docstring for why there's no
      "recall" here)
    - LLM-as-a-judge relevance & accuracy score (optional cloud call)

All metrics are computed AFTER a plan is produced and logged to
cache/evaluation_log.jsonl so the system can be monitored over time.
"""

import json
import math
import os
import re
import time
from typing import Any

EVAL_LOG_FILE: str = os.path.join("cache", "evaluation_log.jsonl")


# ---------------------------------------------------------------------------
# Token / vector helpers (deterministic, no embeddings required)
# ---------------------------------------------------------------------------

_STOP = {"the", "a", "an", "and", "or", "for", "to", "in", "on", "of", "with",
         "from", "at", "by", "as", "is", "are", "this", "that", "it", "be"}


def _tokens(text: str) -> list[str]:
    text = text.lower()
    text = text.replace("\u2011", "-")
    words = re.findall(r"[a-z0-9]+", text)
    return [w for w in words if w not in _STOP]


def _counts(tokens: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tokens:
        out[t] = out.get(t, 0) + 1
    return out


def cosine_similarity(a: str, b: str) -> float:
    """Cosine similarity of token-frequency vectors (0..1). 1.0 = identical
    vocabulary, 0.0 = no shared words."""
    ca, cb = _counts(_tokens(a)), _counts(_tokens(b))
    if not ca or not cb:
        return 0.0
    dot = sum(v * cb.get(k, 0) for k, v in ca.items())
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return round(dot / (na * nb), 4)


# ---------------------------------------------------------------------------
# Groundedness / faithfulness (deterministic)
# ---------------------------------------------------------------------------

_TEMP_RE = re.compile(r"-?\d+(?:\.\d+)?\s*-\s*-?\d+(?:\.\d+)?\s*°C")
_SINGLE_TEMP_RE = re.compile(r"-?\d+(?:\.\d+)?°C")
_RATE_RE = re.compile(r"1 ([A-Z]{3}) = ([\d.]+) ([A-Z]{3})")
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _itinerary_facts(itinerary: str) -> dict[str, list[str]]:
    """Fact claims in the itinerary that must trace back to fetched data."""
    return {
        "temps": _TEMP_RE.findall(itinerary) + _SINGLE_TEMP_RE.findall(itinerary),
        "rates": [m.group(0) for m in _RATE_RE.finditer(itinerary)],
        "dates": _DATE_RE.findall(itinerary),
    }


def _temp_in_notes(value: str, research_notes: str) -> bool:
    """True if a temperature value appears in the research notes, either
    alone or as part of a fetched range (within 0.6 tolerance)."""
    try:
        v = float(value)
    except ValueError:
        return False
    # numeric tolerance against every fetched temp (range endpoints + solo)
    fetched_nums = [float(n) for n in
                    re.findall(r"-?\d+(?:\.\d+)?", research_notes)]
    if any(abs(v - n) <= 0.6 for n in fetched_nums):
        return True
    # exact standalone match as fallback (e.g. "-2°C" written verbatim)
    return bool(re.search(rf"(?<![\d-]){re.escape(value)}(?![\d.])", research_notes))


def groundedness(itinerary: str, research_notes: str) -> float:
    """Fraction of itinerary fact claims that appear in the fetched research.
    1.0 = every number in the itinerary was actually fetched (no invention)."""
    facts = _itinerary_facts(itinerary)
    total = sum(len(v) for v in facts.values())
    if total == 0:
        return 1.0  # no claims to check — nothing can be ungrounded
    grounded = 0
    for key, values in facts.items():
        for v in values:
            if key == "temps":
                nums = re.findall(r"-?\d+(?:\.\d+)?", v)
                if any(_temp_in_notes(n, research_notes) for n in nums):
                    grounded += 1
            else:
                if v in research_notes:
                    grounded += 1
    return round(grounded / total, 4)


def faithfulness(itinerary: str, research_notes: str) -> float:
    """Faithfulness is groundedness: does the answer stick to the retrieved
    evidence? Computed identically for naming consistency with the review."""
    return groundedness(itinerary, research_notes)


# ---------------------------------------------------------------------------
# Response accuracy (deterministic: dates + historical labeling)
# ---------------------------------------------------------------------------

def response_accuracy(itinerary: str, research_notes: str) -> dict[str, Any]:
    """Check the itinerary's factual integrity: any date in the itinerary
    must match a fetched date, and historical weather must not be called a
    forecast. Returns per-check pass/fail plus an aggregate score."""
    checks: list[dict[str, Any]] = []

    dates = _DATE_RE.findall(itinerary)
    fetched_dates = set(_DATE_RE.findall(research_notes))
    stray = [d for d in dates if d not in fetched_dates]
    checks.append({
        "name": "dates-match-fetched",
        "pass": not stray,
        "detail": f"itinerary dates {dates} vs fetched {sorted(fetched_dates)}",
    })

    hist_fetched = "HISTORICAL weather for" in research_notes
    draft_lower = itinerary.lower()
    miscalled = hist_fetched and "forecast" in draft_lower \
        and "no live forecast" not in draft_lower
    checks.append({
        "name": "historical-not-sold-as-forecast",
        "pass": not miscalled,
        "detail": "historical weather must be labeled historical",
    })

    score = round(sum(1 for c in checks if c["pass"]) / len(checks), 4)
    return {"score": score, "checks": checks}


# ---------------------------------------------------------------------------
# Retrieval metrics (IR-style, over the attraction allow-list)
# ---------------------------------------------------------------------------

def _extract_retrieved(research_notes: str) -> list[str]:
    """Attraction names the retrieval step actually returned (the allow-list).
    Only bullet lines count — the guard block ends with an instruction line
    that must NOT be treated as a place name."""
    m = re.search(r'\*\*\* ALLOW-LIST.*?\*\*\*\n(.+?)(?=\n\*\*\*|\Z)',
                  research_notes, re.DOTALL)
    lines = m.group(1).splitlines() if m else []
    names = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        name = stripped.lstrip("- ").split(" (")[0].strip()
        if name:
            names.append(name)
    return names


def _extract_used(itinerary: str, retrieved: list[str]) -> list[str]:
    """Attractions from the allow-list that actually appear in the itinerary
    (fuzzy substring match, case-insensitive)."""
    lower = itinerary.lower()
    used = []
    for name in retrieved:
        # match the full name OR its significant tokens (e.g. writer dropped
        # a trailing ', Paris')
        if name.lower() in lower:
            used.append(name)
        else:
            first = name.split(",")[0].strip().lower()
            if first and len(first) >= 4 and first in lower:
                used.append(name)
    return used


def retrieval_metrics(itinerary: str, research_notes: str, k: int = 5) -> dict[str, Any]:
    """Precision / allowlist_utilization / precision@k for the attraction
    retrieval step.
       retrieved = the allow-listed places fetched from Wikipedia geosearch
       used      = allow-listed places that actually appear in the itinerary

    HONESTY NOTE (why there's no "recall" key here):
    An earlier version of this function set `relevant = used` and then
    computed `recall = true_positives / len(relevant)`. Because `relevant`
    was defined to BE `used`, true_positives always equaled len(relevant) by
    construction — that "recall" was mathematically guaranteed to read 1.0
    whenever anything was used at all. It looked like a textbook IR recall
    score but wasn't measuring anything, because this project has no
    human-labeled ground-truth set of "every attraction that should have
    been retrieved" for a query — without that, true recall
    (found-and-relevant / all-that-are-actually-relevant) cannot be computed
    honestly.

    What this function CAN compute directly from real pipeline outputs:
      - precision: of the attractions retrieval fetched (the allow-list),
        what fraction did the writer actually use in the final itinerary?
        Low precision means the retrieval step is fetching places that get
        ignored.
      - allowlist_utilization: an honestly-named replacement for the old
        fake "recall". It answers "how much of the fetched allow-list ended
        up leveraged in the final answer?" Given the current pipeline (no
        ground-truth relevant-set to compare against), this is computed the
        same way as precision — the point of introducing this metric under
        its own name is to stop implying a benchmark-style recall that was
        never actually being measured, not to add a second independent
        number.
    """
    retrieved = _extract_retrieved(research_notes)
    if not retrieved:
        return {
            "retrieved_count": 0, "relevant_count": 0, "precision": 0.0,
            "allowlist_utilization": 0.0, f"precision@{k}": 0.0,
            "note": "no allow-list found",
        }

    used = _extract_used(itinerary, retrieved)
    relevant = used  # "relevant" == what the final answer actually used
    relevant_set = {u.lower() for u in relevant}
    tp = len(relevant_set)
    precision = round(tp / len(retrieved), 4) if retrieved else 0.0
    # See HONESTY NOTE above: no ground-truth relevant-set exists, so this
    # is deliberately the same computation as precision, just named for what
    # it actually represents instead of masquerading as IR recall.
    allowlist_utilization = precision
    top_k = retrieved[:k]
    pk_hits = sum(1 for name in top_k if name.lower() in relevant_set)
    pk = round(pk_hits / len(top_k), 4) if top_k else 0.0

    return {
        "retrieved_count": len(retrieved),
        "relevant_count": len(relevant_set),
        "precision": precision,
        "allowlist_utilization": allowlist_utilization,
        f"precision@{k}": pk,
    }


# ---------------------------------------------------------------------------
# LLM-as-a-judge (optional — needs an LLM call, guarded by env flag)
# ---------------------------------------------------------------------------

LLM_JUDGE_PROMPT: str = """You are an impartial evaluation judge for a travel
itinerary produced by an AI agent.

User request: {query}

AI-generated itinerary:
---START---
{itinerary}
---END---

Score the itinerary on THREE dimensions, each 0-10 (10 = perfect):
1. relevance — how well the itinerary addresses the EXPLICIT constraints in
   the user's request: destination, trip duration, month/dates, number and
   type of travelers (e.g. "4 friends", "a couple", "family with kids"),
   budget and currency (if the user gave one), and any other explicit
   requirement they stated. A relevant itinerary should visibly reflect
   each constraint the user actually gave — do not penalize it for
   constraints the user never mentioned.
2. accuracy — are the facts internally consistent and grounded in the
   research? (do NOT penalize missing info, only contradictions/errors).
3. completeness — does it cover overview, day-by-day plan, budget summary,
   and packing list?

Respond with EXACTLY this JSON (no markdown, no extra text):
{{"relevance": <0-10>, "accuracy": <0-10>, "completeness": <0-10>,
 "overall": <0-10>, "justification": "<1-2 sentence reason>"}}"""


def _parse_judge_json(text: str) -> dict[str, Any]:
    """Extract the JSON object from a judge response, tolerating markdown
    fences or surrounding prose."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def llm_as_judge(query: str, itinerary: str) -> dict[str, Any]:
    """LLM-as-a-judge: relevance/accuracy/completeness scores (0-10) for the
    generated itinerary. Uses the active provider (Groq in fast mode, Ollama
    locally). Never raises — on failure returns an error marker so the rest
    of the evaluation still completes."""
    import llm
    try:
        prompt = LLM_JUDGE_PROMPT.format(
            query=query,
            itinerary=itinerary[:4000] if itinerary else "",
        )
        resp = llm.chat(
            messages=[
                {"role": "system", "content": "You are a strict, objective evaluator."},
                {"role": "user", "content": prompt},
            ],
            model="qwen2.5:7b-instruct",
        )
        parsed = _parse_judge_json(resp["message"]["content"])
        return parsed or {"error": "judge returned unparsable JSON"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def evaluate(query: str, itinerary: str, research_notes: str,
             with_llm_judge: bool = False) -> dict[str, Any]:
    """Compute all metrics for one produced plan and log them for monitoring."""
    if not itinerary:
        return {"error": "no itinerary to evaluate"}

    metrics: dict[str, Any] = {
        "timestamp": time.time(),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "query": query,
        "similarity_score": cosine_similarity(research_notes or "", itinerary),
        "groundedness": groundedness(itinerary, research_notes or ""),
        "faithfulness": faithfulness(itinerary, research_notes or ""),
        "response_accuracy": response_accuracy(itinerary, research_notes or ""),
        "retrieval": retrieval_metrics(itinerary, research_notes or ""),
    }
    if with_llm_judge:
        metrics["llm_judge"] = llm_as_judge(query, itinerary)

    _append_log(metrics)
    return metrics


def _append_log(metrics: dict[str, Any]) -> None:
    """Append one evaluation record to the JSONL monitoring log."""
    try:
        os.makedirs("cache", exist_ok=True)
        with open(EVAL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(metrics, ensure_ascii=False) + "\n")
    except OSError:
        pass  # monitoring is best-effort


def evaluate_from_entry(query: str, entry: dict) -> dict[str, Any]:
    """Convenience: run evaluation from a plan entry dict (as stored in the
    query cache / session history)."""
    tool_log = entry.get("tool_log") or []
    research_notes = "\n".join(t["result"] for t in tool_log)
    return evaluate(query, entry.get("itinerary", ""), research_notes)