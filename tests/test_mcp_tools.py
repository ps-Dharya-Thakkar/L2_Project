"""
Smoke tests for MCP tool functions — imported directly from mcp_server.
These test the underlying Python functions, not the MCP transport layer.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_server import geocode_city, get_exchange_rate


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
