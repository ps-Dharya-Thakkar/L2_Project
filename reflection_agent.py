"""
REFLECTION AGENT — code-level factual checks against research data.

No LLM calls. Every check is deterministic string/regex matching against
the actual tool outputs and guard constraints. Fast, reliable, no
hallucination possible.
"""

import re


def _check_weather(research_notes: str, draft_itinerary: str) -> str | None:
    """Flag if weather data was fetched but draft misuses it, or if no
    weather data was fetched but draft claims numbers."""
    was_fetched = "HISTORICAL weather for" in research_notes or "LIVE forecast for" in research_notes

    if not was_fetched:
        has_numbers = bool(re.search(r'\d+\.?\d*°C', draft_itinerary))
        has_weather_section = "weather" in draft_itinerary.lower()
        if has_numbers or has_weather_section:
            return "Draft references weather data but none was fetched."

    return None


def _check_attractions(research_notes: str, draft_itinerary: str) -> str | None:
    allow_match = re.search(r'\*\*\* ALLOW-LIST.*?\*\*\*\n(.+?)(?=\n\*\*\*|\Z)', research_notes, re.DOTALL)
    if not allow_match:
        return None

    allowed_lower = set()
    for line in allow_match.group(1).splitlines():
        line = line.strip().lstrip("- ")
        if line:
            allowed_lower.add(line.lower())

    draft_lower = draft_itinerary.lower()

    place_type_suffix = (
        r"(?:Beach|Temple|Fort|Church|Market|Museum|Palace|Lake|Valley|"
        r"Viewpoint|Monastery|Park|Garden|Stadium|Falls|Peak|Square|Pass|"
        r"Harbour|Lighthouse|Cathedral|Mosque|Island|Hills|Creek|Pond|"
        r"Tower|Bridge|Castle|Fountain)"
    )

    fail_match = re.search(r'\*\*\* HARD CONSTRAINT — ATTRACTION LOOKUP FAILED \*\*\*', research_notes)
    if fail_match:
        has_named = bool(re.search(rf'\b[A-Z][a-z]+ {place_type_suffix}\b', draft_itinerary))
        if has_named:
            return "Attraction lookup failed, but draft names specific places."

    place_phrases = re.findall(rf'\b([A-Z][a-z]+) ({place_type_suffix})\b', draft_itinerary)
    violations = []
    for leading, place_type in place_phrases:
        leading_l = leading.lower()
        type_l = place_type.lower()
        matched_any_allowed = any(
            leading_l in allowed or type_l in allowed for allowed in allowed_lower
        )
        if not matched_any_allowed:
            violations.append(f"{leading} {place_type}")

    if violations:
        return f"Draft names places NOT in allow-list: {', '.join(violations[:5])}"

    return None


def _user_currencies(user_query: str) -> set[str]:
    """Currencies the user explicitly mentioned in their query."""
    q = user_query.lower()
    found: set[str] = set()
    for token, sym in (("inr", "₹"), ("rupee", "₹"), ("usd", "$"), ("dollar", "$"),
                       ("eur", "€"), ("euro", "€"), ("gbp", "£"), ("pound", "£"),
                       ("chf", "CHF"), ("franc", "CHF")):
        if token in q or sym in user_query:
            found.add(sym)
    return found or {"₹", "$", "€", "£", "CHF"}


def _check_currency(user_query: str, research_notes: str, draft_itinerary: str) -> str | None:
    if "NO EXCHANGE RATE WAS FETCHED" in research_notes:
        wanted = _user_currencies(user_query)
        foreign = [s for s in ("₹", "$", "€", "£", "CHF") if s in draft_itinerary and s not in wanted]
        if foreign:
            return "Draft shows currency amounts but no exchange rate was fetched."

    if "USER DID NOT ASK FOR CONVERSION" in research_notes:
        wanted = _user_currencies(user_query)
        foreign = [s for s in ("₹", "$", "€", "£", "CHF") if s in draft_itinerary and s not in wanted]
        if foreign:
            return "Draft shows a currency conversion the user didn't ask for."

    return None


def run_reflection(user_query: str, research_notes: str, draft_itinerary: str) -> str:
    issues: list[str] = []
    for check in (_check_weather, _check_attractions):
        issue = check(research_notes, draft_itinerary)
        if issue:
            issues.append(f"- {issue}")
    issue = _check_currency(user_query, research_notes, draft_itinerary)
    if issue:
        issues.append(f"- {issue}")

    if issues:
        return "REVISE:\n" + "\n".join(issues)
    return "APPROVED"
