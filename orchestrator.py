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
import time
import llm
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


def _trim_tool_result(result: str, max_len: int = 300) -> str:
    """Trim a tool result for the LLM-history copy. Keeps the start of the
    result (coordinates, headline figures) and marks the cut. The FULL
    result is preserved in tool_call_log for the guards/writer/reflection."""
    if len(result) <= max_len:
        return result
    return result[:max_len - 3] + "..."


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

get_exchange_rate(base_currency, target_currency) — ONLY if the user
    mentions two different currencies or asks for a conversion (e.g. "show
    cost in USD too"). Do NOT call it when only one currency appears, and
    NEVER use the same currency for both arguments.

get_nearby_attractions(city, radius_km, region) — ALWAYS call this for any
    destination city. The itinerary writer needs real place names. Pass
    radius_km=15 so famous landmarks on the city's outskirts (e.g. Golconda
    Fort for Hyderabad) are included, not just the city-centre spots.
    Use the same region as geocode_city.

If a tool returns an error (e.g. "ERROR: ... matched no candidate"), do
NOT panic. Do NOT mark the query as invalid. Instead, try again with a
different region or without a region. Errors from tools are recoverable
— try alternative parameters.

After calling ALL needed tools, write a plain-text summary of the facts
you gathered and stop.
"""


# ---------------------------------------------------------------------------
# TOOL-COMPLETENESS POLICY (centralized)
# ---------------------------------------------------------------------------
# This is the single source of truth for "which tools are required for a
# valid travel request". It mirrors SYSTEM_PROMPT's REQUIRED tools section
# exactly (geocode_city / get_weather / get_nearby_attractions always;
# get_exchange_rate only for a two-currency / conversion request) so the
# deterministic validator below and the LLM's own instructions never
# disagree about the policy. If the policy in SYSTEM_PROMPT ever changes,
# update it here too.
# ---------------------------------------------------------------------------

ALWAYS_REQUIRED_TOOLS: tuple[str, ...] = (
    "geocode_city",
    "get_weather",
    "get_nearby_attractions",
)

# ISO currency codes we recognize when scanning the raw user query. Kept to
# common/travel-relevant currencies rather than the full ISO-4217 list.
_CURRENCY_CODES: tuple[str, ...] = (
    "USD", "EUR", "GBP", "INR", "JPY", "AUD", "CAD", "CHF", "CNY", "SGD",
    "AED", "THB", "NZD", "ZAR", "MXN", "BRL", "KRW", "HKD", "IDR", "MYR",
    "PHP", "VND", "TRY", "SAR", "QAR", "RUB", "SEK", "NOK", "DKK", "PLN",
    "EGP", "NPR", "LKR", "PKR", "BDT",
)

# Currency symbols mapped to a representative code, so "$100 in Paris" and
# "convert to €" are recognized even without a 3-letter code.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
}

_CURRENCY_CODE_RE = re.compile(
    r"\b(" + "|".join(_CURRENCY_CODES) + r")\b", re.IGNORECASE
)
_CONVERSION_KEYWORDS_RE = re.compile(
    r"\b(convert|conversion|exchange rate|currency conversion)\b",
    re.IGNORECASE,
)

# SYSTEM_PROMPT's own example of an implicit conversion request is "show
# cost in USD too" — a single currency code plus an "also/too/as well"
# idiom implying "convert from whatever this was originally in". Only
# treated as a conversion signal when at least one currency is present,
# so plain "too" elsewhere in the sentence doesn't trip this.
_IMPLICIT_CONVERSION_RE = re.compile(r"\b(too|also|as well)\b", re.IGNORECASE)


def _currencies_mentioned(user_query: str) -> set[str]:
    """Return the set of distinct currencies (as codes) found in the raw
    user query, via both 3-letter ISO codes and common symbols."""
    found: set[str] = {m.upper() for m in _CURRENCY_CODE_RE.findall(user_query)}
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in user_query:
            found.add(code)
    return found


def _exchange_rate_required(user_query: str) -> bool:
    """Deterministic mirror of SYSTEM_PROMPT's get_exchange_rate rule:
    required when the query mentions two different currencies, OR
    explicitly asks for a conversion/exchange rate."""
    currencies = _currencies_mentioned(user_query)
    if len(currencies) >= 2:
        return True
    if _CONVERSION_KEYWORDS_RE.search(user_query):
        return True
    if currencies and _IMPLICIT_CONVERSION_RE.search(user_query):
        return True
    return False


def get_expected_tools(user_query: str) -> list[str]:
    """Deterministically derive the list of tools required for this
    (already-confirmed-valid) travel query, per the existing tool policy.
    This does NOT ask the LLM to plan ahead — it is only used after the
    ReAct loop has finished, to check what it should have called."""
    expected = list(ALWAYS_REQUIRED_TOOLS)
    if _exchange_rate_required(user_query):
        expected.append("get_exchange_rate")
    return sorted(expected)


def get_executed_tools(tool_call_log: list[dict[str, Any]]) -> list[str]:
    """Tool names that actually went through mcp_client.call_tool and were
    logged (duplicates collapse — a tool called twice still counts once)."""
    return sorted({entry["tool"] for entry in tool_call_log})


def get_missing_tools(expected_tools: list[str], executed_tools: list[str]) -> list[str]:
    """Deterministic set-difference: required tools that were never executed."""
    return sorted(set(expected_tools) - set(executed_tools))


def _is_invalid_query_response(msg: dict[str, Any]) -> bool:
    """True if the model's final (no-tool-call) reply is the SYSTEM_PROMPT's
    INVALID_QUERY sentinel. Completeness validation must not run for these —
    a greeting or off-topic message was never supposed to trigger tools."""
    return (msg.get("content") or "").strip().startswith("INVALID_QUERY")


MAX_TOOL_COMPLETENESS_RETRIES: int = 2

RECOVERY_PROMPT_TEMPLATE: str = """You are continuing a travel research task.

Original user request:
{user_query}

The following required tools were not successfully executed:
{missing_tools}

Already executed tools:
{executed_tools}

You MUST call ONLY the missing tools listed above.
Do not repeat tools that have already been successfully executed.

Use the available native function-calling mechanism.
After calling the missing tools, do not introduce unrelated tool calls."""


def _build_recovery_prompt(user_query: str, missing_tools: list[str],
                            executed_tools: list[str]) -> str:
    return RECOVERY_PROMPT_TEMPLATE.format(
        user_query=user_query,
        missing_tools=", ".join(missing_tools) if missing_tools else "(none)",
        executed_tools=", ".join(executed_tools) if executed_tools else "(none)",
    )


def _log_completeness_check(attempt: int, expected: list[str],
                             executed: list[str], missing: list[str]) -> None:
    print(f"[tool-completeness] attempt={attempt} "
          f"expected={expected} executed={executed} missing={missing} "
          f"complete={not missing}")


async def _execute_tool_calls(tool_calls: list[dict[str, Any]], user_query: str,
                               mcp_client, tool_call_log: list[dict[str, Any]],
                               messages: list[dict[str, Any]]) -> None:
    """Execute a batch of native tool_calls via the existing MCP flow,
    appending to tool_call_log and messages exactly as the main ReAct loop
    does. Shared by the main loop and the completeness-recovery step so
    both go through the identical call/log/trim path."""
    for call in tool_calls:
        name: str = call["function"]["name"]
        args: dict[str, Any] = call["function"]["arguments"]

        if name == "get_weather":
            _normalize_weather_date(args, user_query)

        print(f"  [tool call] {name}({args})")
        _t0 = time.perf_counter()
        result: str = await mcp_client.call_tool(name, args)
        _elapsed = time.perf_counter() - _t0
        tool_call_log.append({
            "tool": name,
            "args": args,
            "result": result,
            "elapsed_seconds": round(_elapsed, 4),
        })

        trimmed = _trim_tool_result(result)
        messages.append({
            "role": "tool",
            "content": trimmed,
            "name": name,
            "tool_call_id": call.get("id"),
        })


async def _run_completeness_recovery(user_query: str, mcp_client, tools,
                                      messages: list[dict[str, Any]],
                                      tool_call_log: list[dict[str, Any]]) -> None:
    """Deterministic tool-call completeness validation + recovery.

    Runs after the ReAct loop has stopped requesting tools for a *valid*
    travel query. Compares the deterministically-derived expected tool set
    against what actually executed; if anything is missing, asks the LLM
    (via one extra request) to call only the missing tools, executes them
    through the normal MCP flow, and re-checks — up to
    MAX_TOOL_COMPLETENESS_RETRIES times. Mutates messages/tool_call_log
    in place; never raises on incompleteness (logs and returns instead).
    """
    expected = get_expected_tools(user_query)

    for attempt in range(MAX_TOOL_COMPLETENESS_RETRIES + 1):
        executed = get_executed_tools(tool_call_log)
        missing = get_missing_tools(expected, executed)
        _log_completeness_check(attempt, expected, executed, missing)

        if not missing:
            return

        if attempt >= MAX_TOOL_COMPLETENESS_RETRIES:
            print(f"[tool-completeness] giving up after {attempt} recovery "
                  f"attempt(s); still missing: {missing}. Returning existing "
                  f"results without these tools.")
            return

        recovery_prompt = _build_recovery_prompt(user_query, missing, executed)
        messages.append({"role": "user", "content": recovery_prompt})

        response = llm.chat(messages=messages, tools=tools, model=ORCH_MODEL)
        msg = response["message"]
        messages.append(msg)

        recovered_calls = msg.get("tool_calls") or []
        if not recovered_calls:
            print(f"  [tool-completeness] recovery attempt {attempt+1}: "
                  f"model made no tool calls. Model said: "
                  f"{msg.get('content', '')[:200]!r}")
            continue

        # Deterministic guard: only execute calls the LLM was actually asked
        # for. The recovery prompt tells the model to call ONLY the missing
        # tools, but nothing stops it from also repeating an already-executed
        # tool or calling something unrelated — so we filter in Python
        # rather than trusting the model to have followed the instruction.
        allowed_calls = [
            call for call in recovered_calls
            if call["function"]["name"] in missing
        ]
        ignored_calls = [
            call for call in recovered_calls
            if call["function"]["name"] not in missing
        ]
        if ignored_calls:
            ignored_names = [call["function"]["name"] for call in ignored_calls]
            print(f"  [tool-completeness] recovery attempt {attempt+1}: "
                  f"ignoring unrequested tool call(s) {ignored_names} — "
                  f"not in missing list {missing}")

        if not allowed_calls:
            continue

        await _execute_tool_calls(allowed_calls, user_query, mcp_client,
                                   tool_call_log, messages)

    # Final observability line if the loop exits via the retry cap above
    # without an early return (kept for clarity; unreachable in practice
    # since the attempt>=MAX branch already returns).


async def run_orchestrator(user_query: str, mcp_client, max_turns: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tools = await mcp_client.list_tools_for_ollama()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_query},
    ]

    tool_call_log: list[dict[str, Any]] = []

    for turn in range(max_turns):
        response = llm.chat(messages=messages, tools=tools, model=ORCH_MODEL) #native function calling
        msg = response["message"]
        messages.append(msg)

        if not msg.get("tool_calls"):
            print(f"  [orchestrator] turn {turn+1}: no tool called. "
                  f"Model said: {msg.get('content', '')[:200]!r}")

            # Deterministic tool-call completeness validation. Only applies
            # to valid travel requests — the model's own INVALID_QUERY
            # sentinel (no tools were ever expected) is left untouched, so
            # existing invalid-query behavior is unaffected.
            if not _is_invalid_query_response(msg):
                await _run_completeness_recovery(
                    user_query, mcp_client, tools, messages, tool_call_log)

            return messages, tool_call_log

        # Trim the copy sent back into the LLM history: the ReAct loop
        # re-sends the whole conversation every turn, so huge tool
        # results (weather text, attraction lists) blow through Groq's
        # per-minute token budget. The FULL result stays in tool_call_log
        # for the guards/writer/reflection, which don't care about the
        # LLM-history copy.
        await _execute_tool_calls(msg["tool_calls"], user_query, mcp_client,
                                   tool_call_log, messages)

    return messages, tool_call_log