#!/usr/bin/env python3
"""house_scan.py — config-driven for-sale home scanner (Redfin).

Runs daily via launchd. Sweeps Redfin filtered searches across the user's
target cities, applies a per-city DESIRABLE-LOCATION gate to new listings,
ranks survivors by value, and delivers a short capped digest to Telegram via
deliver-output.py. Stays silent when there is nothing worth surfacing.

Design:
  - Deterministic Python. No LLM. This is plumbing; keep it reliable.
  - stdlib-only. Shells out to bin/browser (stealth Chromium) and
    scripts/deliver-output.py (Telegram delivery).
  - Config lives in $MINERU_HOUSE_SCAN_CONFIG (default
    $MINERU_HOME/config/house_scan_config.json; a starter lives at
    engine/config/house_scan_config.example.json). Every tunable knob (price
    ceiling, cities, deal thresholds, neighborhood policy, city tiers, ranking
    weights, per-bucket caps) is there, so the user edits without touching code.
    Every place/name literal lives in that file, never in this code.
  - State lives in $MINERU_HOME/cache/house_scan_seen.json, keyed by listing URL. Each
    entry tracks first_seen, last_seen, current price, full price history,
    and which flag types have already been surfaced.
  - The location gate (which listings are desirable enough to surface) lives
    in scripts/house_scan_neighborhoods.py — a config-driven per-city
    allow/deny over ZIP + neighborhood keywords + address text.

Surfacing rule (2026-09-14):
  A NEW listing surfaces only if it passes hard filters (price / beds / baths
  / sqft) AND classify_location() returns INCLUDE for it. Deal flags (price
  cut, stale, below-median $/sqft, keyword, sash) STAY as bonus ranking
  signals + still fire on already-seen listings, but they are NOT the gate
  for new listings. All three buckets (NEW / PRICE↓ / FLAG) are ranked by
  value score and capped per digest so a busy day doesn't fire-hose the user.

Failure mode:
  If parsing yields zero listings across ALL cities in one run, deliver a short
  "scanner needs attention" notice. If at least one city returned listings,
  treat other zero-hits as normal (that city just had nothing that matched).

Invocation (CLI, no launchd):
    /usr/bin/python3 scripts/house_scan.py                # normal run
    /usr/bin/python3 scripts/house_scan.py --dry-run      # skip delivery
    /usr/bin/python3 scripts/house_scan.py --config PATH  # override config

Environment:
    MINERU_HOME               workspace root (default ~/.mineru)
    MINERU_HOUSE_SCAN_CONFIG  config path (default $MINERU_HOME/config/house_scan_config.json)
    MINERU_BRIEFS_ROOT        where briefs_house_scan/ lives (default $MINERU_HOME)
"""

import argparse
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make sibling modules importable when the launchd job runs us from an arbitrary
# working directory (launchd's WorkingDirectory can be anywhere).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from house_scan_neighborhoods import (
    LocationVerdict,
    address_blocklisted,
    classify_location,
    property_type_excluded,
    rank_and_cap,
)
from house_scan_school_verdicts import (
    evaluate_school_gate,
    load_school_verdicts,
    resolve_school_verdict,
)
from house_scan_schools import extract_schools, format_schools_for_bullet

MINERU_ROOT = Path(os.environ.get("MINERU_HOME") or (Path.home() / ".mineru")).expanduser()
BRIEFS_ROOT = Path(os.environ.get("MINERU_BRIEFS_ROOT") or MINERU_ROOT).expanduser()
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("MINERU_HOUSE_SCAN_CONFIG") or (MINERU_ROOT / "config" / "house_scan_config.json")
).expanduser()
DEFAULT_SCHOOL_VERDICTS_PATH = "config/house_scan_school_verdicts.json"
DEFAULT_BROWSER_BIN = MINERU_ROOT / "bin" / "browser"
DEFAULT_DELIVER_BIN = MINERU_ROOT / "scripts" / "deliver-output.py"

# JS extractor injected into the Redfin search page. Returns a JSON string
# (so the browser's `evaluate` response is a plain string we can json.loads).
# Kept as a single expression (arrow function invoked inline) so the browser
# CLI can pass it straight through as `expression=...`.
REDFIN_CARD_EXTRACTOR_JS = r"""(() => {
  const cards = Array.from(document.querySelectorAll('.HomeCardContainer[data-rf-test-name="home-card"]'));
  const out = [];
  for (const c of cards) {
    const priceEl = c.querySelector('[data-rf-test-id="homecard-price"], .bp-Homecard__Price--value');
    const addrEl = c.querySelector('[data-rf-test-id="abp-streetLine"], .bp-Homecard__Address');
    const linkEl = c.querySelector(__LINK_SELECTOR__);
    const statsEl = c.querySelector('.bp-Homecard__Stats, .stats');
    const sashes = Array.from(c.querySelectorAll('[data-rf-test-id="home-sash"]'))
      .map(s => (s.innerText || '').trim())
      .filter(Boolean);
    // Grab a chunk of the surrounding descriptive text; the listing remarks
    // paragraph is embedded above the address inside the same card.
    const cardText = (c.innerText || '').replace(/\s+/g, ' ').trim();
    out.push({
      price: priceEl ? priceEl.innerText.trim() : null,
      address: addrEl ? addrEl.innerText.trim() : null,
      stats: statsEl ? statsEl.innerText.trim() : '',
      href: linkEl ? linkEl.getAttribute('href') : null,
      sashes: sashes,
      card_text: cardText.substring(0, 2000)
    });
  }
  return JSON.stringify(out);
})()"""




def build_card_extractor_js(config: Dict[str, Any]) -> str:
    """Fill the card-link selector; config `redfin_state_code` (e.g. two-letter
    state) adds Redfin's `/<ST>/...` path as a fallback link pattern."""
    selectors = ['a[href*="/home/"]']
    state_code = str(config.get("redfin_state_code") or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", state_code):
        selectors.append(f'a[href^="/{state_code.upper()}/"]')
    return REDFIN_CARD_EXTRACTOR_JS.replace("__LINK_SELECTOR__", json.dumps(", ".join(selectors)))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def today_iso() -> str:
    return datetime.date.today().isoformat()


def parse_price_usd(price_str: Optional[str]) -> Optional[int]:
    """"$1,688,000" → 1688000. Returns None on parse failure."""
    if not price_str:
        return None
    digits = re.sub(r"[^0-9]", "", price_str)
    return int(digits) if digits else None


def parse_stats(stats_text: str) -> Tuple[Optional[float], Optional[float], Optional[int]]:
    """"3 beds\n2.5 baths\n1,250 sq ft" → (3.0, 2.5, 1250)."""
    beds = baths = sqft = None
    bed_m = re.search(r"([\d.]+)\s*beds?", stats_text, re.I)
    if bed_m:
        try:
            beds = float(bed_m.group(1))
        except ValueError:
            pass
    bath_m = re.search(r"([\d.]+)\s*baths?", stats_text, re.I)
    if bath_m:
        try:
            baths = float(bath_m.group(1))
        except ValueError:
            pass
    sqft_m = re.search(r"([\d,]+)\s*sq\s*ft", stats_text, re.I)
    if sqft_m:
        try:
            sqft = int(sqft_m.group(1).replace(",", ""))
        except ValueError:
            pass
    return beds, baths, sqft


# "<street>, <City>, <ST> <zip>": any two-letter US state code, so the parser
# carries no region of its own.
CITY_FROM_ADDR_RE = re.compile(r",\s*([A-Za-z .]+?),\s*[A-Z]{2}\s*\d{5}")


def city_from_address(address: str) -> str:
    m = CITY_FROM_ADDR_RE.search(address or "")
    return m.group(1).strip() if m else ""


def price_per_sqft(price: Optional[int], sqft: Optional[int]) -> Optional[int]:
    if not price or not sqft:
        return None
    return round(price / sqft)


def _keyword_hit(needle_lower: str, haystack_lower: str) -> bool:
    """Word-boundary keyword match, hyphens and spaces both treated as boundaries.

    Guards against short keywords like "tlc" matching mid-word ("particle"), and
    lets a hyphenated form like "as-is" match "as-is" verbatim.
    """
    pattern = r"(?<![a-z0-9])" + re.escape(needle_lower) + r"(?![a-z0-9])"
    return re.search(pattern, haystack_lower) is not None


# ---------------------------------------------------------------------------
# Browser CLI wrapper — shells out to bin/browser
# ---------------------------------------------------------------------------


class BrowserError(RuntimeError):
    pass


def browser_call(browser_bin: str, action: str, timeout: int = 60, **kwargs: Any) -> Dict[str, Any]:
    """Invoke bin/browser with key=value args, return parsed JSON.

    Raises BrowserError on any non-JSON response or explicit {"error": ...}
    payload so the caller can degrade gracefully.
    """
    args = [browser_bin, f"action={action}"]
    for k, v in kwargs.items():
        args.append(f"{k}={v}")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise BrowserError(f"browser {action} timed out after {timeout}s") from e
    if proc.returncode != 0 and not proc.stdout.strip():
        raise BrowserError(f"browser {action} exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise BrowserError(
            f"browser {action} returned non-JSON stdout: {proc.stdout[:200]!r}"
        ) from e
    if isinstance(parsed, dict) and parsed.get("error"):
        raise BrowserError(f"browser {action} error: {parsed['error']}")
    return parsed


# ---------------------------------------------------------------------------
# Redfin URL construction + scrape
# ---------------------------------------------------------------------------


def build_redfin_url(city_slug: str, config: Dict[str, Any]) -> str:
    """Build a Redfin filtered-search URL for one city, sorted newest-first."""
    ceiling = int(config["price_ceiling_usd"])
    # Redfin's URL accepts max-price in raw dollars OR e.g. "2.5M"; raw is exact.
    ceiling_str = f"{ceiling}"
    pt = "+".join(config["property_types"])
    min_beds = int(config["min_beds"])
    min_baths = int(config["min_baths"])
    filter_parts = [
        "sort=lo-days",  # lo-days = newest listings first on Redfin
        f"property-type={pt}",
        f"max-price={ceiling_str}",
        f"min-beds={min_beds}",
        f"min-baths={min_baths}",
    ]
    # Optional sqft floor. Redfin supports min-sqft on the URL, so filter at
    # the source when the config sets one — cheaper than scrolling extra
    # cards. Belt-and-suspenders check in passes_basic_filter() also blocks
    # any leakage.
    min_sqft = int(config.get("min_sqft", 0) or 0)
    if min_sqft > 0:
        filter_parts.append(f"min-sqft={min_sqft}")
    filter_str = ",".join(filter_parts)
    return f"https://www.redfin.com/city/{city_slug}/filter/{filter_str}"


def fetch_city_listings(
    browser_bin: str, city: Dict[str, Any], config: Dict[str, Any], log: logging.Logger
) -> List[Dict[str, Any]]:
    """Open Redfin filter for one city, extract card records, return list.

    Never raises: on any browser or extraction failure returns []. Callers
    aggregate across cities and, if the total is zero, flag "needs attention".
    """
    url = build_redfin_url(city["slug"], config)
    log.info("fetch %s → %s", city["name"], url)
    tab_id: Optional[str] = None
    try:
        opened = browser_call(browser_bin, "open", targetUrl=url, timeout=60)
        tab_id = opened.get("targetId")
        if not tab_id:
            log.warning("no targetId opening %s: %s", city["name"], opened)
            return []
        # Wait for the page to settle so React-rendered cards exist.
        try:
            browser_call(browser_bin, "wait", targetId=tab_id, until="networkidle", timeout=45)
        except BrowserError as e:
            # networkidle occasionally times out but the page is usable; log
            # and press on to evaluate.
            log.warning("wait failed for %s: %s (continuing)", city["name"], e)
        # Small settle beat for slower cards to hydrate.
        time.sleep(1.5)
        got = browser_call(
            browser_bin,
            "evaluate",
            targetId=tab_id,
            expression=build_card_extractor_js(config),
            timeout=60,
        )
        raw = got.get("result")
        if not isinstance(raw, str):
            log.warning("evaluate for %s returned non-string result: %r", city["name"], raw)
            return []
        try:
            listings = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("could not parse card JSON for %s: %s", city["name"], e)
            return []
        # Attach the city label from config so we can filter downstream even if
        # a card's address parses oddly.
        for item in listings:
            item["_scan_city"] = city["name"]
        log.info("%s: %d raw cards", city["name"], len(listings))
        return listings
    except BrowserError as e:
        log.warning("browser error for %s: %s", city["name"], e)
        return []
    finally:
        if tab_id:
            try:
                browser_call(browser_bin, "close", targetId=tab_id, timeout=10)
            except BrowserError as e:
                log.warning("close failed for %s tab %s: %s", city["name"], tab_id, e)


# ---------------------------------------------------------------------------
# Listing normalization + filtering
# ---------------------------------------------------------------------------


def normalize_listing(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a scraped card into a canonical dict; drop entries with no URL/price."""
    href = raw.get("href")
    price = parse_price_usd(raw.get("price"))
    address = (raw.get("address") or "").strip()
    if not href or not price or not address:
        return None
    if href.startswith("/"):
        url = "https://www.redfin.com" + href
    else:
        url = href
    beds, baths, sqft = parse_stats(raw.get("stats") or "")
    # Redfin's per-city searches spill over into adjacent cities (one city's
    # search returns its neighbors' listings too), so trust the parsed
    # address's city first — the _scan_city stamp is only a fallback when the
    # address is unparseable.
    city = city_from_address(address) or raw.get("_scan_city") or ""
    ppsf = price_per_sqft(price, sqft)
    return {
        "url": url,
        "address": address,
        "city": city,
        "price": price,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "price_per_sqft": ppsf,
        "sashes": raw.get("sashes") or [],
        "card_text": (raw.get("card_text") or "").strip(),
    }


def passes_basic_filter(listing: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """Enforce hard filters from config (belt-and-suspenders: URL already filters).

    Missing values (beds/baths/sqft = None) pass — Redfin occasionally omits
    a stats field on a card and we would rather over-include than silently
    drop an otherwise-good listing. The URL-side filter still enforces the
    floors at the source.
    """
    if listing["price"] > int(config["price_ceiling_usd"]):
        return False
    if listing["beds"] is not None and listing["beds"] < config["min_beds"]:
        return False
    if listing["baths"] is not None and listing["baths"] < config["min_baths"]:
        return False
    min_sqft = int(config.get("min_sqft", 0) or 0)
    if min_sqft > 0 and listing.get("sqft") is not None and listing["sqft"] < min_sqft:
        return False
    return True


def compute_deal_flags(
    listing: Dict[str, Any],
    seen_entry: Optional[Dict[str, Any]],
    config: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """Return a list of (flag_type, human_string) tuples for a listing.

    flag_type is a stable tag ("price_cut", "below_median", "stale", "keyword",
    "sash") the caller uses to dedup notifications day-to-day so a static fact
    (e.g. "$/sqft below median") only surfaces once per listing.
    """
    flags: List[Tuple[str, str]] = []
    df = config["deal_flags"]

    # (a) price cut vs any earlier price we've seen for this listing.
    if seen_entry:
        history = seen_entry.get("price_history", [])
        if history:
            first_price = history[0][1]
            if first_price and listing["price"] < first_price:
                drop_pct = (first_price - listing["price"]) / first_price
                if drop_pct >= df["price_cut_pct"]:
                    flags.append(
                        (
                            "price_cut",
                            f"price cut {drop_pct*100:.1f}% from ${first_price:,} → ${listing['price']:,}",
                        )
                    )

    # (b) below-city-$/sqft-median flag.
    ppsf = listing.get("price_per_sqft")
    median_map = config.get("median_price_per_sqft_by_city", {})
    median_ppsf = median_map.get(listing.get("city") or "")
    if ppsf and median_ppsf:
        below_pct = (median_ppsf - ppsf) / median_ppsf
        if below_pct >= df["price_per_sqft_below_city_median_pct"]:
            flags.append(
                (
                    "below_median",
                    f"${ppsf}/sqft is {below_pct*100:.1f}% below {listing['city']} median (${median_ppsf}/sqft)",
                )
            )

    # (c) stale-on-market flag: days we've observed this listing.
    if seen_entry:
        try:
            first_seen = datetime.date.fromisoformat(seen_entry["first_seen"])
            dom = (datetime.date.today() - first_seen).days
            if dom >= df["stale_days_on_market"]:
                flags.append(
                    ("stale", f"stale — {dom} days since first seen ({first_seen})")
                )
        except (KeyError, ValueError):
            pass

    # (d) description keyword hits. Word-boundary match so short keywords ("TLC",
    # "as-is") don't false-positive inside longer words. The `\b` anchor also
    # prevents hyphenated keywords from bleeding across word boundaries.
    card_lower = (listing.get("card_text") or "").lower()
    for kw in df.get("description_keywords", []):
        if kw and _keyword_hit(kw.lower(), card_lower):
            flags.append(("keyword", f'listing text contains "{kw}"'))
            break  # one is enough — don't stack synonyms

    # (e) sash badge flag.
    sash_lower = " | ".join(listing.get("sashes", [])).lower()
    for kw in df.get("sash_keywords", []):
        if kw and _keyword_hit(kw.lower(), sash_lower):
            flags.append(
                (
                    "sash",
                    f'Redfin badge: "{kw}" ({" / ".join(listing["sashes"])})',
                )
            )
            break

    return flags


# ---------------------------------------------------------------------------
# Seen-state persistence
# ---------------------------------------------------------------------------


def load_seen(path: Path, log: logging.Logger) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read seen state at %s: %s (starting fresh)", path, e)
        return {}


def save_seen(path: Path, seen: Dict[str, Dict[str, Any]], log: logging.Logger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(seen, indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError as e:
        log.error("failed to save seen state to %s: %s", path, e)


def upsert_seen(
    seen: Dict[str, Dict[str, Any]], listing: Dict[str, Any]
) -> Tuple[bool, bool]:
    """Update the seen-state entry for one listing.

    Returns (is_new, is_newly_price_changed):
      - is_new: True the very first time we ever see this URL.
      - is_newly_price_changed: True on this run if the observed price differs
        from the most recent recorded price (up or down).
    """
    today = today_iso()
    entry = seen.get(listing["url"])
    if entry is None:
        seen[listing["url"]] = {
            "url": listing["url"],
            "address": listing["address"],
            "city": listing["city"],
            "first_seen": today,
            "last_seen": today,
            "price_history": [[today, listing["price"]]],
            "beds": listing["beds"],
            "baths": listing["baths"],
            "sqft": listing["sqft"],
            # Flag TYPES already surfaced for this listing, so a static fact
            # like below-median $/sqft only lands in the digest once.
            "reported_flags": [],
        }
        return True, False
    entry["last_seen"] = today
    entry["address"] = listing["address"] or entry.get("address")
    entry["city"] = listing["city"] or entry.get("city")
    entry["beds"] = listing["beds"] if listing["beds"] is not None else entry.get("beds")
    entry["baths"] = listing["baths"] if listing["baths"] is not None else entry.get("baths")
    entry["sqft"] = listing["sqft"] if listing["sqft"] is not None else entry.get("sqft")
    history = entry.setdefault("price_history", [])
    last_price = history[-1][1] if history else None
    price_changed = last_price != listing["price"]
    if price_changed:
        history.append([today, listing["price"]])
    return False, price_changed


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------


def format_listing_bullet(
    listing: Dict[str, Any],
    flag_strs: List[str],
    tag: str,
    school_data: Optional[Dict[str, Any]] = None,
    school_verdicts: Optional[List[Dict[str, Any]]] = None,
    vetted_callout_below: float = 7.0,
) -> str:
    """One Telegram-friendly bullet per listing.

    Shape (bold headline + one indented context line each, whitespace
    between items handled by the caller):

        **NEW · $1,499,000 · 3bd/2.5ba · 1,650 sqft · $909/sqft**
          12 Example Ln, Maple Hollow, ST 00001
          Schools 7/10 · 8/10 · 10/10 avg 8.33 (Birch, Cedar, Maple Hollow)
          Why: ...
          https://www.redfin.com/...

    No markdown tables. No `_italic_` (Telegram won't render an italic span
    that contains an internal underscore, and the raw `_` leaks into the
    rendered message). Bold via **…**, everything else plain text.
    """
    price_str = f"${listing['price']:,}"
    beds = listing["beds"]
    baths = listing["baths"]
    sqft = listing["sqft"]
    stat_bits: List[str] = []
    if beds is not None:
        stat_bits.append(f"{int(beds) if beds == int(beds) else beds}bd")
    if baths is not None:
        stat_bits.append(f"{int(baths) if baths == int(baths) else baths}ba")
    if sqft:
        stat_bits.append(f"{sqft:,} sqft")
    if listing.get("price_per_sqft"):
        stat_bits.append(f"${listing['price_per_sqft']}/sqft")
    headline_bits = [tag, price_str] + stat_bits
    lines = [f"**{' · '.join(headline_bits)}**", f"  {listing['address']}"]
    if school_data:
        schools_line = format_schools_for_bullet(school_data)
        # A school passed by a researched include verdict despite a low number
        # is marked so the reader knows why it is here.
        vetted = vetted_in_school_names(
            school_data, listing.get("city") or "", school_verdicts or [], vetted_callout_below
        )
        if vetted:
            schools_line += f" · vetted in: {', '.join(vetted)}"
        lines.append(f"  {schools_line}")
    if flag_strs:
        lines.append(f"  Why: {'; '.join(flag_strs)}")
    lines.append(f"  {listing['url']}")
    return "\n".join(lines)


def vetted_in_school_names(
    school_data: Dict[str, Any],
    listing_city: str,
    school_verdicts: List[Dict[str, Any]],
    callout_below: float = 7.0,
) -> List[str]:
    """Names of assigned schools that carry an explicit include verdict AND sit below `callout_below`.

    `callout_below` is the lowest nonzero elementary/middle fallback floor from config.

    Only those are worth calling out: a 10/10 with an include verdict needs no explanation.
    """
    names: List[str] = []
    for tier in ("elementary", "middle"):
        tier_entry = school_data.get(tier)
        if not isinstance(tier_entry, dict) or not tier_entry.get("name"):
            continue
        verdict = resolve_school_verdict(tier_entry["name"], listing_city, school_verdicts)
        rating = tier_entry.get("rating")
        if verdict and verdict["verdict"] == "include" and rating is not None and float(rating) < callout_below:
            names.append(re.sub(r"\s+(Elementary|Middle|High)\s+School$", "", tier_entry["name"], flags=re.I))
    return names


def build_digest(
    new_listings: List[Tuple[Dict[str, Any], List[str]]],
    reduced_listings: List[Tuple[Dict[str, Any], List[str]]],
    flagged_listings: List[Tuple[Dict[str, Any], List[str]]],
    verify_listings: List[Tuple[Dict[str, Any], List[str]]],
    config: Dict[str, Any],
    seen: Dict[str, Dict[str, Any]],
    school_verdicts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Compose the Telegram digest. Empty string → nothing to deliver.

    Buckets:
      - NEW: passed the location + property-type + optional school hard-min
        gates, ranked by desirability score.
      - PRICE-DROP: existing listings with a fresh price change.
      - FLAG: existing listings where a new deal flag type just fired.
      - VERIFY: listings whose $/sqft is steeply below the city median
        (>= steep_discount_flag_pct). Surfaced separately so an unusually
        cheap listing is treated as "look with skepticism", not "top pick".
    """
    if not (new_listings or reduced_listings or flagged_listings or verify_listings):
        return ""

    total_cap = int(config.get("max_listings_per_digest", 15))

    school_signal_cfg = config.get("school_signal") or {}
    floors = [
        float(school_signal_cfg.get(key, 0) or 0)
        for key in ("min_elementary_rating", "min_middle_rating")
    ]
    vetted_callout_below = min([f for f in floors if f > 0] or [7.0])

    def _bullet(l: Dict[str, Any], flags: List[str], tag: str) -> str:
        school_data = (seen.get(l.get("url") or "") or {}).get("schools")
        return format_listing_bullet(
            l, flags, tag, school_data=school_data, school_verdicts=school_verdicts,
            vetted_callout_below=vetted_callout_below,
        )

    def _section(items: List[Tuple[Dict[str, Any], List[str]]], tag: str) -> List[str]:
        # One blank line BETWEEN bullets so Telegram renders each item as a
        # visually separated block (no tables, whitespace does the work).
        chunks: List[str] = []
        for l, flags in items[:total_cap]:
            chunks.append(_bullet(l, flags, tag))
        return ["", *[c + "\n" for c in chunks[:-1]], chunks[-1]] if chunks else []

    parts: List[str] = [f"**House scan — {today_iso()}**"]
    if new_listings:
        parts.append("")
        parts.append(f"**New in desirable locations ({len(new_listings)})**")
        parts.extend(_section(new_listings, "NEW"))
    if reduced_listings:
        parts.append("")
        parts.append(f"**Newly reduced ({len(reduced_listings)})**")
        parts.extend(_section(reduced_listings, "PRICE-DROP"))
    if flagged_listings:
        parts.append("")
        parts.append(f"**Deal-flagged, existing ({len(flagged_listings)})**")
        parts.extend(_section(flagged_listings, "FLAG"))
    if verify_listings:
        parts.append("")
        parts.append(
            f"**Verify, unusually cheap ({len(verify_listings)})** — treat with skepticism, "
            "usually a condition/location problem, not a windfall"
        )
        parts.extend(_section(verify_listings, "VERIFY"))

    # Housekeeping footer — plain text, NO underscored italic (Telegram breaks
    # italic spans that contain internal underscores, which is what the pre-fix
    # `_ceiling ... neighborhood_policy_` block hit).
    min_sqft = int(config.get("min_sqft", 0) or 0)
    size_str = f"{config['min_beds']}+bd/{config['min_baths']}+ba"
    if min_sqft:
        size_str += f"/{min_sqft}+ sqft"
    cities_str = ", ".join(c["name"] for c in config["cities"])
    parts.append("")
    parts.append(
        f"Filters: ≤${int(config['price_ceiling_usd']):,} · {size_str} · {cities_str}"
    )
    parts.append("Location-gated + property-type-filtered + school-signal-scored.")
    parts.append("Edit house_scan_config.json to tune (address_blocklist for personal vetoes).")
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def write_brief(brief_dir: Path, content: str) -> Path:
    brief_dir.mkdir(parents=True, exist_ok=True)
    path = brief_dir / f"house-scan-{today_iso()}.md"
    path.write_text(content)
    return path


def deliver_brief(deliver_bin: str, brief_path: Path, log: logging.Logger) -> bool:
    if not Path(deliver_bin).exists():
        log.warning("deliver-output.py missing at %s; brief written but not sent", deliver_bin)
        return False
    try:
        proc = subprocess.run(
            [deliver_bin, str(brief_path)], capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        log.error("deliver-output.py timed out")
        return False
    if proc.returncode != 0:
        log.error("deliver-output.py exit %s: %s", proc.returncode, proc.stderr)
        return False
    log.info("delivered: %s", proc.stdout.strip())
    return True


def deliver_raw(deliver_bin: str, text: str, log: logging.Logger) -> bool:
    if not Path(deliver_bin).exists():
        log.warning("deliver-output.py missing; would have sent: %s", text)
        return False
    try:
        proc = subprocess.run(
            [deliver_bin, "--raw", text], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        log.error("deliver-output.py --raw timed out")
        return False
    if proc.returncode != 0:
        log.error("deliver-output.py --raw exit %s: %s", proc.returncode, proc.stderr)
        return False
    log.info("delivered raw: %s", proc.stdout.strip())
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "house-scan.log"
    logger = logging.getLogger("house_scan")
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers on repeated imports.
    if not logger.handlers:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(sh)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description="Config-driven Redfin house deal scanner")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Scan + build digest but do not deliver")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not update seen-state (useful when smoke-testing so state stays clean)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"config not found: {config_path}", file=sys.stderr)
        return 2
    config = json.loads(config_path.read_text())

    log_dir = MINERU_ROOT / config.get("log_path", "logs/house-scan")
    log = setup_logging(log_dir)
    log.info("=== house_scan start (config=%s dry_run=%s) ===", config_path, args.dry_run)

    seen_path = MINERU_ROOT / config.get("seen_state_path", "cache/house_scan_seen.json")
    seen = load_seen(seen_path, log)
    is_bootstrap = len(seen) == 0
    log.info("loaded %d seen listings from %s (bootstrap=%s)", len(seen), seen_path, is_bootstrap)

    # Optional in config; default to the workspace's own tools.
    browser_bin = str(Path(config.get("browser_bin") or DEFAULT_BROWSER_BIN).expanduser())
    deliver_bin = str(Path(config.get("deliver_bin") or DEFAULT_DELIVER_BIN).expanduser())

    # Sweep every city, aggregate cards.
    all_raw: List[Dict[str, Any]] = []
    per_city_counts: Dict[str, int] = {}
    for city in config["cities"]:
        raws = fetch_city_listings(browser_bin, city, config, log)
        per_city_counts[city["name"]] = len(raws)
        all_raw.extend(raws)

    log.info("per-city raw counts: %s", per_city_counts)

    # Extraction total-sanity check: zero cards across every city almost
    # certainly means Redfin markup shifted (or CloakBrowser is 403-ing).
    # Notify the user and bail so silent failure doesn't hide a broken scanner.
    if sum(per_city_counts.values()) == 0:
        log.error("all cities returned 0 cards — parsing broken or blocked")
        if not args.dry_run:
            deliver_raw(
                deliver_bin,
                f"House scanner needs attention: 0 cards across all {len(config['cities'])} cities on {today_iso()}. "
                "Redfin selectors may have shifted, or the stealth browser is blocked. "
                "Check logs/house-scan/house-scan.log.",
                log,
            )
        return 1

    # Normalize + dedup by URL.
    listings_by_url: Dict[str, Dict[str, Any]] = {}
    for raw in all_raw:
        norm = normalize_listing(raw)
        if not norm:
            continue
        if not passes_basic_filter(norm, config):
            continue
        # Prefer the entry from the primary city if we see the same listing in
        # multiple city searches (Redfin sometimes returns nearby overlaps).
        listings_by_url.setdefault(norm["url"], norm)
    listings = list(listings_by_url.values())
    log.info("%d listings after normalize + dedup", len(listings))

    # Classify + compute flags. Update seen-state as we go.
    #
    # Notification-dedup rules:
    #   - LOCATION GATE (added 2026-09-14): a NEW listing surfaces only if
    #     classify_location() returns INCLUDE for it under the config's
    #     `neighborhood_policy` (per-city ZIP + keyword allow/deny). The gate
    #     applies to PRICE↓ and FLAG buckets too — if we intentionally never
    #     surfaced a listing (wrong location), surfacing it later on a price
    #     cut or stale flag would defeat the gate. State is still updated
    #     silently so we don't reconsider excluded listings every run.
    #   - FLAG dedup: most deal flags are static facts about a listing
    #     (below-median $/sqft, MLS "as-is" wording). Fire them ONCE per
    #     listing, not every day. The seen-state entry keeps a `reported_flags`
    #     set of flag TYPES already surfaced.
    new_batch: List[Tuple[Dict[str, Any], List[str]]] = []
    reduced_batch: List[Tuple[Dict[str, Any], List[str]]] = []
    flagged_batch: List[Tuple[Dict[str, Any], List[str]]] = []
    verify_batch: List[Tuple[Dict[str, Any], List[str]]] = []
    excluded_new_by_reason: Dict[str, int] = {}
    included_count = 0

    neighborhood_policy = config.get("neighborhood_policy") or {}
    property_type_excludes = config.get("property_type_excludes") or {}
    address_blocklist = config.get("address_blocklist") or []
    school_signal_cfg = config.get("school_signal") or {}
    fetch_schools_enabled = bool(school_signal_cfg.get("fetch_schools", True))
    max_school_fetches = int(school_signal_cfg.get("max_fetches_per_run", 8))
    min_school_avg = float(school_signal_cfg.get("min_school_rating_avg", 0) or 0)
    fallback_floors = {
        "elementary": float(school_signal_cfg.get("min_elementary_rating", 0) or 0),
        "middle": float(school_signal_cfg.get("min_middle_rating", 0) or 0),
        "high": float(school_signal_cfg.get("min_high_rating", 0) or 0),
    }
    verdicts_path = Path(config.get("school_verdicts_path") or DEFAULT_SCHOOL_VERDICTS_PATH).expanduser()
    if not verdicts_path.is_absolute():
        verdicts_path = MINERU_ROOT / verdicts_path
    school_verdicts = load_school_verdicts(verdicts_path)
    log.info("loaded %d per-school verdicts from %s", len(school_verdicts), verdicts_path)
    # (tier, school, rating) -> count: schools decided by the numeric fallback
    # this run because nobody has researched them yet. Logged at the end so
    # the verdict list can grow.
    unresearched_schools: Dict[Tuple[str, str, Any], int] = {}
    steep_discount_pct = float(config.get("steep_discount_flag_pct", 0.25))
    median_map_for_flag = config.get("median_price_per_sqft_by_city") or {}

    # Track candidates that pass ALL gates so we know who to fetch schools for.
    # This staging list separates "eligible for surfacing" from "actually
    # surfaced this run" — the latter is filtered again by the school hard-min
    # and by ranking + capping downstream.
    school_fetches_used = 0

    def _bump_reason(reason_key: str) -> None:
        excluded_new_by_reason[reason_key] = excluded_new_by_reason.get(reason_key, 0) + 1

    for l in listings:
        prev = seen.get(l["url"])
        if args.no_persist:
            is_new = prev is None
            is_price_changed = False
        else:
            is_new, is_price_changed = upsert_seen(seen, l)
        entry_for_flags = seen.get(l["url"]) if not args.no_persist else prev
        typed_flags = compute_deal_flags(l, entry_for_flags, config)

        entry = seen.get(l["url"]) if not args.no_persist else None
        already_reported = set((entry or {}).get("reported_flags", []))

        # (1) Location gate.
        verdict, verdict_reason = classify_location(l, neighborhood_policy, log)
        if verdict != LocationVerdict.INCLUDE:
            _bump_reason("location_gate: " + verdict_reason.split("(")[0].strip())
            continue

        # (2) Property-type filter (unit/condo/new-construction plan).
        pt_reason = property_type_excluded(l, property_type_excludes)
        if pt_reason:
            _bump_reason("property_type: " + pt_reason.split("(")[0].strip())
            log.info("drop %s (property-type: %s)", l.get("url"), pt_reason)
            continue

        # (3) Address blocklist (the user's manual veto list).
        bl_reason = address_blocklisted(l, address_blocklist)
        if bl_reason:
            _bump_reason("blocklist")
            log.info("drop %s (blocklist: %s)", l.get("url"), bl_reason)
            continue

        included_count += 1

        # (4) Fetch schools ONCE per listing, cache in seen-state. Bounded
        # by max_fetches_per_run so a busy day can't stall the whole scan on
        # detail-page loads. Skips: --no-persist (we'd throw away the cache),
        # fetch_schools=false (kill switch), or a listing that already has
        # school data from a prior run.
        if (
            fetch_schools_enabled
            and not args.no_persist
            and entry is not None
            and not (isinstance(entry.get("schools"), dict) and entry["schools"].get("status") == "ok")
            and school_fetches_used < max_school_fetches
        ):
            log.info("fetching schools for %s", l.get("url"))
            try:
                schools = extract_schools(browser_bin, l["url"], log)
            except (subprocess.TimeoutExpired, OSError) as e:
                schools = {"status": "unknown", "reason": f"{type(e).__name__}: {e}"}
            entry["schools"] = schools
            school_fetches_used += 1

        # (5) School gate: applies to EVERY bucket (NEW, PRICE-DROP, FLAG,
        # VERIFY), same reasoning as the location gate: a listing we would
        # never surface as new must not sneak back in on a price cut or a
        # stale flag. Decided PER SCHOOL by the researched verdict list
        # (house_scan_school_verdicts.json); the numeric floors are only the
        # fallback for schools nobody has researched yet. Missing data ≠
        # blocked; "unknown" flows through tagged so the user can decide.
        school_data = (entry or {}).get("schools") if entry else None
        school_hard_block, fallback_decisions, _vetted = evaluate_school_gate(
            school_data, l.get("city") or "", school_verdicts, fallback_floors
        )
        for decision in fallback_decisions:
            unresearched_schools[decision] = unresearched_schools.get(decision, 0) + 1
        if (
            school_hard_block is None
            and min_school_avg > 0
            and isinstance(school_data, dict)
            and school_data.get("status") == "ok"
            and school_data.get("avg_assigned_rating") is not None
            and float(school_data["avg_assigned_rating"]) < min_school_avg
        ):
            school_hard_block = f"school avg {school_data['avg_assigned_rating']} < min_school_rating_avg {min_school_avg}"
        if school_hard_block:
            _bump_reason("school_hard_min")
            log.info("drop %s (%s)", l.get("url"), school_hard_block)
            continue

        # (6) Steep-discount routing. A listing whose $/sqft is >= steep_pct
        # under the city median goes into the VERIFY bucket instead of NEW —
        # treated as "look carefully", not "top pick". Applies only to NEW
        # listings; a price cut on an existing listing is not routed away.
        ppsf = l.get("price_per_sqft")
        city_median = median_map_for_flag.get(l.get("city") or "")
        is_steep = False
        if is_new and ppsf and city_median and city_median > 0:
            below_frac = (city_median - ppsf) / city_median
            if below_frac >= steep_discount_pct:
                is_steep = True

        # (7) Bucket routing.
        if is_new:
            flag_strs = [s for _, s in typed_flags]
            if is_steep:
                verify_note = (
                    f"unusually cheap ({(city_median - ppsf) / city_median * 100:.0f}% under "
                    f"{l.get('city')} median $/sqft)"
                )
                verify_batch.append((l, [verify_note, *flag_strs]))
            else:
                new_batch.append((l, flag_strs))
            if entry is not None:
                entry["reported_flags"] = sorted(already_reported | {t for t, _ in typed_flags})
        elif is_price_changed:
            flag_strs = [s for _, s in typed_flags]
            reduced_batch.append((l, flag_strs))
            if entry is not None:
                entry["reported_flags"] = sorted(already_reported | {t for t, _ in typed_flags})
        else:
            fresh = [(t, s) for t, s in typed_flags if t not in already_reported]
            if fresh:
                flagged_batch.append((l, [s for _, s in fresh]))
                if entry is not None:
                    entry["reported_flags"] = sorted(
                        already_reported | {t for t, _ in typed_flags}
                    )

    log.info(
        "classified pre-rank: %d new · %d newly reduced · %d flagged (existing) "
        "· %d verify (steep discount) · %d excluded overall · school fetches used: %d",
        len(new_batch),
        len(reduced_batch),
        len(flagged_batch),
        len(verify_batch),
        sum(excluded_new_by_reason.values()),
        school_fetches_used,
    )
    if excluded_new_by_reason:
        log.info("filter exclusions by reason: %s", excluded_new_by_reason)

    if unresearched_schools:
        summary = ", ".join(
            f"{name} ({tier} {rating}/10, x{count})"
            for (tier, name, rating), count in sorted(unresearched_schools.items(), key=lambda kv: -kv[1])
        )
        log.info("schools decided by the numeric fallback (unresearched, add to house_scan_school_verdicts.json): %s", summary)

    # Rank each bucket by desirability score and cap per config. Signal
    # priority in the score is now city_tier + schools + capped intra-city
    # discount, so a great house in a top-tier city ranks above a cheap house
    # in a low-tier city even when the cheap one is 30% under median.
    weights = config.get("value_score_weights") or {}
    medians = config.get("median_price_per_sqft_by_city") or {}
    city_tiers = config.get("city_desirability_tiers") or None
    new_cap = int(config.get("max_new_per_digest", 3))
    reduced_cap = int(config.get("max_reduced_per_digest", 3))
    flagged_cap = int(config.get("max_flagged_per_digest", 3))
    verify_cap = int(config.get("max_verify_per_digest", 2))
    new_batch = rank_and_cap(
        new_batch, seen, medians, weights, new_cap,
        city_tier_overrides=city_tiers, steep_discount_pct=steep_discount_pct,
    )
    reduced_batch = rank_and_cap(
        reduced_batch, seen, medians, weights, reduced_cap,
        city_tier_overrides=city_tiers, steep_discount_pct=steep_discount_pct,
    )
    flagged_batch = rank_and_cap(
        flagged_batch, seen, medians, weights, flagged_cap,
        city_tier_overrides=city_tiers, steep_discount_pct=steep_discount_pct,
    )
    verify_batch = rank_and_cap(
        verify_batch, seen, medians, weights, verify_cap,
        city_tier_overrides=city_tiers, steep_discount_pct=steep_discount_pct,
    )
    log.info(
        "post-rank: %d new · %d newly reduced · %d flagged · %d verify",
        len(new_batch), len(reduced_batch), len(flagged_batch), len(verify_batch),
    )

    # First-run bootstrap: on a fresh state, EVERY listing looks "new" and
    # every static flag (below-median $/sqft, MLS keywords) fires for the
    # first time — that would flood the user with the whole current market on
    # both Day 1 AND Day 2. Instead: on bootstrap, mark all currently-firing
    # flag types as "reported" so Day 2 only surfaces truly new inventory,
    # price changes, or brand-new stale/badge signals; then send the user a short
    # one-liner so he knows the scanner is live.
    if is_bootstrap and not args.no_persist:
        included_count = 0
        for l in listings:
            entry = seen.get(l["url"])
            if entry is None:
                continue
            typed = compute_deal_flags(l, entry, config)
            entry["reported_flags"] = sorted({t for t, _ in typed})
            verdict, _ = classify_location(l, neighborhood_policy, log)
            if verdict == LocationVerdict.INCLUDE:
                included_count += 1
        save_seen(seen_path, seen, log)
        log.info(
            "bootstrap run — seeded %d listings (%d pass the location gate), "
            "pre-marked existing flags as reported",
            len(listings),
            included_count,
        )
        if not args.dry_run:
            deliver_raw(
                deliver_bin,
                f"House scanner primed: watching {len(listings)} listings across "
                f"{', '.join(c['name'] for c in config['cities'])} at ≤${int(config['price_ceiling_usd']):,}, "
                f"{config['min_beds']}+bd/{config['min_baths']}+ba/{int(config.get('min_sqft', 0) or 0)}+ sqft "
                f"({included_count} in desirable locations). "
                "Deltas (new / price cut / stale) start tomorrow.",
                log,
            )
        return 0

    # Gate-health check: cards parsed but the location gate excluded EVERY one of
    # them is almost never a real "quiet market" across every target city — it points at a
    # neighborhood_policy typo or a Redfin markup shift that dropped the city/ZIP
    # from card addresses. Surface it so a silent "nothing to report" can't mask a
    # broken gate. (Bootstrap returns earlier, so this only guards steady-state runs.)
    if listings and included_count == 0:
        log.error(
            "location gate excluded all %d parsed listings — likely policy or markup issue",
            len(listings),
        )
        if not args.dry_run:
            deliver_raw(
                deliver_bin,
                f"House scanner needs attention: all {len(listings)} listings were excluded "
                f"by the location gate on {today_iso()}. Likely a neighborhood_policy typo or a "
                "Redfin markup change dropping city/ZIP from addresses. "
                "Check logs/house-scan/house-scan.log.",
                log,
            )
        return 1

    if not args.no_persist:
        save_seen(seen_path, seen, log)

    digest = build_digest(new_batch, reduced_batch, flagged_batch, verify_batch, config, seen, school_verdicts)
    if not digest:
        log.info("nothing new/notable — staying silent")
        return 0

    brief_dir = BRIEFS_ROOT / config.get("brief_dir", "briefs_house_scan")
    brief_path = write_brief(brief_dir, digest)
    log.info("wrote brief: %s (%d chars)", brief_path, len(digest))

    if args.dry_run:
        log.info("--dry-run: skipping delivery")
        return 0

    if deliver_brief(deliver_bin, brief_path, log):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
