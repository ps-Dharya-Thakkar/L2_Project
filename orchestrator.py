"""
ORCHESTRATOR AGENT — the "brain" of the system.

This agent:
  1. Receives the user's raw travel query.
  2. Is given the list of available MCP tools (weather, currency, geocode).
  3. The LOCAL LLM itself decides, turn by turn, whether it needs a tool
     or whether it already has enough to answer. This is native function
     calling — we are not hand-writing "if weather in query: call tool".
     The model reads the query and reasons about it.
  4. If it calls a tool, we execute it via MCP, feed the result back in,
     and let the model decide again. This repeat-until-done pattern is
     called a ReAct loop (Reason -> Act -> Observe -> repeat).
  5. Once the model stops requesting tools, we hand everything to the
     Writer Agent to produce the final itinerary.
"""

import re
import ollama
from datetime import datetime
from typing import Any

ORCH_MODEL: str = "qwen2.5:7b-instruct"

MONTH_MAP: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}


def _normalize_weather_date(args: dict[str, Any], user_query: str) -> None:
    """If the model passed an empty/blank date to get_weather, extract
    a month from the user query and compute a YYYY-MM-DD."""
    date_val: str = args.get("date") or ""
    if date_val.strip():
        return

    q: str = user_query.lower()
    for month_name, month_num in MONTH_MAP.items():
        if month_name in q:
            today = datetime.now()
            year = today.year
            if month_num < today.month:
                year += 1
            args["date"] = f"{year}-{month_num:02d}-15"
            print(f"  [normalizer] inferred date={args['date']} from '{month_name}' in query")
            return

SYSTEM_PROMPT: str = """You are a travel-planning research agent.
Your ONLY job is to gather facts needed to plan a trip. You do NOT write the
final itinerary yourself — a separate writer will do that.

FIRST, check if the user's message is actually a travel-planning request
(mentions or clearly implies a destination, trip, or travel-related need).
If it is NOT — e.g. it's a greeting like "Hello", a random word, or
anything unrelated to planning travel — reply with EXACTLY this text and
nothing else, and do not call any tool:
INVALID_QUERY: <one short sentence saying why this isn't a travel request>

If it IS a valid travel request, you MUST call EVERY tool listed below
before you stop. Do not stop after only 1 or 2 calls.

REQUIRED tools (call ALL that apply):

geocode_city(city, region) — first, confirm the location.
    If the user specified a region/state/country, pass it EXACTLY as
    written. Example: "Himachal Pradesh" stays as "Himachal Pradesh".
    If the user did NOT specify a region, infer it from your general
    knowledge. Examples: "Goa" → region="India", "Manali" → region="Himachal Pradesh",
    "Paris" → region="France", "London" → region="United Kingdom".
    NEVER pass the city name itself as the region (e.g. do NOT pass
    region="Goa" for city="Goa").

get_weather(city, date, region) — always compute a YYYY-MM-DD date.
    If the user says "December" or any month, compute an approximate date
    like "2026-12-15". NEVER pass empty string for date.
    Use the same region as geocode_city.

get_exchange_rate(base_currency, target_currency) — if user mentions a
    currency or asks for conversion.

get_nearby_attractions(city, region) — ALWAYS call this for any
    destination city. The itinerary writer needs real place names.
    Use the same region as geocode_city.

If a tool returns an error (e.g. "ERROR: ... matched no candidate"), do
NOT panic. Do NOT mark the query as invalid. Instead, try again with a
different region or without a region. Errors from tools are recoverable
— try alternative parameters.

After calling ALL needed tools, write a plain-text summary of the facts
you gathered and stop.
"""


async def run_orchestrator(user_query: str, mcp_client, max_turns: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tools = await mcp_client.list_tools_for_ollama()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_query},
    ]

    tool_call_log: list[dict[str, Any]] = []

    for turn in range(max_turns):
        response = ollama.chat(model=ORCH_MODEL, messages=messages, tools=tools)
        msg = response["message"]
        messages.append(msg)

        if not msg.get("tool_calls"):
            print(f"  [orchestrator] turn {turn+1}: no tool called. "
                  f"Model said: {msg.get('content', '')[:200]!r}")
            return messages, tool_call_log

        for call in msg["tool_calls"]:
            name: str = call["function"]["name"]
            args: dict[str, Any] = call["function"]["arguments"]

            if name == "get_weather":
                _normalize_weather_date(args, user_query)

            print(f"  [tool call] {name}({args})")
            result: str = await mcp_client.call_tool(name, args)
            tool_call_log.append({"tool": name, "args": args, "result": result})

            messages.append({
                "role": "tool",
                "content": result,
                "name": name,
            })

    return messages, tool_call_log
