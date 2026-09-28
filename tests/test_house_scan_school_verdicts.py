"""Guards the per-school verdict gate in scripts/house_scan_school_verdicts.py.

Invariants: an explicit verdict beats the numeric floor in both directions,
same-name schools are told apart by city, K-8s (no middle entry) are judged
on the elementary alone, a tier with floor 0 is not gated, and unknown school
data never blocks. All school and city names are fictional.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from house_scan_school_verdicts import (  # noqa: E402
    evaluate_school_gate,
    load_school_verdicts,
    normalize_school_name,
    resolve_school_verdict,
)

FLOORS = {"elementary": 7, "middle": 7, "high": 0}
VERDICTS = [
    {"name": "Lincoln Elementary School", "cities": ["Maple Hollow"], "verdict": "include", "reason": "deep dive"},
    {"name": "Lincoln Elementary School", "cities": ["Cedar Point"], "verdict": "exclude", "reason": "GS 4"},
    {"name": "Harbor View Elementary School", "cities": [], "verdict": "exclude", "reason": "weak middle grades"},
    {"name": "Maple Hollow Intermediate School", "cities": [], "verdict": "include", "reason": "GS 10"},
]


def school(name: str, rating: int) -> dict:
    return {"name": name, "rating": rating}


def ok(elementary=None, middle=None, high=None) -> dict:
    data = {"status": "ok"}
    if elementary:
        data["elementary"] = elementary
    if middle:
        data["middle"] = middle
    if high:
        data["high"] = high
    return data


def test_normalize_drops_case_punctuation_and_trailing_school():
    assert normalize_school_name("Adams (Jane Q.) Elementary School") == "adams jane q elementary"
    assert normalize_school_name("lincoln elementary") == normalize_school_name("Lincoln Elementary School")
    # Tier words stay, so one city's Central Elementary never matches another's Central Middle.
    assert normalize_school_name("Central Elementary School") != normalize_school_name("Central Middle School")


def test_same_name_school_resolved_by_city():
    assert resolve_school_verdict("Lincoln Elementary School", "Maple Hollow", VERDICTS)["verdict"] == "include"
    assert resolve_school_verdict("Lincoln Elementary School", "Cedar Point", VERDICTS)["verdict"] == "exclude"
    assert resolve_school_verdict("Lincoln Elementary School", "Riverton", VERDICTS) is None


def test_include_verdict_beats_floor():
    data = ok(school("Lincoln Elementary School", 6), school("Maple Hollow Intermediate School", 10))
    block, fallback, vetted = evaluate_school_gate(data, "Maple Hollow", VERDICTS, FLOORS)
    assert block is None
    assert fallback == []
    assert vetted == ["Lincoln Elementary School", "Maple Hollow Intermediate School"]


def test_exclude_verdict_beats_floor():
    data = ok(school("Harbor View Elementary School", 8))  # K-8, no middle entry
    block, _, _ = evaluate_school_gate(data, "Cedar Point", VERDICTS, FLOORS)
    assert block is not None and "Harbor View" in block and "EXCLUDE" in block


def test_unresearched_school_falls_back_to_floor_and_is_reported():
    passing = ok(school("Birch Elementary School", 7), school("Oakridge Middle School", 8))
    block, fallback, _ = evaluate_school_gate(passing, "Riverton", VERDICTS, FLOORS)
    assert block is None
    assert fallback == [("elementary", "Birch Elementary School", 7), ("middle", "Oakridge Middle School", 8)]

    failing = ok(school("Willow Elementary School", 6), school("Summit Intermediate School", 5))
    block, fallback, _ = evaluate_school_gate(failing, "Riverton", VERDICTS, FLOORS)
    assert block is not None and "Willow" in block and "fallback floor 7" in block
    assert fallback == [("elementary", "Willow Elementary School", 6)]  # stops at the first block


def test_high_school_not_gated_when_floor_is_zero():
    data = ok(school("Aspen Elementary School", 10), school("Pine Intermediate School", 10), school("Weak High School", 2))
    block, fallback, _ = evaluate_school_gate(data, "Cedar Point", VERDICTS, FLOORS)
    assert block is None
    assert all(tier != "high" for tier, _, _ in fallback)


def test_unknown_school_data_never_blocks():
    assert evaluate_school_gate({"status": "unknown", "reason": "timeout"}, "Cedar Point", VERDICTS, FLOORS) == (None, [], [])
    assert evaluate_school_gate(None, "Cedar Point", VERDICTS, FLOORS) == (None, [], [])


def test_malformed_verdict_rejected(tmp_path):
    bad = tmp_path / "v.json"
    bad.write_text('{"verdicts": [{"name": "X", "verdict": "maybe"}]}')
    with pytest.raises(ValueError):
        load_school_verdicts(bad)
