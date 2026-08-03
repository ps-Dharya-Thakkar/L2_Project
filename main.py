"""
ENTRY POINT.

Flow:
  user query
      -> Orchestrator Agent (decides + runs tools via MCP, ReAct loop)
      -> Writer Agent (formats everything into a final itinerary)
      -> Reflection Agent (evaluates draft for factual accuracy, may revise)
      -> printed to terminal
"""

import asyncio
import sys
from mcp_client import MCPToolClient
from orchestrator import run_orchestrator
from writer_agent import run_writer
from reflection_agent import run_reflection


def _user_wants_conversion(query: str) -> bool:
    """Detect whether the user's query explicitly asks for a currency
    conversion (e.g. 'also show in USD', 'convert to EUR'). If the user
    only mentions one currency (e.g. 'budget in INR'), we should NOT
    display a converted amount even if the orchestrator fetched an
    exchange rate proactively."""
    q = query.lower()
    conversion_keywords = [
        "also show in", "also in", "convert to", "in usd", "in eur",
        "in dollars", "in pounds", "in euros", "show cost in",
        "show in", "convert to usd", "convert to eur", "convert to gbp",
        "both inr and usd", "both inr and", "inr to usd", "inr to",
        "in dollar", "also in dollars", "inr in usd",
    ]
    return any(kw in q for kw in conversion_keywords)


def _check_ollama_models() -> list[str]:
    """Return a list of missing model names. Empty list means all good."""
    import ollama
    try:
        available = {m.model for m in ollama.list().models}
    except Exception as e:
        return [f"Cannot connect to Ollama: {e}"]
    required = ["qwen2.5:7b-instruct"]
    return [m for m in required if m not in available]


async def plan_trip(user_query: str) -> tuple:
    """Returns (itinerary_markdown, tool_log, guard_notes, reflection_result)
    so both the CLI and the Streamlit UI can use the same underlying logic."""
    client = MCPToolClient(server_script="mcp_server.py")
    try:
        await client.connect()

        print(f"\nUser query: {user_query}\n")
        print("Orchestrator thinking...\n")

        messages, tool_log = await run_orchestrator(user_query, client)

        last_content = (messages[-1].get("content") or "").strip()
        if last_content.startswith("INVALID_QUERY"):
            reason = last_content.split(":", 1)[1].strip() if ":" in last_content else \
                "This doesn't look like a travel-planning request."
            print(f"  [guard] rejected as non-travel query: {reason}")
            rejection = (
                f"Note: This doesn't look like a travel-planning request. {reason}\n\n"
                f"Try something like: \"Plan a 3-day trip to Goa for a couple in "
                f"August, budget in INR.\""
            )
            return rejection, [], [f"Query rejected as non-travel: {reason}"], None

        if tool_log:
            print(f"\n{len(tool_log)} tool call(s) made:")
            for t in tool_log:
                preview = t["result"][:80].replace("\n", " ")
                print(f"  - {t['tool']}({t['args']}) -> {preview}...")
        else:
            print("No tools were needed for this query — answered from reasoning alone.")

        research_notes = "\n".join(t["result"] for t in tool_log)

        called_tools = {t["tool"] for t in tool_log}
        guards = []
        guard_notes = []

        attraction_calls = [t for t in tool_log if t["tool"] == "get_nearby_attractions"]
        attraction_ok = any(
            t["result"].startswith("Verified places near") for t in attraction_calls
        )
        if attraction_calls and not attraction_ok:
            guards.append(
                "*** HARD CONSTRAINT — ATTRACTION LOOKUP FAILED ***\n"
                "The nearby-attractions lookup did NOT return verified results. "
                "You are FORBIDDEN from naming any specific landmark, temple, "
                "fort, lake, market, or viewpoint by name anywhere in this "
                "itinerary. Use only generic descriptions instead, e.g. "
                "'a scenic lakeside spot', 'the old town market area', "
                "'a well-known local viewpoint'. Naming an unverified place is "
                "a critical error."
            )
            msg = "Attraction lookup failed — forcing generic place descriptions"
            guard_notes.append(msg)
            print(f"  [guard] {msg}")
        elif not attraction_calls:
            guards.append(
                "*** HARD CONSTRAINT — NO ATTRACTIONS WERE FETCHED ***\n"
                "The get_nearby_attractions tool was never called. You are "
                "FORBIDDEN from naming any specific landmark, temple, fort, "
                "lake, market, or viewpoint by name anywhere in this itinerary. "
                "Use only generic descriptions instead, e.g. 'a scenic lakeside "
                "spot', 'the old town market area', 'a well-known local "
                "viewpoint'. Naming an unverified place is a critical error."
            )
            msg = "Attractions tool not called — forcing generic place descriptions"
            guard_notes.append(msg)
            print(f"  [guard] {msg}")
        elif attraction_ok:
            best_call = next(t for t in attraction_calls
                              if t["result"].startswith("Verified places near"))
            place_lines = [ln.strip("- ").split(" (")[0]
                            for ln in best_call["result"].splitlines()[1:] if ln.strip()]
            guards.append(
                "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                + "\n".join(f"- {p}" for p in place_lines) +
                "\nAny landmark/temple/market/viewpoint name in your itinerary "
                "MUST be copied exactly from this list. Do not add any other "
                "specific place name from your own knowledge."
            )
            msg = f"Attraction lookup succeeded — {len(place_lines)} places allow-listed"
            guard_notes.append(msg)
            print(f"  [guard] {msg}")

        if "get_weather" not in called_tools:
            guards.append(
                "*** HARD CONSTRAINT — NO WEATHER DATA WAS FETCHED ***\n"
                "You are FORBIDDEN from stating any specific temperature number, "
                "degree range, rain percentage, or the phrases 'historical data' "
                "or 'live forecast' anywhere in this itinerary — none of that was "
                "actually fetched. For the Weather field, write only a plain, "
                "clearly-labeled general knowledge statement."
            )
            msg = "Weather tool not called — forbidding invented weather numbers"
            guard_notes.append(msg)
            print(f"  [guard] {msg}")

        if "get_exchange_rate" not in called_tools:
            guards.append(
                "*** HARD CONSTRAINT — NO EXCHANGE RATE WAS FETCHED ***\n"
                "You are FORBIDDEN from stating any specific exchange rate or "
                "currency conversion figure anywhere in this itinerary — none "
                "was fetched. Give budget estimates in the currency the user "
                "asked about only; do not convert to any other currency."
            )
            msg = "Exchange rate not called — forbidding invented conversion rates"
            guard_notes.append(msg)
            print(f"  [guard] {msg}")
        elif not _user_wants_conversion(user_query):
            guards.append(
                "*** HARD CONSTRAINT — USER DID NOT ASK FOR CONVERSION ***\n"
                "An exchange rate was fetched proactively, but the user's "
                "query did NOT request a currency conversion. You are "
                "FORBIDDEN from showing any converted amount (e.g. USD, EUR, "
                "or any other currency besides what the user asked about). "
                "Give budget estimates ONLY in the currency the user mentioned. "
                "Do not include a dual-currency column or any parenthetical "
                "conversion like '(~$XX)'. If the user asked about INR, show "
                "only INR."
            )
            msg = "Exchange rate fetched but user didn't ask for conversion — suppressing cross-currency display"
            guard_notes.append(msg)
            print(f"  [guard] {msg}")

        if guards:
            research_notes += "\n\n" + "\n\n".join(guards)

        print("\nWriter agent drafting itinerary...\n")
        itinerary = run_writer(user_query, research_notes)

        print("\nReflection agent evaluating draft...\n")
        reflection_result = run_reflection(user_query, research_notes, itinerary)
        print(f"  [reflection] {reflection_result[:200]}")

        if reflection_result.startswith("REVISE"):
            print("\nReflection requested revision — regenerating...\n")
            revised_notes = research_notes + (
                "\n\n*** REFLECTION FEEDBACK — the previous draft had the "
                "following issues that MUST be fixed in this revision:\n"
                f"{reflection_result}\n***"
            )
            itinerary = run_writer(user_query, revised_notes)
            second_reflection = run_reflection(user_query, revised_notes, itinerary)
            print(f"  [reflection after revision] {second_reflection[:200]}")
            reflection_result = reflection_result + " | After revision: " + second_reflection

        return itinerary, tool_log, guard_notes, reflection_result
    finally:
        await client.close()


if __name__ == "__main__":
    missing = _check_ollama_models()
    if missing:
        print("ERROR: Required Ollama model(s) not found:")
        for m in missing:
            print(f"  - {m}")
        print("\nRun the following command(s) to install:")
        for m in missing:
            if not m.startswith("Cannot"):
                print(f"  ollama pull {m}")
        sys.exit(1)

    query = input("Where do you want to go / what do you want planned?\n> ")
    result, _tool_log, _guard_notes, _reflection = asyncio.run(plan_trip(query))
    print("\n" + "=" * 60)
    print(result)
    print("=" * 60)
