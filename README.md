# AI Travel Planner — Multi-Agent System (Local LLM + MCP)

A 2-agent AI system that plans travel itineraries, running on local LLMs
via Ollama and reaching external tools through the Model Context Protocol
(MCP). No paid APIs, no external LLM calls, no API keys required.

## Agents

- **Orchestrator Agent** (`qwen2.5:3b-instruct`) — reasons about your
  query and decides, via native tool-calling, whether it needs live data.
  Runs a ReAct loop (reason → act → observe → repeat).
- **Writer Agent** (`qwen2.5:7b-instruct`) — takes the gathered research
  and writes the final Markdown itinerary. Never calls tools.

## Tools (via MCP)

- `geocode_city` — location lookup with region disambiguation (Open-Meteo)
- `get_weather` — live forecast (trips ≤15 days out) or real historical
  data (further out), never a guess (Open-Meteo)
- `get_exchange_rate` — live currency conversion (Frankfurter)
- `get_nearby_attractions` — real, distance-verified places (Wikipedia
  geosearch)

## Hallucination guards

If a tool wasn't called or failed, the Writer is explicitly forbidden
from inventing that category of fact (weather numbers, exchange rates,
landmark names) — enforced in `main.py`, not just prompted for.

## Project structure

```
travel-agent/
├── app.py            # Streamlit UI
├── main.py             # CLI entry point + plan_trip() logic + hard guards
├── orchestrator.py      # Orchestrator agent
├── writer_agent.py       # Writer agent
├── mcp_server.py          # MCP server exposing the 4 tools
├── mcp_client.py          # MCP client wrapper
├── requirements.txt
├── L2_REVIEW_PREP.md      # Study notes for project review
└── README.md
```

## Setup

1. Install [Ollama](https://ollama.com), then pull both models:
   ```bash
   ollama pull qwen2.5:3b-instruct   # Orchestrator
   ollama pull qwen2.5:7b-instruct   # Writer
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Run it:
   ```bash
   python main.py          # CLI
   streamlit run app.py    # or Streamlit UI
   ```

## Architecture

```
User query (CLI or Streamlit)
      │
      ▼
Orchestrator Agent  ───MCP (stdio/JSON-RPC)───►  MCP Server
(qwen2.5:3b, ReAct   ◄──────tool results──────    - geocode_city
 loop, decides                                     - get_weather
 tool calls)                                       - get_exchange_rate
      │ research notes                             - get_nearby_attractions
      ▼
Code-level hard guards (main.py)
      │
      ▼
Writer Agent (qwen2.5:7b, formatting only, no tools)
      │
      ▼
Final Markdown itinerary → CLI or Streamlit UI
```

## Known limitations

- Small local models (3B class) are not 100% reliable at tool-calling —
  occasionally skip a call it should have made.
- `get_nearby_attractions` uses Wikipedia geosearch (nearest-by-distance),
  not a curated "top tourist attractions" ranking.
- Budget figures are LLM estimates, not live pricing data.
- Guards fully cover the "tool never called/failed" cases, but don't
  validate that every named place in a successful response is strictly
  from the verified list.