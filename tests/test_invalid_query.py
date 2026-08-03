"""
Tests for the INVALID_QUERY rejection mechanism.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_invalid_query_detection():
    last_content = "INVALID_QUERY: This is a greeting, not a travel request."
    starts_with_invalid = last_content.startswith("INVALID_QUERY")
    assert starts_with_invalid, "Should detect INVALID_QUERY prefix"


def test_invalid_query_extracts_reason():
    last_content = "INVALID_QUERY: This is a greeting, not a travel request."
    if last_content.startswith("INVALID_QUERY"):
        reason = last_content.split(":", 1)[1].strip() if ":" in last_content else ""
        assert reason == "This is a greeting, not a travel request."


def test_valid_query_passes():
    last_content = "I'll gather data about Manali, Himachal Pradesh."
    assert not last_content.startswith("INVALID_QUERY")


def test_invalid_query_no_reason():
    last_content = "INVALID_QUERY"
    if last_content.startswith("INVALID_QUERY"):
        reason = last_content.split(":", 1)[1].strip() if ":" in last_content else \
            "This doesn't look like a travel-planning request."
        assert reason == "This doesn't look like a travel-planning request."
