"""
MCP SERVER — the "tool box" the agent is allowed to reach into.

This process is started automatically by the orchestrator (via stdio), you
don't run it by hand. It exposes 4 tools, each backed by a free web service
(no API key needed for any of them):

    geocode_city(city)                          -> lat/lon lookup       (Open-Meteo + Nominatim)
    get_weather(city, date)                      -> live forecast OR
                                                        real historical data (Open-Meteo)
    get_exchange_rate(base, target)               -> live FX rate         (Frankfurter)
    get_nearby_attractions(city, radius_km)       -> real, verified places (Wikipedia)

MCP = Model Context Protocol. It's a standard way to expose tools/data to an
LLM agent over a well-defined JSON-RPC interface, instead of hard-wiring
API calls into your agent code. Any MCP-compatible client (ours, or someone
else's) can talk to this server the same way.
"""

import datetime
import json
import math
import os
import time
from typing import Any
from mcp.server.fastmcp import FastMCP
import requests

mcp = FastMCP("travel-tools")

# ---------------------------------------------------------------------------
# Persistent cache — written to disk so results survive across runs. Every
# plan_trip() spawns a fresh MCP subprocess, so the old in-memory dicts were
# useless between queries. Now geocode/weather/FX/attractions for a city
# already looked up are instant (no network, no LLM tokens burned re-fetching).
# TTLs: geocode is static; weather/FX/attractions are fresher, so they expire.
# ---------------------------------------------------------------------------

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "travel_tools_cache.json")
CACHE_TTL_SECONDS = {
    "geocode": 60 * 60 * 24 * 30,      # coordinates barely change — 30 days
    "weather": 60 * 60 * 12,            # forecasts/historical refresh twice a day
    "fx": 60 * 60 * 24,                 # FX rates change — refresh daily
    "attractions": 60 * 60 * 24 * 7,    # verified places are stable — 7 days
}


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
    except OSError:
        pass  # cache is best-effort; a failed write shouldn't crash a trip


_cache_store = _load_cache()


def _cache_get(tool: str, key: tuple) -> str | None:
    """Return a cached result for tool+key if fresh, else None."""
    skey = json.dumps(_cache_key(key))
    entry = _cache_store.get(tool, {}).get(skey)
    if not entry:
        return None
    ttl = CACHE_TTL_SECONDS.get(tool, 0)
    if ttl and time.time() - entry.get("ts", 0) > ttl:
        return None
    return entry.get("value")


def _cache_set(tool: str, key: tuple, value: str) -> None:
    skey = json.dumps(_cache_key(key))
    _cache_store.setdefault(tool, {})[skey] = {
        "value": value,
        "ts": time.time(),
    }
    _save_cache(_cache_store)


_geocode_cache: dict[tuple[str, str], tuple] = {}
_weather_cache: dict[tuple[str, str, str], str] = {}
_fx_cache: dict[tuple[str, str], str] = {}
_attr_cache: dict[tuple[str, str, int], str] = {}


def _cache_key(parts: tuple) -> tuple:
    return tuple(str(p).strip().lower() for p in parts)


def _get_with_retry(url: str, params: dict, headers: dict = None, retries: int = 1, timeout: int = 10) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1)
    raise last_err


def _geocode_nominatim(city: str, region: str = ""):
    """Fallback geocoder using Nominatim (OpenStreetMap) — free, no API key,
    much better coverage for Indian cities and smaller towns."""
    query = f"{city}, {region}" if region else city
    try:
        r = _get_with_retry(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 3, "addressdetails": 1},
            headers={"User-Agent": "TravelPlannerAgent/1.0 (educational project)"},
        ).json()
        if not r:
            return None, f"'{query}' not found in Nominatim."
        result = r[0]
        display = result.get("display_name", "")
        parts = display.split(", ")
        country = parts[-1] if len(parts) > 1 else ""
        lat = float(result["lat"])
        lon = float(result["lon"])
        name = result.get("name", city)
        return (lat, lon, name, country), None
    except Exception as e:
        return None, f"Nominatim lookup failed: {e}"


def _region_matches(region_l: str, candidate: dict) -> bool:
    """Fuzzy region match: true if region and candidate's admin1/country
    contain each other (either direction). Handles "United States of
    America" vs Open-Meteo's "United States", and "India" vs "India".""" 
    admin1 = (candidate.get("admin1", "") or "").lower()
    country = (candidate.get("country", "") or "").lower()
    return (
        admin1 and (region_l in admin1 or admin1 in region_l)
        or country and (region_l in country or country in region_l)
    )


def _geocode(city: str, region: str = "") -> tuple[float | None, float | None, str, str, str | None] | None:
    city = city.strip()
    region = region.strip()
    key = _cache_key((city, region))

    cached = _cache_get("geocode", key)
    if cached is not None:
        return cached
    if key in _geocode_cache:
        return _geocode_cache[key]

    result = _geocode_uncached(city, region)
    if result is not None:
        _geocode_cache[key] = result
        _cache_set("geocode", key, result)
    return result


def _geocode_uncached(city: str, region: str = "") -> tuple[float | None, float | None, str, str, str | None] | None:

    # Primary: Open-Meteo
    try:
        r = _get_with_retry(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 5},
        ).json()
        results = r.get("results") or []
    except Exception:
        results = []

    if results:
        if len(results) == 1 or not region:
            chosen = results[0]
            warning: str | None = None
            if len(results) > 1:
                others = ", ".join(f"{x['name']}, {x.get('admin1', x.get('country',''))}"
                                    for x in results[1:3])
                warning = (f"NOTE: '{city}' is ambiguous ({len(results)} places share this "
                           f"name, e.g. also {others}). Assumed {chosen['name']}, "
                           f"{chosen.get('admin1', '')}, {chosen.get('country', '')}. "
                           f"If this is wrong, call again with a region, e.g. "
                           f"city='{city}', region='<state or country>'.")
        else:
            region_l = region.lower()
            match = next((x for x in results if _region_matches(region_l, x)), None)
            if not match:
                # Try Nominatim as fallback before giving up
                nom_result, nom_err = _geocode_nominatim(city, region)
                if nom_result:
                    lat, lon, name, country = nom_result
                    return (lat, lon, name, country,
                            f"NOTE: Open-Meteo didn't confirm '{city}' in '{region}' "
                            f"(candidates were mismatched). Resolved via Nominatim to "
                            f"'{name}', {country}.")
                candidates = ", ".join(
                    f"{x['name']}, {x.get('admin1', x.get('country', 'unknown'))}"
                    for x in results
                )
                return (None, None, city, region,
                        f"ERROR: '{city}' in region '{region}' matched no candidate. "
                        f"Candidates: {candidates}. "
                        f"Clarify the city or region and try again.")
            chosen = match
            warning = None

        return (chosen["latitude"], chosen["longitude"], chosen["name"],
                chosen.get("country", ""), warning)

    # Secondary: Nominatim fallback when Open-Meteo returned nothing
    nom_result, nom_err = _geocode_nominatim(city, region)
    if nom_result:
        lat, lon, name, country = nom_result
        return (lat, lon, name, country,
                f"NOTE: Resolved '{city}' to '{name}', {country} via Nominatim "
                f"(Open-Meteo had no results).")
    return None


@mcp.tool()
def geocode_city(city: str, region: str = "") -> str:
    try:
        loc = _geocode(city, region)
        if not loc:
            return f"No location found for '{city}'."
        lat, lon, name, country, warning = loc
        if lat is None:
            return f"ERROR: {warning}"
        out = f"{name}, {country} -> lat={lat}, lon={lon}"
        return f"{out}\n{warning}" if warning else out
    except Exception as e:
        return f"Error looking up city: {e}"


@mcp.tool()
def get_weather(city: str, date: str = "", region: str = "") -> str:
    key = _cache_key((city, date, region))
    cached = _cache_get("weather", key)
    if cached is not None:
        return cached
    if key in _weather_cache:
        return _weather_cache[key]
    result = _get_weather_uncached(city, date, region)
    _weather_cache[key] = result
    _cache_set("weather", key, result)
    return result


def _get_weather_uncached(city: str, date: str = "", region: str = "") -> str:
    try:
        loc = _geocode(city, region)
        if not loc:
            return f"Could not find location: {city}"
        lat, lon, name, _, warning = loc
        if lat is None:
            return f"ERROR: {warning}"

        today = datetime.date.today()
        target_date = None
        if date:
            try:
                target_date = datetime.date.fromisoformat(date)
            except ValueError:
                target_date = None

        days_out = (target_date - today).days if target_date else 0

        if target_date and 0 <= days_out <= 15:
            w = _get_with_retry(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "start_date": target_date.isoformat(),
                    "end_date": target_date.isoformat(),
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                },
            ).json()
            d = w["daily"]
            result = (f"LIVE forecast for {name} on {d['time'][0]}: "
                      f"{d['temperature_2m_min'][0]}-{d['temperature_2m_max'][0]}°C, "
                      f"rain chance {d['precipitation_probability_max'][0]}%")
            return f"{result}\n{warning}" if warning else result

        hist_date = (target_date or today.replace(day=min(today.day, 28)))
        hist_date = hist_date.replace(year=hist_date.year - 1)

        h = _get_with_retry(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": hist_date.isoformat(),
                "end_date": hist_date.isoformat(),
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "timezone": "auto",
            },
        ).json()
        d = h["daily"]
        exact_date = d["time"][0]
        result = (f"HISTORICAL weather for {name}, EXACT DATE={exact_date} "
                  f"(copy this date exactly if you mention it, do not guess a "
                  f"different year): {d['temperature_2m_min'][0]}-"
                  f"{d['temperature_2m_max'][0]}°C, {d['precipitation_sum'][0]}mm "
                  f"precipitation.")
        return f"{result}\n{warning}" if warning else result
    except Exception as e:
        return f"Error fetching weather: {e}"


@mcp.tool()
def get_exchange_rate(base_currency: str, target_currency: str) -> str:
    key = _cache_key((base_currency, target_currency))
    cached = _cache_get("fx", key)
    if cached is not None:
        return cached
    if key in _fx_cache:
        return _fx_cache[key]
    result = _get_exchange_rate_uncached(base_currency, target_currency)
    _fx_cache[key] = result
    _cache_set("fx", key, result)
    return result


def _get_exchange_rate_uncached(base_currency: str, target_currency: str) -> str:
    base = base_currency.upper()
    target = target_currency.upper()
    try:
        r = _get_with_retry(
            "https://api.frankfurter.app/latest",
            params={"from": base, "to": target},
        ).json()
        rate = r["rates"][target]
        return f"1 {base} = {rate} {target}"
    except Exception:
        pass
    try:
        r = _get_with_retry(
            "https://open.er-api.com/v6/latest/" + base,
            params={},
        ).json()
        if r.get("result") != "success":
            return f"Error fetching exchange rate: {r.get('error-type', 'unknown')}"
        rate = r["rates"][target]
        return f"1 {base} = {rate} {target}"
    except Exception as e:
        return f"Error fetching exchange rate: {e}"


_NON_ATTRACTION_MARKERS = (
    "railway station", "railway halt", "assembly constituency", "assembly",
    "taluka", "taluk", "village council", "gram panchayat", "metro station",
    "submission", "census", "proposed ", "under construction", "temple town",
    "central university", "institute of technology",
    "urban development authority", "development authority", "municipal corporation",
    "municipality", "district collectorate", "police station", "fire station",
    "bus station", "high court", "district court", "jubilee hall",
    "college", "university", "school", "hostel", "hotel",
)

_ATTRACTION_TYPE_WORDS = (
    "beach", "fort", "temple", "church", "mosque", "museum", "palace", "lake",
    "garden", "park", "island", "point", "falls", "monastery", "cathedral",
    "viewpoint", "harbour", "lighthouse", "castle", "tower", "bridge", "square",
)

# Suffixes that end landmark names with no standalone type word — 'Charminar'
# (minar), 'Mehrangarh' (garh), 'Hawa Mahal' (mahal), 'Qutb Shahi Tombs' (tomb).
# Without these they rank below 'Public Gardens' and the writer never picks
# the city's actual icons.
_ATTRACTION_TYPE_SUFFIXES = (
    "minar", "mahal", "garh", "fort", "tomb", "ghat", "chowk", "bazaar",
    "mandir", "masjid", "gurudwara", "dargah", "stupa", "durg", "killa",
    "qila", "haveli", "bagh", "sagar", "sarovar", "talab", "kund", "maidan",
)


def _has_landmark_suffix(title: str) -> bool:
    """True if a title ends with a landmark suffix (handling a trailing 's').
    Catches 'Charminar' (minar), 'Mehrangarh' (garh), 'Qutb Shahi Tombs'
    (tomb), 'Hawa Mahal' (mahal)."""
    t = title.lower()
    if t.endswith("s"):
        t = t[:-1]
    return any(len(s) >= 4 and t.endswith(s) for s in _ATTRACTION_TYPE_SUFFIXES)


def _is_landmark_title(title: str) -> bool:
    """True if a geosearch title looks like a landmark: contains a landmark
    type word ('Fort', 'Gardens') or ends with a landmark suffix
    ('Charminar' -> minar)."""
    t = title.lower()
    if any(w in t for w in _ATTRACTION_TYPE_WORDS):
        return True
    return _has_landmark_suffix(title)


def _filter_attractions(places: list[dict]) -> list[dict]:
    """Drop Wikipedia geosearch results that are clearly not tourist
    attractions (railway stations, assembly constituencies, villages,
    talukas) while keeping the rest. Real entries like 'Rachol Fort',
    'Menezes Braganza House', 'Nanda Lake' survive the filter."""
    kept = []
    for p in places:
        title = p["title"].lower().strip()
        if any(marker in title for marker in _NON_ATTRACTION_MARKERS):
            continue
        kept.append(p)

    # Sort: landmarks first (type word OR landmark suffix such as
    # 'minar'/'mahal'/'garh'), and within that group the iconic single-word
    # monuments (suffix names like 'Charminar', 'Hussain Sagar') ahead of
    # generic parks/gardens, then by distance. This lifts the destination's
    # icons to the top of the allow-list so the writer actually picks them.
    # `dist` may be None for a destination-aware-search candidate we could
    # not geocode/enrich — those sort after everything with a known distance
    # within their landmark tier, rather than crashing the comparison.
    def rank(p: dict) -> tuple:
        title = p["title"]
        dist = p.get("dist")
        dist_key = dist if dist is not None else float("inf")
        if not _is_landmark_title(title):
            return (1, 0, 0, dist_key)
        return (0, 0 if _has_landmark_suffix(title) else 1, 0, dist_key)

    kept.sort(key=rank)
    return kept


# ---------------------------------------------------------------------------
# Hybrid candidate generation for get_nearby_attractions.
#
# Wikipedia GeoSearch (Source A) is proximity/density based: in a dense city
# the closest 500 articles can be entirely neighbourhoods, roads, and small
# infrastructure, so a famous landmark slightly further from the exact city
# centroid — or just outside GeoSearch's hard 10km cap — never enters the
# candidate pool at all. No amount of re-ranking recovers a place that was
# never retrieved. Source B (destination-aware Wikipedia search) compensates
# by searching Wikipedia directly for "<city> tourist attractions/landmarks/
# forts/..." so those icons become candidates even when GeoSearch misses
# them, after which they go through the same filtering/ranking as everything
# else — nothing here is auto-trusted.
# ---------------------------------------------------------------------------

# Bounded, fixed set of destination-aware query templates — a constant
# number of API calls regardless of city size, not one call per candidate.
_ATTRACTION_QUERY_SUFFIXES = (
    "tourist attractions", "landmarks", "monuments", "historical places",
    "museums", "forts", "palaces", "temples",
)

_MAX_DESTINATION_SEARCH_QUERIES = 6
_DESTINATION_SEARCH_PER_QUERY_LIMIT = 8

# Category buckets used only for lightweight diversity — not attraction
# detection. Deliberately coarse and generic (no city names).
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fort_palace", ("fort", "palace", "qila", "killa", "durg", "garh", "haveli")),
    ("museum", ("museum",)),
    ("religious", ("temple", "mandir", "masjid", "mosque", "church",
                   "cathedral", "gurudwara", "dargah", "stupa", "monastery")),
    ("market", ("bazaar", "bazar", "market", "chowk", "haat")),
    ("water_scenic", ("lake", "sagar", "beach", "falls", "island", "sarovar",
                       "talab", "ghat", "viewpoint", "garden", "park")),
    ("landmark_tower", ("minar", "tower", "monument", "gate", "square", "bridge")),
)


def _wikipedia_search(query: str, limit: int = 8) -> list[dict]:
    """One Wikipedia full-text search call -> list of {'title': ...}. Best
    effort: any failure just yields no candidates from this query, it never
    raises (the caller treats Source B as optional)."""
    try:
        r = _get_with_retry(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "search",
                "srsearch": query, "srlimit": limit, "format": "json",
            },
            headers={
                "User-Agent": "TravelPlannerAgent/1.0 (student L2 project; educational use)"
            },
            retries=1,
        ).json()
        return [{"title": item["title"]}
                for item in r.get("query", {}).get("search", [])
                if item.get("title")]
    except Exception:
        return []


def _destination_aware_search(city_name: str) -> list[dict]:
    """Source B: search Wikipedia for '<city> tourist attractions', '<city>
    landmarks', '<city> forts', etc. Bounded to a fixed number of queries
    (_MAX_DESTINATION_SEARCH_QUERIES) so this scales with destinations, not
    with candidate count. Titles are returned as raw candidates only — they
    still go through _filter_attractions / ranking / diversity below, they
    are not trusted outright."""
    candidates: list[dict] = []
    seen: set[str] = set()
    for suffix in _ATTRACTION_QUERY_SUFFIXES[:_MAX_DESTINATION_SEARCH_QUERIES]:
        query = f"{city_name} {suffix}"
        for item in _wikipedia_search(query, limit=_DESTINATION_SEARCH_PER_QUERY_LIMIT):
            key = item["title"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"title": item["title"], "source": {"search"}})
    return candidates


def _merge_candidates(geosearch_places: list[dict],
                       search_candidates: list[dict]) -> list[dict]:
    """Merge Source A (GeoSearch, has 'dist') and Source B (destination-aware
    search, no 'dist' yet) candidates, deduplicating case-insensitively by
    normalized title. A title found by both sources keeps GeoSearch's
    distance and gains both source tags (used later as a ranking signal)."""
    merged: dict[str, dict] = {}
    for p in geosearch_places:
        key = p["title"].strip().lower()
        merged[key] = {"title": p["title"], "dist": p.get("dist"),
                        "source": {"geosearch"}}
    for c in search_candidates:
        key = c["title"].strip().lower()
        if key in merged:
            merged[key]["source"] |= c.get("source", {"search"})
        else:
            merged[key] = {"title": c["title"], "dist": None,
                            "source": set(c.get("source", {"search"}))}
    return list(merged.values())


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, sqrt, atan2
    r = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * r * atan2(sqrt(a), sqrt(1 - a))


def _enrich_with_coordinates(candidates: list[dict], dest_lat: float,
                              dest_lon: float, max_lookup: int = 60) -> None:
    """Batch-fetch coordinates (prop=coordinates, up to 50 titles per call)
    for candidates missing a distance — i.e. found only via destination-aware
    search — so they can be geographically sanity-checked and ranked
    alongside GeoSearch results. Bounded to `max_lookup` titles and never
    one API call per candidate. Mutates `candidates` in place; best effort,
    a failed batch just leaves those candidates with dist=None."""
    need = [c for c in candidates if c.get("dist") is None][:max_lookup]
    if not need:
        return
    titles = [c["title"] for c in need]
    coord_by_title: dict[str, tuple[float, float]] = {}
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        try:
            r = _get_with_retry(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query", "prop": "coordinates",
                    "titles": "|".join(batch), "format": "json",
                },
                headers={
                    "User-Agent": "TravelPlannerAgent/1.0 (student L2 project; educational use)"
                },
                retries=1,
            ).json()
            for page in r.get("query", {}).get("pages", {}).values():
                coords = page.get("coordinates")
                title = page.get("title", "")
                if coords and title:
                    coord_by_title[title] = (coords[0]["lat"], coords[0]["lon"])
        except Exception:
            pass  # this batch stays distance-less; ranking treats that neutrally
        time.sleep(0.3)

    for c in need:
        coord = coord_by_title.get(c["title"])
        if coord:
            c["dist"] = _haversine_m(dest_lat, dest_lon, coord[0], coord[1])


def _drop_far_outliers(candidates: list[dict], radius_km: float,
                        factor: float = 3.0, floor_km: float = 40.0) -> list[dict]:
    """Destination-aware search can occasionally surface a same-named place
    in a different city/country. Once a candidate's distance is known (via
    GeoSearch or coordinate enrichment), drop it if it's wildly outside the
    requested radius. Candidates whose distance is still unknown are kept —
    they came from a destination-targeted query and we'd rather rank them
    low than silently discard them."""
    limit_m = max(radius_km * 1000 * factor, floor_km * 1000)
    return [c for c in candidates if c.get("dist") is None or c["dist"] <= limit_m]


def _source_confidence(source) -> int:
    """Ranking signal: a candidate corroborated by both retrieval sources is
    most trustworthy; a candidate found only via the destination-aware
    attraction search is still more targeted than a bare proximity hit from
    GeoSearch alone (source defaults to empty/GeoSearch-only -> 0)."""
    if not source:
        return 0
    if "geosearch" in source and "search" in source:
        return 2
    if "search" in source:
        return 1
    return 0


def _categorize(title: str) -> str:
    t = title.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in t for kw in keywords):
            return category
    return "other"


def _diversify(ranked: list[dict], limit: int = 20,
                max_per_category: int | None = None) -> list[dict]:
    """Lightweight diversity pass over an already popularity-ranked list: cap
    how many results of the same rough category (fort/palace, museum,
    religious site, market, lake/scenic, tower/monument, other) can be taken
    before deferring further ones of that category. Scans the FULL ranked
    list (not just the first `limit` items) so a genuine landmark that
    ranked just outside the naive top-N still gets a chance to displace a
    same-category item that ranked worse — otherwise this degenerates into
    a plain top-N cutoff whenever no category cap is hit early."""
    if not ranked:
        return ranked
    if max_per_category is None:
        max_per_category = max(2, limit // 4)
    selected: list[dict] = []
    deferred: list[dict] = []
    counts: dict[str, int] = {}
    for p in ranked:
        category = _categorize(p["title"])
        if counts.get(category, 0) < max_per_category:
            selected.append(p)
            counts[category] = counts.get(category, 0) + 1
        else:
            deferred.append(p)
    if len(selected) > limit:
        selected = selected[:limit]
    elif len(selected) < limit:
        selected.extend(deferred[: limit - len(selected)])
    return selected


@mcp.tool()
def get_nearby_attractions(city: str, radius_km: float = 15, region: str = "") -> str:
    key = _cache_key((city, radius_km, region))
    cached = _cache_get("attractions", key)
    if cached is not None:
        return cached
    if key in _attr_cache:
        return _attr_cache[key]
    result = _get_nearby_attractions_uncached(city, radius_km, region)
    _attr_cache[key] = result
    _cache_set("attractions", key, result)
    return result


def _rank_by_popularity(places: list[dict], max_candidates: int = 150) -> list[dict]:
    """Re-rank geosearch results by Wikipedia pageviews (last 30 days) so the
    destination's actually-famous landmarks float to the top. Geosearch alone
    is density-based: in a dense city the 50 closest articles are
    neighbourhoods and streets, and icons like Charminar never appear.
    Pageviews are fetched in batches of 50 titles per call (one API call per
    batch), then candidates are sorted by total views."""
    if not places:
        return places
    candidates = places[:max_candidates]
    titles = [p["title"] for p in candidates]
    views: dict[str, int] = {}
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        try:
            r = _get_with_retry(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query", "prop": "pageviews",
                    "titles": "|".join(batch), "format": "json",
                },
                headers={
                    "User-Agent": "TravelPlannerAgent/1.0 (student L2 project; educational use)"
                },
                retries=2,
            ).json()
            for page in r.get("query", {}).get("pages", {}).values():
                data = page.get("pageviews") or {}
                views[page.get("title", "")] = sum(
                    v for v in data.values() if isinstance(v, int))
        except Exception:
            pass  # a failed batch just leaves those titles at 0 views
        time.sleep(0.5)  # stay polite to the Wikipedia API between batches

    # Guard against a pageviews API outage silently producing all-zero
    # views: if that happens, `views` carries no real signal and must not
    # be trusted as if it did — fall back to distance-led ordering (like
    # the pre-hybrid version) instead of letting whatever's left (source
    # confidence) accidentally become the deciding factor.
    views_available = any(v > 0 for v in views.values())

    def rank(p: dict) -> tuple:
        title = p["title"]
        is_landmark = _is_landmark_title(title)
        source_conf = _source_confidence(p.get("source"))
        dist = p.get("dist")
        dist_km = (dist / 1000.0) if dist is not None else 20.0

        # Popularity is the dominant signal among landmarks — a famous fort
        # should beat a minor museum even if the fort was only picked up by
        # one retrieval source. Source confidence and distance are only
        # small tiebreakers on top of it, never the primary sort key.
        if views_available:
            popularity_score = -math.log1p(views.get(title, 0)) * 10.0
        else:
            popularity_score = dist_km  # graceful fallback: closer first

        tiebreak = -source_conf * 1.0 + dist_km * 0.1
        return (0 if is_landmark else 1, popularity_score + tiebreak)

    return sorted(candidates, key=rank)


def _get_nearby_attractions_uncached(city: str, radius_km: float = 15, region: str = "") -> str:
    try:
        loc = _geocode(city, region)
        if not loc:
            return f"Could not find location: {city}"
        lat, lon, name, _, warning = loc
        if lat is None:
            return f"ERROR: {warning}"

        # Source A: Wikipedia GeoSearch. Its gsradius is hard-capped at
        # 10km by the API itself — requesting radius_km=15 does not actually
        # search 15km, so we don't pretend it does. Source B below is what
        # compensates for both this cap and GeoSearch's density bias.
        radius_m = min(int(radius_km * 1000), 10000)
        geosearch_places: list[dict] = []
        try:
            resp = _get_with_retry(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query", "list": "geosearch",
                    "gscoord": f"{lat}|{lon}", "gsradius": radius_m,
                    "gslimit": "500", "format": "json",
                },
                headers={
                    "User-Agent": "TravelPlannerAgent/1.0 (student L2 project; educational use)"
                },
            )
            geosearch_places = resp.json().get("query", {}).get("geosearch", [])
        except Exception:
            geosearch_places = []  # Source B can still carry the whole result

        # Source B: destination-aware Wikipedia search ("<city> tourist
        # attractions", "<city> forts", ...). Recovers famous landmarks that
        # GeoSearch's proximity/density ranking or 10km cap would otherwise
        # exclude entirely. Failing gracefully here is required — GeoSearch
        # alone must still produce a usable (if less complete) list.
        try:
            search_candidates = _destination_aware_search(name)
        except Exception:
            search_candidates = []

        if not geosearch_places and not search_candidates:
            return f"No notable verified places found within {radius_km}km of {name}."

        merged = _merge_candidates(geosearch_places, search_candidates)

        # Enrich a bounded number of distance-less (search-only) candidates
        # with coordinates, batched (<=50 titles/call), so they can be
        # geographically sanity-checked and ranked alongside GeoSearch
        # results without one API call per candidate.
        try:
            _enrich_with_coordinates(merged, lat, lon)
        except Exception:
            pass  # ranking/filtering below tolerate missing distances

        merged = _drop_far_outliers(merged, radius_km)

        places = _filter_attractions(merged)
        if not places:
            return f"No notable verified places found within {radius_km}km of {name}."

        # Rank by landmark likelihood, source confidence, and Wikipedia
        # popularity so the city's icons surface even when GeoSearch's
        # closest-articles list is dominated by neighbourhoods.
        places = _rank_by_popularity(places)

        # Lightweight diversity pass so the final list isn't dominated by
        # one place type (e.g. 15 lakes) purely because that type happened
        # to rank well.
        places = _diversify(places, limit=20)

        lines = [f"Verified places near {name} (sorted by popularity):"]
        for p in places[:20]:
            dist = p.get("dist")
            dist_str = f"{dist:.0f}m away" if isinstance(dist, (int, float)) else "distance unknown"
            lines.append(f"  - {p['title']} ({dist_str})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching nearby attractions: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")