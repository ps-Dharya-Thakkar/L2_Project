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
import os
import sys
import time
import llm
import query_cache
from mcp_client import MCPToolClient
from orchestrator import SYSTEM_PROMPT, ORCH_MODEL, run_orchestrator, _normalize_weather_date
from writer_agent import run_writer
from reflection_agent import run_reflection

MAX_TOOL_REPROMPT_ROUNDS: int = 2  # how many re-requests before giving up


def _get_place_validator():
    """Layer 3 hallucination validation: geocodes candidate place names to
    prove they really exist near the destination. Optional — if the module or
    its deps are unavailable, reflection simply falls back to rule-only."""
    try:
        from place_validation import PlaceValidator
        return PlaceValidator()
    except Exception:
        return None


def _user_wants_conversion(query: str) -> bool:
    """Detect whether the user's query explicitly asks for a currency
    conversion (e.g. 'also show in USD', 'convert to EUR'). A query counts
    only if TWO or more distinct currencies are mentioned, or an explicit
    conversion verb is used. 'budget in INR' alone is NOT a conversion."""
    q = query.lower()

    currency_aliases = {
        "usd": ("usd", "dollar"),
        "eur": ("eur", "euro"),
        "gbp": ("gbp", "pound"),
        "inr": ("inr", "rupee"),
        "aed": ("aed", "dirham"),
        "jpy": ("jpy", "yen"),
        "chf": ("chf", "franc"),
        "aud": ("aud", "australian dollar"),
        "cad": ("cad", "canadian dollar"),
    }
    found = set()
    for code, (codeword, name) in currency_aliases.items():
        if codeword in q or name in q:
            found.add(code)

    if len(found) >= 2:
        return True

    if "convert" in q or "conversion" in q:
        return True

    if found and ("show" in q or "also" in q or "in both" in q):
        return True

    return False


def _check_ollama_models() -> list[str]:
    """Return a list of missing model names. Empty list means all good.
    Returns [] when using the Groq cloud provider (no local model needed)."""
    import llm
    if llm.provider() == "groq":
        return []

    import ollama
    try:
        available = {m.model for m in ollama.list().models}
    except Exception as e:
        return [f"Cannot connect to Ollama: {e}"]
    required = ["qwen2.5:7b-instruct"]
    return [m for m in required if m not in available]


def _llm_judge_enabled() -> bool:
    """Whether the LLM-as-a-judge relevance/accuracy/completeness evaluation
    should run. Runs by DEFAULT so the normal reviewer/demo flow gets
    relevance scoring without needing to set any environment variable.
    Set RUN_LLM_JUDGE=0 (or false/no) to explicitly disable it, e.g. for
    fast offline unit tests that mock the judge instead of calling it."""
    return os.environ.get("RUN_LLM_JUDGE", "1").strip().lower() not in ("0", "false", "no")


def _empty_latency() -> dict:
    """Default shape of the component-latency dict populated by plan_trip().
    Every stage starts measured at 0.0 except revision_writer_seconds, which
    stays None unless reflection actually requests a revision (that stage
    doesn't run on every query, so 0.0 there would be misleading)."""
    return {
        "mcp_connect_seconds": 0.0,
        "orchestrator_seconds": 0.0,
        "tool_completion_seconds": 0.0,
        "writer_seconds": 0.0,
        "reflection_seconds": 0.0,
        "revision_writer_seconds": None,
        "evaluation_seconds": 0.0,
        "total_seconds": 0.0,
    }


def _required_tools(user_query: str) -> set[str]:
    """Plain-Python rule for which tools MUST have been called for a query.
    geocode/weather/attractions are always needed; FX only when the user
    asked for a conversion."""
    required = {"geocode_city", "get_weather", "get_nearby_attractions"}
    if _user_wants_conversion(user_query):
        required.add("get_exchange_rate")
    return required


def _missing_required_tools(user_query: str, tool_log: list[dict]) -> set[str]:
    """Tools that SHOULD have been called but weren't. Pure Python — this is
    the reviewer's 'plain python fallback' that catches the LLM forgetting
    a required tool."""
    called = {t["tool"] for t in tool_log}
    return _required_tools(user_query) - called


async def _complete_missing_tool_calls(user_query: str, mcp_client,
                                       tool_log: list[dict],
                                       max_rounds: int = MAX_TOOL_REPROMPT_ROUNDS) -> list[dict]:
    """If the orchestrator forgot a required tool, re-request the LLM to make
    exactly that call, execute it via MCP, and append to the shared tool_log.
    Caps at max_rounds re-prompts so a stubborn model can't loop forever."""
    tools = await mcp_client.list_tools_for_ollama()

    for _ in range(max_rounds):
        missing = _missing_required_tools(user_query, tool_log)
        if not missing:
            break
        names = ", ".join(sorted(missing))
        print(f"  [completeness] plain-Python check found missing tool(s): "
              f"{names} — re-prompting model...")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_query},
            {"role": "assistant", "content":
                "I gathered some data but I still need to call these "
                f"required tools I missed: {names}. Calling them now."},
        ]
        response = llm.chat(messages=messages, tools=tools, model=ORCH_MODEL)
        msg = response["message"]
        made_call = False
        for call in msg.get("tool_calls") or []:
            name: str = call["function"]["name"]
            args: dict = call["function"]["arguments"]
            if name not in missing:
                continue  # ignore anything not requested
            if name == "get_weather":
                _normalize_weather_date(args, user_query)
            print(f"  [tool call (completeness re-prompt)] {name}({args})")
            _t0 = time.perf_counter()
            result: str = await mcp_client.call_tool(name, args)
            _elapsed = time.perf_counter() - _t0
            tool_log.append({
                "tool": name,
                "args": args,
                "result": result,
                "elapsed_seconds": round(_elapsed, 4),
            })
            made_call = True
        if not made_call:
            break  # model refused / returned nothing useful — don't loop forever
    return tool_log


async def plan_trip(user_query: str) -> tuple:
    """Returns (itinerary_markdown, tool_log, guard_notes, reflection_result,
    metrics) so both the CLI and the Streamlit UI can use the same underlying
    logic. The invalid-query rejection returns a 4-tuple (no metrics)."""
    total_start = time.perf_counter()
    latency = _empty_latency()

    client = MCPToolClient(server_script="mcp_server.py")
    try:
        _t0 = time.perf_counter()
        await client.connect()
        latency["mcp_connect_seconds"] = round(time.perf_counter() - _t0, 4)

        print(f"\nUser query: {user_query}\n")
        print("Orchestrator thinking...\n")

        _t0 = time.perf_counter()
        messages, tool_log = await run_orchestrator(user_query, client)
        latency["orchestrator_seconds"] = round(time.perf_counter() - _t0, 4)

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

        # Tool-call completeness fallback (plain Python): if the model forgot
        # any required tool (e.g. skipped weather or never called FX after a
        # conversion request), re-prompt it to make that exact call instead
        # of silently letting the guards forbid the data.
        _t0 = time.perf_counter()
        tool_log = await _complete_missing_tool_calls(user_query, client, tool_log)
        latency["tool_completion_seconds"] = round(time.perf_counter() - _t0, 4)
        missing_after = _missing_required_tools(user_query, tool_log)
        if missing_after:
            print(f"  [completeness] still missing after re-prompts: "
                  f"{sorted(missing_after)} — guards will handle it")
        else:
            print("  [completeness] all required tools were called ✓")

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

        fx_calls = [t for t in tool_log if t["tool"] == "get_exchange_rate"]
        fx_ok = any(
            t["result"].startswith("1 ") for t in fx_calls
        )
        if "get_exchange_rate" not in called_tools or not fx_ok:
            guards.append(
                "*** HARD CONSTRAINT — NO EXCHANGE RATE WAS FETCHED ***\n"
                "You are FORBIDDEN from stating any specific exchange rate or "
                "currency conversion figure anywhere in this itinerary — none "
                "was fetched. Give budget estimates in the currency the user "
                "asked about only; do not convert to any other currency."
            )
            if "get_exchange_rate" not in called_tools:
                msg = "Exchange rate not called — forbidding invented conversion rates"
            else:
                msg = "Exchange rate lookup failed — forbidding invented conversion rates"
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
        _t0 = time.perf_counter()
        itinerary = run_writer(user_query, research_notes)
        latency["writer_seconds"] = round(time.perf_counter() - _t0, 4)

        print("\nReflection agent evaluating draft...\n")
        validator = _get_place_validator()
        _t0 = time.perf_counter()
        reflection_result = run_reflection(user_query, research_notes, itinerary,
                                           validator=validator)
        latency["reflection_seconds"] = round(time.perf_counter() - _t0, 4)
        print(f"  [reflection] {reflection_result[:200]}")

        if reflection_result.startswith("REVISE"):
            print("\nReflection requested revision — regenerating...\n")
            _t0 = time.perf_counter()
            revised_notes = research_notes + (
                "\n\n*** REFLECTION FEEDBACK — the previous draft had the "
                "following issues that MUST be fixed in this revision:\n"
                f"{reflection_result}\n***"
            )
            itinerary = run_writer(user_query, revised_notes)
            second_reflection = run_reflection(user_query, revised_notes, itinerary,
                                               validator=validator)
            latency["revision_writer_seconds"] = round(time.perf_counter() - _t0, 4)
            print(f"  [reflection after revision] {second_reflection[:200]}")
            reflection_result = reflection_result + " | After revision: " + second_reflection

        # Evaluation & monitoring (mentor review requirement): compute the
        # metrics for this produced plan and log them. LLM-as-a-judge
        # relevance/accuracy/completeness runs by default (see
        # _llm_judge_enabled); if the judge model/provider is unavailable,
        # evaluation.llm_as_judge() catches the error and returns an
        # {"error": ...} marker instead of raising, so the planner always
        # completes.
        import evaluation
        _t0 = time.perf_counter()
        metrics = evaluation.evaluate(
            user_query, itinerary, research_notes,
            with_llm_judge=_llm_judge_enabled(),
        )
        latency["evaluation_seconds"] = round(time.perf_counter() - _t0, 4)

        latency["total_seconds"] = round(time.perf_counter() - total_start, 4)
        metrics["latency"] = latency

        print(f"  [evaluation] similarity={metrics.get('similarity_score')} "
              f"groundedness={metrics.get('groundedness')} "
              f"accuracy={metrics.get('response_accuracy', {}).get('score')} "
              f"precision={metrics.get('retrieval', {}).get('precision')} "
              f"allowlist_utilization={metrics.get('retrieval', {}).get('allowlist_utilization')} "
              f"total_seconds={latency['total_seconds']}")

        return itinerary, tool_log, guard_notes, reflection_result, metrics
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
    cached = query_cache.get(query)
    if cached is not None:
        print("\n[query-cache] Same query was planned earlier today — returning "
              "the saved itinerary instantly (no LLM calls).")
        print("\n" + "=" * 60)
        print(cached["itinerary"])
        print("=" * 60)
        sys.exit(0)
    result, _tool_log, _guard_notes, _reflection, metrics = asyncio.run(plan_trip(query))
    # The actual measured end-to-end execution time, computed inside
    # plan_trip() and stored at metrics["latency"]["total_seconds"] — no
    # longer hardcoded to 0.0.
    _total_elapsed = (metrics or {}).get("latency", {}).get("total_seconds", 0.0)
    query_cache.set(query, {
        "query": query,
        "itinerary": result,
        "tool_log": _tool_log,
        "guard_notes": _guard_notes,
        "reflection_result": _reflection,
        "metrics": metrics,
        "elapsed": _total_elapsed,
    })
    print("\n" + "=" * 60)
    print(result)
    print("=" * 60)

    if metrics:
        print("\n[Evaluation metrics]")
        print(f"  Similarity (itinerary vs research): {metrics['similarity_score']}")
        print(f"  Groundedness: {metrics['groundedness']}")
        print(f"  Faithfulness: {metrics['faithfulness']}")
        acc = metrics.get("response_accuracy", {})
        print(f"  Response accuracy: {acc.get('score')}")
        ret = metrics.get("retrieval", {})
        print(f"  Retrieval precision (retrieved -> used in itinerary): {ret.get('precision')}")
        print(f"  Retrieval allowlist_utilization: {ret.get('allowlist_utilization')}  "
              f"precision@5: {ret.get('precision@5')}")
        if metrics.get("llm_judge"):
            j = metrics["llm_judge"]
            if "error" in j:
                print(f"  LLM-as-judge: unavailable ({j['error']})")
            else:
                print(f"  LLM-as-judge: relevance={j.get('relevance')} "
                      f"accuracy={j.get('accuracy')} completeness={j.get('completeness')} "
                      f"overall={j.get('overall')}")

        lat = metrics.get("latency", {})
        if lat:
            print("\n[Component latency]")
            print(f"  MCP connect:        {lat.get('mcp_connect_seconds')}s")
            print(f"  Orchestrator:       {lat.get('orchestrator_seconds')}s")
            print(f"  Tool completion:    {lat.get('tool_completion_seconds')}s")
            print(f"  Writer:             {lat.get('writer_seconds')}s")
            print(f"  Reflection:         {lat.get('reflection_seconds')}s")
            if lat.get("revision_writer_seconds") is not None:
                print(f"  Revision writer:    {lat.get('revision_writer_seconds')}s")
            print(f"  Evaluation:         {lat.get('evaluation_seconds')}s")
            print(f"  Total:              {lat.get('total_seconds')}s")

        if _tool_log:
            print("\n" + "=" * 60)
            print("⏱️ TOOL PERFORMANCE")
            print("=" * 60)

            total_tool_time = 0.0

            for t in _tool_log:
                tool_name = t["tool"]
                elapsed = t.get("elapsed_seconds")

                if elapsed is not None:
                    elapsed = float(elapsed)
                    total_tool_time += elapsed

                    print(
                        f"  {tool_name:<30} "
                        f"{elapsed:.2f}s"
                    )
                else:
                    print(
                        f"  {tool_name:<30} "
                        f"n/a"
                    )

            print("-" * 60)
            print(
                f"  {'TOTAL TOOL EXECUTION TIME':<30} "
                f"{total_tool_time:.2f}s"
            )
            print("=" * 60)