# Demo Transcript — AI Travel Planner

> This transcript shows the current (working) flow after all fixes. It uses
> the Goa query — the case that originally resolved to the wrong country.

## Test Run: "Plan a 3-day trip to Goa for a couple in August, budget 15000 INR"

### Step 1: Preflight check

```
> python main.py
[preflight] Ollama model available: qwen2.5:7b-instruct
```

Only one model is required now — all three agents (Orchestrator, Writer,
and Reflection) use `qwen2.5:7b-instruct`.

### Step 2: User query entered

```
Where do you want to go / what do you want planned?
> Plan a 3-day trip to Goa for a couple in August, budget 15000 INR
```

Note: the user never mentions "India". The Orchestrator prompt now tells it
to infer the country/region from general knowledge, so it passes
`region='India'` (it is forbidden from passing `region='Goa'`).

### Step 3: Orchestrator ReAct loop

```
Orchestrator thinking...

[tool call] geocode_city({'city': 'Goa', 'region': 'India'})
[tool call] get_weather({'city': 'Goa', 'date': '2023-08-15', 'region': 'India'})
[tool call] get_exchange_rate({'base_currency': 'INR', 'target_currency': 'USD'})
[tool call] get_nearby_attractions({'city': 'Goa', 'radius_km': 10, 'region': 'India'})

[orchestrator] turn 2: no tool called. Model said: "Here are the facts gathered..."
```

All four tools were called. Note that `get_exchange_rate` was fetched
proactively even though the user did not ask for a conversion — the code
guard below will suppress any USD display.

### Step 4: Tool results

```
4 tool call(s) made:
  - geocode_city({'city': 'Goa', 'region': 'India'}) -> Goa, India -> lat=15.30, lon=74.08
      NOTE: Open-Meteo didn't confirm 'Goa' in 'India' (candidates mismatched).
      Resolved via Nominatim to 'Goa', India.
  - get_weather({'city': 'Goa', 'date': '2023-08-15', 'region': 'India'})
      -> HISTORICAL weather for Goa, EXACT DATE=2022-08-15: 24.3-29.2°C, 1.2mm precipitation.
  - get_exchange_rate({'base_currency': 'INR', 'target_currency': 'USD'}) -> 1 INR = 0.01045 USD
  - get_nearby_attractions({'city': 'Goa', 'radius_km': 10, 'region': 'India'})
      -> Verified places near Goa (sorted by distance): - Panchawadi (1893m away) - ...
```

**Why this used to be wrong:** Open-Meteo's database has no "Goa, India".
It only knows Genoa (Italy) and Goa (Philippines). Before the fix, this
query silently resolved to Genoa. Now, when Open-Meteo can't confirm the
region, `_geocode` falls back to Nominatim (OpenStreetMap), which correctly
returns the Indian state of Goa — no more Genoa, and no more "Goa, Norway"
(which happened when the model wrongly passed `region='Goa'` instead of
`region='India'`).

### Step 5: Hard guards applied

```
[guard] Attraction lookup succeeded — 20 places allow-listed
[guard] Exchange rate fetched but user didn't ask for conversion — suppressing cross-currency display
```

- The attraction guard extracts the 20 verified place names into an
  allow-list. The Writer may ONLY use these names.
- The exchange-rate guard fires because the user only asked for INR. It
  injects a directive forbidding any USD/EUR amounts in the itinerary.

### Step 6: Writer generates draft

```
Writer agent drafting itinerary...

[writer] Draft produced (Markdown)
```

The Writer prompt forbids fabrication: attraction names must come exactly
from the allow-list, budget amounts must be INR only (no conversion since
the user didn't ask), and weather must copy the EXACT DATE from the notes.

### Step 7: Reflection evaluation (deterministic)

```
Reflection agent evaluating draft...

[reflection] APPROVED
```

The Reflection is NOT an LLM. It runs three Python checks:

1. **Weather** — research notes contain `HISTORICAL weather for`, so the
   draft's 24.3-29.2°C is valid.
2. **Attractions** — every place phrase in the draft (e.g. "Panchawadi",
   "Menezes Braganza House") matches an allow-listed name.
3. **Currency** — user asked only for INR; draft shows only ₹, no `$`.

If any check fails it returns `REVISE: <issue>` and the Writer regenerates
once with the issue added to its context.

### Step 8: Final itinerary output

```
============================================================

## Trip overview
Visit Goa, India; 3 days; perfect for a couple looking to relax and explore.

## Day-by-day plan

### Day 1 — Arrival & Panchawadi
- **Weather:** On this date last year: 24.3-29.2°C (historical data, no live forecast available yet).
- **Morning:** Arrive and check into a hotel; explore nearby Panchawadi area.
- **Afternoon:** Relax on the beach, swim or sunbathe.
- **Evening:** Dinner at a local restaurant in Panchawadi.
- **Estimated day budget:** ₹3,000-4,000

### Day 2 — Old Town Exploration & Sanvordem
- **Weather:** On this date last year: 24.3-29.2°C (historical data).
- **Morning:** Visit the local market area near Sanvordem.
- **Afternoon:** Explore historical landmarks like Menezes Braganza House.
- **Evening:** Enjoy traditional Goan cuisine in Curchorem.
- **Estimated day budget:** ₹3,500-4,500

### Day 3 — Beach Day & Departure
- **Weather:** On this date last year: 24.3-29.2°C (historical data).
- **Morning:** Spend the morning at the beach; relax.
- **Afternoon:** Visit local railway stations for scenic views.
- **Evening:** Farewell dinner in Panchawadi.
- **Estimated day budget:** ₹3,000-4,000

## Budget summary
| Day   | Estimated Daily Budget |
|-------|------------------------|
| 1     | ₹3,000-4,000           |
| 2     | ₹3,500-4,500           |
| 3     | ₹3,000-4,000           |
| Total | ₹9,500-12,500          |

All amounts in INR only — no USD shown (user never asked).

## Practical notes
- Weather in August is typically warm with occasional light rain.
- Local transport includes taxis and shared autos.

## Packing list
- Light clothing suitable for warm weather
- Sunscreen and sunglasses
- Beachwear
- Comfortable walking shoes
- Rain gear

============================================================
```

All budgets in INR, all place names from the verified list, weather is real
historical data clearly labeled. The Goa bug — the reason this project
needed a re-evaluation — is fixed.

## Other validated test queries

| Query | Result |
|-------|--------|
| 4-day Manali, December, budget in INR | ✅ Correct geocode (India), weather historical, INR only |
| 2-day Paris, family, Sept, budget EUR + "also show cost in INR" | ✅ Shows EUR primary + INR converted at fetched rate (1 EUR = 109.808 INR) |
| 3-day Jaipur, October, budget 12000 INR | ✅ Reflection caught "Amber Fort" (not allow-listed) → removed in revision |
| 3-day Rishikesh, November, budget INR + "convert to USD as well" | ✅ USD conversion shown (user asked) — 10000 INR ≈ 97.65 USD |
| Weekend London, couple, budget 800 GBP | ✅ GBP only, verified London places, APPROVED |
| 3-day Varanasi, July, budget 8000 INR | ✅ INR only, verified places, APPROVED |
| 2-day Springfield, budget 500 USD | ✅ Resolves consistently to Springfield, Missouri (fuzzy region match), USD budget not flagged |
| "Tell me a joke" / "Hello" | ✅ Rejected with INVALID_QUERY, no tools called |
