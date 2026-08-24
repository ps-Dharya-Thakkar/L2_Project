"""
QUERY-LEVEL CACHE — stores complete plan results keyed by the user's query
text, so re-running an identical query returns the saved itinerary instantly
with zero LLM calls.

This is separate from the tool cache in mcp_server.py (which caches individual
geocode/weather/FX/attraction lookups). The tool cache already saves those
network calls, but the ~1 minute wall-clock time is dominated by the 20s
spacing between Groq LLM calls — which run fresh every time. This module
caches the FINAL plan so a repeat query skips the whole LLM pipeline.
"""

import hashlib
import json
import os
import re
import time

CACHE_DIR: str = "cache"
CACHE_FILE: str = os.path.join(CACHE_DIR, "query_cache.json")
QUERY_CACHE_TTL_SECONDS: int = 24 * 60 * 60  # base: 24h for a general itinerary

# The mentor review (L2) flagged that a flat 24h TTL is wrong for a plan that
# embeds VOLATILE data (weather, FX). A plan about "tomorrow's weather" must
# not be served from a cache written 23h ago. So the query cache is now
# freshness-aware: the effective TTL of a cached plan is the MINIMUM TTL of
# every data component baked into it (same idea as the per-tool TTLs in
# mcp_server.py). A static plan (geocode only) lives 24h; one with weather
# lives 2h; one with FX lives 1h; attractions sit in the middle.
QUERY_TOOL_TTL_SECONDS: dict[str, int] = {
    "geocode_city": 30 * 24 * 60 * 60,           # static — coordinates don't change
    "get_weather": 2 * 60 * 60,                  # forecasts change hour to hour
    "get_exchange_rate": 1 * 60 * 60,            # FX moves intraday
    "get_nearby_attractions": 24 * 60 * 60,      # verified places are stable
}


def _entry_tools(entry: dict) -> set[str]:
    """Tool names that were called to build a cached plan entry."""
    return {t.get("tool", "") for t in entry.get("tool_log", [])}


def effective_ttl(entry: dict) -> int:
    """Freshness-aware TTL for a cached plan: the minimum TTL of every tool
    used to build it, never exceeding the 24h base. Empty/legacy entries
    (no tool_log) fall back to the base TTL."""
    tools = _entry_tools(entry)
    if not tools:
        return QUERY_CACHE_TTL_SECONDS
    per_tool = [ttl for name, ttl in QUERY_TOOL_TTL_SECONDS.items() if name in tools]
    if not per_tool:
        return QUERY_CACHE_TTL_SECONDS
    return min([QUERY_CACHE_TTL_SECONDS] + per_tool)


def _normalize(query: str) -> str:
    """Normalize a query for cache-keying: lowercase, collapse whitespace,
    strip punctuation. 'Plan a  trip   to  Goa!' and 'plan a trip to goa'
    therefore hit the same entry."""
    text = query.lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def _cache_key(query: str) -> str:
    return hashlib.sha256(_normalize(query).encode("utf-8")).hexdigest()


def _load() -> dict:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(store: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=0)
    except OSError:
        pass  # best-effort; a failed write shouldn't break a trip


def get(query: str) -> dict | None:
    """Return the cached plan entry for a query, or None if absent/expired.

    Expiry uses the freshness-aware effective TTL — a plan that baked in
    weather data expires after 2h even though the base TTL is 24h, so a
    re-ask about "tomorrow's weather" never returns stale data."""
    entry = _load().get(_cache_key(query))
    if not entry:
        return None
    ttl = effective_ttl(entry.get("entry") or {})
    if time.time() - entry.get("ts", 0) > ttl:
        return None
    return entry.get("entry")


def set(query: str, entry: dict) -> None:
    """Store a completed plan (entry) keyed by the normalized query."""
    store = _load()
    store[_cache_key(query)] = {"entry": entry, "ts": time.time()}
    _save(store)


def cached_query_count() -> int:
    """Number of live (non-expired) cached queries — for UI status display."""
    store = _load()
    now = time.time()
    live = 0
    for key, v in store.items():
        ttl = effective_ttl(v.get("entry") or {})
        if now - v.get("ts", 0) <= ttl:
            live += 1
    return live