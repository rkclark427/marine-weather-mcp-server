"""NDBC realtime2 observation fetcher + fixed-width-ish text parser.

Gotchas discovered during build-time verification (2026-07-06):
- The realtime2 URL is case-sensitive: https://www.ndbc.noaa.gov/data/
  realtime2/knsw3.txt 404s while .../KNSW3.txt works. Station IDs must
  always be uppercased before building the URL.
- Not every station registered with NDBC actually reports. KNSW3
  (Kenosha Light) is a real, currently-registered station but is
  flagged met=n in NDBC's own station metadata and has no realtime2
  feed at all - a 404 here does not necessarily mean a typo.
- Despite the docs suggesting some (e.g. land) stations omit columns,
  every station observed during verification used the same 19-column
  layout, with "MM" filling in unavailable fields. The parser reads
  column names from the file's own header line rather than assuming
  a fixed layout, so it tolerates either case.
- Rows are newest-first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://www.ndbc.noaa.gov/data/realtime2"
LOCAL_TZ = ZoneInfo("America/Chicago")

MS_TO_KNOTS = 1.9438452
M_TO_FEET = 3.2808399


class NDBCError(Exception):
    """Raised for unrecoverable NDBC failures with an actionable message."""


def _c_to_f(value: float | None) -> float | None:
    return None if value is None else value * 9 / 5 + 32


def _parse_value(raw: str) -> float | None:
    if raw == "MM":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_ndbc_text(text: str) -> list[dict[str, Any]]:
    """Parse a realtime2 text file into a list of observation dicts.

    Each observation has UTC and America/Chicago local timestamps plus
    wind (knots), waves (feet), pressure (hPa), and temps (degF).
    Missing ("MM") fields become None rather than raising.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return []

    columns = lines[0].lstrip("#").split()
    rows = lines[2:]  # lines[1] is the units header

    observations: list[dict[str, Any]] = []
    for row in rows:
        fields = row.split()
        if len(fields) < len(columns):
            continue
        raw = dict(zip(columns, fields))

        try:
            dt_utc = datetime(
                int(raw["YY"]),
                int(raw["MM"]),
                int(raw["DD"]),
                int(raw["hh"]),
                int(raw["mm"]),
                tzinfo=timezone.utc,
            )
        except (KeyError, ValueError):
            continue

        wspd = _parse_value(raw.get("WSPD", "MM"))
        gst = _parse_value(raw.get("GST", "MM"))
        wvht = _parse_value(raw.get("WVHT", "MM"))
        pres = _parse_value(raw.get("PRES", "MM"))
        ptdy = _parse_value(raw.get("PTDY", "MM"))
        atmp = _parse_value(raw.get("ATMP", "MM"))
        wtmp = _parse_value(raw.get("WTMP", "MM"))
        dewp = _parse_value(raw.get("DEWP", "MM"))
        tide = _parse_value(raw.get("TIDE", "MM"))

        observations.append(
            {
                "time_utc": dt_utc.isoformat(),
                "time_local": dt_utc.astimezone(LOCAL_TZ).isoformat(),
                "wind_dir_deg": _parse_value(raw.get("WDIR", "MM")),
                "wind_speed_kt": None if wspd is None else round(wspd * MS_TO_KNOTS, 1),
                "wind_gust_kt": None if gst is None else round(gst * MS_TO_KNOTS, 1),
                "wave_height_ft": None if wvht is None else round(wvht * M_TO_FEET, 1),
                "dominant_wave_period_s": _parse_value(raw.get("DPD", "MM")),
                "avg_wave_period_s": _parse_value(raw.get("APD", "MM")),
                "wave_dir_deg": _parse_value(raw.get("MWD", "MM")),
                "pressure_hpa": pres,
                "pressure_tendency_hpa": ptdy,
                "air_temp_f": None if atmp is None else round(_c_to_f(atmp), 1),
                "water_temp_f": None if wtmp is None else round(_c_to_f(wtmp), 1),
                "dewpoint_f": None if dewp is None else round(_c_to_f(dewp), 1),
                "visibility_nmi": _parse_value(raw.get("VIS", "MM")),
                "tide_ft": tide,
            }
        )
    return observations


async def get_buoy_obs(station_id: str, hours: int = 3) -> list[dict[str, Any]]:
    """Fetch and parse the latest observations for a station, most recent first.

    Filters to observations within `hours` of the most recent reading
    (not wall-clock time) so a slightly stale feed doesn't come back empty.
    """
    station_upper = station_id.upper()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"{BASE_URL}/{station_upper}.txt")
        except httpx.HTTPError as exc:
            raise NDBCError(
                f"NDBC request for station {station_upper} failed ({exc!r}). "
                "Try an NWS gridpoint forecast or a nearby station instead."
            ) from exc

    if resp.status_code == 404:
        raise NDBCError(
            f"NDBC station {station_upper} returned no data (404). It may not "
            "report meteorological observations at all (check marine_list_registry "
            "notes) or may be temporarily/seasonally offline. Try a nearby station "
            "or marine_get_point_forecast instead."
        )
    resp.raise_for_status()

    observations = parse_ndbc_text(resp.text)
    if not observations:
        return []

    latest = datetime.fromisoformat(observations[0]["time_utc"])
    cutoff = latest.timestamp() - hours * 3600
    return [
        obs
        for obs in observations
        if datetime.fromisoformat(obs["time_utc"]).timestamp() >= cutoff
    ]
