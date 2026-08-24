"""
REFLECTION AGENT — code-level factual checks against research data.

No LLM calls. Every check is deterministic string/regex matching against
the actual tool outputs and guard constraints. Fast, reliable, no
hallucination possible.

Checks performed:
  1. Weather — if data was fetched, the draft must cite the EXACT fetched
     temperature range (within a small tolerance) and the EXACT DATE= for
     historical data, and must not label live forecasts as historical data
     (or vice versa). If nothing was fetched, the draft must not invent
     numbers.
  2. Attractions — place names in the draft must resolve against the
     allow-list via normalized / fuzzy matching (multi-word names,
     punctuation, case). Unknown names are flagged.
  3. Currency — amounts in a currency the user didn't ask about are flagged
     when no rate was fetched or no conversion was requested.
"""

import re
import unicodedata


# ---------------------------------------------------------------------------
# Normalization helpers used by several checks
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Case-fold + strip punctuation, for robust name matching. Accented
    letters are transliterated (é->e) so 'Saint Étienne' == 'saint etienne'.
    Keeps letters, digits and spaces."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("\u2011", "-")
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _weather_facts(research_notes: str) -> list[dict]:
    """Parse every weather fact block in research notes into a dict:
       {kind: 'live'|'historical', date: str|None, tmin: float|None,
        tmax: float|None, raw: str}
    This is the machine-readable ground truth the draft must match."""
    facts: list[dict] = []
    for m in re.finditer(
        r"(LIVE forecast for|HISTORICAL weather for) "
        r"([^,]+?)(?:, EXACT DATE=(\d{4}-\d{2}-\d{2}))?"
        r":\s*(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)°C",
        research_notes,
    ):
        facts.append({
            "kind": "live" if m.group(1).startswith("LIVE") else "historical",
            "date": m.group(3),
            "tmin": float(m.group(4)),
            "tmax": float(m.group(5)),
        })
    return facts


_TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*°C")


def _draft_temps(draft_itinerary: str) -> list[tuple[float, float]]:
    """All 'X-Y°C' ranges the draft states."""
    return [(float(a), float(b)) for a, b in _TEMP_RE.findall(draft_itinerary)]


def _check_weather(research_notes: str, draft_itinerary: str) -> str | None:
    facts = _weather_facts(research_notes)
    draft_temps = _draft_temps(draft_itinerary)

    if facts:
        # 1. Every draft temperature range must match a fetched range within
        #    a small tolerance (±0.6°C for formatting like 24 vs 24.3).
        expected = {(f["tmin"], f["tmax"]) for f in facts}
        for lo, hi in draft_temps:
            if not any(
                abs(lo - elo) <= 0.6 and abs(hi - ehi) <= 0.6
                for elo, ehi in expected
            ):
                return (f"Weather values not grounded: draft states {lo:.1f}-"
                        f"{hi:.1f}°C but fetched data was "
                        f"{min(e[0] for e in expected):.1f}-"
                        f"{max(e[1] for e in expected):.1f}°C.")

        # 2. Live/historical labeling must match the fetched data.
        kinds = {f["kind"] for f in facts}
        draft_lower = draft_itinerary.lower()
        if "live" in kinds and "historical data" in draft_lower:
            return ("Draft labels a LIVE forecast as 'historical data'.")
        if kinds == {"historical"}:
            # historical data must never be sold as a forecast
            if "forecast" in draft_lower and "no live forecast" not in draft_lower:
                return ("Draft labels HISTORICAL weather as a forecast.")

        # 3. Historical data must cite the EXACT DATE= when a year appears:
        #    any YYYY-MM-DD or 4-digit year in the draft weather section must
        #    match the fetched EXACT DATE's year. (A draft that writes "this
        #    date last year" without a number is fine — only wrong numbers
        #    are flagged.)
        for f in facts:
            if f["kind"] == "historical" and f["date"]:
                if f["date"] in draft_itinerary:
                    continue  # full date copied verbatim — perfect
                years = re.findall(r"\b(20\d{2})\b", draft_itinerary)
                wrong = [y for y in years if y != f["date"][:4]]
                if wrong:
                    return (f"Historical weather year mismatch: draft says "
                            f"{wrong[0]}, fetched EXACT DATE is {f['date']}.")

        # 4. A draft with NO weather section at all when data WAS fetched is
        #    also a defect (drops real data) — but only when the itinerary
        #    has a weather field header. (The template always includes one.)
        if "weather" not in draft_itinerary.lower():
            return "Fetched weather data but the draft never mentions it."

        return None

    # No weather was fetched -> hard guard text is in research notes. The
    # draft must not claim specific numbers unless it honestly writes the
    # 'no weather data gathered' phrase.
    has_numbers = bool(re.search(r'\d+\.?\d*°C', draft_itinerary))
    has_weather_section = "weather" in draft_itinerary.lower() \
        and "no weather data gathered" not in draft_itinerary.lower()
    if has_numbers or has_weather_section:
        return "Draft references weather data but none was fetched."

    return None


# ---------------------------------------------------------------------------
# Attractions — robust fuzzy allow-list matching
# ---------------------------------------------------------------------------

# Multi-strategy landmark detection. A place can be caught by ANY of:
#   (1) a place-type token anywhere in the phrase  ("Baga Beach", "Marine
#       Drive", "Times Square", "Hawa Mahal")
#   (2) a place-type suffix on the final word     ("Charminar", "Mehrangarh",
#       "Qutub Minar" as a single hyphenated token)
#   (3) a travel-context word right before it     ("Visit Charminar",
#       "Jantar Mantar", "wander through Old Town")
#   (4) being wrapped in bold/emphasis markers    ("**Swaroop Sagar Lake**")
# This avoids the old keyword-only blind spot where landmark names with no
# 'Beach/Temple/Fort...' word (e.g. Charminar, Jantar Mantar) slipped through.
_PLACE_TYPE_WORDS = {
    "beach", "temple", "fort", "church", "market", "museum", "palace",
    "lake", "valley", "viewpoint", "monastery", "park", "garden", "stadium",
    "falls", "peak", "square", "pass", "harbour", "harbor", "lighthouse",
    "cathedral", "mosque", "island", "hills", "creek", "pond", "tower",
    "bridge", "castle", "fountain", "point", "head", "gate", "drive",
    "road", "street", "lane", "way", "avenue", "boulevard", "trail",
    "house", "mansion", "mahal", "mandir", "masjid", "gurudwara", "dargah",
    "chowk", "bazaar", "ghat", "minar", "burj", "stupa", "vihara",
    "darwaza", "haveli", "bagh", "sagar", "sarovar", "talab", "kund",
    "maidan", "qila", "killa", "observatory", "aquarium", "zoo", "plaza",
    "promenade", "causeway", "dam", "reservoir", "cove", "bay", "cape",
    "cliff", "canyon", "desert", "waterfall", "spring", "geyser", "glacier",
    "mountain", "hill", "hilltop", "gorge", "cave", "grotto", "forest",
    "jungle", "reserve", "sanctuary", "gallery", "theatre", "theater",
    "opera", "arena", "colosseum", "basilica", "chapel", "shrine", "convent",
    "abbey", "memorial", "monument", "statue", "mausoleum", "tomb",
    "cemetery", "obelisk", "clock", "citadel", "keep", "arch", "column",
    "colonnade", "wall", "rampart", "ashram", "planetarium", "library",
}

# Suffixes that end real landmark names but are not standalone tokens, so the
# token-based check above would miss them ("Charminar" -> "minar").
_PLACE_TYPE_SUFFIXES = {
    "minar", "garh", "mahal", "sagar", "kund", "bagh", "chowk", "ghat",
    "bazaar", "mandir", "masjid", "nagar", "pura", "abad", "durg", "kot",
    "darwaza", "haveli", "sarovar", "talab", "maidan", "ganj", "mohalla",
    "gurudwara", "dargah", "stupa", "vihar", "jharna", "teertha",
}

# Verbs/prepositions that signal a named place is coming next. A capitalized
# phrase directly preceded by one of these is treated as a landmark mention
# even if it carries no place-type keyword.
_PLACE_CONTEXT_WORDS = {
    "visit", "explore", "see", "enjoy", "head", "walk", "drive", "ride",
    "tour", "check", "admire", "discover", "reach", "stop", "catch", "take",
    "spend", "watch", "marvel", "wander", "stroll", "hike", "trek", "sail",
    "cruise", "dine", "shop", "relax", "go", "stay", "board", "catch",
    "famous", "historic", "iconic", "known", "ancient", "popular",
    "beautiful", "landmark", "heritage", "gateway",
    "at", "to", "near", "in", "around", "towards", "by", "past", "from",
    "on", "into", "inside", "outside", "beside", "behind", "across",
    "through", "over", "under", "via", "opposite", "along",
}

# Structural/template words that only appear capitalized at line or heading
# starts and must never be treated as landmark names.
_TEMPLATE_WORDS = {
    "day", "morning", "afternoon", "evening", "night", "weather", "budget",
    "estimated", "total", "practical", "packing", "overview", "summary",
    "trip", "note", "notes", "arrival", "departure", "welcome",
    "introduction", "highlights", "itinerary", "plan", "sightseeing",
    "top", "best", "must", "things", "places", "attraction", "attractions",
    "sights", "landmarks", "guide", "tips", "do", "see", "date", "last", "year",
    "today", "tomorrow", "forecast", "historical", "live", "avg", "high",
    "low", "temp", "temperature", "conditions", "sky", "wind", "humidity",
    "precipitation", "sunny", "cloudy", "rain", "rainfall", "daytime",
    "nighttime", "hour", "hours", "time", "min", "max", "expect",
    "expects", "suggests", "during", "stay", "first", "last", "next",
    "month", "week", "season", "weather", "overview", "summary", "budget",
    "total", "day", "days", "night", "nights",
    "monsoon", "summer", "winter", "spring", "autumn",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday",
    "inr", "usd", "eur", "gbp", "aed", "chf", "jpy", "aud", "cad", "nzd",
    "sgd", "hkd", "cny", "rub", "zar", "ksh", "thb", "vnd", "rs", "yes",
    "no", "est", "approx", "approx", "mh", "state", "region", "city",
    "town", "village", "resort", "hotel", "hostel", "restaurant", "cafe",
    "café", "guesthouse", "homestay", "airport", "station", "terminal",
    "jetty", "ferry", "port", "docks", "transport", "railway", "roadway",
    "highway", "expressway", "metro", "rail", "bus", "taxi", "auto", "ferry",
}

# Leading tokens that are generic modifiers/verbs and should be stripped off
# the front of a candidate phrase before matching ("Visit X", "the Baga Beach").
_LEADER_WORDS = {
    "visit", "explore", "see", "enjoy", "head", "go", "take", "walk",
    "spend", "stop", "check", "catch", "watch", "reach", "ride", "stay",
    "shop", "dine", "relax", "try", "tour", "admire", "discover", "wander",
    "stroll", "hike", "trek", "sail", "cruise", "the", "a", "an", "old",
    "new", "famous", "historic", "iconic", "great", "little", "big",
    "upper", "lower", "central", "north", "south", "east", "west", "main",
    "royal", "grand", "majestic", "ancient", "beautiful", "scenic", "top",
    "best", "popular", "at", "to", "near", "in", "on", "by", "around",
    "towards", "into", "via", "through", "past", "from", "heritage",
    "landmark", "iconic", "must", "see", "do",
}

# Region qualifiers that make a destination phrase ('North Goa', 'Old Delhi')
# read as a region rather than an attraction.
_PLACE_QUALIFIERS = {
    "north", "south", "east", "west", "old", "new", "central", "upper",
    "lower", "greater", "city", "town", "downtown", "uptown", "main",
    "coastal", "hill", "mountain", "suburban", "urban", "rural", "island",
}

_CAP_SEQ = re.compile(
    r"(?<![A-Za-z0-9])"
    r"([A-Z\u00C0-\u017F][\w'’.-]*(?:"
    r"\s+(?:of|de|du|la|le|the|and|&|st|saint|san|da|di|del|van|von)\s+"
    r"[A-Z\u00C0-\u017F][\w'’.-]*"
    r"|\s+[A-Z\u00C0-\u017F][\w'’.-]*){0,5})"
)

_BOLD_PLACE = re.compile(
    r"\*\*([A-Z\u00C0-\u017F][\w'’.-]*(?:\s+[A-Z\u00C0-\u017F][\w'’.-]*){0,5})\*\*"
)


def _destination_words(research_notes: str) -> set[str]:
    """Extract the destination city/country/region the trip is about, so it is
    never mistaken for a hallucinated landmark. Sources: the geocode result
    ('Udaipur, India -> lat=...'), the 'Verified places near X' line and the
    weather-report lines."""
    words: set[str] = set()
    for m in re.finditer(
            r'\b([A-Za-z][\w .\'’-]+),\s*([A-Za-z][\w .\'’-]+)\s*->\s*lat=',
            research_notes):
        words.update(_norm(m.group(1)).split())
        words.update(_norm(m.group(2)).split())
    for m in re.finditer(
            r'Verified places near ([A-Za-z][\w .\'’-]+)', research_notes):
        words.update(_norm(m.group(1)).split())
    for m in re.finditer(
            r'(?:LIVE forecast for|HISTORICAL weather for|Weather report for)\s+'
            r'([A-Za-z][\w .\'’-]+)', research_notes):
        words.update(_norm(m.group(1)).split())
    return {w for w in words if w}


def _has_place_marker(phrase: str) -> bool:
    """True if the phrase contains a place-type token or ends with a
    place-type suffix ('Charminar', 'Hawa Mahal')."""
    tokens = _norm(phrase).split()
    if not tokens:
        return False
    if any(t in _PLACE_TYPE_WORDS for t in tokens):
        return True
    last = tokens[-1]
    return any(len(s) >= 4 and last.endswith(s) for s in _PLACE_TYPE_SUFFIXES)


def _trim_leader(phrase: str) -> str:
    tokens = phrase.split()
    while tokens and tokens[0].lower() in _LEADER_WORDS:
        tokens = tokens[1:]
    return " ".join(tokens).strip(" ,;:*_-.!?")


def _is_generic_type_only(phrase: str) -> bool:
    """A lone generic type word ('Fort', 'Beach') is a description, not a
    landmark name."""
    tokens = _norm(phrase).split()
    return len(tokens) == 1 and tokens[0] in _PLACE_TYPE_WORDS


def _is_template(phrase: str) -> bool:
    """Skip phrases made of template/structure words ('Day 1', 'Weather',
    'Estimated budget')."""
    tokens = {t.strip(":-*.,;!?()[]") for t in phrase.lower().split()}
    return bool(tokens & _TEMPLATE_WORDS)


def _is_destination(phrase: str, dest_words: set[str]) -> bool:
    if not dest_words:
        return False
    tokens = _norm(phrase).split()
    if not tokens:
        return True
    if all(t in dest_words for t in tokens):
        return True
    if len(tokens) <= 2:
        non_dest = [t for t in tokens if t not in dest_words]
        if non_dest and all(t in _PLACE_QUALIFIERS for t in non_dest):
            return True
    return False


def _extract_allow_list(research_notes: str) -> list[str]:
    """Return the normalized allow-listed place names, preferring the
    ALLOW-LIST block, falling back to the 'Verified places near' list."""
    allow_match = re.search(
        r'\*\*\* ALLOW-LIST.*?\*\*\*\n(.+?)(?=\n\*\*\*|\Z)',
        research_notes, re.DOTALL,
    )
    lines: list[str] = []
    if allow_match:
        lines = allow_match.group(1).splitlines()
    else:
        v_match = re.search(
            r'Verified places near .*?\(sorted by distance\):\n(.+?)(?=\n\n|\Z)',
            research_notes, re.DOTALL,
        )
        if v_match:
            lines = v_match.group(1).splitlines()
    allowed = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue  # skip the guard's trailing instruction line
        name = stripped.lstrip("- ").split(" (")[0].strip()
        if name:
            allowed.append(_norm(name))
    return allowed


def _draft_place_phrases(draft_itinerary: str) -> list[str]:
    """Find candidate landmark mentions in the draft using four strategies
    (context-precursor, type keyword, type suffix, bold emphasis). Phrases
    that are pure template/structure words or lone generic type words are
    dropped before returning."""
    candidates: set[str] = set()
    # Strategy 1+2+3: capitalized phrase that is either directly preceded by a
    # travel-context word or carries a place-type marker.
    for m in _CAP_SEQ.finditer(draft_itinerary):
        phrase = m.group(1).strip()
        prev = draft_itinerary[max(0, m.start() - 40):m.start()]
        prev_words = re.findall(r"[A-Za-z]+", prev)
        prev_word = prev_words[-1].lower() if prev_words else ""
        # A capitalized sequence can swallow its own leading verb
        # ('Visit Jantar Mantar'), so the first token counts as context too.
        # Modifier leaders (The/Famous/New) are deliberately NOT included —
        # 'Famous for its beaches' would otherwise false-positive.
        first_word = phrase.split()[0].lower() if phrase.split() else ""
        context_ok = (
            prev_word in _PLACE_CONTEXT_WORDS
            or first_word in _PLACE_CONTEXT_WORDS
        )
        if context_ok or _has_place_marker(phrase):
            candidates.add(phrase)
    # Strategy 4: bold/emphasis-wrapped names ("**Swaroop Sagar Lake**").
    for m in _BOLD_PLACE.finditer(draft_itinerary):
        candidates.add(m.group(1).strip())

    cleaned: list[str] = []
    for p in candidates:
        p = _trim_leader(p)
        if not p:
            continue
        if _is_generic_type_only(p):
            continue
        if _is_template(p):
            continue
        cleaned.append(p)
    return cleaned


def _matches_allow(name: str, allowed_norm: list[str]) -> bool:
    """Fuzzy containment match: the draft phrase is accepted if it is a
    substring of, contains, or shares a long token sequence with an allowed
    name. Handles the writer dropping a trailing ', Paris' or adding an
    article ('the Baga Beach')."""
    n = _norm(name)
    if not n:
        return True
    for a in allowed_norm:
        if n == a or n in a or a in n:
            return True
        # token-level: every significant token in the phrase appears in the
        # same allowed entry -> e.g. 'Saint-Jean-le-Rond' vs
        # 'Church of Saint-Jean-le-Rond, Paris'
        n_tokens = {t for t in re.split(r"[-\s]", n) if len(t) >= 3}
        a_tokens = {t for t in re.split(r"[-\s]", a) if len(t) >= 3}
        if n_tokens and a_tokens and n_tokens.issubset(a_tokens):
            return True
    return False


def _apply_validator(candidates: list[str], research_notes: str,
                     validator) -> tuple:
    """Layer 3 — prove each candidate is a REAL place near the destination by
    geocoding it (place_validation.PlaceValidator). Returns (verified,
    rejected) as lists of (name, verdict). Without a validator (offline /
    rule-only path) every candidate is 'rejected' so nothing silently passes."""
    if not candidates:
        return [], []
    if validator is None:
        return [], [(n, {"found": False, "reason": "no validator"})
                    for n in candidates]
    try:
        from place_validation import parse_destination
    except Exception:
        return [], [(n, {"found": False, "reason": "no validator"})
                    for n in candidates]
    dest = parse_destination(research_notes)
    if dest is None:
        return [], [(n, {"found": False, "reason": "no destination coords"})
                    for n in candidates]
    return validator.validate_many(candidates, dest["city"],
                                   dest["lat"], dest["lon"], max_n=5)


def _check_attractions(research_notes: str, draft_itinerary: str,
                       validator=None) -> str | None:
    fail_match = re.search(
        r'\*\*\* HARD CONSTRAINT — ATTRACTION LOOKUP FAILED \*\*\*', research_notes)
    dest_words = _destination_words(research_notes)
    # The destination city/region/country is not a landmark and must never be
    # flagged ('fly into Goa', 'North Goa', 'Udaipur, India').
    candidates = [p for p in _draft_place_phrases(draft_itinerary)
                  if not _is_destination(p, dest_words)]

    if fail_match:
        # No allow-list exists — the draft must not name ANY landmark unless
        # Layer 3 can confirm it is a real place near the destination.
        if not candidates:
            return None
        verified, rejected = _apply_validator(candidates, research_notes, validator)
        if rejected:
            msg = (f"Attraction lookup failed; draft names places that could "
                   f"not be confirmed as real: "
                   f"{', '.join(list(dict.fromkeys([n for n, _ in rejected]))[:5])}")
            if verified:
                msg += (f" (verified via geocoding: "
                        f"{', '.join(n for n, _ in verified[:5])})")
            return msg
        return None

    allowed = _extract_allow_list(research_notes)
    if not allowed:
        # No allow-list AND no failure marker: treat like the guard's
        # 'NO ATTRACTIONS WERE FETCHED' constraint — generic only.
        if not candidates:
            return None
        verified, rejected = _apply_validator(candidates, research_notes, validator)
        if rejected:
            msg = (f"Draft names specific places that could not be verified "
                   f"as real: "
                   f"{', '.join(list(dict.fromkeys([n for n, _ in rejected]))[:5])}")
            if verified:
                msg += (f" (verified via geocoding: "
                        f"{', '.join(n for n, _ in verified[:5])})")
            return msg
        return None

    violations = [p for p in candidates if not _matches_allow(p, allowed)]
    if not violations:
        return None
    verified, rejected = _apply_validator(violations, research_notes, validator)
    if rejected:
        msg = (f"Draft names places NOT in allow-list and not confirmed as "
               f"real: "
               f"{', '.join(list(dict.fromkeys([n for n, _ in rejected]))[:5])}")
        if verified:
            msg += (f" (verified via geocoding: "
                    f"{', '.join(n for n, _ in verified[:5])})")
        return msg
    return None


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

def _user_currencies(user_query: str) -> set[str]:
    """Currencies the user explicitly mentioned in their query."""
    q = user_query.lower()
    found: set[str] = set()
    for token, sym in (("inr", "₹"), ("rupee", "₹"), ("usd", "$"), ("dollar", "$"),
                       ("eur", "€"), ("euro", "€"), ("gbp", "£"), ("pound", "£"),
                       ("chf", "CHF"), ("franc", "CHF"), ("aed", "AED"),
                       ("dirham", "AED")):
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


def run_reflection(user_query: str, research_notes: str, draft_itinerary: str,
                   validator=None) -> str:
    issues: list[str] = []
    issue = _check_weather(research_notes, draft_itinerary)
    if issue:
        issues.append(f"- {issue}")
    issue = _check_attractions(research_notes, draft_itinerary, validator=validator)
    if issue:
        issues.append(f"- {issue}")
    issue = _check_currency(user_query, research_notes, draft_itinerary)
    if issue:
        issues.append(f"- {issue}")

    if issues:
        return "REVISE:\n" + "\n".join(issues)
    return "APPROVED"