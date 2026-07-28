"""
STREAMLIT UI — a thin visual layer over the exact same agent pipeline used
by main.py (CLI). No agent logic lives here; this only calls plan_trip()
and displays what comes back.

Run with:
    streamlit run app.py
"""

import asyncio
import streamlit as st

from main import plan_trip

st.set_page_config(page_title="AI Travel Planner", page_icon="🧳", layout="centered")

st.title("🧳 AI Travel Planner")
st.caption(
    "Multi-agent system — Orchestrator + Writer agents, running locally via "
    "Ollama, with tool access through MCP (weather, currency, geocoding, "
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
        with st.spinner("Orchestrator is thinking and gathering research..."):
            itinerary, tool_log, guard_notes = asyncio.run(plan_trip(query))

        # --- Agent reasoning: which tools were called and why ------------
        if tool_log:
            with st.expander(
                f"🔧 Agent reasoning — {len(tool_log)} tool call(s) made", expanded=False
            ):
                for t in tool_log:
                    st.markdown(f"**`{t['tool']}`**`({t['args']})`")
                    st.code(t["result"], language="text")
        else:
            st.info(
                "No tools were needed for this query — the Orchestrator "
                "answered from reasoning alone."
            )

        # --- Hallucination guards, if any fired ---------------------------
        if guard_notes:
            with st.expander("🛡️ Hallucination guards triggered", expanded=False):
                st.caption(
                    "These fire automatically in code whenever a fact-providing "
                    "tool wasn't called or failed, to stop the Writer agent from "
                    "inventing data that would look real."
                )
                for note in guard_notes:
                    st.warning(note)

        # --- Final itinerary -----------------------------------------------
        st.markdown("---")
        st.markdown(itinerary)

st.markdown("---")
st.caption(
    "⚠️ Budget figures are LLM estimates, not live prices. Weather is either "
    "a real live forecast (trips within ~15 days) or real historical data "
    "from one year ago (further out) — never an invented guess."
)