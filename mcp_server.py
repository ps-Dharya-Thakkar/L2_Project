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
import time
from typing import Any
from mcp.server.fastmcp import FastMCP
import requests

mcp = FastMCP("travel-tools")


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
    try:
        r = _get_with_retry(
            "https://api.frankfurter.app/latest",
            params={"from": base_currency.upper(), "to": target_currency.upper()},
        ).json()
        rate = r["rates"][target_currency.upper()]
        return f"1 {base_currency.upper()} = {rate} {target_currency.upper()}"
    except Exception as e:
        return f"Error fetching exchange rate: {e}"


@mcp.tool()
def get_nearby_attractions(city: str, radius_km: float = 8, region: str = "") -> str:
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
                "gslimit": 12, "format": "json",
            },
            headers={
                "User-Agent": "TravelPlannerAgent/1.0 (student L2 project; educational use)"
            },
        )
        data = resp.json()

        places = data.get("query", {}).get("geosearch", [])
        if not places:
            return f"No notable verified places found within {radius_km}km of {name}."

        lines = [f"Verified places near {name} (sorted by distance):"]
        for p in places:
            lines.append(f"  - {p['title']} ({p['dist']:.0f}m away)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching nearby attractions: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
