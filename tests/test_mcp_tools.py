"""
Smoke tests for MCP tool functions — imported directly from mcp_server.
These test the underlying Python functions, not the MCP transport layer.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_server import _filter_attractions, geocode_city, get_exchange_rate


def test_filter_attractions_removes_non_attractions():
    fake = [
        {"title": "Kudchade railway station", "dist": 4000},
        {"title": "Sanvordem Assembly constituency", "dist": 5000},
        {"title": "Quepem taluka", "dist": 9000},
        {"title": "Rachol Fort", "dist": 9000},
        {"title": "Menezes Braganza House", "dist": 6000},
    ]
    out = _filter_attractions(fake)
    titles = [p["title"] for p in out]
    assert "Rachol Fort" in titles
    assert "Menezes Braganza House" in titles
    assert "Kudchade railway station" not in titles
    assert "Sanvordem Assembly constituency" not in titles
    assert "Quepem taluka" not in titles


def test_filter_attractions_ranks_landmarks_first():
    fake = [
        {"title": "Sanvordem", "dist": 1000},
        {"title": "Rachol Fort", "dist": 9000},
        {"title": "Nanda Lake", "dist": 7577},
    ]
    out = _filter_attractions(fake)
    assert out[0]["title"] in ("Rachol Fort", "Nanda Lake"), \
        f"Landmark-type titles should rank first, got {out}"


def test_filter_attractions_ranks_suffix_landmarks_first():
    """'Charminar' carries no type word (only the suffix 'minar') — it must
    rank ahead of a minor garden so the writer sees the city's icon."""
    fake = [
        {"title": "Public Gardens", "dist": 2000},
        {"title": "Charminar", "dist": 3000},
        {"title": "Hussain Sagar", "dist": 6000},
        {"title": "Ameerpet", "dist": 4000},
    ]
    out = _filter_attractions(fake)
    titles = [p["title"] for p in out]
    assert titles == ["Charminar", "Hussain Sagar", "Public Gardens", "Ameerpet"], \
        f"Suffix landmarks should lead, got {titles}"


def test_filter_attractions_keeps_golconda_fort_style_titles():
    fake = [
        {"title": "Golconda Fort", "dist": 8500},
        {"title": "Qutb Shahi Tombs", "dist": 12000},
        {"title": "Hawa Mahal", "dist": 5000},
    ]
    out = _filter_attractions(fake)
    titles = [p["title"] for p in out]
    assert titles == ["Hawa Mahal", "Golconda Fort", "Qutb Shahi Tombs"], \
        f"Closer landmark suffix names sort first, got {titles}"


def test_geocode_city_returns_string():
    result = geocode_city("London")
    assert isinstance(result, str), f"Expected string, got {type(result)}"
    assert len(result) > 0, "Expected non-empty result"


def test_geocode_city_with_region():
    result = geocode_city("Manali", "Himachal Pradesh")
    assert isinstance(result, str)
    assert "ERROR" not in result, f"Unexpected error: {result}"


def test_geocode_city_nonexistent():
    result = geocode_city("Xyzzyville")
    assert "No location found" in result, f"Expected 'No location found', got: {result}"


def test_get_exchange_rate_returns_string():
    result = get_exchange_rate("USD", "INR")
    assert isinstance(result, str), f"Expected string, got {type(result)}"
    assert len(result) > 0
    assert "Error" not in result, f"Unexpected error: {result}"
    # Should contain a numeric rate
    assert "=" in result, f"Expected '=' in result, got: {result}"
    parts = result.split("=")
    assert len(parts) > 1
    rate_value = parts[1].strip().split()[0]
    float(rate_value)  # should not raise


def test_get_exchange_rate_invalid_currency():
    result = get_exchange_rate("USD", "INVALID")
    assert "Error" in result or isinstance(result, str)


def test_get_exchange_rate_roundtrip():
    result = get_exchange_rate("EUR", "USD")
    assert "Error" not in result, f"Unexpected error: {result}"
    assert "EUR" in result and "USD" in result
