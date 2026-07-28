"""
MCP SERVER — the "tool box" the agent is allowed to reach into.

This process is started automatically by the orchestrator (via stdio), you
don't run it by hand. It exposes 4 tools, each backed by a free web service
(no API key needed for any of them):

    geocode_city(city)                          -> lat/lon lookup       (Open-Meteo)
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
from mcp.server.fastmcp import FastMCP
import requests

mcp = FastMCP("travel-tools")


def _get_with_retry(url: str, params: dict, headers: dict = None, retries: int = 1, timeout: int = 10):
    """Small helper: retry once after a short pause on transient network
    errors (timeouts, connection resets), instead of failing the whole tool
    call on a one-off network hiccup."""
    last_err = None
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


def _geocode(city: str, region: str = ""):
    """Internal helper — not exposed as a tool itself, used by the others.
    Fetches multiple candidates (some city names are ambiguous, e.g. there's
    a 'Manali' in Himachal Pradesh AND a 'Manali' suburb of Chennai). If a
    `region` hint is given (state/country), it's used to pick the right
    match. Returns (lat, lon, display_name, country, warning_or_none)."""
    city = city.strip()
    region = region.strip()
    r = _get_with_retry(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 5},
    ).json()
    results = r.get("results") or []
    if not results:
        return None

    if len(results) == 1 or not region:
        chosen = results[0]
        warning = None
        if len(results) > 1:
            # Multiple places share this name and we have no region hint to
            # disambiguate -- proceed with the top match, but say so clearly
            # instead of silently guessing.
            others = ", ".join(f"{x['name']}, {x.get('admin1', x.get('country',''))}"
                                for x in results[1:3])
            warning = (f"NOTE: '{city}' is ambiguous ({len(results)} places share this "
                       f"name, e.g. also {others}). Assumed {chosen['name']}, "
                       f"{chosen.get('admin1', '')}, {chosen.get('country', '')}. "
                       f"If this is wrong, call again with a region, e.g. "
                       f"city='{city}', region='<state or country>'.")
    else:
        region_l = region.lower()
        match = next((x for x in results
                      if region_l in (x.get('admin1', '') or '').lower()
                      or region_l in (x.get('country', '') or '').lower()), None)
        chosen = match or results[0]
        warning = None if match else (
            f"NOTE: region '{region}' didn't match any candidate for '{city}'; "
            f"defaulted to {chosen['name']}, {chosen.get('admin1', '')}.")

    return (chosen["latitude"], chosen["longitude"], chosen["name"],
            chosen.get("country", ""), warning)


@mcp.tool()
def geocode_city(city: str, region: str = "") -> str:
    """Look up latitude/longitude and country for a city name. Pass `region`
    (state/province/country) if the city name might be ambiguous, e.g.
    city='Manali', region='Himachal Pradesh' -- some city names (like
    Manali) refer to more than one real place."""
    try:
        loc = _geocode(city, region)
        if not loc:
            return f"No location found for '{city}'."
        lat, lon, name, country, warning = loc
        out = f"{name}, {country} -> lat={lat}, lon={lon}"
        return f"{out}\n{warning}" if warning else out
    except Exception as e:
        return f"Error looking up city: {e}"


@mcp.tool()
def get_weather(city: str, date: str = "", region: str = "") -> str:
    """Get weather for a city. Pass `date` as YYYY-MM-DD if the trip date is
    known. If that date is within the next ~15 days, this returns a REAL
    live forecast. If the date is further out (or omitted), live forecasts
    don't exist yet -- this instead returns REAL historical weather from the
    same calendar date one year ago, clearly labeled as historical, so you
    have real data to reason with instead of guessing. Pass `region`
    (state/country) if the city name might be ambiguous, e.g. city='Manali',
    region='Himachal Pradesh' -- some city names refer to more than one
    real place."""
    try:
        loc = _geocode(city, region)
        if not loc:
            return f"Could not find location: {city}"
        lat, lon, name, _, warning = loc

        today = datetime.date.today()
        target_date = None
        if date:
            try:
                target_date = datetime.date.fromisoformat(date)
            except ValueError:
                target_date = None

        days_out = (target_date - today).days if target_date else 0

        if target_date and 0 <= days_out <= 15:
            # Real live forecast is available for this date.
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

        # Too far out (or no date given) -> use real historical data as a
        # grounded stand-in for "typical" conditions, instead of an LLM guess.
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
        result = (f"No live forecast available yet ({name} trip date is more than "
                  f"~15 days out). HISTORICAL weather, EXACT DATE={exact_date} "
                  f"(copy this date exactly if you mention it, do not guess a "
                  f"different year): {d['temperature_2m_min'][0]}-"
                  f"{d['temperature_2m_max'][0]}°C, {d['precipitation_sum'][0]}mm "
                  f"precipitation.")
        return f"{result}\n{warning}" if warning else result
    except Exception as e:
        return f"Error fetching weather: {e}"


@mcp.tool()
def get_exchange_rate(base_currency: str, target_currency: str) -> str:
    """Get the live currency exchange rate from base_currency to
    target_currency, e.g. base_currency='USD', target_currency='INR'."""
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
    """Get a list of REAL, verified points of interest near a city, sorted
    by distance, using Wikipedia's geosearch. ALWAYS call this before
    naming specific places/temples/villages/viewpoints in an itinerary --
    do not invent or guess attraction names, some 'well known' places may
    actually be hours away from the city. radius_km caps at ~10km. Pass
    `region` (state/country) if the city name might be ambiguous, e.g.
    city='Manali', region='Himachal Pradesh'."""
    try:
        loc = _geocode(city, region)
        if not loc:
            return f"Could not find location: {city}"
        lat, lon, name, _, warning = loc

        radius_m = min(int(radius_km * 1000), 10000)  # Wikipedia API hard cap
        resp = _get_with_retry(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "geosearch",
                "gscoord": f"{lat}|{lon}", "gsradius": radius_m,
                "gslimit": 12, "format": "json",
            },
            headers={
                # Wikipedia's API rejects/blocks requests with no User-Agent
                # (returns an empty/non-JSON body) -- this identifies our app.
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