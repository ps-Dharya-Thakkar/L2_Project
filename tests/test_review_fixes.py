"""
Tests for the mentor-review fixes:
  1. Tool-call completeness checker (plain-Python required-tool check)
  2. Freshness-aware query-cache TTL (weather expires in 2h, static 24h)
  3. Detailed weather validation in the reflection agent
  4. Robust fuzzy attraction allow-list matching
  5. Evaluation metrics (groundedness, similarity, retrieval, judge parse)
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main
import orchestrator
from main import _required_tools, _missing_required_tools, _user_wants_conversion
from reflection_agent import (_check_weather, _check_attractions, _norm,
                              run_reflection)
from place_validation import PlaceValidator
import query_cache
import evaluation


class TestToolCompleteness(unittest.TestCase):

    def test_required_always_geocode_weather_attractions(self):
        req = _required_tools("Plan a trip to Goa in August")
        self.assertIn("geocode_city", req)
        self.assertIn("get_weather", req)
        self.assertIn("get_nearby_attractions", req)
        self.assertNotIn("get_exchange_rate", req)

    def test_required_fx_when_conversion_requested(self):
        req = _required_tools("budget 2500 AED, also show in INR")
        self.assertIn("get_exchange_rate", req)

    def test_no_missing_when_all_called(self):
        tool_log = [
            {"tool": "geocode_city", "args": {}, "result": "ok"},
            {"tool": "get_weather", "args": {}, "result": "ok"},
            {"tool": "get_nearby_attractions", "args": {}, "result": "ok"},
        ]
        self.assertEqual(
            _missing_required_tools("Plan a trip to Goa", tool_log), set())

    def test_missing_detects_forgotten_weather(self):
        tool_log = [
            {"tool": "geocode_city", "args": {}, "result": "ok"},
            {"tool": "get_nearby_attractions", "args": {}, "result": "ok"},
        ]
        missing = _missing_required_tools("Plan a trip to Goa", tool_log)
        self.assertIn("get_weather", missing)

    def test_missing_detects_forgotten_fx(self):
        tool_log = [
            {"tool": "geocode_city", "args": {}, "result": "ok"},
            {"tool": "get_weather", "args": {}, "result": "ok"},
            {"tool": "get_nearby_attractions", "args": {}, "result": "ok"},
        ]
        missing = _missing_required_tools(
            "budget in INR, show USD too", tool_log)
        self.assertIn("get_exchange_rate", missing)


class TestQueryCacheFreshnessTTL(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_file = query_cache.CACHE_FILE
        query_cache.CACHE_FILE = os.path.join(self._tmp, "query_cache.json")
        query_cache._save({})

    def tearDown(self):
        query_cache.CACHE_FILE = self._orig_file

    def _entry(self, tools):
        return {"itinerary": "X", "tool_log": [
            {"tool": t, "args": {}, "result": "ok"} for t in tools],
            "guard_notes": [], "reflection_result": "APPROVED", "elapsed": 1.0}

    def test_static_entry_lives_24h(self):
        self.assertEqual(
            query_cache.effective_ttl(self._entry(["geocode_city"])),
            24 * 60 * 60)

    def test_weather_entry_ttl_is_2h(self):
        self.assertEqual(
            query_cache.effective_ttl(self._entry(["get_weather"])),
            2 * 60 * 60)

    def test_fx_entry_ttl_is_1h(self):
        self.assertEqual(
            query_cache.effective_ttl(self._entry(["get_exchange_rate"])),
            1 * 60 * 60)

    def test_mixed_entry_uses_minimum(self):
        ttl = query_cache.effective_ttl(
            self._entry(["get_weather", "get_exchange_rate"]))
        self.assertEqual(ttl, 1 * 60 * 60)

    def test_attractions_entry_24h(self):
        self.assertEqual(
            query_cache.effective_ttl(self._entry(["get_nearby_attractions"])),
            24 * 60 * 60)

    def test_get_expires_weather_before_static(self):
        # Weather plan written 3h ago must be a miss; static plan still hits.
        query_cache.set("weather query", self._entry(["get_weather"]))
        query_cache.set("static query", self._entry(["geocode_city"]))
        store = query_cache._load()
        store[query_cache._cache_key("weather query")]["ts"] -= 3 * 60 * 60
        store[query_cache._cache_key("static query")]["ts"] -= 3 * 60 * 60
        query_cache._save(store)
        self.assertIsNone(query_cache.get("weather query"))
        self.assertIsNotNone(query_cache.get("static query"))

    def test_legacy_entry_without_tool_log_uses_base_ttl(self):
        self.assertEqual(
            query_cache.effective_ttl({"itinerary": "Y"}),
            query_cache.QUERY_CACHE_TTL_SECONDS)


class TestDetailedWeatherValidation(unittest.TestCase):

    def test_approves_fetched_range_copied(self):
        notes = ("HISTORICAL weather for Goa, EXACT DATE=2022-08-15: "
                 "24.3-29.2°C, 0mm precipitation.")
        draft = ("- Weather: On this date last year: 24.3-29.2°C\n"
                 "- Estimated day budget: ₹3,000-4,000")
        self.assertIsNone(_check_weather(notes, draft))

    def test_flags_invented_temperature(self):
        notes = ("LIVE forecast for Goa on 2026-08-28: 18-31°C, "
                 "rain chance 12%")
        draft = "- Weather: Expect 5-9°C during your stay"
        result = _check_weather(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("not grounded", result)

    def test_flags_historical_sold_as_forecast(self):
        notes = ("HISTORICAL weather for Manali, EXACT DATE=2025-12-15: "
                 "-2-9°C, 5mm precipitation.")
        draft = ("- Weather: The forecast for Manali in December is "
                 "-2-9°C with some snowfall.")
        result = _check_weather(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("HISTORICAL", result)

    def test_flags_wrong_year_for_historical(self):
        notes = ("HISTORICAL weather for Udaipur, EXACT DATE=2025-09-15: "
                 "24-30°C, 0mm precipitation.")
        draft = ("- Weather: On 2024-09-15 last year it was 24-30°C "
                 "(historical data).")
        result = _check_weather(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("year mismatch", result)

    def test_live_tolerance_rounding_allowed(self):
        # draft rounds 24.3-29.2 to 24-29 — must be accepted (0.6 tolerance)
        notes = ("LIVE forecast for Goa on 2026-08-28: 24.3-29.2°C, "
                 "rain chance 10%")
        draft = "- Weather: Real forecast: 24-29°C today."
        self.assertIsNone(_check_weather(notes, draft))

    def test_flags_dropped_weather_section(self):
        notes = ("LIVE forecast for Goa on 2026-08-28: 18-31°C, "
                 "rain chance 12%")
        draft = "- Morning: Relax at the beach"
        result = _check_weather(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("never mentions", result)


class TestRobustAttractionCheck(unittest.TestCase):

    def test_norm_transliterates_accents(self):
        self.assertEqual(_norm("Saint Étienne, Paris"), "saint etienne paris")

    def test_multiword_place_approved(self):
        notes = ("Verified places near Udaipur (sorted by distance):\n"
                 "  - Bohra Ganesh Temple (300m away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Bohra Ganesh Temple\n")
        draft = "- Morning: Visit Bohra Ganesh Temple"
        self.assertIsNone(_check_attractions(notes, draft))

    def test_visit_verb_not_false_positive(self):
        notes = ("Verified places near Paris (sorted by distance):\n"
                 "  - Cathedral of Saint Étienne, Paris (1m away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Cathedral of Saint Étienne, Paris\n")
        draft = "- Morning: Visit Cathedral of Saint Étienne, Paris."
        self.assertIsNone(_check_attractions(notes, draft))

    def test_hallucinated_place_still_flagged(self):
        notes = ("Verified places near Paris (sorted by distance):\n"
                 "  - Cathedral of Saint Étienne, Paris (1m away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Cathedral of Saint Étienne, Paris\n")
        draft = "- Morning: Visit Eiffel Tower for views."
        result = _check_attractions(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("Eiffel", result)

    def test_writer_dropped_trailing_city_still_approved(self):
        # Writer may drop ', Paris' — full allow name contains the draft name
        notes = ("Verified places near Paris (sorted by distance):\n"
                 "  - Church of Saint-Jean-le-Rond, Paris (18m away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Church of Saint-Jean-le-Rond, Paris\n")
        draft = "- Afternoon: Visit Church of Saint-Jean-le-Rond"
        self.assertIsNone(_check_attractions(notes, draft))


class TestEvaluationMetrics(unittest.TestCase):

    def test_groundedness_perfect_when_all_fetched(self):
        notes = ("HISTORICAL weather for Goa, EXACT DATE=2022-08-15: "
                 "24.3-29.2°C, 0mm precipitation.\n"
                 "1 INR = 0.01045 USD")
        draft = ("- Weather: On this date last year: 24-29°C\n"
                 "- Budget: 1 INR = 0.01045 USD")
        self.assertEqual(evaluation.groundedness(draft, notes), 1.0)

    def test_groundedness_low_when_invented(self):
        notes = ("HISTORICAL weather for Goa, EXACT DATE=2022-08-15: "
                 "24.3-29.2°C, 0mm precipitation.")
        draft = "- Weather: Expect -5-2°C and heavy snow"
        self.assertLess(evaluation.groundedness(draft, notes), 1.0)

    def test_similarity_identical_is_1(self):
        self.assertEqual(evaluation.cosine_similarity("Goa beach trip",
                                                      "Goa beach trip"), 1.0)

    def test_similarity_disjoint_is_0(self):
        self.assertEqual(evaluation.cosine_similarity("Goa beach trip",
                                                      "Moscow winter tour"), 0.0)

    def test_faithfulness_equals_groundedness(self):
        notes = "LIVE forecast for Goa on 2026-08-28: 18-31°C, rain 12%"
        draft = "- Weather: 18-31°C"
        self.assertEqual(evaluation.faithfulness(draft, notes),
                         evaluation.groundedness(draft, notes))

    def test_response_accuracy_flags_stray_date(self):
        notes = "HISTORICAL weather for Goa, EXACT DATE=2022-08-15: 24.3-29.2°C"
        draft = "- Weather on 2024-01-01: 24-29°C"
        acc = evaluation.response_accuracy(draft, notes)
        self.assertFalse(acc["checks"][0]["pass"])
        self.assertLess(acc["score"], 1.0)

    def test_retrieval_precision(self):
        notes = ("Verified places near Goa (sorted by distance):\n"
                 "  - Baga Beach (300m away)\n"
                 "  - Calangute Beach (900m away)\n"
                 "  - Panjim (2km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Baga Beach\n- Calangute Beach\n- Panjim\n")
        draft = "- Morning: Relax at Baga Beach"
        ret = evaluation.retrieval_metrics(draft, notes, k=5)
        # 1 of 3 retrieved used -> precision ~0.33, precision@5 ~0.33
        self.assertAlmostEqual(ret["precision"], 1 / 3, places=3)
        self.assertAlmostEqual(ret["precision@5"], 1 / 3, places=3)
        self.assertEqual(ret["relevant_count"], 1)

    def test_retrieval_ignores_instruction_line(self):
        """The ALLOW-LIST guard block ends with an instruction sentence — it
        must NOT be counted as a retrieved place."""
        notes = ("*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Baga Beach\n- Calangute Beach\n"
                 "Any landmark/temple/market/viewpoint name in your itinerary "
                 "MUST be copied exactly from this list.\n"
                 "*** HARD CONSTRAINT — NO EXCHANGE RATE WAS FETCHED ***")
        ret = evaluation._extract_retrieved(notes)
        self.assertEqual(len(ret), 2)

    def test_judge_json_parsing(self):
        raw = '```json\n{"relevance": 8, "accuracy": 9, "completeness": 7, "overall": 8, "justification": "Good"}\n```'
        out = evaluation._parse_judge_json(raw)
        self.assertEqual(out["relevance"], 8)
        self.assertEqual(out["overall"], 8)

    def test_evaluate_logs_and_returns(self):
        notes = "LIVE forecast for Goa on 2026-08-28: 18-31°C, rain 12%"
        metrics = evaluation.evaluate("trip to Goa", "- Weather: 18-31°C",
                                      notes, with_llm_judge=False)
        self.assertIn("similarity_score", metrics)
        self.assertIn("groundedness", metrics)
        self.assertIn("retrieval", metrics)
        self.assertIn("response_accuracy", metrics)


class TestEdgeCaseAttractionDetection(unittest.TestCase):
    """Reviewer asked for creative handling of landmarks whose names carry NO
    obvious type keyword (Charminar, Marine Drive, Times Square, Jantar
    Mantar). The old keyword-only pattern let these hallucinated names slip
    through."""

    def test_charminar_flagged_when_not_allowlisted(self):
        notes = ("Verified places near Hyderabad (sorted by distance):\n"
                 "  - Chowmahalla Palace (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Chowmahalla Palace\n")
        draft = "- Morning: Visit Charminar for views over the old city."
        result = _check_attractions(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("Charminar", result)

    def test_charminar_approved_when_allowlisted(self):
        notes = ("Verified places near Hyderabad (sorted by distance):\n"
                 "  - Charminar (500m away)\n"
                 "  - Chowmahalla Palace (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Charminar, Hyderabad\n"
                 "- Chowmahalla Palace\n")
        draft = "- Morning: Visit Charminar, then Chowmahalla Palace."
        self.assertIsNone(_check_attractions(notes, draft))

    def test_marine_drive_flagged_when_not_allowlisted(self):
        notes = ("Verified places near Mumbai (sorted by distance):\n"
                 "  - Gateway of India (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Gateway of India\n")
        draft = "- Evening: Walk along Marine Drive, then visit the Gateway."
        result = _check_attractions(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("Marine Drive", result)

    def test_times_square_flagged_when_not_allowlisted(self):
        notes = ("Verified places near New York (sorted by distance):\n"
                 "  - Central Park (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Central Park\n")
        draft = "- Morning: Stroll through Times Square for a photo stop."
        result = _check_attractions(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("Times Square", result)

    def test_jantar_mantar_flagged_when_not_allowlisted(self):
        notes = ("Verified places near Jaipur (sorted by distance):\n"
                 "  - Hawa Mahal (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Hawa Mahal\n")
        draft = "- Afternoon: Visit Jantar Mantar."
        result = _check_attractions(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("Jantar Mantar", result)

    def test_bold_place_detected(self):
        notes = ("Verified places near Hyderabad (sorted by distance):\n"
                 "  - Chowmahalla Palace (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Chowmahalla Palace\n")
        draft = "- Morning: Visit **Charminar** for breakfast."
        result = _check_attractions(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("Charminar", result)

    def test_destination_not_flagged(self):
        notes = ("Goa, India -> lat=15.3, lon=74.08\n"
                 "Verified places near Goa (sorted by distance):\n"
                 "  - Baga Beach (300m away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Baga Beach\n")
        draft = ("## Day 1\n"
                 "- Fly into Goa, then head north to North Goa.\n"
                 "- Morning: Relax at Baga Beach")
        self.assertIsNone(_check_attractions(notes, draft))

    def test_template_words_not_flagged(self):
        notes = ("Goa, India -> lat=15.3, lon=74.08\n"
                 "HISTORICAL weather for Goa, EXACT DATE=2022-08-15: "
                 "24.3-29.2°C\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Baga Beach\n")
        draft = ("## Trip overview\nGoa, India; 3 days.\n"
                 "## Day 1\n- Weather: On this date last year: 24.3-29.2°C\n"
                 "- Morning: Relax at Baga Beach\n"
                 "- Estimated day budget: ₹3,000")
        self.assertIsNone(_check_attractions(notes, draft))

    def test_generic_type_word_not_flagged(self):
        notes = ("Goa, India -> lat=15.3, lon=74.08\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Baga Beach\n")
        draft = ("- Morning: Spend the morning at the beach.\n"
                 "- Afternoon: Explore the old fort nearby.")
        self.assertIsNone(_check_attractions(notes, draft))

    def test_fail_lookup_flags_charminar(self):
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "*** HARD CONSTRAINT — ATTRACTION LOOKUP FAILED ***")
        draft = "- Morning: Visit Charminar for the views."
        result = _check_attractions(notes, draft)
        self.assertIsNotNone(result)
        self.assertIn("Charminar", result)

    def test_reflection_end_to_end_charminar(self):
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "HISTORICAL weather for Hyderabad, EXACT DATE=2022-08-15: "
                 "23-32°C\n"
                 "Verified places near Hyderabad (sorted by distance):\n"
                 "  - Chowmahalla Palace (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Chowmahalla Palace\n")
        draft = ("## Day 1\n- Weather: On this date last year: 23-32°C\n"
                 "- Morning: Visit Charminar, then Chowmahalla Palace.")
        result = run_reflection("trip to Hyderabad", notes, draft)
        self.assertIn("REVISE", result)
        self.assertIn("Charminar", result)


class TestLayer3GeocodeValidation(unittest.TestCase):
    """Layer 3 of the hybrid pipeline: candidate names that fail the rule-based
    allow-list match are geocoded. Real places near the destination are kept;
    names that don't exist (or exist far away) are flagged. This is the
    'prove it's a real place' step, not just 'does it look like a place'."""

    def _validator(self, locations):
        def lookup(name, city=""):
            loc = locations.get(name.lower())
            if loc is None:
                return None
            lat, lon = loc
            return {"lat": lat, "lon": lon, "name": name,
                    "display_name": f"{name}, Testland"}
        return PlaceValidator(lookup=lookup)

    def test_real_place_outside_allowlist_is_verified(self):
        # 'Charminar' is a genuine landmark near Hyderabad but was missed by
        # the attraction lookup. Geocoding proves it exists -> APPROVED.
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "Verified places near Hyderabad (sorted by distance):\n"
                 "  - Chowmahalla Palace (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Chowmahalla Palace\n")
        draft = "- Morning: Visit Charminar, then Chowmahalla Palace."
        v = self._validator({"charminar": (17.3833, 78.4746)})
        self.assertIsNone(_check_attractions(notes, draft, validator=v))

    def test_fake_place_still_flagged(self):
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "Verified places near Hyderabad (sorted by distance):\n"
                 "  - Chowmahalla Palace (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Chowmahalla Palace\n")
        draft = "- Morning: Visit Dragon Moon Palace for the views."
        v = self._validator({})  # nothing geocodes
        result = _check_attractions(notes, draft, validator=v)
        self.assertIsNotNone(result)
        self.assertIn("Dragon Moon Palace", result)

    def test_real_but_far_away_is_flagged(self):
        # Marine Drive exists (in Mumbai) but is ~700km from Hyderabad —
        # wrong destination, so it must still be flagged.
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "Verified places near Hyderabad (sorted by distance):\n"
                 "  - Chowmahalla Palace (1km away)\n"
                 "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
                 "- Chowmahalla Palace\n")
        draft = "- Evening: Walk along Marine Drive."
        v = self._validator({"marine drive": (18.9356, 72.8246)})
        result = _check_attractions(notes, draft, validator=v)
        self.assertIsNotNone(result)
        self.assertIn("Marine Drive", result)

    def test_fail_lookup_real_place_approved_with_validator(self):
        # Attraction lookup failed, but the candidate geocodes to a real place
        # near the destination -> verified, not a blanket rejection.
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "*** HARD CONSTRAINT — ATTRACTION LOOKUP FAILED ***")
        draft = "- Morning: Visit Charminar for the views."
        v = self._validator({"charminar": (17.3833, 78.4746)})
        self.assertIsNone(_check_attractions(notes, draft, validator=v))

    def test_fail_lookup_fake_place_flagged_even_with_validator(self):
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "*** HARD CONSTRAINT — ATTRACTION LOOKUP FAILED ***")
        draft = "- Morning: Visit Dragon Moon Palace for the views."
        v = self._validator({})
        result = _check_attractions(notes, draft, validator=v)
        self.assertIsNotNone(result)
        self.assertIn("Dragon Moon Palace", result)

    def test_run_reflection_with_validator(self):
        notes = ("Hyderabad, India -> lat=17.38, lon=78.47\n"
                 "HISTORICAL weather for Hyderabad, EXACT DATE=2022-08-15: "
                 "23-32°C\n"
                 "*** HARD CONSTRAINT — ATTRACTION LOOKUP FAILED ***")
        draft = ("## Day 1\n- Weather: On this date last year: 23-32°C\n"
                 "- Morning: Visit Charminar.")
        v = self._validator({"charminar": (17.3833, 78.4746)})
        self.assertEqual(
            run_reflection("trip to Hyderabad", notes, draft, validator=v),
            "APPROVED")


class _FakeMCPClient:
    """Minimal stand-in for MCPToolClient — just enough surface for
    run_orchestrator()/_complete_missing_tool_calls() to call tools without
    talking to a real MCP server."""

    async def list_tools_for_ollama(self):
        return []

    async def call_tool(self, name, args):
        return f"fake-result-for-{name}"


class TestToolCallLatencyObservability(unittest.TestCase):
    """Reviewer requirement #1: every MCP tool call is timed with
    time.perf_counter() and the tool_log entry gets an elapsed_seconds
    field, without changing the existing tool/args/result fields."""

    def test_orchestrator_tool_log_has_elapsed_seconds(self):
        tool_call_response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "geocode_city",
                                   "arguments": {"city": "Goa", "region": "India"}},
                     "id": "1"},
                ],
            }
        }
        stop_response = {"message": {"content": "done gathering facts",
                                     "tool_calls": None}}
        calls = {"n": 0}

        def fake_chat(messages, tools, model):
            calls["n"] += 1
            return tool_call_response if calls["n"] == 1 else stop_response

        with patch.object(orchestrator.llm, "chat", side_effect=fake_chat):
            messages, tool_log = asyncio.run(
                orchestrator.run_orchestrator("Plan a trip to Goa", _FakeMCPClient()))

        self.assertEqual(len(tool_log), 1)
        entry = tool_log[0]
        # existing fields preserved
        self.assertEqual(entry["tool"], "geocode_city")
        self.assertEqual(entry["args"], {"city": "Goa", "region": "India"})
        self.assertEqual(entry["result"], "fake-result-for-geocode_city")
        # new field
        self.assertIn("elapsed_seconds", entry)
        self.assertIsInstance(entry["elapsed_seconds"], float)
        self.assertGreaterEqual(entry["elapsed_seconds"], 0.0)

    def test_completeness_fallback_tool_log_has_elapsed_seconds(self):
        tool_call_response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_weather",
                                   "arguments": {"city": "Goa", "date": "",
                                                 "region": "India"}}},
                ],
            }
        }
        starting_log = [
            {"tool": "geocode_city", "args": {}, "result": "ok",
             "elapsed_seconds": 0.001},
            {"tool": "get_nearby_attractions", "args": {}, "result": "ok",
             "elapsed_seconds": 0.001},
        ]

        with patch.object(main.llm, "chat", return_value=tool_call_response):
            tool_log = asyncio.run(main._complete_missing_tool_calls(
                "Plan a trip to Goa in August", _FakeMCPClient(),
                starting_log, max_rounds=1))

        weather_entries = [t for t in tool_log if t["tool"] == "get_weather"]
        self.assertEqual(len(weather_entries), 1)
        self.assertIn("elapsed_seconds", weather_entries[0])
        self.assertIsInstance(weather_entries[0]["elapsed_seconds"], float)
        self.assertGreaterEqual(weather_entries[0]["elapsed_seconds"], 0.0)
        # pre-existing entries untouched
        self.assertEqual(tool_log[0]["elapsed_seconds"], 0.001)


class TestComponentLatencyStructure(unittest.TestCase):
    """Reviewer requirement #2: component-level latency dict has the
    expected keys, non-negative values, and revision_writer_seconds is only
    populated when a revision actually happens."""

    def test_empty_latency_has_expected_keys(self):
        latency = main._empty_latency()
        expected = {
            "mcp_connect_seconds", "orchestrator_seconds",
            "tool_completion_seconds", "writer_seconds",
            "reflection_seconds", "revision_writer_seconds",
            "evaluation_seconds", "total_seconds",
        }
        self.assertEqual(set(latency.keys()), expected)
        self.assertIsNone(latency["revision_writer_seconds"])
        for key, value in latency.items():
            if key == "revision_writer_seconds":
                continue
            self.assertIsInstance(value, float)
            self.assertGreaterEqual(value, 0.0)

    def test_plan_trip_measures_real_total_latency_no_revision(self):
        """End-to-end (heavily mocked) run confirming metrics['latency']
        exists inside the returned metrics dict, the 5-tuple contract is
        unchanged, and total_seconds is a real measured value — not the old
        hardcoded 0.0 — while revision_writer_seconds stays None because
        reflection approved on the first pass."""

        class FakeClient:
            async def connect(self):
                await asyncio.sleep(0.001)

            async def close(self):
                pass

        async def fake_run_orchestrator(query, client, max_turns=5):
            await asyncio.sleep(0.001)
            return (
                [{"role": "assistant", "content": "gathered facts"}],
                [
                    {"tool": "geocode_city", "args": {}, "result": "ok",
                     "elapsed_seconds": 0.001},
                    {"tool": "get_weather", "args": {}, "result": "ok",
                     "elapsed_seconds": 0.001},
                    {"tool": "get_nearby_attractions", "args": {}, "result":
                     "Verified places near Goa (sorted by distance):\n"
                     "  - Baga Beach (1km away)\n", "elapsed_seconds": 0.001},
                ],
            )

        async def fake_complete_missing(query, client, tool_log, max_rounds=2):
            return tool_log

        def fake_run_writer(query, notes):
            return "## Day 1\n- Morning: Relax at Baga Beach"

        def fake_run_reflection(query, notes, itinerary, validator=None):
            return "APPROVED"

        with patch.object(main, "MCPToolClient", return_value=FakeClient()), \
             patch.object(main, "run_orchestrator", side_effect=fake_run_orchestrator), \
             patch.object(main, "_complete_missing_tool_calls", side_effect=fake_complete_missing), \
             patch.object(main, "run_writer", side_effect=fake_run_writer), \
             patch.object(main, "run_reflection", side_effect=fake_run_reflection), \
             patch.object(evaluation, "llm_as_judge",
                          return_value={"relevance": 9, "accuracy": 8,
                                        "completeness": 9, "overall": 9}), \
             patch.dict(os.environ, {"RUN_LLM_JUDGE": "1"}):
            result = asyncio.run(
                main.plan_trip("Plan a 3-day trip to Goa for 4 friends in December"))

        # 5-tuple contract unchanged
        itinerary, tool_log, guard_notes, reflection_result, metrics = result
        self.assertIsInstance(itinerary, str)
        self.assertIsInstance(tool_log, list)
        self.assertIsInstance(metrics, dict)

        self.assertIn("latency", metrics)
        lat = metrics["latency"]
        self.assertGreater(lat["total_seconds"], 0.0)
        self.assertIsNone(lat["revision_writer_seconds"])
        self.assertIn("llm_judge", metrics)
        self.assertEqual(metrics["llm_judge"]["relevance"], 9)


class TestLLMJudgeDefaultOn(unittest.TestCase):
    """Reviewer requirement #3: relevance evaluation (LLM-as-a-judge) runs
    by default, without needing RUN_LLM_JUDGE=1, while still failing
    gracefully if the judge model/provider is unavailable."""

    def setUp(self):
        self._orig = os.environ.pop("RUN_LLM_JUDGE", None)

    def tearDown(self):
        if self._orig is not None:
            os.environ["RUN_LLM_JUDGE"] = self._orig
        else:
            os.environ.pop("RUN_LLM_JUDGE", None)

    def test_enabled_by_default_with_no_env_var_set(self):
        self.assertTrue(main._llm_judge_enabled())

    def test_can_be_explicitly_disabled(self):
        os.environ["RUN_LLM_JUDGE"] = "0"
        self.assertFalse(main._llm_judge_enabled())
        os.environ["RUN_LLM_JUDGE"] = "false"
        self.assertFalse(main._llm_judge_enabled())

    def test_evaluate_invokes_judge_by_default_when_mocked(self):
        """The normal evaluation flow now calls the judge without any env
        var — mocked here so the test doesn't depend on a live model."""
        with patch.object(evaluation, "llm_as_judge",
                          return_value={"relevance": 9, "accuracy": 8,
                                        "completeness": 9, "overall": 9}):
            metrics = evaluation.evaluate(
                "trip to Goa", "- Weather: 18-31°C",
                "LIVE forecast for Goa on 2026-08-28: 18-31°C, rain 12%",
                with_llm_judge=main._llm_judge_enabled())
        self.assertIn("llm_judge", metrics)
        self.assertEqual(metrics["llm_judge"]["relevance"], 9)

    def test_graceful_failure_when_judge_unavailable(self):
        """If the judge provider throws, llm_as_judge() must catch it and
        return an error marker — evaluation must still complete."""
        with patch("llm.chat", side_effect=ConnectionError("provider down")):
            result = evaluation.llm_as_judge("trip to Goa", "- Day 1: relax")
        self.assertIn("error", result)


class TestJudgePromptCoversConstraints(unittest.TestCase):
    """Relevance must be judged against explicit user constraints, including
    currency (not just budget as a number)."""

    def test_prompt_mentions_currency_and_core_constraints(self):
        prompt = evaluation.LLM_JUDGE_PROMPT.lower()
        for term in ("destination", "duration", "travelers", "budget", "currency"):
            self.assertIn(term, prompt)


class TestRetrievalMetricRename(unittest.TestCase):
    """Reviewer requirement #5: the old 'recall' key (mathematically
    guaranteed to read 1.0 — it was recall against its own 'used' set) is
    gone, replaced by an honestly-named allowlist_utilization metric."""

    _NOTES = ("*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
              "- Baga Beach\n- Calangute Beach\n- Panjim\n")
    _DRAFT = "- Morning: Relax at Baga Beach"

    def test_recall_key_removed(self):
        ret = evaluation.retrieval_metrics(self._DRAFT, self._NOTES)
        self.assertNotIn("recall", ret)

    def test_allowlist_utilization_present_and_matches_precision(self):
        ret = evaluation.retrieval_metrics(self._DRAFT, self._NOTES)
        self.assertIn("allowlist_utilization", ret)
        self.assertEqual(ret["allowlist_utilization"], ret["precision"])
        self.assertAlmostEqual(ret["allowlist_utilization"], 1 / 3, places=3)

    def test_empty_allowlist_reports_zero_utilization_not_recall(self):
        ret = evaluation.retrieval_metrics("- Morning: Relax", "no allow list here")
        self.assertNotIn("recall", ret)
        self.assertEqual(ret["allowlist_utilization"], 0.0)


if __name__ == "__main__":
    unittest.main()