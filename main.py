"""
ENTRY POINT.

Flow:
  user query
      -> Orchestrator Agent (decides + runs tools via MCP, ReAct loop)
      -> Writer Agent (formats everything into a final itinerary)
      -> printed to terminal
"""

import asyncio
from mcp_client import MCPToolClient
from orchestrator import run_orchestrator
from writer_agent import run_writer


async def plan_trip(user_query: str):
    """Returns (itinerary_markdown, tool_log, guard_notes) so both the CLI
    and the Streamlit UI can use the same underlying logic."""
    client = MCPToolClient(server_script="mcp_server.py")
    await client.connect()

    print(f"\nUser query: {user_query}\n")
    print("Orchestrator thinking...\n")

    messages, tool_log = await run_orchestrator(user_query, client)

    # --- Reject non-travel queries before ever reaching the Writer --------
    # The Orchestrator is instructed to reply with "INVALID_QUERY: <reason>"
    # for anything that isn't actually a travel-planning request. We check
    # for that marker IN CODE (not just trust the prompt) and short-circuit
    # here -- the Writer never even runs, so it can't invent a fake trip.
    last_content = (messages[-1].get("content") or "").strip()
    if last_content.startswith("INVALID_QUERY"):
        await client.close()
        reason = last_content.split(":", 1)[1].strip() if ":" in last_content else \
            "This doesn't look like a travel-planning request."
        print(f"  [guard] rejected as non-travel query: {reason}")
        rejection = (
            f"⚠️ **This doesn't look like a travel-planning request.** {reason}\n\n"
            f"Try something like: *\"Plan a 3-day trip to Goa for a couple in "
            f"August, budget in INR.\"*"
        )
        return rejection, [], [f"Query rejected as non-travel: {reason}"]

    if tool_log:
        print(f"\n{len(tool_log)} tool call(s) made:")
        for t in tool_log:
            preview = t["result"][:80].replace("\n", " ")
            print(f"  - {t['tool']}({t['args']}) -> {preview}...")
    else:
        print("No tools were needed for this query — answered from reasoning alone.")

    research_notes = "\n".join(t["result"] for t in tool_log)

    # --- Hard guards against fabricated tool-style data --------------------
    # We don't rely on the Writer's prompt alone. For each tool, if it was
    # NEVER called this turn, we inject an unmissable directive forbidding
    # the Writer from inventing data that would look like that tool's output
    # (specific temperatures, "historical/live forecast" phrasing, exchange
    # rates). This is checked IN CODE so it can't be silently skipped.
    called_tools = {t["tool"] for t in tool_log}
    guards = []
    guard_notes = []  # short human-readable versions, for display in the UI

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
    elif attraction_ok:
        # Re-extract and restate the verified place names as an explicit
        # allow-list in code, so the Writer can't lose the constraint by
        # paraphrasing the tool's sentence -- this is a plain, unambiguous
        # list sitting right next to the rule that references it.
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
            "clearly-labeled general knowledge statement, e.g. 'No weather "
            "data gathered — <city> is generally <season description> around "
            "this time, based on general knowledge, not fetched data.'"
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

    if guards:
        research_notes += "\n\n" + "\n\n".join(guards)

    print("\nWriter agent drafting itinerary...\n")
    itinerary = run_writer(user_query, research_notes)

    await client.close()
    return itinerary, tool_log, guard_notes


if __name__ == "__main__":
    query = input("Where do you want to go / what do you want planned?\n> ")
    result, _tool_log, _guard_notes = asyncio.run(plan_trip(query))
    print("\n" + "=" * 60)
    print(result)
    print("=" * 60)