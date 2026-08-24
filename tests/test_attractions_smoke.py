"""
Smoke tests for MCP tool functions — imported directly from mcp_server.
These test the underlying Python functions, not the MCP transport layer.
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from mcp_server import (
    _filter_attractions, geocode_city, get_exchange_rate,
    _get_nearby_attractions_uncached, _merge_candidates,
    _destination_aware_search, _rank_by_popularity, _diversify,
)


def test_filter_attractions_removes_non_attractions():
    fake = [
        {"title": "Kudchade railway station", "dist": 4000},
        {"title": "Sanvordem Assembly constituency", "dist": 5000},
        {"title": "Quepem taluka", "dist": 9000},
        {"title": "Rachol Fort", "dist": 9000},
        {"title": "Menezes Braganza House", "dist": 6000},
    ]
    out = _filter_attractions(fake)
    titles = [p["title"] for p in out]
    assert "Rachol Fort" in titles
    assert "Menezes Braganza House" in titles
    assert "Kudchade railway station" not in titles
    assert "Sanvordem Assembly constituency" not in titles
    assert "Quepem taluka" not in titles


def test_filter_attractions_ranks_landmarks_first():
    fake = [
        {"title": "Sanvordem", "dist": 1000},
        {"title": "Rachol Fort", "dist": 9000},
        {"title": "Nanda Lake", "dist": 7577},
    ]
    out = _filter_attractions(fake)
    assert out[0]["title"] in ("Rachol Fort", "Nanda Lake"), \
        f"Landmark-type titles should rank first, got {out}"


def test_filter_attractions_ranks_suffix_landmarks_first():
    """'Charminar' carries no type word (only the suffix 'minar') — it must
    rank ahead of a minor garden so the writer sees the city's icon."""
    fake = [
        {"title": "Public Gardens", "dist": 2000},
        {"title": "Charminar", "dist": 3000},
        {"title": "Hussain Sagar", "dist": 6000},
        {"title": "Ameerpet", "dist": 4000},
    ]
    out = _filter_attractions(fake)
    titles = [p["title"] for p in out]
    assert titles == ["Charminar", "Hussain Sagar", "Public Gardens", "Ameerpet"], \
        f"Suffix landmarks should lead, got {titles}"


def test_filter_attractions_keeps_golconda_fort_style_titles():
    fake = [
        {"title": "Golconda Fort", "dist": 8500},
        {"title": "Qutb Shahi Tombs", "dist": 12000},
        {"title": "Hawa Mahal", "dist": 5000},
    ]
    out = _filter_attractions(fake)
    titles = [p["title"] for p in out]
    assert titles == ["Hawa Mahal", "Golconda Fort", "Qutb Shahi Tombs"], \
        f"Closer landmark suffix names sort first, got {titles}"


def test_geocode_city_returns_string():
    result = geocode_city("London")
    assert isinstance(result, str), f"Expected string, got {type(result)}"
    assert len(result) > 0, "Expected non-empty result"


def test_geocode_city_with_region():
    result = geocode_city("Manali", "Himachal Pradesh")
    assert isinstance(result, str)
    assert "ERROR" not in result, f"Unexpected error: {result}"


def test_geocode_city_nonexistent():
    result = geocode_city("Xyzzyville")
    assert "No location found" in result, f"Expected 'No location found', got: {result}"


def test_get_exchange_rate_returns_string():
    result = get_exchange_rate("USD", "INR")
    assert isinstance(result, str), f"Expected string, got {type(result)}"
    assert len(result) > 0
    assert "Error" not in result, f"Unexpected error: {result}"
    # Should contain a numeric rate
    assert "=" in result, f"Expected '=' in result, got: {result}"
    parts = result.split("=")
    assert len(parts) > 1
    rate_value = parts[1].strip().split()[0]
    float(rate_value)  # should not raise


def test_get_exchange_rate_invalid_currency():
    result = get_exchange_rate("USD", "INVALID")
    assert "Error" in result or isinstance(result, str)


def test_get_exchange_rate_roundtrip():
    result = get_exchange_rate("EUR", "USD")
    assert "Error" not in result, f"Unexpected error: {result}"
    assert "EUR" in result and "USD" in result


# ---------------------------------------------------------------------------
# Hybrid candidate-generation pipeline (Source A + Source B) tests.
# All external HTTP is mocked — no real network access required.
# ---------------------------------------------------------------------------

def _json_response(payload):
    m = MagicMock()
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def _geosearch_payload(items):
    return {"query": {"geosearch": items}}


def _search_payload(titles):
    return {"query": {"search": [{"title": t} for t in titles]}}


def _pageviews_payload(views_by_title):
    pages = {}
    for i, (title, total) in enumerate(views_by_title.items()):
        pages[str(i)] = {"title": title, "pageviews": {"2026-01-01": total}}
    return {"query": {"pages": pages}}


def _coordinates_payload(coords_by_title):
    pages = {}
    for i, (title, latlon) in enumerate(coords_by_title.items()):
        entry = {"title": title}
        if latlon is not None:
            entry["coordinates"] = [{"lat": latlon[0], "lon": latlon[1]}]
        pages[str(i)] = entry
    return {"query": {"pages": pages}}


def test_hybrid_retrieval_includes_landmarks_not_just_random_nearby_entities():
    """1. A destination retrieval should surface landmark-type results (e.g.
    a fort/palace) and not be limited to the obscure nearby entities that
    plain GeoSearch density-bias would otherwise dominate the pool with.
    The production code is not made city-specific by this test."""
    geosearch_items = [
        {"title": "Some Public Gardens", "dist": 1500},
        {"title": "A Random Neighborhood", "dist": 1800},
    ]
    # The famous landmark is missing from raw GeoSearch (simulating it being
    # just outside the 10km cap / crowded out by density) but is recoverable
    # via destination-aware search.
    search_titles_by_query = {
        "tourist attractions": ["Example Grand Fort"],
        "landmarks": ["Example Grand Fort"],
        "monuments": [],
        "historical places": [],
        "museums": [],
        "forts": ["Example Grand Fort"],
    }

    call_log = []

    def fake_get(url, params=None, headers=None, timeout=10):
        call_log.append(params.get("action") if params else None)
        if params.get("list") == "geosearch":
            return _json_response(_geosearch_payload(geosearch_items))
        if params.get("list") == "search":
            srsearch = params.get("srsearch", "")
            for suffix, titles in search_titles_by_query.items():
                if srsearch.endswith(suffix):
                    return _json_response(_search_payload(titles))
            return _json_response(_search_payload([]))
        if params.get("prop") == "pageviews":
            return _json_response(_pageviews_payload({
                "Example Grand Fort": 50000,
                "Some Public Gardens": 200,
                "A Random Neighborhood": 10,
            }))
        if params.get("prop") == "coordinates":
            return _json_response(_coordinates_payload({
                "Example Grand Fort": (17.38, 78.48),
            }))
        return _json_response({})

    import mcp_server
    with patch.object(mcp_server, "_geocode",
                       return_value=(17.3616, 78.4747, "Example City", "Example Country", None)), \
         patch.object(mcp_server.requests, "get", side_effect=fake_get):
        result = mcp_server._get_nearby_attractions_uncached("Example City", 15)

    assert "Example Grand Fort" in result
    # landmark should be listed ahead of the low-popularity generic entries
    fort_pos = result.find("Example Grand Fort")
    neighborhood_pos = result.find("A Random Neighborhood")
    assert fort_pos != -1
    assert neighborhood_pos == -1 or fort_pos < neighborhood_pos


def test_landmark_missing_from_geosearch_enters_via_destination_search():
    """2. A famous landmark absent from raw GeoSearch results must still
    enter the candidate pool through destination-aware search."""
    geosearch_items = [{"title": "Unrelated Street", "dist": 500}]
    search_candidates = _destination_aware_search_stub_titles = ["Iconic Palace"]

    def fake_search(city_name):
        return [{"title": "Iconic Palace", "source": {"search"}}]

    import mcp_server
    with patch.object(mcp_server, "_destination_aware_search", side_effect=fake_search):
        merged = mcp_server._merge_candidates(geosearch_items, mcp_server._destination_aware_search("Example City"))

    titles = [c["title"] for c in merged]
    assert "Iconic Palace" in titles
    assert "Unrelated Street" in titles


def test_candidate_merging_deduplicates_case_insensitively():
    """3. Candidate merging deduplicates correctly, case-insensitively."""
    import mcp_server
    geosearch_items = [{"title": "Charminar", "dist": 300}]
    search_candidates = [
        {"title": "charminar", "source": {"search"}},
        {"title": "New Landmark", "source": {"search"}},
    ]
    merged = mcp_server._merge_candidates(geosearch_items, search_candidates)
    titles_lower = [c["title"].lower() for c in merged]
    assert titles_lower.count("charminar") == 1
    assert "new landmark" in titles_lower
    charminar_entry = next(c for c in merged if c["title"].lower() == "charminar")
    # merged entry should carry both source tags and keep GeoSearch's distance
    assert charminar_entry["source"] == {"geosearch", "search"}
    assert charminar_entry["dist"] == 300


def test_non_tourist_infrastructure_ranks_below_major_attractions():
    """4. Obvious non-tourist infrastructure/neighborhood results are
    filtered or ranked below major attractions."""
    import mcp_server
    places = [
        {"title": "City Railway Station", "dist": 200, "source": {"geosearch"}},
        {"title": "Some Municipal Corporation", "dist": 250, "source": {"geosearch"}},
        {"title": "Grand Old Fort", "dist": 4000, "source": {"geosearch"}},
    ]
    filtered = mcp_server._filter_attractions(places)
    titles = [p["title"] for p in filtered]
    assert "City Railway Station" not in titles
    assert "Some Municipal Corporation" not in titles
    assert "Grand Old Fort" in titles


def test_destination_search_failure_degrades_gracefully():
    """6. API failures in the additional search source degrade gracefully
    and GeoSearch still works."""
    geosearch_items = [{"title": "Historic City Fort", "dist": 1200}]

    def fake_get(url, params=None, headers=None, timeout=10):
        if params.get("list") == "geosearch":
            return _json_response(_geosearch_payload(geosearch_items))
        if params.get("list") == "search":
            raise ConnectionError("simulated Wikipedia search outage")
        if params.get("prop") == "pageviews":
            return _json_response(_pageviews_payload({"Historic City Fort": 1000}))
        if params.get("prop") == "coordinates":
            return _json_response(_coordinates_payload({}))
        return _json_response({})

    import mcp_server
    with patch.object(mcp_server, "_geocode",
                       return_value=(10.0, 20.0, "Example City", "Example Country", None)), \
         patch.object(mcp_server.requests, "get", side_effect=fake_get):
        result = mcp_server._get_nearby_attractions_uncached("Example City", 15)

    assert "Historic City Fort" in result
    assert "Error fetching nearby attractions" not in result


def test_diversify_caps_category_dominance():
    """Lightweight diversity: a list dominated by one category shouldn't
    crowd out everything else in the final top-N."""
    import mcp_server
    ranked = (
        [{"title": f"Some Lake {i}", "dist": i * 100, "source": {"geosearch"}} for i in range(10)]
        + [{"title": "City Museum", "dist": 5000, "source": {"geosearch"}}]
        + [{"title": "Old Fort", "dist": 6000, "source": {"geosearch"}}]
        + [{"title": "Grand Temple", "dist": 7000, "source": {"geosearch"}}]
        + [{"title": "Central Bazaar", "dist": 8000, "source": {"geosearch"}}]
    )
    out = mcp_server._diversify(ranked, limit=6, max_per_category=2)
    categories = [mcp_server._categorize(p["title"]) for p in out]
    # with enough diverse alternatives available, the cap should hold and
    # the final top-6 should not be dominated by a single category
    assert categories.count("water_scenic") <= 2
    assert "museum" in categories
    assert "fort_palace" in categories


def test_source_confidence_does_not_outrank_real_popularity():
    """Regression test: a famous landmark found via only ONE retrieval
    source (e.g. GeoSearch only) with genuinely high pageviews must still
    outrank a mediocre place found via BOTH sources with low pageviews.
    Source corroboration is a tiebreaker, never a primary sort key that can
    override real popularity — this was the bug behind museums/temples with
    unknown distance outranking Golconda Fort in a real Hyderabad run."""
    import mcp_server

    candidates = [
        {"title": "Golconda Fort", "dist": 8160, "source": {"geosearch"}},
        {"title": "Minor District Museum", "dist": 2247, "source": {"geosearch", "search"}},
        {"title": "Small Local Museum", "dist": None, "source": {"geosearch", "search"}},
    ]

    def fake_get(url, params=None, headers=None, timeout=10):
        views = {"Golconda Fort": 45000, "Minor District Museum": 300,
                  "Small Local Museum": 150}
        pages = {str(i): {"title": t, "pageviews": {"d": v}}
                 for i, (t, v) in enumerate(views.items())}
        return _json_response({"query": {"pages": pages}})

    import mcp_server as m
    with patch.object(m.requests, "get", side_effect=fake_get):
        ranked = m._rank_by_popularity(candidates)

    titles = [p["title"] for p in ranked]
    assert titles.index("Golconda Fort") < titles.index("Minor District Museum")
    assert titles.index("Golconda Fort") < titles.index("Small Local Museum")


def test_pageviews_outage_falls_back_to_distance_not_source_confidence():
    """If the pageviews API returns nothing usable (simulated outage: all
    zero), ranking must fall back to distance-led ordering, not silently
    let source confidence become the deciding factor."""
    import mcp_server as m
    candidates = [
        {"title": "Far Fort", "dist": 9000, "source": {"geosearch"}},
        {"title": "Near Fort", "dist": 1000, "source": {"geosearch", "search"}},
    ]

    def fake_get(url, params=None, headers=None, timeout=10):
        return _json_response({"query": {"pages": {}}})  # no pageviews data at all

    with patch.object(m.requests, "get", side_effect=fake_get):
        ranked = m._rank_by_popularity(candidates)

    assert [p["title"] for p in ranked] == ["Near Fort", "Far Fort"]