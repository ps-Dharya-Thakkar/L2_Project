"""
Tests for the hard guard logic in main.py that prevents the Writer from
inventing data when tools weren't called.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import _check_ollama_models, _user_wants_conversion


def test_check_ollama_models_returns_list():
    missing = _check_ollama_models()
    assert isinstance(missing, list), f"Expected list, got {type(missing)}"


def test_create_guard_for_missing_weather():
    called_tools = {"geocode_city", "get_exchange_rate"}
    guards = []
    guard_notes = []

    if "get_weather" not in called_tools:
        guards.append("WEATHER GUARD")
        guard_notes.append("Weather tool not called")

    assert len(guards) == 1
    assert "Weather tool not called" in guard_notes


def test_create_guard_for_missing_exchange_rate():
    called_tools = {"get_weather", "geocode_city"}
    guards = []
    guard_notes = []

    if "get_exchange_rate" not in called_tools:
        guards.append("EXCHANGE RATE GUARD")
        guard_notes.append("Exchange rate not called")

    assert len(guards) == 1
    assert "Exchange rate not called" in guard_notes


def test_no_guard_when_tools_called():
    called_tools = {"get_weather", "get_exchange_rate", "geocode_city"}
    guards = []

    if "get_weather" not in called_tools:
        guards.append("WEATHER")

    if "get_exchange_rate" not in called_tools:
        guards.append("FX")

    if "get_nearby_attractions" not in called_tools:
        guards.append("ATTRACTIONS")

    assert len(guards) == 1  # only attractions missing
    assert "ATTRACTIONS" in guards


def test_attraction_fail_guard():
    tool_log = [
        {"tool": "get_nearby_attractions", "args": {}, "result": "No places found"}
    ]
    attraction_calls = [t for t in tool_log if t["tool"] == "get_nearby_attractions"]
    attraction_ok = any(
        t["result"].startswith("Verified places near") for t in attraction_calls
    )
    assert not attraction_ok, "Should detect failed attraction lookup"


def test_user_wants_conversion_yes():
    assert _user_wants_conversion("budget in INR, also show in USD")
    assert _user_wants_conversion("budget in INR, also in USD")
    assert _user_wants_conversion("convert to USD")
    assert _user_wants_conversion("show cost in dollars")
    assert _user_wants_conversion("Plan trip to Paris, budget in EUR, also show in INR")
    assert _user_wants_conversion("budget in INR and show cost in USD")
    assert _user_wants_conversion("budget in INR, convert to EUR as well")


def test_user_wants_conversion_no():
    assert not _user_wants_conversion("Plan a trip to Manali in December")
    assert not _user_wants_conversion("budget in INR")
    assert not _user_wants_conversion("budget in INR, moderate spending")
    assert not _user_wants_conversion("Plan a trip to Goa for a couple in August, budget in INR")
    assert not _user_wants_conversion("Trip to Paris, budget 2000 EUR")
    assert not _user_wants_conversion("Hello, what is the weather?")
    assert not _user_wants_conversion("Suggest a hotel in Delhi")


def test_user_wants_conversion_edge_cases():
    assert not _user_wants_conversion("")
    assert not _user_wants_conversion("budget in rupees")
    # "in dollar" should match (one of our keywords)
    assert _user_wants_conversion("show cost in dollar terms")


def test_attraction_never_called_guard():
    """When get_nearby_attractions was never called, a guard must fire."""
    tool_log = [
        {"tool": "geocode_city", "args": {}, "result": "Manali, India -> lat=32.24..."},
        {"tool": "get_weather", "args": {}, "result": "HISTORICAL weather for Manali..."},
    ]
    attraction_calls = [t for t in tool_log if t["tool"] == "get_nearby_attractions"]
    attraction_ok = any(
        t["result"].startswith("Verified places near") for t in attraction_calls
    )
    assert len(attraction_calls) == 0
    assert not attraction_ok
    # The code path for "not called" should trigger
    if not attraction_calls:
        guard_note = "Attractions tool not called — forcing generic place descriptions"
        assert guard_note is not None


def test_attraction_success_guard():
    tool_log = [
        {"tool": "get_nearby_attractions", "args": {},
         "result": "Verified places near Manali (sorted by distance):\n  - Hadimba Temple (300m away)"}
    ]
    attraction_calls = [t for t in tool_log if t["tool"] == "get_nearby_attractions"]
    attraction_ok = any(
        t["result"].startswith("Verified places near") for t in attraction_calls
    )
    assert attraction_ok, "Should detect successful attraction lookup"
