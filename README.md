# AI Travel Planner — Multi-Agent System (Local LLM + MCP)

A 3-agent AI system that plans travel itineraries from a plain-language query.
It runs through a **provider layer** (`llm.py`) that routes LLM calls to
**Ollama** (default, fully local, `qwen2.5:7b-instruct`) or **Groq** (cloud,
fast mode, `openai/gpt-oss-120b`) — the agents don't care which backend is
active. Real-world data (weather, currency, geocoding, nearby attractions)
reaches the system through the **Model Context Protocol (MCP)**, using only
free APIs with no API keys. See "Fast mode" below.

## Agents

- **Orchestrator Agent** — reasons about your query and decides, via native
  tool-calling, whether it needs live data. Runs a ReAct loop
  (reason → act → observe → repeat).
- **Writer Agent** — takes the gathered research and writes the final
  Markdown itinerary. Never calls tools.
- **Reflection Agent** (code-level, deterministic) — evaluates the draft
  itinerary against the actual fetched data. Flags hallucinated weather
  numbers, place names not in the verified list, or exchange rates that
  weren't fetched. May request a revision cycle.

All three agents share the same model through the provider layer:
`qwen2.5:7b-instruct` locally, or `openai/gpt-oss-120b` on Groq. The
Reflection is NOT an LLM call — it is deterministic Python string/regex
checks, so it can never itself hallucinate.

## Tools (via MCP)

- `geocode_city` — location lookup with region disambiguation
  (Open-Meteo primary + Nominatim/OSM fallback for better India/town coverage)
- `get_weather` — live forecast (trips ≤15 days out) or real historical
  data (further out), never a guess (Open-Meteo Forecast/Archive)
- `get_exchange_rate` — live currency conversion (Frankfurter, with a
  free `open.er-api.com` fallback for currencies like AED that Frankfurter
  doesn't support)
- `get_nearby_attractions` — real, distance-verified places (Wikipedia
  geosearch)

## Caching & sessions

- **Disk-backed tool cache** — every `geocode_city`, `get_weather`,
  `get_exchange_rate`, and `get_nearby_attractions` result is persisted to
  `cache/travel_tools_cache.json`. Because each trip spawns a fresh MCP
  subprocess, the old in-memory cache died between queries; now repeated
  cities/rates/places are served instantly from disk with **zero network
  calls and zero LLM tokens** spent re-fetching. TTLs per tool (geocode 30d,
  weather 12h, FX 1d, attractions 7d) keep data fresh.
- **Query-level cache** (`query_cache.py`) — the tool cache saves network
  calls, but the ~1 minute wall-clock time of a trip is dominated by the 20s
  spacing between Groq LLM calls, which run fresh every time. So the FINAL
  plan is also cached per query (`cache/query_cache.json`, keyed by
  normalized query text). Re-running an identical query returns the saved
  itinerary **instantly with zero LLM calls** — the UI shows a green
  "Served from query cache" banner and the sidebar shows the cached-query
  count. Both the Streamlit UI (`app.py`) and the CLI (`main.py`) use it.
  **Freshness-aware TTL (mentor review):** instead of a flat 24h, each
  cached plan's effective TTL is the *minimum* TTL of the tools used to
  build it — weather plans expire after 2h, FX plans after 1h, attractions
  after 24h, static (geocode-only) plans after 24h. So asking "what's the
  weather tomorrow?" and re-asking a few hours later never returns stale
  data, while a general itinerary still reuses the cache all day.
- **Evaluation & monitoring** (`evaluation.py`) — every produced plan is
  scored and logged to `cache/evaluation_log.jsonl`: similarity (cosine,
  itinerary vs research), groundedness/faithfulness (facts used == facts
  fetched), response accuracy (dates/labels), and retrieval precision /
  recall / precision@k (allow-listed attractions the itinerary actually
  used). An optional **LLM-as-a-judge** (relevance / accuracy / completeness,
  0-10) runs when `RUN_LLM_JUDGE=1`. The UI shows all metrics per plan.
- **Streamlit session history** — the UI stores every plan in
  `st.session_state`, so you can browse past itineraries from the sidebar
  without re-running the pipeline, and download any itinerary as Markdown.

## Hallucination guards (five layers)

1. **Prompt engineering** — Writer and Orchestrator prompts forbid fabrication.
2. **Code-level hard guards** — If a tool wasn't called or failed, `main.py`
   injects an unmissable directive forbidding the Writer from inventing
   that category of fact. Also suppresses cross-currency display when the
   user never asked for a conversion.
3. **Reflection agent** — Deterministic code-level checks that compare the
   draft against the actual tool output (weather numbers, allow-listed
   place names, requested currencies). Requests a revision if violated.
   Landmark detection uses four strategies so names with no obvious type
   keyword are still caught: a place-type token anywhere in the phrase
   (`Baga Beach`, `Marine Drive`, `Times Square`), a place-type suffix on
   the final word (`Charminar`, `Mehrangarh`), a travel-context word right
   before it (`Visit Jantar Mantar`), or bold/emphasis wrapping
   (`**Swaroop Sagar Lake**`). Template/heading words (`Day 1`, `Weather`,
   `Estimated budget`) and the trip's own destination (`North Goa`) are
   excluded so they never false-positive.
4. **Tool-call completeness fallback** — after the Orchestrator finishes, a
   plain-Python check verifies every *required* tool was called
   (geocode/weather/attractions always; FX when a conversion is requested).
   If one was forgotten, the model is re-prompted to make exactly that call
   (capped at 2 rounds) instead of silently leaving the data out.
5. **Evaluation metrics** — groundedness, similarity, response accuracy,
   retrieval precision/recall — reported per plan and logged for monitoring.

### Hybrid place validation (Layer 3 of the attraction check)

The attraction check is a three-stage pipeline. Stage 1 (reflection rules)
decides whether a phrase **looks** like a place; Stage 2 filters generic /
template / destination words; Stage 3 (`place_validation.py`) **proves** it
is a real place near the trip destination:

    Generated itinerary
            |
            v
    Candidate extractor  (4 heuristics: type / suffix / context / bold)
            |
            v
    Filter generic, template & destination words
            |
            v
    Geocode each candidate  (Nominatim, cached)   <- Layer 3
            |                       |
            v                       v
      found + within ~60km     not found / far away
      of the destination           |
            |                       v
          Keep them              Flag -> revise

So `Dragon Moon Palace` is rejected even though it contains the keyword
*"Palace"* — geocoding can't find it near the destination. `Jantar Mantar`
mentioned for a Jaipur trip is verified (3.9 km from Jaipur). `Marine Drive`
mentioned for a Goa trip is flagged: it *exists*, but in Mumbai ~400 km away,
so it is not relevant to the destination. The validator is injected into
`run_reflection`, so the offline test suite uses a stub and stays
deterministic. Future evolution: replace the rule-based extractor with a
real NER model (spaCy LOC/GPE/FAC) — Stage 3 validation is unchanged.

## Project structure

```
travel-agent/
├── app.py                     # Streamlit UI (session history + download)
├── llm.py                     # LLM provider layer (Ollama default / Groq fast mode)
├── main.py                    # CLI entry point + preflight + guards + pipeline
├── orchestrator.py            # Orchestrator agent (ReAct loop)
├── writer_agent.py            # Writer agent (Markdown formatting)
├── reflection_agent.py        # Reflection (deterministic code-level checks)
├── place_validation.py        # Layer 3: geocodes candidates to prove they
│                              #   are real places near the destination
├── evaluation.py              # Evaluation metrics + LLM-as-judge + monitoring log
├── mcp_server.py              # MCP server exposing the 4 tools + disk cache
├── mcp_client.py              # MCP client wrapper
├── query_cache.py             # Query-level plan cache (freshness-aware TTL)
├── requirements.txt           # Python dependencies (pinned)
├── .gitignore                 # Ignores venv/, __pycache__/, cache/, .pytest_cache/
├── cache/                     # Disk cache for tool + query results (gitignored)
├── demo_transcript.md         # Walkthrough of a complete agent run
├── README.md
├── screenshots/               # Evidence of working system (see below)
└── tests/                     # Unit tests (130 tests, all pass)
    ├── __init__.py
    ├── test_geocode.py        # Geocoding disambiguation + region matching (8 tests)
    ├── test_mcp_tools.py      # MCP tool function smoke tests (8 tests)
    ├── test_cache.py          # Persistent disk cache (6 tests)
    ├── test_query_cache.py    # Query-level plan cache (6 tests)
    ├── test_guards.py         # Hallucination guard logic (13 tests)
    ├── test_llm_fallback.py   # Groq→Ollama fallback + token hygiene (11 tests)
    ├── test_reflection.py     # Reflection checks (12 tests)
    ├── test_review_fixes.py   # Mentor-review fixes: completeness, cache TTL,
    │                          #   detailed weather, creative landmark detection,
    │                          #   geocode validation, evaluation metrics (50 tests)
    ├── test_place_validation.py  # Layer 3 geocoding validation (10 tests)
    └── test_invalid_query.py  # INVALID_QUERY rejection (4 tests)
```

> **Committed to GitHub:** All files above.
> **Gitignored (not committed):** `venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `cache/`, `changes_understanding.md`

## Setup

1. Install [Ollama](https://ollama.com), then pull the model:
   ```bash
   ollama pull qwen2.5:7b-instruct   # Orchestrator + Writer + Reflection
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

4. Run tests:
   ```bash
   pytest tests/ -v
   ```

## Fast mode (Groq cloud LLM — optional)

By default everything runs on the local Ollama model (no keys needed). For
dramatically lower latency, the same pipeline can route its LLM calls to
[Groq](https://console.groq.com) (`openai/gpt-oss-120b`, free tier, runs on
their GPUs) via a provider layer in `llm.py`:

```powershell
setx LLM_PROVIDER "groq"
setx GROQ_API_KEY "gsk-..."
# open a new terminal, then:
python main.py
```

On Groq the Orchestrator + Writer calls typically complete in seconds
(instead of minutes on a CPU-only laptop). The ReAct tool-calling and the
guard/reflection layers work identically — only the model inference moves
to the cloud. Unset both vars (or remove `LLM_PROVIDER`) to fall back to
local Ollama.

## Evidence / Proof of Working System

This section demonstrates that the pipeline runs end-to-end and produces
real, tool-grounded output — not just that the code compiles.

### 1. CLI run — full ReAct loop + final itinerary

Shows the Orchestrator making tool calls (geocode → weather → attractions),
the Writer producing the draft, and the Reflection agent approving it.

```
screenshots/L2_SS_1.png
screenshots/L2_SS_2.png
```

![CLI run showing ReAct tool calls](screenshots/L2_SS_1.png)
![CLI run showing final itinerary](screenshots/L2_SS_2.png)

### 2. Streamlit UI — same query, rendered in the browser

```
screenshots/L2_SS_3.png
screenshots/L2_SS_4.png
```

![Streamlit UI showing query input](screenshots/L2_SS_3.png)
![Streamlit UI displaying the generated itinerary](screenshots/L2_SS_4.png)

## L2 Review Context

### Architecture Overview

A multi-agent AI system for travel itinerary generation using local LLMs (Ollama) and the Model Context Protocol (MCP).

### Agents

| Agent | Model | Role |
|-------|-------|------|
| Orchestrator | `qwen2.5:7b` / `gpt-oss-120b` | ReAct loop: decides tool calls, gathers research |
| Writer | `qwen2.5:7b` / `gpt-oss-120b` | Formats research into Markdown itinerary |
| Reflector | code-level checks | Deterministic validation of draft vs fetched data |

### MCP Tools (all free, no API keys)

| Tool | API | What it does |
|------|-----|-------------|
| `geocode_city(city, region)` | Open-Meteo Geocoding + Nominatim | City → lat/lon with region disambiguation |
| `get_weather(city, date, region)` | Open-Meteo Forecast/Archive | Live forecast (≤15 days) or historical data |
| `get_exchange_rate(base, target)` | Frankfurter + `open.er-api.com` fallback | Live currency conversion (incl. AED) |
| `get_nearby_attractions(city, radius, region)` | Wikipedia Geosearch | Real, distance-verified POIs |

### Hallucination Guards

1. **Code-level hard guards** (`main.py`): If a tool was never called or failed, an explicit directive is injected into the Writer's context, forbidding it from inventing that category of data.
2. **Reflection agent**: Reviews the draft against the actual research data. Flags temperature numbers not in fetched weather, place names not in verified list, exchange rates not fetched, etc.
3. **Prompt engineering**: Both Writer and Orchestrator prompts contain strict constraints against fabrication.

### Key Design Decisions

- **One model everywhere**: All three agents share one model per provider —
  `qwen2.5:7b-instruct` (Ollama) and `openai/gpt-oss-120b` (Groq fast mode).
  The original plan used a 3B for the orchestrator/reflection (faster) and 7B
  for the writer, but 3B proved unreliable at tool-calling and at following
  the reflection instructions (misspelled regions, skipped tools, bogus
  "no weather data" flags). Upgrading everything to 7B fixed these.
- **Provider layer (`llm.py`)**: A single `llm.chat()` entry point routes to
  Groq (fast) or Ollama (local) automatically, returning one unified message
  shape to the agents. This is why the same pipeline runs ~60x faster on Groq
  without any agent-code changes, and falls back to Ollama during rate limits.
- **Query-level cache**: The finished plan is cached per query
  (`query_cache.py`, 24h TTL, normalized keys) so re-running the same query
  returns instantly with zero LLM calls — ideal for demos and free-tier
  budget management.
- **Deterministic reflection instead of an LLM reviewer**: The 7B model
  still hallucinated when asked to "review" the draft (it flagging the
  geocode coordinates as weather data). Replaced with pure Python checks
  — 100% reliable, instant, and still demonstrates the review step.
- **MCP boundary**: Agent never calls HTTP directly — goes through MCP stdio
  session. Decouples tools from agent logic.
- **Region-based geocoding with fuzzy matching**: Pass region (state/country)
  to disambiguate city names. Region matching uses containment in BOTH
  directions (`"United States of America"` matches Open-Meteo's `"United
  States"`) so every tool call resolves to the SAME location. Nominatim
  (OSM) is the fallback when Open-Meteo can't confirm a region — this is
  what makes "Goa, India" resolve to the Indian state instead of Genoa.
- **Orchestrator infers region from knowledge**: The prompt tells the model
  to infer a country/region when the user doesn't give one ("Goa" →
  `region='India'`), and to NEVER pass the city name as its own region.
- **Preflight check**: On startup, verifies the Ollama model exists and
  Ollama is running. Gives clear install instructions if missing.
- **Safe cleanup**: MCP client lifecycle wrapped in `try/finally` to prevent
  noisy subprocess/session failures.
- **`sys.executable` for the MCP subprocess**: The client launches
  `mcp_server.py` using the same Python interpreter (`sys.executable`)
  instead of a hardcoded `"python"`, so it works inside any venv.

### Reflection Design

The reflection agent (`reflection_agent.py`) is deterministic — no LLM call.
It runs three regex/string checks after the Writer:

1. **Weather check**: If research notes contain `HISTORICAL weather for`
   or `LIVE forecast for`, weather WAS fetched. If neither is present but
   the draft cites temperature numbers, flag it.
2. **Attraction allow-list check**: Parse the `*** ALLOW-LIST ***` guard
   from the research notes. Flag any place-phrase in the draft whose
   leading word OR type word (Temple/Beach/Fort/...) does not appear in
   any allow-listed name. (Handles `Visit Cathedral of Saint Étienne`
   correctly — "Cathedral" matches the allow-list — while still catching
   `Visit Eiffel Tower` when Eiffel Tower was never fetched.)
3. **Currency check**: Determine which currencies the user mentioned in
   their query. If the notes say no exchange rate was fetched, or that the
   user did NOT ask for conversion, flag any currency symbol in the draft
   the user never asked about. (A "budget 500 USD" query showing `$500` is
   fine — `$` is what the user wanted.)

Returns `APPROVED` or `REVISE: <specific issues>`. If `REVISE`, the Writer
regenerates with the issues added to context (max 1 revision cycle).

## Architecture

```
User query (CLI or Streamlit) ──► Query-level cache check (query_cache.py)
                                       │ miss
                                       ▼
Orchestrator Agent  ───MCP (stdio/JSON-RPC)───►  MCP Server
(qwen2.5:7b / gpt-oss-120b,             ◄────── - geocode_city
 ReAct loop, decides                          - get_weather
 tool calls)                                  - get_exchange_rate
       │ research notes                       - get_nearby_attractions
       ▼                                        (each disk-cached)
Code-level hard guards (main.py)
       │
       ▼
Writer Agent (formatting only, no tools)
       │ draft itinerary
       ▼
Reflection (deterministic Python checks, no LLM)
       │
       ├── APPROVED ──► Final Markdown itinerary → CLI or Streamlit UI
       │                     └──► cached per query (instant re-runs)
       └── REVISE ────► Writer regenerates (max 1 revision cycle)
                            │
                            ▼
                        Final output

LLM calls at every step go through the provider layer (llm.py):
  Groq (openai/gpt-oss-120b, fast)  ── rate-limit handled ──►  Ollama
  (qwen2.5:7b-instruct, local fallback)
```

### LLM Provider Layer (`llm.py`)

```
llm.chat(messages, tools)
   │
   ├─ provider() == "groq"  ──► _groq_chat()   (openai/gpt-oss-120b, ~0.3s/call)
   │                              │ 20s spacing, 429 retry (body-aware wait),
   │                              │ quota exhausted? ──► _ollama_chat() fallback
   └─ provider() == "ollama" ──► _ollama_chat() (qwen2.5:7b-instruct, offline)
                                     └─ both return the same unified message shape
```

Both caches keep the demo fast and the free tier affordable:
- **Tool cache** (`mcp_server.py`): geocode/weather/FX/attractions persisted
  to `cache/travel_tools_cache.json` with per-tool TTLs (30d / 12h / 1d / 7d).
- **Query cache** (`query_cache.py`): whole finished plans keyed by normalized
  query text, 24h TTL — re-running a query skips the entire LLM pipeline.

## Changelog (recent)

All changes below were made and verified in this session. Every one is covered
by the test suite (130 tests, all passing) and demonstrated live against Groq
and Ollama.

### Mentor-review fixes (L2 follow-up)
- **Evaluation & monitoring (`evaluation.py`, new module):** every produced
  plan is scored and appended to `cache/evaluation_log.jsonl`:
  - `groundedness` / `faithfulness` — fraction of itinerary fact claims
    (temperatures, FX rates, dates) that trace back to actually-fetched
    research; 1.0 = nothing invented. Temperatures are matched within ±0.6°C
    so the writer's rounding (24 vs 24.3) is not punished.
  - `similarity_score` — cosine similarity of token-frequency vectors between
    the research notes and the final itinerary.
  - `response_accuracy` — dates in the itinerary must match fetched dates;
    historical weather must not be sold as a forecast.
  - `retrieval_metrics` — IR-style precision / recall / precision@k over the
    attraction allow-list: retrieved = places fetched from Wikipedia,
    relevant = places the itinerary actually used.
  - **LLM-as-a-judge** (`llm_as_judge`) — optional single LLM call scoring
    relevance / accuracy / completeness (0-10) plus a justification, enabled
    with `RUN_LLM_JUDGE=1`. Wired into `main.py` (CLI prints the block),
    `app.py` (metrics expander with `st.metric`), and always logged.
- **Tool-call completeness fallback (`main.py`):** a plain-Python
  `_required_tools()` / `_missing_required_tools()` decides which tools MUST
  have run for a query (geocode, weather, attractions always; FX iff a
  conversion is requested). If the Orchestrator forgot one, the model is
  re-prompted to make exactly that call and the result is appended to the
  shared tool log — capped at 2 rounds so a stubborn model can't loop. The
  old behavior was to silently fall into a hard guard; now it is recovered
  first, and guards only fire if recovery truly fails.
- **Detailed weather validation (`reflection_agent.py`):** the reflection
  agent now parses the fetched weather fact (live vs historical, exact
  min-max, EXACT DATE=) and checks that the draft (a) states a temperature
  range grounded in the fetched data, (b) labels historical data as
  historical (never as a forecast), (c) uses the correct year when it
  mentions a date, and (d) actually includes a weather section when data
  was fetched.
- **Robust attraction matching (`reflection_agent.py`):** place-name checks
  moved from a naive two-word regex to normalized + fuzzy matching.
  `_norm()` transliterates accents (Saint Étienne → saint etienne) and the
  matcher accepts multi-word names (Bohra Ganesh Temple), names the writer
  truncated (dropped ", Paris"), and preposition forms (Church of
  Saint-Jean-le-Rond), while generic leader verbs ("Visit X") no longer
  produce false positives. Hallucinated places are still caught.
- **Creative landmark detection (`reflection_agent.py`):** the attraction
  check no longer relies on a fixed `Beach/Temple/Fort...` keyword list.
  A landmark is now caught by ANY of four strategies, so names with no
  obvious type word are still detected:
  1. a place-type token anywhere in the phrase — `Baga Beach`,
     `Marine Drive` (Drive), `Times Square` (Square);
  2. a place-type **suffix** on the final word — `Charminar` (minar),
     `Mehrangarh` (garh), `Hawa Mahal` (mahal);
  3. a travel-context word directly before the capitalized phrase —
     `Visit Jantar Mantar`, `walk along Marine Drive`;
  4. bold/emphasis wrapping — `**Swaroop Sagar Lake**`.
  To stay false-positive-free it also (a) strips generic leaders/modifiers
  (`Visit`, `the`, `famous`), (b) drops lone generic type words (`the
  beach`, `an old fort`), (c) drops template/heading words (`Day 1`,
  `Weather`, `Estimated day budget`, `Top attractions`), and (d) whitelists
  the trip's own destination parsed from the geocode/weather/attraction
  results, so `fly into Goa`, `North Goa` or a bolded `**Udaipur**` title
  are never flagged. Verified end-to-end on the real Udaipur run: a
  hallucinated `Moti Magri` is caught, the real itinerary approves.
- **Freshness-aware query-cache TTL (`query_cache.py`):** the flat 24h TTL
  was wrong for plans that embed volatile data. `effective_ttl()` now returns
  the MINIMUM TTL across the tools a plan used: weather 2h, FX 1h,
  attractions 24h, static 24h. A plan about "tomorrow's weather" expires in
  2h even though a general itinerary still caches all day — exactly the
  per-component permutation the reviewer asked for.
- **Hybrid place validation (`place_validation.py`, new module):** the
  attraction check is now a 3-stage pipeline — (1) rule-based candidate
  extraction, (2) generic/template/destination filtering, (3) **geocoding
  validation** that *proves* a candidate is a real place near the requested
  destination instead of only judging whether the text "looks like" a
  landmark. `PlaceValidator` geocodes each candidate via Nominatim (cached,
  paced to 1 req/s) and measures the great-circle distance: found + within
  60 km → verified/kept; not found or far away → flagged for revision.
  Verified live: `Jantar Mantar` for a Jaipur trip is accepted (3.9 km);
  `Dragon Moon Palace` for a Goa trip is rejected (geocoder finds nothing);
  `Marine Drive` for a Goa trip is rejected (exists, but in Mumbai ~400 km
  away — not relevant). Wired into `run_reflection`/`_check_attractions` as
  an injected validator, so the offline test suite stays deterministic with
  a stub lookup (16 new tests). Future evolution: swap the rule-based
  extractor for a real NER model (spaCy LOC/GPE/FAC) — Layer 3 is unchanged.
- **Icon-surfacing retrieval fix (`mcp_server.py`):** a Hyderabad itinerary
  was skipping Charminar, Hussain Sagar and Golconda Fort for Gyan Bagh
  Palace and Public Gardens. Root cause: Wikipedia geosearch is
  *density-based* — in a dense city the 50 closest geo-tagged articles are
  neighbourhoods and infrastructure, and the icons sit at positions 50-250.
  Fixed by (a) fetching 500 candidates (gslimit=500) instead of 50, (b)
  widening the non-attraction filter (drops colleges, hospitals, municipal
  corporations, high courts, hotels...), and (c) re-ranking the candidates by
  **Wikipedia pageview popularity (30-day)**, with landmarks always ranked
  ahead of neighbourhoods. Live check: Hyderabad now leads with Charminar,
  Hussain Sagar, Chowmahalla Palace, Falaknuma Palace; Jaipur with Amber
  Fort, City Palace, Albert Hall, Hawa Mahal; Udaipur with Fateh Sagar Lake,
  Bagore Ki Haveli, Gangaur Ghat, Jagdish Temple, Lake Pichola.

### LLM provider layer (`llm.py`)
- Added a unified provider layer: `llm.chat()` routes to Groq (fast mode) or
  Ollama (local) automatically, returning the same `{"message": {...}}` shape
  to the agents — the ReAct loop in `orchestrator.py` and the writer work
  unchanged on either backend.
- `_getenv()` reads env vars with a **Windows registry fallback**
  (`HKCU\Environment`), so Groq credentials work even in a terminal opened
  before `setx` ran.
- `_normalize_tool_calls()` keeps the **byte-exact `arguments_raw` JSON** of
  assistant `tool_calls` so they can be replayed identically to Groq on
  subsequent turns (fixes intermittent HTTP 400s).
- `_to_groq_messages()` / `_to_ollama_messages()` convert the unified history
  to each provider's required format, pairing tool results with their
  assistant tool-call ids/names.
- **Ollama responses are normalized too**: pydantic `Message`/`ToolCall`
  objects are converted to plain dicts with a `call_N` id per tool call, so a
  Groq→Ollama fallback mid-conversation no longer breaks the next turn.

### Groq rate-limit resilience (`llm.py`)
- `_retry_after()` honors Groq's `Retry-After` / `x-ratelimit-reset-*` headers
  (parses `1m30s`/`42s`/`185ms` formats) with a 30s cap.
- **Fixed: `185ms` was parsed as 185 minutes.** Groq sends token-reset in
  milliseconds (`x-ratelimit-reset-tokens: 185ms`), but the old parser saw the
  `"m"` and read it as 185 minutes → a false 180s backoff latch while Groq
  had actually recovered in 0.2s (and the Groq website showed tokens
  available). `_parse_reset_seconds()` now checks `ms` before `m`/`s`.
- **Daily-quota (TPD) detection:** the real blocker on the free tier is a
  **100k tokens/day** budget per model, which Groq reports only in the 429
  error *body* ("Please try again in 8m28.896s.") — the minute-level headers
  say "ready in 1ms". `_body_error_msg()` + `_parse_body_retry()` now parse
  that wait from the body and it takes priority over the headers for both the
  retry wait and the backoff latch.
- A 429 with a **short reset (<15s, `GROQ_LONG_RESET_SECONDS`)** is retried
  immediately instead of falling back; only a genuinely long reset (2nd hit)
  triggers the Groq→Ollama fallback. A backoff latch (capped at 180s) skips
  Groq entirely during long windows so the rest of a run is not stalled.
- Calls are spaced ≥20s apart (`GROQ_MIN_INTERVAL`) so a multi-turn trip no
  longer blows the per-minute token budget in a single minute.
- **Result:** the heavy multi-city query (Jaipur + Jodhpur + Udaipur, AED→GBP
  + AED→INR) completes in **~85s on Groq alone** with zero 429s and zero
  fallback.
- **Model availability note:** Groq occasionally removes models from an
  account (e.g. `llama-3.3-70b-versatile` was decommissioned mid-session,
  causing silent 404s → Ollama fallback). `GROQ_MODEL` is now
  `openai/gpt-oss-120b` (verified function-calling, 8k tokens/min). The
  provider layer treats a 4xx model error as a real failure and reports it
  instead of guessing.

### Windows console encoding (`llm.py`)
- **Fixed: `UnicodeEncodeError: 'charmap' codec can't encode character
  '\u2011'`.** Groq models emit typographic characters (U+2011 non-breaking
  hyphen, U+202F narrow no-break space, `≈`, `‑`) that Windows' default
  cp1252 console cannot print. `llm.py` now reconfigures stdout/stderr to
  UTF-8 with `errors="replace"` on import (guarded for non-reconfigurable
  streams), so every pipeline `print()` is crash-safe regardless of console
  encoding. Verified: full Groq run printing `‑`, ` `, and `≈` characters.

### Token hygiene (`orchestrator.py`)
- `_trim_tool_result()` trims tool results to 300 chars before they go back
  into the LLM message history (the ReAct loop re-sends the full conversation
  every turn). The **full** result still lives in `tool_call_log` for the
  guards, writer, and reflection — so no data is lost, only LLM tokens.

### Hallucination-guard fixes (`main.py`)
- **`_user_wants_conversion()` rewritten** to detect distinct currencies:
  a query counts as a conversion request only when two or more currencies are
  mentioned or an explicit conversion verb is used. This fixed the bug where
  *"show the total breakdown in GBP and in INR"* was wrongly flagged as
  "user did NOT ask for conversion" — which then forbade the writer from
  showing exactly what the user requested.
- **FX guard now detects failed lookups**: if `get_exchange_rate` was called
  but returned an error (e.g. unsupported currency), the "NO EXCHANGE RATE
  WAS FETCHED" constraint fires instead of silently skipping. Previously a
  failed call fell through with no guard at all.

### Exchange-rate fallback (`mcp_server.py`)
- `get_exchange_rate` now tries **Frankfurter first, then a free no-key
  fallback** (`open.er-api.com`). This fixed the `404 Not Found` for AED
  (UAE Dirham), which is not among Frankfurter's ~30 ECB currencies. Verified:
  `1 AED = 26.005 INR`, `1 AED = 0.2012 GBP`.

### Persistent cache (`mcp_server.py`)
- Tool results are now **disk-backed** (`cache/travel_tools_cache.json`) with
  per-tool TTLs (geocode 30d, weather 12h, FX 1d, attractions 7d). Before,
  each trip spawned a fresh MCP subprocess so the in-memory caches died every
  query; now repeated cities/rates/places are served instantly with **zero
  network calls and zero LLM tokens**. Verified: a warm lookup made 0 HTTP
  requests. The `cache/` directory is gitignored.

### Query-level cache (`query_cache.py`, new module)
- The tool cache saves network calls, but a trip's ~1 minute wall-clock time
  is dominated by the 20s spacing between Groq LLM calls — which run fresh
  every time. So the FINAL plan is cached per query in
  `cache/query_cache.json`, keyed by a normalized hash of the query text
  (lowercase, whitespace/punctuation-stripped, so
  `"Plan a  trip to   Goa!"` hits the same entry as `"plan a trip to goa"`).
- A repeat query now returns the saved itinerary **instantly, with zero LLM
  calls** — wired into both the Streamlit UI (`app.py`, green "Served from
  query cache" banner + cached-query count in the sidebar) and the CLI
  (`main.py`, prints a `[query-cache]` note and exits early).
- **Freshness-aware expiry (mentor review):** TTL is no longer flat 24h —
  `effective_ttl()` takes the minimum TTL across the tools used to build a
  plan (weather 2h, FX 1h, attractions 24h, static 24h), so volatile plans
  expire early while static ones live all day.
- This is a big win for demos: pre-run each showcase query once, and the
  live review runs them instantly without burning the Groq daily budget.

### Streamlit UI (`app.py`)
- **Non-technical-friendly design**: tabbed layout — **🗺️ Your Itinerary**
  (the plan + download), **✅ Quality Report** (color-coded gauges with a
  plain-language verdict, e.g. "✅ Excellent quality — every fact verified",
  plus the optional LLM-as-a-judge scores), and **🔎 How it was built**
  (real tools called, safety checks that fired, provider + timing).
- **One-click example queries**: five showcase buttons above the input box
  that fill the query field automatically — ideal for demos.
- **Session history**: every plan is stored in `st.session_state`; the sidebar
  lists past queries so you can re-view an old itinerary without re-running
  the pipeline. A "Clear history" button resets the session.
- **Download** button exports any itinerary as `.md`.
- **Sidebar status** shows the active LLM provider (Groq/Ollama), cache
  status, and a friendly project description.

### Tests
- `tests/test_llm_fallback.py` (13 tests) — Groq→Ollama fallback, backoff
  latch, `call_N` id generation, reset-header parsing (`185ms`/`1m30s`/`1m`),
  body-based daily-quota retry, tool-result trimming.
- `tests/test_cache.py` (6 tests) — disk cache round-trip, key normalization,
  TTL expiry, cross-process persistence.
- `tests/test_query_cache.py` (6 tests) — query-cache round-trip, key
  normalization, miss-on-different-query, TTL expiry, live-count.
- `tests/test_review_fixes.py` (32 tests, new) — tool-call completeness
  checker, freshness-aware cache TTL, detailed weather validation, robust
  fuzzy attraction matching, and the evaluation-metrics functions.
- `tests/test_place_validation.py` (16 tests, new) — hybrid Layer-3 place-name
  validation: distance math, destination parsing, injected-lookup validator,
  allow-list + geocoding interaction across all guard branches.
- `tests/test_mcp_tools.py` — added landmark-title ranking and
  popularity-ranking cases for the Hyderabad-style icon-surfacing fix.
- Suite grew from 42 → **130 tests, all passing**.

## Known limitations

- **Groq free tier has a daily token budget per model (~100k/day).** The app
  now detects this honestly ("daily quota — waiting Xs") and falls back to
  Ollama during the wait, but running ~6-7 full trips in one day will
  legitimately exhaust it — that's a real free-tier constraint, not a bug.
  The query-level cache mitigates this for demos: pre-run showcase queries
  once, and repeat runs cost zero tokens.
- Local 7B models are still not 100% reliable at tool-calling — occasionally
  they skip a call they should have made, or pass an approximate date when
  the user gives no month. **Mitigations:** the tool-call completeness
  fallback re-prompts the model for any required-but-missing tool, the
  date normalizer fills in approximate dates, and the hard guards forbid
  invention if recovery fails.
- `get_nearby_attractions` uses Wikipedia geosearch (nearest-by-distance) as
  the source of *verified* nearby places, then re-ranks the candidates by
  Wikipedia pageview popularity (last 30 days) so the destination's actual
  icons surface instead of nearby-but-obscure streets. The built-in filter
  drops non-attractions (railway stations, assembly constituencies, talukas,
  villages, colleges, hospitals) and landmarks always rank ahead of
  neighbourhoods — but a landmark with no geosearch coverage (e.g. a monument
  whose article lacks coordinates) can still be missed. This is a deliberate
  tradeoff: verified-but-obscure beats famous-but-unverified.
- Budget figures are LLM estimates, not live pricing data.
- `get_weather` only returns live forecasts within ~15 days; further-out
  trips use historical archive data (clearly labeled), because no API can
  predict the weather months ahead.