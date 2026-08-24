"""
LAYER 3 — place validation (hybrid hallucination detection).

The reflection agent's rule-based extractor (Layer 1) decides whether text
LOOKS like a landmark (type keyword / suffix / travel context / bold), and
Layer 2 filters out generic, template and destination words. This module is
the final, strongest step: it PROVES a candidate is a real place near the
requested destination by geocoding it and measuring the distance.

    Generated itinerary
            |
            v
    Candidate extractor  (reflection_agent._draft_place_phrases)
            |
            v
    Filter generic / template / destination words
            |
            v
    [ THIS MODULE ] geocode each candidate
            |                       |
            v                       v
      real + near destination   not found / far away
            |                       |
            v                       v
          Keep them              Flag / regenerate

The difference from the old check: "does this text look like an attraction?"
becomes "is this actually a real place relevant to the trip?". A hallucinated
"Dragon Moon Palace" might pass a keyword check ('palace'), but it will fail
geocoding validation.

Future evolution: Layer 2 can be upgraded to a real NER model (spaCy LOC /
GPE / FAC tags). Layer 3 is unchanged — it validates any entity you feed it.
"""

import math
import re
import time
from typing import Callable

import requests

_EARTH_RADIUS_KM = 6371.0

_USER_AGENT = "TravelPlannerAgent/1.0 (educational project)"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two coordinates."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2)
    return _EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_destination(research_notes: str) -> dict | None:
    """Extract the destination city/country + coordinates from the geocode
    line the research step produced
    ('Udaipur, India -> lat=24.58, lon=73.71')."""
    m = re.search(
        r'\b([A-Za-z][\w .\'’-]+),\s*([A-Za-z][\w .\'’-]+)\s*->\s*'
        r'lat=(-?\d+(?:\.\d+)?),\s*lon=(-?\d+(?:\.\d+)?)',
        research_notes,
    )
    if not m:
        return None
    return {
        "city": m.group(1).strip(),
        "country": m.group(2).strip(),
        "lat": float(m.group(3)),
        "lon": float(m.group(4)),
    }


def _nominatim_lookup(name: str, city: str = ""):
    """Geocode a candidate name via Nominatim (OpenStreetMap) — free, no API
    key. Returns {'lat', 'lon', 'name', 'display_name'} or None if not found."""
    query = f"{name}, {city}" if city else name
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 3, "addressdetails": 1},
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
        if not results:
            return None
        first = results[0]
        return {
            "lat": float(first["lat"]),
            "lon": float(first["lon"]),
            "name": first.get("name") or name,
            "display_name": first.get("display_name") or name,
        }
    except Exception:
        return None


class PlaceValidator:
    """Proves candidates are real places near the destination.

    lookup: callable(name, city) -> {'lat', 'lon', 'name', 'display_name'}
            or None. Override for tests (offline stub) or a different geocoder.
    max_radius_km: a found place must lie within this distance of the
            destination to count as 'relevant to the trip' — existing but
            far away (e.g. 'Marine Drive' named for a Goa trip) is flagged.
    """

    def __init__(self, lookup: Callable | None = None, max_radius_km: float = 60.0):
        self._max_radius_km = max_radius_km
        self._cache: dict[str, dict] = {}
        if lookup is None:
            # Nominatim asks for <= 1 request/sec — pace the real lookup.
            def _polite(name: str, city: str = ""):
                time.sleep(1.1)
                return _nominatim_lookup(name, city)
            self._lookup: Callable = _polite
        else:
            self._lookup = lookup

    def validate(self, name: str, dest_city: str = "",
                 dest_lat: float | None = None,
                 dest_lon: float | None = None) -> dict:
        """Verdict for one candidate:
        {'found', 'distance_km', 'within_radius', 'matched_name'}."""
        key = f"{name.lower()}|{dest_city.lower()}"
        if key in self._cache:
            return self._cache[key]
        result = self._lookup(name, dest_city)
        if result is None:
            verdict = {
                "found": False,
                "distance_km": None,
                "within_radius": False,
                "matched_name": None,
            }
        else:
            dist = (haversine_km(dest_lat, dest_lon, result["lat"], result["lon"])
                    if dest_lat is not None and dest_lon is not None else None)
            verdict = {
                "found": True,
                "distance_km": dist,
                "within_radius": dist is not None and dist <= self._max_radius_km,
                "matched_name": result["display_name"],
            }
        self._cache[key] = verdict
        return verdict

    def validate_many(self, names: list[str], dest_city: str = "",
                      dest_lat: float | None = None,
                      dest_lon: float | None = None,
                      max_n: int = 5) -> tuple:
        """Split candidates into (verified, rejected). A candidate is
        verified only if the geocoder finds it AND it lies within
        max_radius_km of the destination."""
        verified, rejected = [], []
        for name in names[:max_n]:
            verdict = self.validate(name, dest_city, dest_lat, dest_lon)
            (verified if verdict["found"] and verdict["within_radius"]
             else rejected).append((name, verdict))
        return verified, rejected
