"""
STREAMLIT UI — a thin visual layer over the exact same agent pipeline used
by main.py (CLI). No agent logic lives here; this only calls plan_trip()
and displays what comes back.

Designed for non-technical users:
  - One-click example queries.
  - Tabbed layout: itinerary first, then a friendly "Quality report" with
    color-coded gauges and a plain-language verdict, then the technical
    "How it was built" view (tools, guards, provider).
  - Session history + download, provider/cache status in the sidebar.
"""

import asyncio
import time

import streamlit as st

import query_cache
from main import plan_trip

st.set_page_config(page_title="AI Travel Planner", page_icon="🧳", layout="wide")

if "history" not in st.session_state:
    st.session_state.history = []

TOOL_LABELS = {
    "geocode_city": ("📍", "City location"),
    "get_weather": ("🌤️", "Weather data"),
    "get_exchange_rate": ("💱", "Currency exchange rate"),
    "get_nearby_attractions": ("🏛️", "Nearby attractions"),
}

EXAMPLE_QUERIES = [
    "Plan a 3-day trip to Udaipur, India in September for a couple, budget 25000 INR",
    "Plan a 5-day beach trip to Goa for a family in November, 60000 INR",
    "Plan a 3-day trip to Goa from 28th August 2026 for a couple, budget 20000 INR",
    "Plan a 7-day trip to Dubai for a family of four, budget 12000 AED, show the total breakdown in AED and INR",
    "Plan a 7-day trip covering Jaipur, Jodhpur, and Udaipur in October for a couple, budget 2500 AED, show the total in GBP and INR",
]


def _provider_label() -> str:
    import llm
    try:
        return llm.provider().upper()
    except Exception:
        return "ollama"


def _tool_label(name: str) -> str:
    icon, label = TOOL_LABELS.get(name, ("🔧", name))
    return f"{icon} **{label}**"

def _tool_performance(tool_log: list[dict]) -> None:
    """Display individual MCP tool-call timings."""

    if not tool_log:
        return

    st.markdown("### ⏱️ MCP Tool Performance")

    timed_calls = [
        t for t in tool_log
        if t.get("elapsed_seconds") is not None
    ]

    if not timed_calls:
        st.warning(
            "No tool timing data available. This may be an old cached result."
        )
        return

    total_tool_time = sum(
        float(t["elapsed_seconds"])
        for t in timed_calls
    )

    for index, t in enumerate(timed_calls, start=1):
        icon, label = TOOL_LABELS.get(
            t["tool"],
            ("🔧", t["tool"])
        )

        elapsed = float(t["elapsed_seconds"])

        col1, col2 = st.columns([3, 1])

        with col1:
            st.markdown(f"**{index}. {icon} {label}**")
            st.caption(f"Arguments: `{t.get('args', {})}`")

        with col2:
            st.metric(
                "Execution Time",
                f"{elapsed:.2f}s"
            )

    st.divider()

    st.metric(
        "⚡ Total Tool Execution Time",
        f"{total_tool_time:.2f}s"
    )


def _pct(value) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _verdict(metrics: dict) -> tuple[str, str]:
    """Return (verdict message, streamlit alert type) in plain language."""
    if not metrics or "error" in metrics:
        return ("No quality data for this plan.", "info")
    acc = _pct(metrics.get("response_accuracy", {}).get("score", 1.0))
    grounded = _pct(metrics.get("groundedness", 1.0))
    scores = [acc, grounded]
    ret = metrics.get("retrieval", {})
    if ret.get("retrieved_count"):
        scores.append(_pct(ret.get("precision")))
    judge = metrics.get("llm_judge")
    if judge and "error" not in judge and judge.get("overall") is not None:
        scores.append(_pct(float(judge["overall"]) / 10.0))
    avg = sum(scores) / len(scores)
    if avg >= 0.9:
        return ("✅ Excellent quality — every fact in your itinerary was verified against real data.", "success")
    if avg >= 0.7:
        return ("👍 Good quality — most facts verified, a few things worth a quick check.", "warning")
    return ("⚠️ Needs attention — some information could not be fully verified.", "error")


def _metric_row(label: str, value: float, explanation: str, emoji: str) -> None:
    """One friendly metric: colored bar + label + plain-language meaning."""
    v = _pct(value)
    color = "green" if v >= 0.8 else ("orange" if v >= 0.6 else "red")
    bar_html = f"""
    <div style="margin-bottom:0.9rem;">
      <div style="display:flex;justify-content:space-between;">
        <span><b>{emoji} {label}</b></span>
        <span style="color:{color};font-weight:bold;">{v:.0%}</span>
      </div>
      <div style="background:#eee;border-radius:8px;height:14px;">
        <div style="background:{color};width:{max(v*100,1):.0f}%;height:14px;border-radius:8px;"></div>
      </div>
      <div style="color:#666;font-size:0.85rem;margin-top:2px;">{explanation}</div>
    </div>"""
    st.markdown(bar_html, unsafe_allow_html=True)


def _quality_report(metrics: dict, reflection_result: str) -> None:
    if not metrics or "error" in metrics:
        st.info("This plan has no evaluation data yet.")
        return

    verdict, kind = _verdict(metrics)
    if kind == "success":
        st.success(verdict)
    elif kind == "warning":
        st.warning(verdict)
    elif kind == "error":
        st.error(verdict)
    else:
        st.info(verdict)

    st.markdown("### How trustworthy is this itinerary?")
    _metric_row(
        "Facts are grounded in real data",
        metrics.get("groundedness", 0),
        "The share of temperatures, dates and prices that trace back to data the system actually fetched — 100% means nothing was invented.",
        "🧮",
    )
    _metric_row(
        "Factual accuracy",
        metrics.get("response_accuracy", {}).get("score", 0),
        "Whether dates and weather labels in the plan match the fetched data exactly.",
        "🎯",
    )
    _metric_row(
        "Matches your request",
        metrics.get("similarity_score", 0),
        "How closely the final plan reflects your query (destination, dates, budget).",
        "🔗",
    )

    ret = metrics.get("retrieval", {})
    if ret.get("retrieved_count"):
        st.markdown("### Nearby places used")
        st.markdown(
            f"Found **{ret['retrieved_count']}** verified nearby places, and the itinerary "
            f"featured **{ret['relevant_count']}** of them."
        )
        _metric_row(
            "Precision (used vs found)",
            ret.get("precision", 0),
            "Of all the verified places found, how many actually made it into your plan.",
            "🏛️",
        )
        _metric_row(
            "Precision@5 (top 5)",
            ret.get("precision@5", 0),
            "Of the 5 closest places, how many appear in the plan.",
            "🥇",
        )

    judge = metrics.get("llm_judge")
    if judge and "error" not in judge:
        st.markdown("### Reviewer score (AI judge)")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Relevance", f"{judge.get('relevance', '?')}/10",
                  help="Does it answer what you asked for?")
        c2.metric("Accuracy", f"{judge.get('accuracy', '?')}/10",
                  help="Are the facts consistent and correct?")
        c3.metric("Completeness", f"{judge.get('completeness', '?')}/10",
                  help="Does it cover everything a good plan needs?")
        c4.metric("Overall", f"{judge.get('overall', '?')}/10")
        if judge.get("justification"):
            st.caption(f"Judge's note: _{judge['justification']}_")

    st.markdown("### Verification result")
    if reflection_result and reflection_result.startswith("APPROVED"):
        st.success("✅ The itinerary passed the automatic fact-check.")
    else:
        st.warning("⚠️ The itinerary needed revision during generation.")
        st.caption(reflection_result or "")

    st.caption("Every plan's scores are saved to `cache/evaluation_log.jsonl` for monitoring.")


def _how_it_was_built(entry: dict) -> None:
    tool_log = entry.get("tool_log") or []
    guard_notes = entry.get("guard_notes") or []
    elapsed = entry.get("elapsed")

    st.markdown("### What the system did")
    st.caption(
        "Three agents worked together: a research agent gathered real facts via "
        "tools, a writer turned them into your plan, and a checker verified it "
        "against the real data."
    )

    if elapsed:
        st.metric("⏱️ Time taken", f"{elapsed:.1f}s")
    st.metric("🖥️ LLM provider", _provider_label())
    st.metric("📦 Cached queries", str(query_cache.cached_query_count()))

    _tool_performance(tool_log)

    st.markdown("### Real data fetched")

    if tool_log:
        for t in tool_log:
            elapsed = t.get("elapsed_seconds")

            timing = (
                f" · ⏱️ {float(elapsed):.2f}s"
                if elapsed is not None
                else ""
            )

            with st.expander(
                f"{_tool_label(t['tool'])}{timing}  ·  `{t['args']}`",
                expanded=False
            ):
                st.code(t["result"], language="text")
    else:
        st.info("No tools were needed for this query — the Orchestrator answered from reasoning alone.")

    if guard_notes:
        st.markdown("### Safety checks that fired")
        st.caption(
            "These automatic checks stop the AI from inventing weather, prices or "
            "place names that were never actually fetched."
        )
        for note in guard_notes:
            st.warning(note)

    st.markdown("### Place-name verification")
    st.caption(
        "Every landmark name in the itinerary was geocoded and cross-checked "
        "against the trip's destination — it must resolve to a real place "
        "nearby, or it gets flagged for revision. Nothing is accepted just "
        "because it sounds like an attraction."
    )


def _render_entry(entry: dict) -> None:
    itinerary = entry.get("itinerary", "")
    metrics = entry.get("metrics")
    reflection_result = entry.get("reflection_result")
    tool_log = entry.get("tool_log") or []

    st.markdown(f"### ✏️ Your request")
    st.write(entry.get("query", ""))

    if not tool_log and metrics is None:
        # A rejection / informational result — just show it plainly.
        st.markdown("---")
        st.markdown(itinerary)
        return

    tabs = st.tabs(["🗺️ Your Itinerary", "✅ Quality Report", "🔎 How it was built"])

    with tabs[0]:
        st.markdown(itinerary or "_No itinerary produced._")
        st.download_button(
            "⬇️ Download itinerary (.md)",
            data=itinerary,
            file_name=f"itinerary_{int(time.time())}.md",
            mime="text/markdown",
            use_container_width=True,
        )

    with tabs[1]:
        _quality_report(metrics, reflection_result)

    with tabs[2]:
        _how_it_was_built(entry)


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("⚙️ Status")
    st.caption(f"**LLM provider:** {_provider_label()}")
    st.caption("**Cache:** disk-backed (location / weather / currency / places)")
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

    st.header("ℹ️ About")
    st.caption(
        "A multi-agent AI that plans real trips: it fetches live weather, "
        "exchange rates and verified places, then cross-checks its own answer "
        "so nothing is made up."
    )

# ---------------------------------------------------------------- main
st.title("🧳 AI Travel Planner")
st.caption(
    "Tell me where you want to go, for how long, for whom, and your budget — "
    "I'll build a real, fact-checked itinerary."
)

# Example buttons live ABOVE the text_area widget so clicking one can set
# st.session_state.query_input before the widget is instantiated (Streamlit
# forbids mutating a widget's state after the widget exists).
st.caption("Try an example — just click one:")
cols = st.columns(len(EXAMPLE_QUERIES))
for i, example in enumerate(EXAMPLE_QUERIES):
    with cols[i]:
        if st.button(example[:38] + ("…" if len(example) > 38 else ""),
                     key=f"ex_{i}", use_container_width=True):
            st.session_state.query_input = example
            st.rerun()

query = st.text_area(
    "Where do you want to go / what do you want planned?",
    height=90,
    key="query_input",
    placeholder=(
        "e.g. Plan a 4-day trip to Manali, Himachal Pradesh for a couple "
        "from Delhi in December. Budget in INR, also show cost in USD."
    ),
)

plan_clicked = st.button("🚀 Plan my trip", type="primary", use_container_width=True)

if plan_clicked:
    query = st.session_state.get("query_input", "").strip()
    if not query:
        st.warning("Please enter a travel query first.")
    else:
        started = time.time()

        cached = query_cache.get(query)
        if cached is not None:
            entry = {**cached, "elapsed": time.time() - started}
            st.session_state.history.append(entry)
            st.session_state.selected = len(st.session_state.history) - 1
            st.success(
                "⚡ Served instantly from cache — this exact request was planned "
                "recently, so no AI calls were needed."
            )
            _render_entry(entry)
            st.rerun()

        with st.spinner("Researching and building your itinerary (this takes about a minute)..."):
            itinerary, tool_log, guard_notes, reflection_result, metrics = asyncio.run(
                plan_trip(query)
            )
        elapsed = time.time() - started
        entry = {
            "query": query,
            "itinerary": itinerary,
            "tool_log": tool_log,
            "guard_notes": guard_notes,
            "reflection_result": reflection_result,
            "metrics": metrics,
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
    "Budget figures are estimates, not live prices. Weather is either a real "
    "live forecast (trips within ~15 days) or real historical data (further "
    "out) — never an invented guess."
)