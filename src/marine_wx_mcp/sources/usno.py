"""US Naval Observatory rstt/oneday API client - sunrise/sunset/civil twilight.

No API key required. Build-time verification (2026-07-06):
- The `tz` query param is the STANDARD (non-DST) UTC offset; `dst=true`
  adds the daylight-saving hour on top of it. Passing the already-DST
  offset (e.g. tz=-5 for Chicago in summer) with dst=true silently
  computes an extra hour off (UTC-4, wrong) - confirmed by comparing
  against the tz-less (UTC) response for the same date. Always pass the
  zone's STANDARD offset and let dst=true/false do the shifting; see
  _std_offset_and_dst below, which derives both from zoneinfo per date
  rather than hardcoding an offset.
- sundata only carries civil twilight (Begin/End Civil Twilight, Rise,
  Upper Transit, Set) - no nautical or astronomical twilight. Fine for
  harbor-approach/passage-planning visibility, not for celestial nav
  sight timing.
- Response times are "HH:MM  DT"/"HH:MM  ST" strings (note double
  space before the zone label), not ISO timestamps - must be combined
  with the requested date and zone manually.
- Missing/invalid coords returns 400 with a JSON {"error": ...} body;
  a malformed date returns a 500 HTML error page (not JSON) - both are
  surfaced as USNOError rather than crashing.
"""

from __future__ import annotations

import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://aa.usno.navy.mil/api/rstt/oneday"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})")


class USNOError(Exception):
    """Raised for unrecoverable USNO API failures with an actionable message."""


def _std_offset_and_dst(when: date_cls, tz_name: str) -> tuple[float, bool]:
    """Standard (non-DST) UTC offset in hours, and whether DST applies on `when`."""
    tz = ZoneInfo(tz_name)
    sample = datetime(when.year, when.month, when.day, 12, tzinfo=tz)
    utc_offset = sample.utcoffset() or timedelta(0)
    dst_offset = sample.dst() or timedelta(0)
    std_hours = (utc_offset - dst_offset).total_seconds() / 3600
    return std_hours, dst_offset != timedelta(0)


def _parse_time_events(
    entries: list[dict[str, Any]], year: int, month: int, day: int, tz_name: str
) -> dict[str, str]:
    events: dict[str, str] = {}
    for entry in entries:
        match = _TIME_RE.match(entry["time"])
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        local_dt = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz_name))
        events[entry["phen"]] = local_dt.isoformat()
    return events


async def get_sun_times(
    lat: float, lon: float, when: date_cls, tz_name: str = "America/Chicago"
) -> dict[str, Any]:
    """Sunrise/transit/sunset, civil twilight, and moon data for one day.

    Returns {"date", "tz_name", "events": {phen: iso_local_datetime},
    "moon_events": {phen: iso_local_datetime}, "moon_phase": {...}}.
    moon_events may have 0-2 entries some days (moonrise/set don't occur
    every calendar day, since the moon rises ~50min later each day) -
    that's real, not a parsing failure.
    """
    std_offset, is_dst = _std_offset_and_dst(when, tz_name)
    params = {
        "date": when.isoformat(),
        "coords": f"{lat},{lon}",
        "tz": std_offset,
        "dst": "true" if is_dst else "false",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(BASE_URL, params=params)
        except httpx.HTTPError as exc:
            raise USNOError(
                f"USNO sun/twilight request failed ({exc!r}). Try again shortly."
            ) from exc

    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", resp.text[:200])
        except ValueError:
            detail = resp.text[:200]
        raise USNOError(
            f"USNO API returned {resp.status_code} for {when.isoformat()} at "
            f"{lat},{lon}: {detail}"
        )

    data = resp.json()["properties"]["data"]
    events = _parse_time_events(data.get("sundata", []), when.year, when.month, when.day, tz_name)
    moon_events = _parse_time_events(
        data.get("moondata", []), when.year, when.month, when.day, tz_name
    )

    closest_phase = None
    cp = data.get("closestphase")
    if cp:
        match = _TIME_RE.match(cp["time"])
        cp_time = None
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            cp_time = datetime(
                cp["year"], cp["month"], cp["day"], hour, minute, tzinfo=ZoneInfo(tz_name)
            ).isoformat()
        closest_phase = {"phase": cp["phase"], "time": cp_time}

    moon_phase = {
        "current_phase": data.get("curphase"),
        "fraction_illuminated": data.get("fracillum"),
        "closest_phase": closest_phase,
    }

    return {
        "date": when.isoformat(),
        "tz_name": tz_name,
        "events": events,
        "moon_events": moon_events,
        "moon_phase": moon_phase,
    }
