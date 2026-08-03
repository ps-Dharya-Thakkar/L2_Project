"""
Tests for the code-level reflection agent — deterministic checks that
replace the unreliable LLM-based reflection.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reflection_agent import run_reflection


def test_approves_when_all_data_present():
    notes = (
        "Goa, India -> lat=15.3, lon=74.08\n"
        "HISTORICAL weather for Goa, EXACT DATE=2022-08-15: 24.3-29.2°C\n"
        "Verified places near Goa (sorted by distance):\n"
        "  - Baga Beach (300m away)\n"
        "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
        "- Baga Beach\n"
        "- Panchawadi\n"
        "*** HARD CONSTRAINT — USER DID NOT ASK FOR CONVERSION ***"
    )
    draft = (
        "## Trip overview\nGoa, India; 3 days; a couple.\n"
        "## Day 1\n- Weather: On this date last year: 24.3-29.2°C\n"
        "- Morning: Relax at Baga Beach\n"
        "- Estimated day budget: ₹3,000-4,000"
    )
    assert run_reflection("trip to Goa", notes, draft) == "APPROVED"


def test_flags_usd_when_no_exchange_rate():
    notes = (
        "Goa, India -> lat=15.3, lon=74.08\n"
        "*** HARD CONSTRAINT — NO EXCHANGE RATE WAS FETCHED ***"
    )
    draft = (
        "## Day 1\n"
        "- Estimated day budget: ₹4,000 (~$48)"
    )
    result = run_reflection("trip to Goa, budget in INR", notes, draft)
    assert "REVISE" in result
    assert "currency" in result.lower()


def test_flags_usd_when_conversion_not_requested():
    notes = (
        "1 INR = 0.01045 USD\n"
        "*** HARD CONSTRAINT — USER DID NOT ASK FOR CONVERSION ***"
    )
    draft = "Estimated day budget: ₹4,000 (~$48)"
    result = run_reflection("trip to Goa, budget 15000 INR", notes, draft)
    assert "REVISE" in result
    assert "conversion" in result.lower()


def test_approves_inr_only_even_when_rate_fetched():
    notes = (
        "1 INR = 0.01045 USD\n"
        "*** HARD CONSTRAINT — USER DID NOT ASK FOR CONVERSION ***"
    )
    draft = "Estimated day budget: ₹4,000"
    assert run_reflection("trip to Goa, budget 15000 INR", notes, draft) == "APPROVED"


def test_flags_place_not_in_allowlist():
    notes = (
        "Verified places near Manali (sorted by distance):\n"
        "  - Hadimba Temple (300m away)\n"
        "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
        "- Hadimba Temple\n"
    )
    draft = (
        "## Day 1\n"
        "- Morning: Visit Hadimba Temple, then Solang Valley"
    )
    result = run_reflection("trip to Manali", notes, draft)
    assert "REVISE" in result
    assert "Solang" in result


def test_approves_when_place_in_allowlist():
    notes = (
        "Verified places near Manali (sorted by distance):\n"
        "  - Hadimba Temple (300m away)\n"
        "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
        "- Hadimba Temple\n"
    )
    draft = (
        "## Day 1\n"
        "- Morning: Visit Hadimba Temple"
    )
    assert run_reflection("trip to Manali", notes, draft) == "APPROVED"


def test_flags_weather_numbers_when_no_weather_fetched():
    notes = (
        "Goa, India -> lat=15.3, lon=74.08\n"
        "*** HARD CONSTRAINT — NO WEATHER DATA WAS FETCHED ***"
    )
    draft = (
        "## Day 1\n"
        "- Weather: Expect 15-22°C during your stay"
    )
    result = run_reflection("trip to Goa", notes, draft)
    assert "REVISE" in result
    assert "weather" in result.lower()


def test_approves_place_prefixed_by_generic_verb():
    """'Visit Cathedral' where 'Cathedral of Saint Étienne, Paris' is in the
    allow-list must NOT be flagged — 'Visit' is a generic verb."""
    notes = (
        "Verified places near Paris (sorted by distance):\n"
        "  - Cathedral of Saint Étienne, Paris (1m away)\n"
        "  - Church of Saint-Jean-le-Rond, Paris (18m away)\n"
        "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
        "- Cathedral of Saint Étienne, Paris\n"
        "- Church of Saint-Jean-le-Rond, Paris\n"
    )
    draft = (
        "## Day 1\n"
        "- Morning: Visit Cathedral of Saint Étienne, Paris — explore history.\n"
        "- Afternoon: Visit Church of Saint-Jean-le-Rond, Paris."
    )
    assert run_reflection("trip to Paris", notes, draft) == "APPROVED"


def test_flags_hallucinated_place_with_generic_verb():
    """'Visit Eiffel Tower' when Eiffel Tower is NOT in the allow-list must
    be flagged even though 'Visit' is a generic verb."""
    notes = (
        "Verified places near Paris (sorted by distance):\n"
        "  - Cathedral of Saint Étienne, Paris (1m away)\n"
        "*** ALLOW-LIST — ONLY THESE PLACE NAMES MAY BE USED ***\n"
        "- Cathedral of Saint Étienne, Paris\n"
    )
    draft = (
        "## Day 1\n"
        "- Morning: Visit Eiffel Tower for views over the city."
    )
    result = run_reflection("trip to Paris", notes, draft)
    assert "REVISE" in result
    assert "Eiffel" in result


def test_approves_usd_budget_when_user_asked_usd():
    """A USD budget (e.g. Springfield 'budget 500 USD') with no exchange
    rate fetched must NOT be flagged — $ is the user's own currency."""
    notes = "*** HARD CONSTRAINT — NO EXCHANGE RATE WAS FETCHED ***"
    draft = (
        "## Budget summary\n"
        "Day 1  $250\n"
        "Day 2  $250\n"
        "Total  $500"
    )
    assert run_reflection("trip to Springfield, budget 500 USD", notes, draft) == "APPROVED"


def test_flags_usd_when_inr_budget_and_no_rate():
    """INR budget but draft shows USD without a fetched rate → flag."""
    notes = "*** HARD CONSTRAINT — NO EXCHANGE RATE WAS FETCHED ***"
    draft = "Estimated day budget: ₹4,000 (~$48)"
    result = run_reflection("trip to Goa, budget in INR", notes, draft)
    assert "REVISE" in result
