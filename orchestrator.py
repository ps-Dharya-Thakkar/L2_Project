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

import ollama

ORCH_MODEL = "qwen2.5:3b-instruct"  # llama3.2:3b was tested and unreliable at emitting tool_calls in this setup — qwen2.5:3b-instruct fires tools correctly and is still fast since it only handles the decision loop

SYSTEM_PROMPT = """You are a travel-planning research agent.
Your ONLY job is to gather facts needed to plan a trip. You do NOT write the
final itinerary yourself — a separate writer will do that.

FIRST, check if the user's message is actually a travel-planning request
(mentions or clearly implies a destination, trip, or travel-related need).
If it is NOT — e.g. it's a greeting like "Hello", a random word, or
anything unrelated to planning travel — reply with EXACTLY this text and
nothing else, and do not call any tool:
INVALID_QUERY: <one short sentence saying why this isn't a travel request>

If it IS a valid travel request, continue as below.

Call a tool when the query needs real-world, current, or location-specific
data:
- weather: call get_weather(city, date, region). If the user gave or
  implied a travel date/month, work out an approximate YYYY-MM-DD and pass
  it — this gets you a real live forecast when possible, or real historical
  data instead of a guess when the date is too far out.
- currency exchange rates: get_exchange_rate
- confirming/looking up a place: geocode_city(city, region)
- get_nearby_attractions(city, region): ALWAYS call this before the writer
  needs to name specific temples/villages/viewpoints/markets — it returns
  real, distance-verified places so the plan doesn't reference attractions
  that are actually hours away.

Some city names are ambiguous (e.g. there is a "Manali" in Himachal Pradesh
AND a different "Manali" near Chennai). Always pass the `region` argument
(state/country) when you know it, to get the correct location.

CRITICAL RULE: never write text like "I will fetch the weather" or "let me
check the exchange rate" — that is not a tool call and wastes a turn. If you
need a tool, call it immediately in the same turn. Only write plain text
once you are done calling tools and are ready to summarize.

Do NOT call a tool for things that are subjective, creative, or things you
already know, e.g. "suggest a romantic theme for day 2" or "what's a good
souvenir to bring back" — reason about those yourself, no tool needed.

Once you have gathered enough information (or immediately, if no tool is
needed at all), reply with a short plain-text summary of what you found and
stop calling tools.
"""


async def run_orchestrator(user_query: str, mcp_client, max_turns: int = 5):
    tools = await mcp_client.list_tools_for_ollama()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_query},
    ]

    tool_call_log = []

    for turn in range(max_turns):
        response = ollama.chat(model=ORCH_MODEL, messages=messages, tools=tools)
        msg = response["message"]
        messages.append(msg)

        if not msg.get("tool_calls"):
            # Model decided it has enough info — exit the loop.
            # DEBUG: if this triggers when you expected a tool call, print
            # msg["content"] below to see the model's reasoning/refusal.
            print(f"  [orchestrator] turn {turn+1}: no tool called. "
                  f"Model said: {msg.get('content', '')[:200]!r}")
            return messages, tool_call_log

        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            print(f"  [tool call] {name}({args})")

            result = await mcp_client.call_tool(name, args)
            tool_call_log.append({"tool": name, "args": args, "result": result})

            messages.append({
                "role": "tool",
                "content": result,
                "name": name,
            })

    return messages, tool_call_log