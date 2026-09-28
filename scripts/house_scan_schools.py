#!/usr/bin/env python3
"""house_scan_schools.py — Extract GreatSchools ratings for a Redfin listing.

Redfin's per-listing detail page has a "Schools" section powered by GreatSchools
that names the assigned elementary / middle / high schools and gives each a 1-10
rating. School quality is a first-class ranking signal, so this
module opens the listing page in the stealth browser, walks the schools
section, and returns a small structured dict for house_scan.py to cache and
score against.

Design:
  - stdlib-only. Shells out to bin/browser (CloakBrowser) the same way
    house_scan.py's search-page extractor does.
  - Best-effort + fail-quiet: any browser or parsing failure returns
    SchoolsUnknown (a sentinel dict) so the caller can surface the listing
    with "school data unknown" rather than crash.
  - Rate-limited by the caller: house_scan.py only fetches detail pages for
    the handful of candidates that already passed the location gate. This
    module doesn't loop; it does ONE listing at a time.

Parsing:
  Redfin's schools section renders as a flat block of innerText with a
  predictable shape (verified 2026-09-16 across four listings, houses and a
  condo in several cities):

      Schools
      K - 12
      Preschool & daycare
      <School Name>
      \n
      Public K-5 · Assigned · 0.5mi
      \n
      6/10
      <next school...>

  The role (elementary/middle/high) is derived from the grade code:
    - K-5 / K-6 / K-8 with N/A middle -> elementary
    - 6-8 / 7-8 -> middle
    - 9-12 -> high

  When a listing has NO assigned public schools shown (rare — usually
  new-construction plans without an address yet), the section is present but
  the assigned block is empty; extract_schools() returns SchoolsUnknown so
  the caller can degrade gracefully.
"""

import json
import logging
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

# Timeouts kept generous — Redfin detail pages are heavy (map tiles, photos,
# GreatSchools iframe). The caller only fetches N per run (default 6), so a
# slow tail per fetch is acceptable.
DETAIL_PAGE_LOAD_TIMEOUT_S = 45
DETAIL_PAGE_SETTLE_SECONDS = 2.0
CLOSE_TAB_TIMEOUT_S = 10

# innerText of the schools section is small (<1KB) so a fixed cap is fine.
SCHOOLS_INNER_TEXT_CAP_CHARS = 4000

# Sentinel we return when we cannot parse a real rating (browser error, section
# missing, no assigned schools rendered). The caller distinguishes this from a
# genuine "no schools" case by presence of the "reason" key.
SchoolsUnknown = {"status": "unknown"}


# JS extractor: pull the schools-section innerText, plus a coarse detail-page
# flag ("propertyType") so we can double-check the listing is a house/townhome.
# Returns a JSON string so the browser CLI hands back one plain string field.
REDFIN_SCHOOLS_EXTRACTOR_JS = r"""(() => {
  const section = document.querySelector('#schools-scroll') ||
                  document.querySelector('.schools');
  const inner = section ? (section.innerText || '').substring(0, 4000) : '';
  // Property-type text lives in different DOM slots depending on the
  // listing template; try a few and take the first non-empty one.
  const typeSelectors = [
    '[data-rf-test-id="abp-propertyType"]',
    '.propertyDetailsHeader .subText',
    '.HomeMainStats .statsValue',
    '[data-rf-test-name="abp-propertyType"]',
  ];
  let propertyType = '';
  for (const sel of typeSelectors) {
    const el = document.querySelector(sel);
    if (el && (el.innerText || '').trim()) {
      propertyType = el.innerText.trim();
      break;
    }
  }
  return JSON.stringify({schools_text: inner, property_type: propertyType});
})()"""


def _browser_call(
    browser_bin: str, action: str, timeout: int = 30, **kwargs: Any
) -> Dict[str, Any]:
    """Thin wrapper around bin/browser identical in spirit to house_scan.py's."""
    args = [browser_bin, f"action={action}"]
    for k, v in kwargs.items():
        args.append(f"{k}={v}")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"browser {action} exit {proc.returncode}: {proc.stderr.strip()}")
    parsed = json.loads(proc.stdout)
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(f"browser {action} error: {parsed['error']}")
    return parsed


def _classify_role(grade_code: str) -> Optional[str]:
    """Map a grade span like 'K-5' / '6-8' / '9-12' to elementary/middle/high.

    Redfin's format uses hyphens between grades. K-N spans starting at K are
    always elementary (or elementary-plus-middle, which we still count as
    elementary since it's the youngest-grade school in that stack). 6-8, 7-8,
    and 6-12 middle-range codes are middle. 9-12 is high.
    """
    code = grade_code.strip().upper().replace(" ", "")
    if not code:
        return None
    # Normalize "PK-5" / "TK-5" / "P-K" prefixes to K.
    code = re.sub(r"^(P|T)?K", "K", code)
    if code.startswith("K") or code.startswith("1-"):
        # K-5, K-6, K-8, 1-5. Middle-inclusive K-8 still lands as elementary
        # because the family's elementary-age child attends it first.
        return "elementary"
    if re.match(r"^(6|7)-(7|8|12)$", code):
        return "middle"
    if code.startswith("9-"):
        return "high"
    # Fallback: infer from starting grade number.
    start_match = re.match(r"^(\d+)", code)
    if start_match:
        start = int(start_match.group(1))
        if start <= 5:
            return "elementary"
        if start <= 8:
            return "middle"
        return "high"
    return None


# Regex matches one school block inside the schools innerText:
#   <name>\n\nPublic <grades> • Assigned • <distance>mi\n\n<rating>/10
#
# The bullet character is U+2022 in the rendered text ("•"). We keep it out of
# the pattern by using \s+.\s+ style delimiters (any single non-word char run).
#
# Name group intentionally excludes newlines so it captures ONLY the immediate
# line before the "Public …" line (not the whole section header stack that
# precedes it: "Schools\nK - 12\nPreschool & daycare\n<Name>"). This is what
# makes the extractor work when the schools block sits at the very top of the
# innerText (the first school's name is otherwise glued to the header).
SCHOOL_BLOCK_RE = re.compile(
    r"([^\n]+?)\s*\n\s*\n"                                    # name (single line)
    r"\s*Public\s+([A-Za-z0-9\-]+)\s*[^\w\n]+\s*Assigned\s*[^\w\n]+\s*([0-9.]+)\s*mi"  # grades + distance
    r"\s*\n\s*\n"
    r"\s*(\d{1,2})\s*/\s*10",                                 # rating
    re.IGNORECASE,
)


def parse_schools_inner_text(inner_text: str) -> Dict[str, Any]:
    """Parse the schools section innerText into a structured dict.

    Returns a dict with keys among {elementary, middle, high}. Each value is
    `{"name": str, "rating": int, "distance_mi": float, "grades": str}`.
    Missing roles are omitted (not set to None) so `.get(role, {}).get("rating")`
    reads cleanly in the caller. Also includes an "avg_assigned_rating" float
    (mean of the ratings we found), which is what the scoring uses.

    Returns SchoolsUnknown when no assigned-school block matches — the section
    was present but the extractor could not find a single rated school (e.g.
    empty new-construction plan pages).
    """
    if not inner_text or "Provided by GreatSchools" not in inner_text:
        # The GreatSchools footer is the reliable "the section actually rendered"
        # marker. Without it we'd rather admit we don't know.
        return dict(SchoolsUnknown, reason="schools section absent or unrendered")
    schools: Dict[str, Any] = {}
    ratings: List[int] = []
    for match in SCHOOL_BLOCK_RE.finditer(inner_text):
        name = match.group(1).strip()
        grades = match.group(2).strip()
        try:
            distance = float(match.group(3))
        except ValueError:
            distance = None
        try:
            rating = int(match.group(4))
        except ValueError:
            continue
        if rating < 1 or rating > 10:
            continue
        role = _classify_role(grades)
        if not role:
            continue
        # First occurrence per role wins — Redfin lists assigned schools in
        # elem/middle/high order, so the FIRST elementary block is the
        # assigned one and the "nearby" list (if scraped later) is ignored.
        if role in schools:
            continue
        schools[role] = {
            "name": name,
            "rating": rating,
            "distance_mi": distance,
            "grades": grades,
        }
        ratings.append(rating)
    if not schools:
        return dict(SchoolsUnknown, reason="section rendered but no rated assigned schools found")
    schools["avg_assigned_rating"] = round(sum(ratings) / len(ratings), 2)
    schools["status"] = "ok"
    return schools


def extract_schools(
    browser_bin: str,
    listing_url: str,
    log: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Open a Redfin detail page and return the assigned schools + ratings.

    Returns either a parsed dict from parse_schools_inner_text (status="ok")
    or SchoolsUnknown-with-reason (status="unknown"). Never raises.
    """
    tab_id: Optional[str] = None
    try:
        opened = _browser_call(browser_bin, "open", targetUrl=listing_url, timeout=DETAIL_PAGE_LOAD_TIMEOUT_S)
        tab_id = opened.get("targetId")
        if not tab_id:
            return dict(SchoolsUnknown, reason=f"no targetId opening {listing_url}")
        try:
            _browser_call(
                browser_bin, "wait",
                targetId=tab_id, until="networkidle", timeout=DETAIL_PAGE_LOAD_TIMEOUT_S,
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            # Non-fatal — the page is usually usable even if networkidle times out.
            if log is not None:
                log.debug("networkidle timeout on %s, continuing", listing_url)
        time.sleep(DETAIL_PAGE_SETTLE_SECONDS)
        got = _browser_call(
            browser_bin, "evaluate",
            targetId=tab_id, expression=REDFIN_SCHOOLS_EXTRACTOR_JS, timeout=DETAIL_PAGE_LOAD_TIMEOUT_S,
        )
        raw = got.get("result")
        if not isinstance(raw, str):
            return dict(SchoolsUnknown, reason=f"evaluate returned non-string: {type(raw).__name__}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return dict(SchoolsUnknown, reason=f"could not parse extractor JSON: {e}")
        parsed = parse_schools_inner_text(payload.get("schools_text") or "")
        # Round-trip the property_type field so the caller can double-check
        # a listing that leaked past the URL-side house/townhouse filter.
        parsed["property_type_hint"] = (payload.get("property_type") or "").strip()
        return parsed
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return dict(SchoolsUnknown, reason=f"{type(e).__name__}: {e}")
    finally:
        if tab_id:
            try:
                _browser_call(browser_bin, "close", targetId=tab_id, timeout=CLOSE_TAB_TIMEOUT_S)
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
                # Tab hygiene, never a hard failure.
                pass


def format_schools_for_bullet(schools: Dict[str, Any]) -> str:
    """Short human-readable schools line for the digest bullet.

    Examples:
      "Schools 7/10 · 8/10 · 10/10 avg 8.3 (Birch, Cedar, Maple Hollow)"
      "Schools unknown — could not fetch"
    """
    if not schools or schools.get("status") != "ok":
        reason = (schools or {}).get("reason") or "unknown"
        return f"Schools unknown ({reason})"
    parts = []
    names = []
    for role in ("elementary", "middle", "high"):
        entry = schools.get(role)
        if isinstance(entry, dict):
            parts.append(f"{entry['rating']}/10")
            # Trim the redundant "School" suffix for brevity.
            short_name = re.sub(r"\s+(Elementary|Middle|High)\s+School$", "", entry["name"], flags=re.I)
            names.append(short_name)
    if not parts:
        return "Schools unknown (no assigned schools parsed)"
    avg = schools.get("avg_assigned_rating")
    avg_str = f" avg {avg}" if avg is not None else ""
    return f"Schools {' · '.join(parts)}{avg_str} ({', '.join(names)})"
