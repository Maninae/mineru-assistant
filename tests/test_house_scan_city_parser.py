"""Guards city_from_address in scripts/house_scan.py.

Invariants: the LAST configured city in the address's city segment wins
(case-insensitive, whole words), a street named after a configured city never
wins, and an address naming no configured city falls back to the raw parsed
segment with one INFO log line carrying the raw address. City names are
fictional except where the test reproduces a real garbled Redfin card shape.
"""

import logging
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from house_scan import city_from_address, known_city_names_from_config, normalize_listing  # noqa: E402

KNOWN_CITIES = ["Maple Hollow", "Cedar Point", "Redwood City", "Oakvale"]


def test_garbled_builder_plan_prefix_resolves_to_configured_city():
    address = "Hackberry Plan, Bdntin Redwood City, CA 94065"
    assert city_from_address(address, KNOWN_CITIES) == "Redwood City"


def test_normal_address():
    assert city_from_address("12 Elm St, Oakvale, CA 94000", KNOWN_CITIES) == "Oakvale"


def test_two_word_city_case_insensitive():
    assert city_from_address("40 Birch Ln, maple hollow, CA 94001", KNOWN_CITIES) == "Maple Hollow"


def test_multi_city_segment_last_occurrence_wins():
    address = "Willow Plan, Cedar Point Maple Hollow, CA 94002"
    assert city_from_address(address, KNOWN_CITIES) == "Maple Hollow"


def test_street_named_after_configured_city_does_not_win():
    assert city_from_address("100 Cedar Point Ave, Oakvale, CA 94003", KNOWN_CITIES) == "Oakvale"


def test_no_configured_city_falls_back_and_logs_raw_address(caplog):
    address = "7 Pine Rd, Fernwick, CA 94004"
    with caplog.at_level(logging.INFO, logger="house_scan"):
        assert city_from_address(address, KNOWN_CITIES) == "Fernwick"
    city_lines = [r for r in caplog.records if "city parse" in r.getMessage()]
    assert len(city_lines) == 1
    assert city_lines[0].levelno == logging.INFO
    assert address in city_lines[0].getMessage()


def test_configured_city_match_does_not_log(caplog):
    with caplog.at_level(logging.INFO, logger="house_scan"):
        city_from_address("12 Elm St, Oakvale, CA 94000", KNOWN_CITIES)
    assert not [r for r in caplog.records if "city parse" in r.getMessage()]


def test_known_city_names_merge_scan_list_and_policy_keys():
    config = {
        "cities": [{"name": "Oakvale"}, {"name": "Cedar Point"}],
        "neighborhood_policy": {"cities": {"Oakvale": {}, "Fernwick": {}}},
    }
    assert known_city_names_from_config(config) == ["Oakvale", "Cedar Point", "Fernwick"]


def test_normalize_listing_uses_known_cities():
    raw = {
        "href": "/CA/x/home/1",
        "price": "$1,500,000",
        "address": "Hackberry Plan, Bdntin Redwood City, CA 94065",
        "stats": "3 beds 2 baths 1,600 sq ft",
        "_scan_city": "Oakvale",
    }
    assert normalize_listing(raw, KNOWN_CITIES)["city"] == "Redwood City"
