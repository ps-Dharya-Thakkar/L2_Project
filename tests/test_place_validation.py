"""
Layer 3 — place validation. Proves a candidate place name actually exists
near the requested destination (geocoding), instead of just "looking like"
an attraction name.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from place_validation import (PlaceValidator, haversine_km, parse_destination,
                              _nominatim_lookup)


def _stub_validator(locations: dict):
    """locations: {lowercase name: (lat, lon)} — None-value names are absent."""
    def lookup(name: str, city: str = ""):
        loc = locations.get(name.lower())
        if loc is None:
            return None
        lat, lon = loc
        return {"lat": lat, "lon": lon, "name": name,
                "display_name": f"{name}, Testland"}
    return PlaceValidator(lookup=lookup)


class TestHaversine(unittest.TestCase):

    def test_same_point_zero(self):
        self.assertEqual(haversine_km(24.58, 73.71, 24.58, 73.71), 0.0)

    def test_one_degree_of_latitude(self):
        # 1 degree of latitude is ~111.19 km at the equator.
        self.assertAlmostEqual(haversine_km(0.0, 0.0, 1.0, 0.0), 111.19, places=1)

    def test_hyderabad_to_mumbai_is_far(self):
        self.assertGreater(
            haversine_km(17.38, 78.47, 18.94, 72.82), 500)


class TestParseDestination(unittest.TestCase):

    def test_parses_geocode_line(self):
        notes = ("Udaipur, India -> lat=24.58, lon=73.71\n"
                 "HISTORICAL weather for Udaipur, EXACT DATE=2022-08-15")
        dest = parse_destination(notes)
        self.assertIsNotNone(dest)
        self.assertEqual(dest["city"], "Udaipur")
        self.assertEqual(dest["country"], "India")
        self.assertEqual(dest["lat"], 24.58)
        self.assertEqual(dest["lon"], 73.71)

    def test_none_when_no_geocode_line(self):
        self.assertIsNone(parse_destination("some other text, no coords"))


class TestPlaceValidator(unittest.TestCase):

    def test_found_near_destination_is_verified(self):
        v = _stub_validator({"charminar": (17.3833, 78.4746)})
        verdict = v.validate("Charminar", "Hyderabad", 17.38, 78.47)
        self.assertTrue(verdict["found"])
        self.assertTrue(verdict["within_radius"])

    def test_found_but_far_away_is_not_relevant(self):
        # Marine Drive geocodes to Mumbai — 700km from Hyderabad.
        v = _stub_validator({"marine drive": (18.9356, 72.8246)})
        verdict = v.validate("Marine Drive", "Hyderabad", 17.38, 78.47)
        self.assertTrue(verdict["found"])
        self.assertFalse(verdict["within_radius"])

    def test_not_found_is_rejected(self):
        v = _stub_validator({})
        verdict = v.validate("Dragon Moon Palace", "Goa", 15.3, 74.08)
        self.assertFalse(verdict["found"])
        self.assertFalse(verdict["within_radius"])

    def test_validate_many_splits_verified_and_rejected(self):
        v = _stub_validator({
            "charminar": (17.3833, 78.4746),
            "dragon moon palace": None,
            "marine drive": (18.9356, 72.8246),  # far from Hyderabad
        })
        verified, rejected = v.validate_many(
            ["Charminar", "Dragon Moon Palace", "Marine Drive"],
            "Hyderabad", 17.38, 78.47)
        verified_names = [n for n, _ in verified]
        rejected_names = [n for n, _ in rejected]
        self.assertIn("Charminar", verified_names)
        self.assertIn("Dragon Moon Palace", rejected_names)
        self.assertIn("Marine Drive", rejected_names)

    def test_results_are_cached(self):
        calls = []

        def lookup(name, city=""):
            calls.append(name)
            return {"lat": 17.38, "lon": 78.47, "name": name,
                    "display_name": f"{name}, Testland"}

        v = PlaceValidator(lookup=lookup)
        v.validate("Charminar", "Hyderabad", 17.38, 78.47)
        v.validate("Charminar", "Hyderabad", 17.38, 78.47)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()