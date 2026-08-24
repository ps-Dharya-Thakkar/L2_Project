"""
Tests for the deterministic tool-call completeness validation layer added
to orchestrator.py.

No real LLM, MCP, or network calls are made — llm.chat and the MCP client's
call_tool are mocked/stubbed throughout.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import orchestrator


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

class FakeMCPClient:
    """Minimal stand-in for MCPToolClient. Never touches the network or a
    real subprocess — call_tool just returns a canned string."""

    def __init__(self, tool_results: dict[str, str] | None = None) -> None:
        self.tool_results = tool_results or {}
        self.calls: list[tuple[str, dict]] = []

    async def list_tools_for_ollama(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
            for n in ("geocode_city", "get_weather", "get_exchange_rate", "get_nearby_attractions")
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return self.tool_results.get(name, f"OK:{name}")


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": args}}


def _assistant_msg(tool_calls: list[dict] | None = None, content: str = "") -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ===========================================================================
# 1-5: get_expected_tools / get_missing_tools (pure deterministic logic)
# ===========================================================================

def test_expected_tools_base_case_no_currency():
    expected = orchestrator.get_expected_tools("Plan a 3-day trip to Goa in December")
    assert expected == sorted(["geocode_city", "get_weather", "get_nearby_attractions"])
    assert "get_exchange_rate" not in expected


def test_expected_tools_two_currencies_mentioned():
    expected = orchestrator.get_expected_tools(
        "Plan a trip to Paris, I want costs in USD and EUR")
    assert "get_exchange_rate" in expected


def test_expected_tools_explicit_conversion_keyword():
    expected = orchestrator.get_expected_tools(
        "Plan a trip to Tokyo and convert prices to my home currency")
    assert "get_exchange_rate" in expected


def test_all_required_tools_executed_no_missing():
    log = [
        {"tool": "geocode_city", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
        {"tool": "get_weather", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
        {"tool": "get_nearby_attractions", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
    ]
    expected = orchestrator.get_expected_tools("Plan a trip to Goa")
    executed = orchestrator.get_executed_tools(log)
    assert orchestrator.get_missing_tools(expected, executed) == []


def test_one_required_tool_missing():
    log = [
        {"tool": "geocode_city", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
        {"tool": "get_weather", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
    ]
    expected = orchestrator.get_expected_tools("Plan a trip to Goa")
    executed = orchestrator.get_executed_tools(log)
    missing = orchestrator.get_missing_tools(expected, executed)
    assert missing == ["get_nearby_attractions"]


def test_multiple_tools_missing():
    log = [
        {"tool": "geocode_city", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
    ]
    expected = orchestrator.get_expected_tools("Plan a trip to Goa, convert INR to USD")
    executed = orchestrator.get_executed_tools(log)
    missing = orchestrator.get_missing_tools(expected, executed)
    assert missing == sorted(["get_weather", "get_exchange_rate", "get_nearby_attractions"])


def test_no_exchange_rate_required_when_single_currency():
    expected = orchestrator.get_expected_tools("Plan a trip to Manali, budget in rupees")
    assert "get_exchange_rate" not in expected


def test_currency_conversion_requested_marks_exchange_required():
    expected = orchestrator.get_expected_tools(
        "Plan a trip to London, show cost in USD too")
    assert "get_exchange_rate" in expected


# ===========================================================================
# 6: invalid/non-travel query should not force validation
# ===========================================================================

def test_invalid_query_response_detected():
    msg = {"role": "assistant", "content": "INVALID_QUERY: this is just a greeting"}
    assert orchestrator._is_invalid_query_response(msg) is True


def test_valid_query_response_not_flagged_invalid():
    msg = {"role": "assistant", "content": "Here is a summary of facts gathered..."}
    assert orchestrator._is_invalid_query_response(msg) is False


@pytest.mark.asyncio
async def test_invalid_query_end_to_end_skips_validation():
    """The orchestrator should return immediately on INVALID_QUERY without
    ever invoking the completeness recovery path (no extra LLM call)."""
    fake_client = FakeMCPClient()

    llm_response = {"message": _assistant_msg(content="INVALID_QUERY: not a travel request")}

    with patch("orchestrator.llm.chat", return_value=llm_response) as mock_chat, \
         patch("orchestrator._run_completeness_recovery", new_callable=AsyncMock) as mock_recovery:
        messages, tool_call_log = await orchestrator.run_orchestrator("hello there", fake_client)

    mock_recovery.assert_not_called()
    assert mock_chat.call_count == 1
    assert tool_call_log == []


# ===========================================================================
# 7: duplicate execution records should not incorrectly affect validation
# ===========================================================================

def test_duplicate_execution_records_deduplicated():
    log = [
        {"tool": "geocode_city", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
        {"tool": "geocode_city", "args": {}, "result": "ok retry", "elapsed_seconds": 0.2},
        {"tool": "get_weather", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
        {"tool": "get_nearby_attractions", "args": {}, "result": "ok", "elapsed_seconds": 0.1},
    ]
    expected = orchestrator.get_expected_tools("Plan a trip to Goa")
    executed = orchestrator.get_executed_tools(log)
    assert executed == sorted(["geocode_city", "get_weather", "get_nearby_attractions"])
    assert orchestrator.get_missing_tools(expected, executed) == []


# ===========================================================================
# 8: recovery succeeds — missing tool executed, validation passes
# ===========================================================================

@pytest.mark.asyncio
async def test_recovery_succeeds_end_to_end():
    """Main loop calls only 2 of 3 required tools, then stops. Recovery
    should detect get_nearby_attractions missing, ask the LLM again, get a
    tool call for it, execute it via the fake MCP client, and finish with
    zero missing tools — with no infinite loop or crash."""
    fake_client = FakeMCPClient()

    # Turn 1: model calls geocode_city + get_weather
    turn1 = {"message": _assistant_msg(tool_calls=[
        _tool_call("c1", "geocode_city", {"city": "Goa", "region": "India"}),
        _tool_call("c2", "get_weather", {"city": "Goa", "date": "2026-12-15", "region": "India"}),
    ])}
    # Turn 2: model stops calling tools (forgot get_nearby_attractions)
    turn2 = {"message": _assistant_msg(content="Here's what I found so far.")}
    # Recovery turn: model calls the missing tool only
    recovery_turn = {"message": _assistant_msg(tool_calls=[
        _tool_call("c3", "get_nearby_attractions", {"city": "Goa", "radius_km": 15, "region": "India"}),
    ])}

    with patch("orchestrator.llm.chat", side_effect=[turn1, turn2, recovery_turn]):
        messages, tool_call_log = await orchestrator.run_orchestrator(
            "Plan a trip to Goa in December", fake_client)

    executed = orchestrator.get_executed_tools(tool_call_log)
    expected = orchestrator.get_expected_tools("Plan a trip to Goa in December")
    assert orchestrator.get_missing_tools(expected, executed) == []
    assert ("get_nearby_attractions", {"city": "Goa", "radius_km": 15, "region": "India"}) in fake_client.calls


# ===========================================================================
# 9: retry limit reached — no infinite loop, exits safely
# ===========================================================================

@pytest.mark.asyncio
async def test_retry_limit_reached_no_infinite_loop():
    """Model never calls the missing tool, even across recovery attempts.
    The system must stop after MAX_TOOL_COMPLETENESS_RETRIES and return
    normally instead of looping forever or crashing."""
    fake_client = FakeMCPClient()

    turn1 = {"message": _assistant_msg(tool_calls=[
        _tool_call("c1", "geocode_city", {"city": "Goa", "region": "India"}),
    ])}
    turn2 = {"message": _assistant_msg(content="Done.")}
    # Every recovery attempt: model refuses to call anything.
    recovery_no_call = {"message": _assistant_msg(content="I don't have more info.")}

    call_sequence = [turn1, turn2, recovery_no_call, recovery_no_call]

    with patch("orchestrator.llm.chat", side_effect=call_sequence) as mock_chat:
        messages, tool_call_log = await orchestrator.run_orchestrator(
            "Plan a trip to Goa", fake_client)

    # 1 initial call-turn + 1 stop-turn + MAX_TOOL_COMPLETENESS_RETRIES recovery turns
    assert mock_chat.call_count == 2 + orchestrator.MAX_TOOL_COMPLETENESS_RETRIES
    executed = orchestrator.get_executed_tools(tool_call_log)
    assert executed == ["geocode_city"]  # unchanged — recovery never succeeded
    # Should return safely without raising.


@pytest.mark.asyncio
async def test_recovery_filters_out_unrequested_tool_calls():
    """If the recovery LLM ignores the 'call ONLY the missing tools'
    instruction and returns one genuinely missing tool plus one
    already-executed/unrelated tool, only the genuinely missing tool
    should actually be executed."""
    fake_client = FakeMCPClient()

    # Turn 1: model calls geocode_city + get_weather (get_nearby_attractions missing)
    turn1 = {"message": _assistant_msg(tool_calls=[
        _tool_call("c1", "geocode_city", {"city": "Goa", "region": "India"}),
        _tool_call("c2", "get_weather", {"city": "Goa", "date": "2026-12-15", "region": "India"}),
    ])}
    # Turn 2: model stops calling tools
    turn2 = {"message": _assistant_msg(content="Here's what I found so far.")}
    # Recovery turn: model returns the genuinely missing tool PLUS an
    # already-executed tool it was told not to repeat.
    recovery_turn = {"message": _assistant_msg(tool_calls=[
        _tool_call("c3", "get_nearby_attractions", {"city": "Goa", "radius_km": 15, "region": "India"}),
        _tool_call("c4", "geocode_city", {"city": "Goa", "region": "India"}),  # already executed
    ])}

    with patch("orchestrator.llm.chat", side_effect=[turn1, turn2, recovery_turn]):
        messages, tool_call_log = await orchestrator.run_orchestrator(
            "Plan a trip to Goa in December", fake_client)

    # Only ONE get_nearby_attractions call and ONE geocode_city call (from
    # turn 1) should have gone through the MCP client — the duplicate
    # geocode_city from the recovery step must have been filtered out.
    attractions_calls = [c for c in fake_client.calls if c[0] == "get_nearby_attractions"]
    geocode_calls = [c for c in fake_client.calls if c[0] == "geocode_city"]
    assert len(attractions_calls) == 1
    assert len(geocode_calls) == 1  # not 2 — the recovery duplicate was ignored

    executed = orchestrator.get_executed_tools(tool_call_log)
    expected = orchestrator.get_expected_tools("Plan a trip to Goa in December")
    assert orchestrator.get_missing_tools(expected, executed) == []

    # tool_call_log itself should only have 3 entries total (2 from turn 1,
    # 1 from recovery), not 4.
    assert len(tool_call_log) == 3


@pytest.mark.asyncio
async def test_all_tools_executed_first_pass_no_recovery_call():
    """When the model does everything right the first time, no recovery
    request should ever be made (no extra llm.chat calls)."""
    fake_client = FakeMCPClient()

    turn1 = {"message": _assistant_msg(tool_calls=[
        _tool_call("c1", "geocode_city", {"city": "Goa", "region": "India"}),
        _tool_call("c2", "get_weather", {"city": "Goa", "date": "2026-12-15", "region": "India"}),
        _tool_call("c3", "get_nearby_attractions", {"city": "Goa", "radius_km": 15, "region": "India"}),
    ])}
    turn2 = {"message": _assistant_msg(content="Summary of facts.")}

    with patch("orchestrator.llm.chat", side_effect=[turn1, turn2]) as mock_chat:
        messages, tool_call_log = await orchestrator.run_orchestrator(
            "Plan a trip to Goa in December", fake_client)

    assert mock_chat.call_count == 2  # no recovery round
    executed = orchestrator.get_executed_tools(tool_call_log)
    expected = orchestrator.get_expected_tools("Plan a trip to Goa in December")
    assert orchestrator.get_missing_tools(expected, executed) == []