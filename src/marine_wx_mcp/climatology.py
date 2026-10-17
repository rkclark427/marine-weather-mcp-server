"""Historical "what is this week usually like" analysis for a place and date window.

Pulls the same calendar window (e.g. Jan 9-16) for each of the last N
complete years from Open-Meteo's archive (ERA5 reanalysis wind/gust/
direction/cloud) and marine history (waves/swell), restricts each day to
daylight sailing hours, and scores every day against the region's
green/yellow/red thresholds (scoring.py).

Answers planning questions months ahead of any real forecast ("how often
is the Barbuda channel inside our limits in mid-January?"), not "what
will happen this year".

Caveats surfaced to the user in the formatted output:
- ERA5 is a ~25 km reanalysis: it smooths squalls and local acceleration
  around headlands, and its gusts are modelled, not measured. Treat the
  result as the typical background trade-wind regime, not peak gusts.
- Historical waves come from Open-Meteo's marine archive; which wave
  model actually answered is reported per year (see
  openmeteo.get_marine_history).
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marine_wx_mcp.scoring import COLORS, score_rows
from marine_wx_mcp.sources import openmeteo
from marine_wx_mcp.sources.openmeteo import OpenMeteoError

ERA5_LAG_DAYS = 7  # archive data trails real time by ~5 days; stay clear of it
MAX_YEARS = 30


class ClimatologyError(Exception):
    """Raised for bad inputs (dates) with an actionable message."""


def parse_mmdd(text: str) -> tuple[int, int]:
    try:
        month, day = (int(p) for p in text.strip().split("-"))
        date(2001, month, day)  # validates (non-leap year; Feb 29 rejected)
    except (ValueError, TypeError) as exc:
        raise ClimatologyError(
            f"Invalid month-day '{text}' - use MM-DD, e.g. '01-09'."
        ) from exc
    return month, day


def year_windows(
    start_mmdd: str, end_mmdd: str, years: int, today: date | None = None
) -> list[tuple[date, date]]:
    """The last `years` complete occurrences of the window, oldest first.

    A window that wraps the new year (e.g. 12-28 to 01-04) ends in the
    following calendar year.
    """
    if not 1 <= years <= MAX_YEARS:
        raise ClimatologyError(f"years must be between 1 and {MAX_YEARS}.")
    (sm, sd), (em, ed) = parse_mmdd(start_mmdd), parse_mmdd(end_mmdd)
    wraps = (em, ed) < (sm, sd)
    today = today or date.today()
    cutoff = today - timedelta(days=ERA5_LAG_DAYS)

    windows: list[tuple[date, date]] = []
    year = today.year
    while len(windows) < years and year > 1940:
        start = date(year, sm, sd)
        end = date(year + 1 if wraps else year, em, ed)
        if end <= cutoff:
            windows.append((start, end))
        year -= 1
    return list(reversed(windows))


def _rows(weather: dict[str, Any] | None, marine: dict[str, Any] | None) -> list[dict[str, Any]]:
    by_ts: dict[str, dict[str, Any]] = {}
    if weather:
        for i, ts in enumerate(weather.get("timestamps", [])):
            def _w(key, _i=i):
                vals = weather.get(key) or []
                return vals[_i] if _i < len(vals) else None
            by_ts[ts] = {
                "time": ts,
                "wind_kt": _w("wind_speed_10m"),
                "gust_kt": _w("wind_gusts_10m"),
                "wind_dir_deg": _w("wind_direction_10m"),
                "cloud_cover_pct": _w("cloud_cover"),
            }
    if marine and not marine.get("no_data"):
        for i, ts in enumerate(marine.get("timestamps", [])):
            def _m(key, _i=i):
                vals = marine.get(key) or []
                return vals[_i] if _i < len(vals) else None
            row = by_ts.setdefault(ts, {"time": ts})
            row["wave_height_ft"] = _m("wave_height_ft")
            row["swell_height_ft"] = _m("swell_wave_height_ft")
            row["swell_direction_deg"] = _m("swell_wave_direction")
            row["swell_period_s"] = _m("swell_wave_period")
    return [by_ts[k] for k in sorted(by_ts)]


def score_days(
    rows: list[dict[str, Any]],
    tz_name: str,
    thresholds: dict[str, Any] | None,
    day_start_hour: int,
    day_end_hour: int,
) -> list[dict[str, Any]]:
    """Group hourly rows into local days, keep daylight sailing hours, score each day."""
    zone = ZoneInfo(tz_name)
    days: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        local = datetime.fromisoformat(row["time"]).astimezone(zone)
        if day_start_hour <= local.hour <= day_end_hour:
            days.setdefault(local.date(), []).append(row)

    scored = []
    for day, day_rows in sorted(days.items()):
        score = score_rows(day_rows, thresholds)
        def _mx(key, _r=day_rows):
            vals = [r.get(key) for r in _r if r.get(key) is not None]
            return max(vals) if vals else None
        scored.append(
            {
                "date": day.isoformat(),
                "categories": {k: c["color"] for k, c in score["categories"].items()},
                "advisories": score["advisories"],
                "max_wind_kt": _mx("wind_kt"),
                "max_gust_kt": _mx("gust_kt"),
                "max_seas_ft": _mx("wave_height_ft"),
                "max_swell_ft": _mx("swell_height_ft"),
                "max_cloud_pct": _mx("cloud_cover_pct"),
            }
        )
    return scored


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def summarize(days: list[dict[str, Any]]) -> dict[str, Any]:
    cat_keys: list[str] = []
    for d in days:
        for k in d["categories"]:
            if k not in cat_keys:
                cat_keys.append(k)
    counts = {}
    for k in cat_keys:
        counts[k] = {c: sum(1 for d in days if d["categories"].get(k) == c) for c in COLORS}
        counts[k]["no_data"] = sum(1 for d in days if d["categories"].get(k) is None)
    dist = {}
    for key in ("max_wind_kt", "max_gust_kt", "max_seas_ft", "max_swell_ft", "max_cloud_pct"):
        vals = [d[key] for d in days if d[key] is not None]
        dist[key] = {
            "median": round(statistics.median(vals), 1) if vals else None,
            "p90": _pct(vals, 0.9),
            "max": round(max(vals), 1) if vals else None,
            "n": len(vals),
        }
    return {
        "days": len(days),
        "counts": counts,
        "daily_max_distribution": dist,
        "advisory_days": sum(1 for d in days if d["advisories"]),
    }


YEAR_CONCURRENCY = 2


async def get_climatology(
    lat: float,
    lon: float,
    tz_name: str,
    start_mmdd: str,
    end_mmdd: str,
    years: int,
    thresholds: dict[str, Any] | None,
    day_start_hour: int = 7,
    day_end_hour: int = 17,
) -> dict[str, Any]:
    windows = year_windows(start_mmdd, end_mmdd, years)
    if not windows:
        raise ClimatologyError("No complete historical windows available yet for those dates.")

    # Two years at a time: firing all years at once tripped Open-Meteo's
    # free-tier rate limit (429) live on 2026-10-11, losing wind for half
    # the years. openmeteo._get also retries 429s with backoff.
    gate = asyncio.Semaphore(YEAR_CONCURRENCY)

    async def _fetch(start: date, end: date):
        # Pad a day each side so local-time days at the edges are complete.
        s, e = (start - timedelta(days=1)).isoformat(), (end + timedelta(days=1)).isoformat()
        async with gate:
            weather, marine = await asyncio.gather(
                openmeteo.get_archive_weather(lat, lon, s, e),
                openmeteo.get_marine_history(lat, lon, s, e),
                return_exceptions=True,
            )
        return weather, marine

    results = await asyncio.gather(*(_fetch(s, e) for s, e in windows))

    per_year = []
    all_days: list[dict[str, Any]] = []
    for (start, end), (weather, marine) in zip(windows, results):
        entry: dict[str, Any] = {"start": start.isoformat(), "end": end.isoformat()}
        if isinstance(weather, Exception):
            entry["weather_error"] = str(weather)
            weather = None
        if isinstance(marine, Exception):
            entry["marine_error"] = str(marine)
            marine = None
        entry["wave_source"] = (marine or {}).get("source") if marine else None
        days = [
            d
            for d in score_days(_rows(weather, marine), tz_name, thresholds, day_start_hour, day_end_hour)
            if start.isoformat() <= d["date"] <= end.isoformat()
        ]
        entry["days"] = days
        entry["summary"] = summarize(days)
        per_year.append(entry)
        all_days.extend(days)

    if all(isinstance(w, Exception) for w, _ in results):
        first = next(w for w, _ in results)
        raise OpenMeteoError(f"Every historical request failed; first error: {first}")

    return {
        "lat": lat,
        "lon": lon,
        "timezone": tz_name,
        "window": f"{start_mmdd} to {end_mmdd}",
        "daylight_hours": [day_start_hour, day_end_hour],
        "years": [w[0].year for w in windows],
        "per_year": per_year,
        "overall": summarize(all_days),
        "thresholds_status": (thresholds or {}).get("status"),
    }
