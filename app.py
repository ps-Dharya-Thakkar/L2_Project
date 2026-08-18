"""
STREAMLIT UI — a thin visual layer over the exact same agent pipeline used
by main.py (CLI). No agent logic lives here; this only calls plan_trip()
and displays what comes back.

Session features:
  - history: every plan is stored in st.session_state so you can browse
    past itineraries without re-running the pipeline.
  - provider/cache status shown in the sidebar.
  - download each itinerary as Markdown.
"""

import asyncio
import time

import streamlit as st

import query_cache
from main import plan_trip

st.set_page_config(page_title="AI Travel Planner", page_icon="🧳", layout="wide")

if "history" not in st.session_state:
    st.session_state.history = []


def _provider_label() -> str:
    import llm
    try:
        return llm.provider().upper()
    except Exception:
        return "ollama"


def _render_entry(entry: dict) -> None:
    """Draw one completed plan (tool log, guards, reflection, itinerary)."""
    itinerary = entry["itinerary"]
    tool_log = entry["tool_log"]
    guard_notes = entry["guard_notes"]
    reflection_result = entry["reflection_result"]

    st.markdown(f"**Query:** {entry['query']}")
    if entry.get("elapsed"):
        st.caption(f"Completed in {entry['elapsed']:.1f}s")

    if tool_log:
        with st.expander(
            f"🧠 Agent reasoning — {len(tool_log)} tool call(s) made", expanded=False
        ):
            for t in tool_log:
                st.markdown(f"**`{t['tool']}`**`({t['args']})`")
                st.code(t["result"], language="text")
    else:
        st.info(
            "No tools were needed for this query — the Orchestrator "
            "answered from reasoning alone."
        )

    if guard_notes:
        with st.expander("🛡️ Hallucination guards triggered", expanded=False):
            st.caption(
                "These fire automatically in code whenever a fact-providing "
                "tool wasn't called or failed, to stop the Writer agent from "
                "inventing data that would look real."
            )
            for note in guard_notes:
                st.warning(note)

    if reflection_result:
        with st.expander("🧪 Reflection evaluation", expanded=False):
            is_approved = reflection_result.startswith("APPROVED")
            if is_approved:
                st.success("Draft itinerary approved by reflection agent.")
            else:
                st.warning(f"Draft required revision:\n\n{reflection_result}")

    st.markdown("---")
    st.markdown(itinerary)

    st.download_button(
        "⬇️ Download itinerary (.md)",
        data=itinerary,
        file_name=f"itinerary_{int(time.time())}.md",
        mime="text/markdown",
        use_container_width=True,
    )


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("⚙️ Status")
    st.caption(f"**LLM provider:** {_provider_label()}")
    st.caption("**Cache:** disk-backed (geocode/weather/FX/attractions)")
    st.caption(f"**Cached queries:** {query_cache.cached_query_count()}")

    st.header("🗂️ Session history")
    if st.session_state.history:
        st.caption(f"{len(st.session_state.history)} plan(s) in this session.")
        for i, e in enumerate(st.session_state.history):
            if st.button(
                f"{i + 1}. {e['query'][:45]}{'...' if len(e['query']) > 45 else ''}",
                key=f"hist_{i}",
                use_container_width=True,
            ):
                st.session_state.selected = i
                st.rerun()
        if st.button("🧹 Clear history", use_container_width=True):
            st.session_state.history = []
            st.session_state.pop("selected", None)
            st.rerun()
    else:
        st.caption("No plans yet — run a query to build history.")

# ---------------------------------------------------------------- main
st.title("🧳 AI Travel Planner")
st.caption(
    "Multi-agent system — Orchestrator + Writer + Reflection agents, running locally via "
    "Ollama or fast Groq, with tool access through MCP (weather, currency, geocoding, "
    "verified nearby attractions)."
)

query = st.text_area(
    "Where do you want to go / what do you want planned?",
    height=100,
    placeholder=(
        "e.g. Plan a 4-day trip to Manali, Himachal Pradesh for a couple "
        "from Delhi in December. Budget in INR, also show cost in USD."
    ),
)

plan_clicked = st.button("Plan my trip", type="primary")

if plan_clicked:
    if not query.strip():
        st.warning("Please enter a travel query first.")
    else:
        started = time.time()

        cached = query_cache.get(query)
        if cached is not None:
            entry = {**cached, "elapsed": time.time() - started}
            st.session_state.history.append(entry)
            st.session_state.selected = len(st.session_state.history) - 1
            st.success(
                "⚠️ Served from query cache — same query was planned earlier "
                "today, so no LLM calls were needed. (Instant re-run.)"
            )
            _render_entry(entry)
            st.rerun()

        with st.spinner("Orchestrator is thinking and gathering research..."):
            itinerary, tool_log, guard_notes, reflection_result = asyncio.run(
                plan_trip(query)
            )
        elapsed = time.time() - started
        entry = {
            "query": query,
            "itinerary": itinerary,
            "tool_log": tool_log,
            "guard_notes": guard_notes,
            "reflection_result": reflection_result,
            "elapsed": elapsed,
        }
        query_cache.set(query, entry)
        st.session_state.history.append(entry)
        st.session_state.selected = len(st.session_state.history) - 1
        _render_entry(entry)
        st.rerun()

elif "selected" in st.session_state and st.session_state.history:
    idx = st.session_state.selected
    if 0 <= idx < len(st.session_state.history):
        _render_entry(st.session_state.history[idx])

st.markdown("---")
st.caption(
    "Budget figures are LLM estimates, not live prices. Weather is either "
    "a real live forecast (trips within ~15 days) or real historical data "
    "from one year ago (further out) — never an invented guess."
)
