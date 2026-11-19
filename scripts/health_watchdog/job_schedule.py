"""Expected cadence of a launchd job, derived from its plist schedule keys.

Python 3.9 compatible (see config.py). Pure: plist dict in, seconds out.

The cadence is the LONGEST gap between two consecutive scheduled fires, not the
average: a Mon/Wed/Fri job has a 3-day gap over the weekend, and judging it by
its 2.3-day average would flag every Monday morning.

- `StartInterval`: the interval itself.
- `StartCalendarInterval` (dict or list of dicts): every fire minute in a week is
  enumerated (a missing Weekday/Hour/Minute key means "every"), and the largest
  circular gap wins. Entries with `Day` count as monthly (31 d), `Month` as yearly (366 d).
- KeepAlive services and RunAtLoad-only plists have no cadence (None).
"""

from typing import Any, Dict, List, Optional, Set

MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
MONTHLY_CADENCE_SECONDS = 31 * 24 * 3600
YEARLY_CADENCE_SECONDS = 366 * 24 * 3600
# launchd accepts both 0 and 7 for Sunday.
LAUNCHD_SUNDAY_ALIAS = 7


def calendar_entries(plist: Dict[str, Any]) -> List[Dict[str, Any]]:
    """StartCalendarInterval normalized to a list of dicts ([] when absent or malformed)."""
    calendar = plist.get("StartCalendarInterval")
    if isinstance(calendar, dict):
        return [calendar]
    if isinstance(calendar, list):
        return [entry for entry in calendar if isinstance(entry, dict)]
    return []


def integer_key(entry: Dict[str, Any], key: str) -> Optional[int]:
    """The entry's integer value for `key`, or None when absent (bools are not ints here)."""
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def weekly_fire_minutes(entries: List[Dict[str, Any]]) -> Set[int]:
    """Every minute-of-week (0 = Sunday 00:00) at which one of the entries fires."""
    fire_minutes = set()  # type: Set[int]
    for entry in entries:
        weekday = integer_key(entry, "Weekday")
        hour = integer_key(entry, "Hour")
        minute = integer_key(entry, "Minute")
        weekdays = range(7) if weekday is None else [weekday % LAUNCHD_SUNDAY_ALIAS]
        hours = range(24) if hour is None else [hour]
        minutes = range(MINUTES_PER_HOUR) if minute is None else [minute]
        for day in weekdays:
            for hour_of_day in hours:
                for minute_of_hour in minutes:
                    fire_minutes.add(day * MINUTES_PER_DAY + hour_of_day * MINUTES_PER_HOUR + minute_of_hour)
    return fire_minutes


def longest_circular_gap_minutes(fire_minutes: Set[int]) -> int:
    """Largest gap between consecutive fires, wrapping Saturday night into Sunday."""
    ordered = sorted(fire_minutes)
    gaps = [later - earlier for earlier, later in zip(ordered, ordered[1:])]
    gaps.append(ordered[0] + MINUTES_PER_WEEK - ordered[-1])
    return max(gaps)


def expected_cadence_seconds(plist: Dict[str, Any]) -> Optional[int]:
    """Longest expected gap between runs in seconds, or None for unscheduled/KeepAlive plists."""
    if plist.get("KeepAlive"):
        return None
    interval = plist.get("StartInterval")
    if isinstance(interval, int) and not isinstance(interval, bool) and interval > 0:
        return interval
    entries = calendar_entries(plist)
    if not entries:
        return None
    if any(integer_key(entry, "Month") is not None for entry in entries):
        return YEARLY_CADENCE_SECONDS
    if any(integer_key(entry, "Day") is not None for entry in entries):
        return MONTHLY_CADENCE_SECONDS
    return longest_circular_gap_minutes(weekly_fire_minutes(entries)) * 60
