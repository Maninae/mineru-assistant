"""Guards the house-scan mechanism against its shipped example config.

Invariants: the example config parses and drives the location gate end to
end; ZIPs and cities are parsed for any two-letter state (no region baked
into code); a city with no policy entry is excluded; the config path follows
MINERU_HOUSE_SCAN_CONFIG / MINERU_HOME; the digest names no install path.
"""

import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "engine" / "config" / "house_scan_config.example.json"
sys.path.insert(0, str(SCRIPTS_DIR))

import house_scan  # noqa: E402
from house_scan_neighborhoods import (  # noqa: E402
    LocationVerdict,
    address_blocklisted,
    classify_location,
    extract_zip_from_address,
    rank_and_cap,
)


def load_example_config() -> dict:
    return json.loads(EXAMPLE_CONFIG_PATH.read_text())


def make_listing(address: str, card_text: str = "", price: int = 1_000_000, sqft: int = 1600) -> dict:
    return {
        "url": "https://www.redfin.com/ST/x/" + address.replace(" ", "-"),
        "address": address,
        "city": house_scan.city_from_address(address),
        "price": price,
        "beds": 3.0,
        "baths": 2.0,
        "sqft": sqft,
        "price_per_sqft": round(price / sqft),
        "sashes": [],
        "card_text": card_text,
    }


def test_zip_and_city_parse_for_any_state():
    assert extract_zip_from_address("12 Example Ln, Maple Hollow, ST 00001") == "00001"
    assert extract_zip_from_address("12345 Long Rd, Cedar Point, ST 00002-1234") == "00002"
    assert extract_zip_from_address("12345 Long Rd") is None
    assert extract_zip_from_address("x, Town, ST 55555", r"(\d{5})$") == "55555"
    assert house_scan.city_from_address("12 Example Ln, Maple Hollow, ST 00001") == "Maple Hollow"


def test_location_gate_follows_example_policy():
    policy = load_example_config()["neighborhood_policy"]

    def verdict(address: str, card_text: str = "") -> LocationVerdict:
        return classify_location(make_listing(address, card_text), policy)[0]

    assert verdict("1 A St, Maple Hollow, ST 00001") == LocationVerdict.INCLUDE
    assert verdict("1 A St, Maple Hollow, ST 00003") == LocationVerdict.EXCLUDE
    assert verdict("1 A St, Maple Hollow, ST 00002") == LocationVerdict.EXCLUDE
    assert verdict("1 A St, Maple Hollow, ST 00002", "Charming Old Orchard home") == LocationVerdict.INCLUDE
    assert verdict("1 A St, Cedar Point, ST 00009") == LocationVerdict.INCLUDE
    assert verdict("1 A St, Elsewhere, ST 00009") == LocationVerdict.EXCLUDE


def test_blocklist_and_ranking_use_config_tiers():
    config = load_example_config()
    blocked = make_listing("12 Example Ln, Cedar Point, ST 00009")
    assert address_blocklisted(blocked, config["address_blocklist"])
    top = make_listing("1 A St, Maple Hollow, ST 00001")
    cheap = make_listing("2 B St, Cedar Point, ST 00009", price=800_000)
    ranked = rank_and_cap(
        [(cheap, []), (top, [])], {}, config["median_price_per_sqft_by_city"],
        config["value_score_weights"], 1, city_tier_overrides=config["city_desirability_tiers"],
    )
    assert ranked[0][0] is top


def test_config_path_follows_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    monkeypatch.delenv("MINERU_HOUSE_SCAN_CONFIG", raising=False)
    monkeypatch.delenv("MINERU_BRIEFS_ROOT", raising=False)
    module = importlib.reload(house_scan)
    assert module.DEFAULT_CONFIG_PATH == tmp_path / "config" / "house_scan_config.json"
    assert module.BRIEFS_ROOT == tmp_path
    override = tmp_path / "elsewhere.json"
    monkeypatch.setenv("MINERU_HOUSE_SCAN_CONFIG", str(override))
    assert importlib.reload(house_scan).DEFAULT_CONFIG_PATH == override


def test_digest_mentions_no_install_path():
    config = load_example_config()
    listing = make_listing("1 A St, Maple Hollow, ST 00001")
    digest = house_scan.build_digest([(listing, [])], [], [], [], config, {}, [])
    assert "Maple Hollow" in digest
    assert "/Users/" not in digest and "scripts/" not in digest
