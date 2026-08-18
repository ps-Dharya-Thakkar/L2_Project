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
)

_ATTRACTION_TYPE_WORDS = (
    "beach", "fort", "temple", "church", "mosque", "museum", "palace", "lake",
    "garden", "park", "island", "point", "falls", "monastery", "cathedral",
    "viewpoint", "harbour", "lighthouse", "castle", "tower", "bridge", "square",
)


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

    # Sort: entries whose title contains a landmark-type word first, then
    # by distance (stabilises ordering, keeps the list curated).
    def rank(p: dict) -> tuple:
        t = p["title"].lower()
        has_type = any(w in t for w in _ATTRACTION_TYPE_WORDS)
        return (0 if has_type else 1, p["dist"])

    kept.sort(key=rank)
    return kept


@mcp.tool()
def get_nearby_attractions(city: str, radius_km: float = 8, region: str = "") -> str:
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


def _get_nearby_attractions_uncached(city: str, radius_km: float = 8, region: str = "") -> str:
    try:
        loc = _geocode(city, region)
        if not loc:
            return f"Could not find location: {city}"
        lat, lon, name, _, warning = loc
        if lat is None:
            return f"ERROR: {warning}"

        radius_m = min(int(radius_km * 1000), 10000)
        resp = _get_with_retry(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "geosearch",
                "gscoord": f"{lat}|{lon}", "gsradius": radius_m,
                "gslimit": 50, "format": "json",
            },
            headers={
                "User-Agent": "TravelPlannerAgent/1.0 (student L2 project; educational use)"
            },
        )
        data = resp.json()

        places = data.get("query", {}).get("geosearch", [])
        if not places:
            return f"No notable verified places found within {radius_km}km of {name}."

        places = _filter_attractions(places)
        if not places:
            return f"No notable verified places found within {radius_km}km of {name}."

        lines = [f"Verified places near {name} (sorted by distance):"]
        for p in places[:12]:
            lines.append(f"  - {p['title']} ({p['dist']:.0f}m away)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching nearby attractions: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
