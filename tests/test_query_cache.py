import os
import tempfile
import unittest

import query_cache


class QueryCacheTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_file = query_cache.CACHE_FILE
        query_cache.CACHE_FILE = os.path.join(self._tmp, "query_cache.json")
        query_cache._save({})

    def tearDown(self):
        query_cache.CACHE_FILE = self._orig_file

    def test_set_get_roundtrip(self):
        entry = {"itinerary": "## Trip", "tool_log": [], "guard_notes": [],
                 "reflection_result": "APPROVED", "elapsed": 12.3}
        query_cache.set("Plan a 3-day trip to Udaipur, India", entry)
        got = query_cache.get("Plan a 3-day trip to Udaipur, India")
        self.assertEqual(got["itinerary"], "## Trip")
        self.assertEqual(got["reflection_result"], "APPROVED")

    def test_get_miss_returns_none(self):
        self.assertIsNone(query_cache.get("never cached query"))

    def test_normalization_ignores_case_spacing_punct(self):
        entry = {"itinerary": "X", "tool_log": [], "guard_notes": [],
                 "reflection_result": "APPROVED", "elapsed": 1.0}
        query_cache.set("Plan a  trip to   Goa!", entry)
        got = query_cache.get("plan a trip to goa")
        self.assertIsNotNone(got)
        self.assertEqual(got["itinerary"], "X")

    def test_different_query_is_a_miss(self):
        query_cache.set("Plan a trip to Goa", {"itinerary": "G"})
        self.assertIsNone(query_cache.get("Plan a trip to Manali"))

    def test_ttl_expiry(self):
        entry = {"itinerary": "Y", "tool_log": [], "guard_notes": [],
                 "reflection_result": "APPROVED", "elapsed": 1.0}
        query_cache.set("expiring query", entry)
        store = query_cache._load()
        key = query_cache._cache_key("expiring query")
        store[key]["ts"] -= query_cache.QUERY_CACHE_TTL_SECONDS + 1
        query_cache._save(store)
        self.assertIsNone(query_cache.get("expiring query"))

    def test_count_only_live_entries(self):
        query_cache.set("one", {"itinerary": "1"})
        query_cache.set("two", {"itinerary": "2"})
        self.assertEqual(query_cache.cached_query_count(), 2)
        store = query_cache._load()
        key = query_cache._cache_key("one")
        store[key]["ts"] -= query_cache.QUERY_CACHE_TTL_SECONDS + 1
        query_cache._save(store)
        self.assertEqual(query_cache.cached_query_count(), 1)


if __name__ == "__main__":
    unittest.main()