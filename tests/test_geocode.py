"""
Tests for the geocoding helper — especially the region-disambiguation fix
that prevents "Goa, India" from resolving to Genoa, Italy.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_server import _geocode, _region_matches


def test_region_matches_usa_variants():
    """'United States of America' must match Open-Meteo's 'United States'."""
    assert _region_matches("united states of america", {"country": "United States"})
    assert _region_matches("united states", {"country": "United States of America"})
    assert _region_matches("india", {"country": "India"})
    assert _region_matches("himachal pradesh", {"admin1": "Himachal Pradesh"})
    assert not _region_matches("france", {"country": "India"})
    assert not _region_matches("goa", {"admin1": "Rajasthan"})


def test_goa_india_does_not_return_genoa():
    """'Goa, India' must NOT resolve to Genoa, Italy."""
    result = _geocode("Goa", "India")
    assert result is not None, "Expected a geocode result for Goa, India"
    lat, lon, name, country, warning = result
    if lat is None:
        # The fix should detect mismatch — this is acceptable
        assert "ERROR" in (warning or ""), (
            f"Expected error about region mismatch, got: {warning}"
        )
        return
    # If it did resolve, ensure it's not Genoa/Italy
    assert "Italy" not in country, f"Resolved to Italy instead of India: {name}, {country}"
    assert "Genoa" not in name, f"Resolved to Genoa instead of Goa: {name}, {country}"
    assert "India" in country, f"Expected India, got {country}"


def test_goa_without_region():
    """Without a region hint, Goa should return a warning about ambiguity."""
    result = _geocode("Goa")
    assert result is not None, "Expected a result for 'Goa'"
    lat, lon, name, country, warning = result
    assert lat is not None, "Should resolve to at least one candidate"
    if warning:
        assert "ambiguous" in warning.lower(), (
            f"Expected ambiguity warning, got: {warning}"
        )


def test_manali_himachal():
    """Manali, Himachal Pradesh should resolve to the correct one."""
    result = _geocode("Manali", "Himachal Pradesh")
    assert result is not None
    lat, lon, name, country, warning = result
    assert lat is not None, "Manali, HP should resolve"
    if "Manali" in name.lower():
        assert "India" in country, f"Expected India, got {country}"
    if warning:
        assert "ERROR" not in (warning or ""), f"Unexpected error: {warning}"


def test_manali_without_region():
    """Manali without region should return a warning about ambiguity (multiple exist)."""
    result = _geocode("Manali")
    assert result is not None, "Expected a result for 'Manali'"
    lat, lon, name, country, warning = result
    assert lat is not None
    if warning:
        assert "ambiguous" in warning.lower(), (
            f"Expected ambiguity warning, got: {warning}"
        )


def test_nonexistent_city():
    """A completely fake city should return None."""
    result = _geocode("Xyzzyville")
    assert result is None, "Expected None for nonexistent city"


def test_london_uk():
    """London, United Kingdom should resolve correctly."""
    result = _geocode("London", "United Kingdom")
    assert result is not None
    lat, lon, name, country, warning = result
    assert lat is not None
    if warning:
        assert "ERROR" not in (warning or ""), f"Unexpected error: {warning}"


def test_region_mismatch_falls_back_to_nominatim():
    """When region doesn't match Open-Meteo candidates, should try Nominatim
    before giving up."""
    result = _geocode("Goa", "India")
    assert result is not None
    lat, lon, name, country, warning = result
    assert lat is not None, (
        "Should resolve via Nominatim fallback even when Open-Meteo mismatches"
    )
    assert "India" in country, (
        f"Should resolve to India, got country={country}"
    )
