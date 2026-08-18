"""Tests for the persistent disk cache in mcp_server.py."""

import os
import sys
import tempfile
import time

import mcp_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _isolate_cache():
    """Point the cache at a temp file so tests never touch the real one."""
    mcp_server.CACHE_FILE = os.path.join(
        tempfile.mkdtemp(), "test_cache.json")
    mcp_server.CACHE_DIR = os.path.dirname(mcp_server.CACHE_FILE)
    mcp_server._cache_store = {}


def test_cache_set_and_get_roundtrip():
    _isolate_cache()
    mcp_server._cache_set("fx", ("eur", "inr"), "1 EUR = 100 INR")
    assert mcp_server._cache_get("fx", ("eur", "inr")) == "1 EUR = 100 INR"


def test_cache_get_missing_returns_none():
    _isolate_cache()
    assert mcp_server._cache_get("weather", ("abc", "2026-01-01", "xyz")) is None


def test_cache_key_normalization():
    _isolate_cache()
    mcp_server._cache_set("geocode", ("Paris", " France "), "res")
    # case/whitespace-insensitive lookup
    assert mcp_server._cache_get("geocode", ("PARIS", "france")) == "res"


def test_cache_ttl_expiry():
    _isolate_cache()
    mcp_server._cache_set("fx", ("usd", "inr"), "1 USD = 80 INR")
    # pretend it's old
    skey = mcp_server._cache_key(("usd", "inr"))
    skey = __import__("json").dumps(skey)
    entry = mcp_server._cache_store["fx"][skey]
    entry["ts"] = time.time() - mcp_server.CACHE_TTL_SECONDS["fx"] - 10
    assert mcp_server._cache_get("fx", ("usd", "inr")) is None


def test_cache_persists_across_reload():
    _isolate_cache()
    mcp_server._cache_set("attractions", ("goa", 8, "india"), "Verified places near Goa")
    # fresh store == fresh subprocess
    fresh = mcp_server._load_cache()
    assert "attractions" in fresh
    assert len(fresh["attractions"]) == 1


def test_save_creates_file():
    _isolate_cache()
    assert not os.path.exists(mcp_server.CACHE_FILE)
    mcp_server._cache_set("fx", ("gbp", "inr"), "1 GBP = 100 INR")
    assert os.path.exists(mcp_server.CACHE_FILE)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__]))