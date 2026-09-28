#!/usr/bin/env python3
"""house_scan_neighborhoods.py — Location gate + value scoring for house_scan.py.

The scanner should whisper, not fire-hose. Redfin's per-city searches across
several target cities can return ~100 new listings/day, and the user only
cares about a handful of them: the DESIRABLE-LOCATION ones. This module is where
"desirable" becomes a config-driven, deterministic rule.

Two responsibilities:

  1. `classify_location(listing, policy) -> (verdict, reason)`
     Per-city allow/deny policy over listing address + card description.
     Evaluation order (first hit wins). Include keywords intentionally beat
     exclude keywords: an include name (a real neighborhood name) is stronger
     evidence of where a home actually IS than a passing mention of an
     excluded area ("walk to <excluded area> shopping" in the description). The include list
     should stay curated to real neighborhood names, not amenities.

       1. `require_city_in_address` mismatch -> EXCLUDE (address is in a
          different city than this one's policy, e.g. a neighboring city's
          card returned by this city's search).
       2. `include_keywords` hit in address or card text -> INCLUDE (rescues
          a mixed-ZIP listing whose description names a good neighborhood).
       3. `exclude_keywords` hit -> EXCLUDE.
       4. Address ZIP in `exclude_zips` -> EXCLUDE.
       5. Address ZIP in `include_zips` -> INCLUDE.
       6. Address ZIP in `mixed_zips` -> policy['mixed_zip_default'].
       7. Fall back to city_policy.default_verdict, else global default.

  2. `score_listing(listing, seen_entry, medians, weights) -> float`
     Ranking signal for deciding WHICH of several desirable candidates to
     surface first when the digest is capped. Higher = better deal.

All tables and thresholds live in `house_scan_config.json` under
`neighborhood_policy`, `city_desirability_tiers` and `value_score_weights`.
This module holds only the regexes, the enum, and the pure evaluation logic:
no hard-coded place names, so the user tunes without touching code.
"""

import datetime
import logging
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# Redfin addresses always end ", <ST> <zip>". Anchoring on the two-letter state
# code keeps a five-digit street number from false-matching. Overridable via
# config `neighborhood_policy.zip_regex` (one capture group = the ZIP).
ZIP_REGEX = re.compile(r"\b[A-Z]{2}\s+(\d{5})(?:-\d{4})?\b")

# Ranking-score constants. Kept UPPER_SNAKE so config overrides are opt-in
# rather than mandatory (a missing weight uses the constant).
#
# Revised 2026-09-16: schools + city desirability tier are now first-class
# signals. Intra-city discount is CAPPED (see BELOW_MEDIAN_CONTRIB_CAP) so a
# cheap house in a weak city cannot dominate a great house in a strong city.
DEFAULT_WEIGHT_BELOW_MEDIAN_PPSF = 6.0    # value tell, but capped (see below)
DEFAULT_WEIGHT_PRICE_CUT = 4.0             # seller motivation
DEFAULT_WEIGHT_DOM_BONUS = 0.02            # patience discount
DEFAULT_WEIGHT_SQFT_BONUS = 0.001          # small nudge for larger homes
DEFAULT_WEIGHT_CITY_TIER = 4.0             # per tier-step above baseline
DEFAULT_WEIGHT_SCHOOLS = 1.2               # per 1.0 above SCHOOL_BASELINE_RATING
SCHOOL_BASELINE_RATING = 5.0               # avg <5/10 → school penalty, ≥5 → bonus
SQFT_BONUS_FLOOR = 1500                    # sqft bonus only kicks in above this
DOM_BONUS_CEILING_DAYS = 60                # stop rewarding stale past 2 months
# Cap on how much the below-median $/sqft signal can contribute to the score,
# even for an extreme intra-city discount. Without this, a listing that's 30%
# under a weak-city median (typical red-flag territory: bad street, condition
# problem) buries a strong-neighborhood listing that's within 5% of median. The cap
# is expressed as a fraction of city_tier's max contribution so tuning stays
# proportional.
BELOW_MEDIAN_CONTRIB_CAP_FRACTION = 0.5    # of city_tier weight × top tier
STEEP_DISCOUNT_PENALTY = 3.0               # subtracted when a listing tripwires steep_discount

# City desirability tiers. Higher = more desirable / stronger schools + fabric.
# The tier table itself lives in config `city_desirability_tiers`; this module
# ships no place names. Baseline tier for scoring is 0 (a neutral city adds
# nothing / subtracts nothing); each tier step up adds DEFAULT_WEIGHT_CITY_TIER.
DEFAULT_CITY_TIERS: Dict[str, int] = {}
CITY_TIER_MAX = 3          # used to size the below-median cap


class LocationVerdict(int, Enum):
    """Outcome of the location gate for one listing."""
    INCLUDE = 1
    EXCLUDE = 2
    UNKNOWN = 3


def extract_zip_from_address(address: str, zip_regex: Optional[str] = None) -> Optional[str]:
    """Return the ZIP from a Redfin card address, or None.

    `zip_regex` (from config) overrides the default; its first capture group is the ZIP.
    """
    if not address:
        return None
    pattern = re.compile(zip_regex) if zip_regex else ZIP_REGEX
    match = pattern.search(address)
    return match.group(1) if match else None


def address_contains_city(address: str, city_name: str) -> bool:
    """Case-insensitive substring match for city name inside an address line.

    Redfin addresses look like "12 Example Ln, Maple Hollow, ST 00001", so a
    plain substring test is safe (we're not matching against arbitrary text).
    """
    if not address or not city_name:
        return False
    return city_name.lower() in address.lower()


def keyword_matches(needle: str, haystack: str) -> bool:
    """Word-boundary keyword match, hyphens and spaces both treated as breaks.

    Guards short keywords like "TLC" from matching mid-word (e.g. "particle"),
    and lets hyphenated forms like "Lyon-Hoag" match "Lyon-Hoag" verbatim.
    Case-insensitive.
    """
    if not needle or not haystack:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(needle.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, haystack.lower()) is not None


def verdict_from_config_string(value: str) -> LocationVerdict:
    """Parse a config string ('include'/'exclude') into a LocationVerdict.

    Any other value (including missing/None) falls through to UNKNOWN so the
    caller can decide the sane default.
    """
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "include":
            return LocationVerdict.INCLUDE
        if lowered == "exclude":
            return LocationVerdict.EXCLUDE
    return LocationVerdict.UNKNOWN


def resolve_city_key(listing_city: str, cities_policy: Dict[str, Any]) -> str:
    """Pick the config city key whose name matches this listing's parsed city.

    Case-insensitive so a Redfin scrape that yields "maple hollow" still lands
    on the "Maple Hollow" policy entry. Returns "" when nothing matches.
    """
    if not listing_city:
        return ""
    lowered = listing_city.strip().lower()
    for key in cities_policy.keys():
        if key.lower() == lowered:
            return key
    return ""


def classify_location(
    listing: Dict[str, Any],
    neighborhood_policy: Dict[str, Any],
    log: Optional[logging.Logger] = None,
) -> Tuple[LocationVerdict, str]:
    """Return (verdict, human-readable reason) for one listing under policy.

    Args:
      listing: normalized listing dict from house_scan.py (needs `address`,
               `card_text`, `city` at minimum).
      neighborhood_policy: the `neighborhood_policy` block from house_scan_config.json.
      log: optional logger for per-listing evaluation traces.

    Returns:
      (LocationVerdict, reason_string) suitable for logging/debugging.
    """
    address = listing.get("address") or ""
    card_text = listing.get("card_text") or ""
    listing_city = listing.get("city") or ""

    global_default = verdict_from_config_string(
        neighborhood_policy.get("default_verdict", "include")
    )
    cities_policy = neighborhood_policy.get("cities", {}) or {}
    city_key = resolve_city_key(listing_city, cities_policy)

    # Step 0. Unknown city = EXCLUDE. Redfin's per-city searches spill into
    # cities we never asked for (nearby towns outside the target list);
    # without this guard such a card has an empty per-city policy and falls
    # through to the global default_verdict ("include"). Added 2026-09-24.
    if not city_key:
        reason = (
            f"city '{listing_city}' has no neighborhood_policy entry"
            if listing_city else f"no city parsed from address '{address}'"
        )
        if log is not None:
            log.debug("EXCLUDE %s: %s", listing.get("url"), reason)
        return LocationVerdict.EXCLUDE, reason
    city_policy: Dict[str, Any] = cities_policy.get(city_key, {})

    # Step 1. City-of-address consistency. Redfin's per-city searches spill
    # into adjacent cities (one city's query returns its neighbors'
    # listings); we should apply the ACTUAL address city's policy, not the
    # city whose search returned this card. The listing dict's `city` field
    # is already resolved from the address in normalize_listing, so we mostly
    # need to guard the case where the config policy explicitly requires
    # `city_in_address` and the address doesn't confirm it.
    require_city = city_policy.get("require_city_in_address")
    if require_city and not address_contains_city(address, require_city):
        reason = f"address '{address}' does not name required city '{require_city}'"
        if log is not None:
            log.debug("EXCLUDE %s: %s", listing.get("url"), reason)
        return LocationVerdict.EXCLUDE, reason

    # Step 2. Include keywords first. Rationale: naming a real neighborhood
    # is stronger evidence of where the home IS than a passing mention of an
    # excluded amenity ("walk to <excluded area> shopping"), so rescue takes
    # priority over deny.
    for keyword in city_policy.get("include_keywords", []) or []:
        if keyword_matches(keyword, card_text) or keyword_matches(keyword, address):
            reason = f'{listing_city or "city"} include keyword "{keyword}" matched'
            if log is not None:
                log.debug("INCLUDE %s: %s", listing.get("url"), reason)
            return LocationVerdict.INCLUDE, reason

    # Step 3. Exclude keywords (address OR card description).
    for keyword in city_policy.get("exclude_keywords", []) or []:
        if keyword_matches(keyword, card_text) or keyword_matches(keyword, address):
            reason = f'{listing_city or "city"} exclude keyword "{keyword}" matched'
            if log is not None:
                log.debug("EXCLUDE %s: %s", listing.get("url"), reason)
            return LocationVerdict.EXCLUDE, reason

    # Steps 4-6. ZIP-based rules.
    listing_zip = extract_zip_from_address(address, neighborhood_policy.get("zip_regex"))
    if listing_zip:
        if listing_zip in (city_policy.get("exclude_zips", []) or []):
            reason = f"ZIP {listing_zip} in {listing_city} exclude_zips"
            if log is not None:
                log.debug("EXCLUDE %s: %s", listing.get("url"), reason)
            return LocationVerdict.EXCLUDE, reason
        if listing_zip in (city_policy.get("include_zips", []) or []):
            reason = f"ZIP {listing_zip} in {listing_city} include_zips"
            if log is not None:
                log.debug("INCLUDE %s: %s", listing.get("url"), reason)
            return LocationVerdict.INCLUDE, reason
        if listing_zip in (city_policy.get("mixed_zips", []) or []):
            mixed_default_str = city_policy.get("mixed_zip_default", "exclude")
            mixed_verdict = verdict_from_config_string(mixed_default_str)
            reason = (
                f"ZIP {listing_zip} in {listing_city} mixed_zips "
                f"(default {mixed_default_str})"
            )
            if log is not None:
                log.debug("%s %s: %s", mixed_verdict.name, listing.get("url"), reason)
            return mixed_verdict, reason

    # Step 7. Fallback to city_policy default, then global default.
    city_default_str = city_policy.get("default_verdict")
    if city_default_str is not None:
        default_verdict = verdict_from_config_string(city_default_str)
        reason = f"{listing_city} default_verdict={city_default_str}"
    else:
        default_verdict = global_default
        reason = f"global default_verdict for '{listing_city or 'unknown city'}'"
    if log is not None:
        log.debug("%s %s: %s", default_verdict.name, listing.get("url"), reason)
    return default_verdict, reason


def _resolve_city_tier(city: str, tier_overrides: Optional[Dict[str, int]]) -> int:
    """Look up a city's desirability tier, applying config overrides on top.

    Case-insensitive against DEFAULT_CITY_TIERS keys so a config that spells
    a city slightly differently ("Maple Hollow" vs "maple hollow") still lands.
    An unknown city returns 0 (neutral baseline), so a listing outside the
    configured tiers doesn't accidentally get punished or boosted.
    """
    merged = dict(DEFAULT_CITY_TIERS)
    if tier_overrides:
        for key, val in tier_overrides.items():
            try:
                merged[key] = int(val)
            except (TypeError, ValueError):
                continue
    if not city:
        return 0
    lowered = city.strip().lower()
    for key, tier in merged.items():
        if key.lower() == lowered:
            return tier
    return 0


def _extract_school_avg(school_data: Optional[Dict[str, Any]]) -> Optional[float]:
    """Return the average assigned-schools rating if present, else None."""
    if not isinstance(school_data, dict) or school_data.get("status") != "ok":
        return None
    avg = school_data.get("avg_assigned_rating")
    try:
        return float(avg) if avg is not None else None
    except (TypeError, ValueError):
        return None


def score_listing(
    listing: Dict[str, Any],
    seen_entry: Optional[Dict[str, Any]],
    median_ppsf_by_city: Dict[str, int],
    weights: Optional[Dict[str, float]] = None,
    city_tier_overrides: Optional[Dict[str, int]] = None,
    school_data: Optional[Dict[str, Any]] = None,
    steep_discount_pct: float = 0.25,
) -> float:
    """Return a value-ranking score. Higher = better deal for the user.

    Revised 2026-09-16 to make absolute desirability dominate intra-city
    discount. Signals, in decreasing weight:

      - **City desirability tier** (config `city_desirability_tiers`).
        Encodes the fabric of the city + typical school district; the
        noisiest but most load-bearing signal.
      - **Assigned-school average rating** from GreatSchools (per listing,
        via house_scan_schools.py). Rewards above SCHOOL_BASELINE_RATING;
        penalizes below.
      - **$/sqft below the city's median** (CAPPED — a huge intra-city
        discount can't dominate a strong-fabric listing near median).
      - **Steep-discount penalty** — a discount past `steep_discount_pct`
        is a red flag (condition/location problem), not a rank-boost.
      - Recent price cut vs first observed price for this URL.
      - Days-on-market (patience discount, capped at DOM_BONUS_CEILING_DAYS).
      - Generous interior square footage (small bonus above SQFT_BONUS_FLOOR).

    All weights are config-tunable; missing weights fall back to the
    DEFAULT_WEIGHT_* module constants so existing configs keep working after
    adding this module.
    """
    if weights is None:
        weights = {}
    below_median_weight = float(
        weights.get("below_median_ppsf", DEFAULT_WEIGHT_BELOW_MEDIAN_PPSF)
    )
    price_cut_weight = float(weights.get("price_cut", DEFAULT_WEIGHT_PRICE_CUT))
    dom_weight = float(weights.get("dom_bonus", DEFAULT_WEIGHT_DOM_BONUS))
    sqft_weight = float(weights.get("sqft_bonus", DEFAULT_WEIGHT_SQFT_BONUS))
    city_tier_weight = float(weights.get("city_tier", DEFAULT_WEIGHT_CITY_TIER))
    school_weight = float(weights.get("schools", DEFAULT_WEIGHT_SCHOOLS))

    score = 0.0

    # (a) City desirability tier — the dominant absolute-quality signal so a
    # cheap listing in a low-tier city can't unseat a solid top-tier one.
    city_tier = _resolve_city_tier(listing.get("city") or "", city_tier_overrides)
    score += city_tier_weight * city_tier

    # (b) School rating (average of assigned elementary/middle/high). Missing
    # school data is neutral: we neither boost nor punish "unknown".
    school_avg = _extract_school_avg(school_data)
    if school_avg is not None:
        score += school_weight * (school_avg - SCHOOL_BASELINE_RATING)

    # (c) Value vs city median $/sqft — bounded so it can't drown out (a)+(b).
    # A steep-discount tripwire beyond steep_discount_pct SUBTRACTS score
    # instead of adding, because it usually signals condition or location
    # problems (busy street, cash-only, teardown) rather than a windfall.
    ppsf = listing.get("price_per_sqft")
    median = median_ppsf_by_city.get(listing.get("city") or "")
    if ppsf and median and median > 0:
        below_fraction = max(0.0, (median - ppsf) / median)
        contribution = below_median_weight * below_fraction
        # Cap the pure-discount contribution proportional to top-tier city bonus.
        cap = city_tier_weight * CITY_TIER_MAX * BELOW_MEDIAN_CONTRIB_CAP_FRACTION
        if cap > 0:
            contribution = min(contribution, cap)
        score += contribution
        if below_fraction >= steep_discount_pct:
            score -= STEEP_DISCOUNT_PENALTY

    # (d) Price cut vs first observed price + days-on-market.
    if seen_entry:
        history = seen_entry.get("price_history") or []
        if history:
            first_price = history[0][1]
            current_price = listing.get("price")
            if first_price and current_price and current_price < first_price:
                drop_fraction = (first_price - current_price) / first_price
                score += price_cut_weight * drop_fraction

        first_seen_str = seen_entry.get("first_seen")
        if first_seen_str:
            try:
                first_seen = datetime.date.fromisoformat(first_seen_str)
                dom = (datetime.date.today() - first_seen).days
                score += dom_weight * min(max(dom, 0), DOM_BONUS_CEILING_DAYS)
            except ValueError:
                pass

    # (e) Larger-home nudge.
    sqft = listing.get("sqft")
    if sqft and sqft > SQFT_BONUS_FLOOR:
        score += sqft_weight * (sqft - SQFT_BONUS_FLOOR)

    return score


def rank_and_cap(
    items: List[Tuple[Dict[str, Any], List[str]]],
    seen_by_url: Dict[str, Dict[str, Any]],
    median_ppsf_by_city: Dict[str, int],
    weights: Optional[Dict[str, float]],
    cap: int,
    city_tier_overrides: Optional[Dict[str, int]] = None,
    steep_discount_pct: float = 0.25,
) -> List[Tuple[Dict[str, Any], List[str]]]:
    """Sort (listing, flag_strs) tuples by descending value score and cap to `cap`.

    A cap of 0 or negative disables the cap (returns all items in ranked order).

    School data is pulled from the seen-state entry under the "schools" key —
    house_scan.py caches the extraction there once per listing so we don't
    hit the detail page every day.
    """
    scored: List[Tuple[float, int, Dict[str, Any], List[str]]] = []
    for index, (listing, flag_strs) in enumerate(items):
        seen_entry = seen_by_url.get(listing.get("url") or "")
        school_data = (seen_entry or {}).get("schools") if seen_entry else None
        score = score_listing(
            listing, seen_entry, median_ppsf_by_city, weights,
            city_tier_overrides=city_tier_overrides,
            school_data=school_data,
            steep_discount_pct=steep_discount_pct,
        )
        # Tie-break by original index so ordering is deterministic when
        # scores collide (e.g. two brand-new listings with no prior state).
        scored.append((score, index, listing, flag_strs))
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    ranked = [(listing, flag_strs) for _, _, listing, flag_strs in scored]
    if cap and cap > 0:
        return ranked[:cap]
    return ranked


# ---------------------------------------------------------------------------
# Property-type + address blocklist filters (post-location-gate)
# ---------------------------------------------------------------------------

# Address / URL fragments that signal a condo unit or new-construction
# builder-plan page rather than a real single-family or standalone townhome:
#   - URL contains "/unit-" (Redfin's condo-unit path)
#   - Address contains a "#<number>" or "Unit <letter>" suffix
#   - Address ends in "Plan" (new-construction floor plan without a real lot)
#
# These are TOGGLED by config `property_type_excludes` — keep the code side
# generic and let the user add/remove patterns without editing this file.
DEFAULT_UNIT_URL_PATTERNS = [r"/unit-[^/]+/"]
DEFAULT_UNIT_ADDRESS_PATTERNS = [
    r"#\s*[A-Za-z0-9\-]+",           # "#1503", "# G"
    r"\bUnit\s+[A-Za-z0-9\-]+",      # "Unit E", "Unit 3"
    r"\bApt\s+[A-Za-z0-9\-]+",       # "Apt B"
]
DEFAULT_PLAN_ADDRESS_PATTERNS = [
    r"^\s*Plan\s+\d+\s+Plan\b",      # "Plan 2 Plan, ..."
    r"\bPlan\b\s*,",                 # "Hackberry Plan, ..."
]


def _first_pattern_match(patterns: List[str], text: str, flags: int = re.IGNORECASE) -> Optional[str]:
    """Return the first pattern that hits `text`, else None."""
    for pat in patterns:
        try:
            if re.search(pat, text, flags):
                return pat
        except re.error:
            continue
    return None


def property_type_excluded(
    listing: Dict[str, Any],
    property_type_excludes: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Return a reason string if the listing looks like a unit/condo/plan.

    property_type_excludes shape (from config):
        {
          "exclude_units_and_condos": true,
          "exclude_new_construction_plans": true,
          "unit_url_patterns": ["..."],      # override defaults
          "unit_address_patterns": ["..."],
          "plan_address_patterns": ["..."]
        }
    """
    if not property_type_excludes:
        return None
    address = listing.get("address") or ""
    url = listing.get("url") or ""
    if property_type_excludes.get("exclude_units_and_condos", True):
        url_pats = property_type_excludes.get("unit_url_patterns") or DEFAULT_UNIT_URL_PATTERNS
        addr_pats = property_type_excludes.get("unit_address_patterns") or DEFAULT_UNIT_ADDRESS_PATTERNS
        hit = _first_pattern_match(url_pats, url) or _first_pattern_match(addr_pats, address)
        if hit:
            return f"looks like a unit/condo (matched {hit!r})"
    if property_type_excludes.get("exclude_new_construction_plans", True):
        plan_pats = property_type_excludes.get("plan_address_patterns") or DEFAULT_PLAN_ADDRESS_PATTERNS
        hit = _first_pattern_match(plan_pats, address)
        if hit:
            return f"looks like a new-construction builder plan (matched {hit!r})"
    return None


def address_blocklisted(
    listing: Dict[str, Any],
    address_blocklist: Optional[List[str]],
) -> Optional[str]:
    """Return a reason if the listing's address matches any blocklist substring.

    The blocklist is a simple case-insensitive substring match against the
    address, deliberately dumb so the user can drop in "12 Example Ln" or a
    whole street like "Example Ln" without wrestling with regex. Blocklist
    entries can be a full address (permanent veto of that one listing) or a
    partial street/pocket (permanent veto of any listing on it).
    """
    if not address_blocklist:
        return None
    address = (listing.get("address") or "").lower()
    for needle in address_blocklist:
        if not isinstance(needle, str) or not needle.strip():
            continue
        if needle.strip().lower() in address:
            return f"address matches blocklist entry {needle.strip()!r}"
    return None
