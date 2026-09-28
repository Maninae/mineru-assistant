"""Per-school researched verdicts for the house-scan school gate.

The gate is decided PER SCHOOL by a researched include/exclude verdict, not
by a GreatSchools x/10 floor. The verdict list lives in the file named by
config `school_verdicts_path` (default $MINERU_HOME/config/
house_scan_school_verdicts.json); each entry carries the reason, the date,
and the source it came from. The numeric floors in
house_scan_config.json survive ONLY as the fallback for schools nobody has
researched yet, and every fallback decision is logged so the list grows.

- Matching is by normalized school name (case/punctuation-insensitive,
  trailing "School" dropped) plus an optional city list, because the same
  name recurs across a region (a "Lincoln Elementary" in one city can be a
  keep while the same-named school in the next city is a drop).
- Only the tiers the config floors gate on are decided here; a tier whose
  floor is 0 and has no verdict is ignored (that is how "high floor off" works).
- A K-8 shows up as the elementary with no middle entry; no middle = no decision.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

INCLUDE = "include"
EXCLUDE = "exclude"
GATED_TIERS = ("elementary", "middle", "high")


def normalize_school_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse spaces, drop a trailing 'school'."""
    lowered = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    collapsed = re.sub(r"\s+", " ", lowered).strip()
    return re.sub(r"\s+school$", "", collapsed)


def load_school_verdicts(path: Path) -> List[Dict[str, Any]]:
    """Read the verdict list; a missing file means an empty list (floors only)."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entries = data.get("verdicts", data) if isinstance(data, dict) else data
    for entry in entries:
        if entry.get("verdict") not in (INCLUDE, EXCLUDE):
            raise ValueError(f"school verdict for {entry.get('name')!r} must be include/exclude")
    return list(entries)


def resolve_school_verdict(
    school_name: str,
    listing_city: str,
    verdicts: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the matching verdict entry, or None when the school is unresearched.

    An entry with an empty `cities` list matches any listing city; otherwise
    the listing's city must be in the list (case-insensitive).
    """
    wanted = normalize_school_name(school_name)
    city = (listing_city or "").strip().lower()
    for entry in verdicts:
        if normalize_school_name(entry.get("name", "")) != wanted:
            continue
        cities = [c.strip().lower() for c in (entry.get("cities") or [])]
        if cities and city not in cities:
            continue
        return entry
    return None


def evaluate_school_gate(
    school_data: Optional[Dict[str, Any]],
    listing_city: str,
    verdicts: List[Dict[str, Any]],
    floors: Dict[str, float],
) -> Tuple[Optional[str], List[Tuple[str, str, Any]], List[str]]:
    """Decide whether a listing's assigned schools block it.

    Args:
        school_data: the cached `schools` dict from seen-state (status "ok" or not).
        listing_city: the listing's parsed city, used to disambiguate same-name schools.
        verdicts: entries from load_school_verdicts().
        floors: {"elementary": 7, "middle": 7, "high": 0}; 0 = that tier is not gated.
    Returns:
        (block_reason or None, fallback_decisions, vetted_in_names)
        - fallback_decisions: [(tier, school name, rating)] decided by the numeric
          floor because no verdict exists; the caller logs them as unresearched.
        - vetted_in_names: schools passed by an explicit include verdict, for the digest.

    - Unknown school data never blocks.
    - An explicit verdict always beats the floor, in either direction.
    """
    if not isinstance(school_data, dict) or school_data.get("status") != "ok":
        return None, [], []
    fallback: List[Tuple[str, str, Any]] = []
    vetted_in: List[str] = []
    for tier in GATED_TIERS:
        tier_entry = school_data.get(tier)
        if not isinstance(tier_entry, dict) or not tier_entry.get("name"):
            continue
        name = tier_entry["name"]
        rating = tier_entry.get("rating")
        verdict = resolve_school_verdict(name, listing_city, verdicts)
        if verdict is not None:
            if verdict["verdict"] == EXCLUDE:
                return f"{tier} {name} is a researched EXCLUDE ({verdict.get('reason', '')[:80]})", fallback, vetted_in
            vetted_in.append(name)
            continue
        floor = float(floors.get(tier, 0) or 0)
        if floor <= 0 or rating is None:
            continue
        fallback.append((tier, name, rating))
        if float(rating) < floor:
            return f"{tier} {name} {rating}/10 < fallback floor {floor:g} (unresearched)", fallback, vetted_in
    return None, fallback, vetted_in
