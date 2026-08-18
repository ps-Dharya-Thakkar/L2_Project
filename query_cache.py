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
QUERY_CACHE_TTL_SECONDS: int = 24 * 60 * 60  # 24h — plans expire naturally


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
    """Return the cached plan entry for a query, or None if absent/expired."""
    entry = _load().get(_cache_key(query))
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > QUERY_CACHE_TTL_SECONDS:
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
    return sum(
        1 for v in store.values()
        if now - v.get("ts", 0) <= QUERY_CACHE_TTL_SECONDS
    )