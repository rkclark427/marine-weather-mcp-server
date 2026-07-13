"""NOAA CO-OPS (Center for Operational Oceanographic Products and Services)
tides, water levels, and current predictions - nationwide, no API key.

Build-time verification (2026-07-13):
- Three DISJOINT station lists, not one list with capability flags:
  "tidepredictions" (3498 stations, astronomical tide highs/lows),
  "waterlevels" (301 stations, observed/real-time water level - this is
  the one Great Lakes stations use), "currentpredictions" (4430
  stations, ebb/flood/slack). Confirmed live Lake Michigan has ZERO
  tidepredictions/currentpredictions stations (no astronomical tide on
  the Great Lakes) but does have waterlevels stations (e.g. Milwaukee,
  Calumet Harbor) - don't assume one product type covers a region just
  because another does.
- Station metadata carries a `greatlakes` boolean - use it to pick the
  right datum: "IGLD" (International Great Lakes Datum) for Great Lakes
  stations, "MLLW" (Mean Lower Low Water) for tidal coastal stations.
  Requesting `product=predictions` (tide predictions) against a Great
  Lakes station returns a clean {"error": {"message": "Great Lakes
  stations don't have Predictions data."}} - not a crash, not empty
  data presented as zero tide - surfaced as an actionable CoopsError.
- The datagetter endpoint wants `date=today` (or explicit YYYYMMDD), not
  `begin_date=today` - the latter silently 400s with an HTML error page
  instead of JSON. For a multi-day *range*, `date=today&range=<hours>`
  does NOT reliably extend past the current day for `interval=hilo`
  (confirmed live: range=48 returned only today's 4 events) - use
  explicit `begin_date=YYYYMMDD&end_date=YYYYMMDD` instead, which does
  give the full multi-day window.
- Error responses (bad station, bad datum, no data) all share the same
  {"error": {"message": ...}} JSON shape regardless of cause - checked
  for uniformly rather than trying to distinguish by content.
- Station lists are large (station metadata bulk fetch ~0.7-3.8MB per
  type) but change rarely - cached to disk with a long TTL (24h),
  matching the navcen.py pattern, not fetched live per tool call.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from marine_wx_mcp.sources._geo import haversine_nm

MDAPI_BASE = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
DATAGETTER_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
CACHE_DIR = Path.home() / ".marine_wx_mcp_cache" / "coops"
CACHE_TTL_SECONDS = 24 * 3600

STATION_TYPES = ("tidepredictions", "waterlevels", "currentpredictions")
MAX_STATION_DISTANCE_NM = 50


class CoopsError(Exception):
    """Raised for unrecoverable CO-OPS failures with an actionable message."""


async def _get_json(url: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise CoopsError(f"CO-OPS request failed ({exc!r}). Try again shortly.") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise CoopsError(
            f"CO-OPS returned a non-JSON response for {url} - likely a malformed "
            "request (e.g. wrong date format)."
        ) from exc
    if isinstance(data, dict) and "error" in data:
        raise CoopsError(f"CO-OPS error: {data['error'].get('message', data['error'])}")
    return data


def _cache_path(station_type: str) -> Path:
    return CACHE_DIR / f"stations_{station_type}.json"


async def get_stations(station_type: str) -> list[dict[str, Any]]:
    """All stations of one type (tidepredictions/waterlevels/currentpredictions), cached."""
    if station_type not in STATION_TYPES:
        raise CoopsError(f"Unknown station type '{station_type}'. Valid: {STATION_TYPES}")

    path = _cache_path(station_type)
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    data = await _get_json(f"{MDAPI_BASE}/stations.json", params={"type": station_type})
    stations = data.get("stations", [])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stations, f)
    return stations


def find_nearest_station(
    stations: list[dict[str, Any]], lat: float, lon: float
) -> dict[str, Any] | None:
    """The single closest station in a list, with `_distance_nm` added. None if list is empty."""
    if not stations:
        return None
    best = min(stations, key=lambda s: haversine_nm(lat, lon, s["lat"], s["lng"]))
    distance = haversine_nm(lat, lon, best["lat"], best["lng"])
    return {**best, "_distance_nm": round(distance, 1)}


def _date_range(days: int) -> tuple[str, str]:
    today = date.today()
    return today.strftime("%Y%m%d"), (today + timedelta(days=days)).strftime("%Y%m%d")


async def get_tide_predictions(station_id: str, days: int = 2) -> list[dict[str, Any]]:
    """High/low tide predictions for a tidal station. Raises CoopsError for Great Lakes stations."""
    begin, end = _date_range(days)
    data = await _get_json(
        DATAGETTER_URL,
        params={
            "product": "predictions",
            "datum": "MLLW",
            "station": station_id,
            "time_zone": "lst_ldt",
            "units": "english",
            "interval": "hilo",
            "format": "json",
            "begin_date": begin,
            "end_date": end,
        },
    )
    return data.get("predictions", [])


async def get_water_level(station_id: str, greatlakes: bool) -> dict[str, Any] | None:
    """Latest observed water level for a station (tidal or Great Lakes)."""
    data = await _get_json(
        DATAGETTER_URL,
        params={
            "product": "water_level",
            "datum": "IGLD" if greatlakes else "MLLW",
            "station": station_id,
            "time_zone": "lst_ldt",
            "units": "english",
            "format": "json",
            "date": "latest",
        },
    )
    readings = data.get("data", [])
    return readings[0] if readings else None


async def get_current_predictions(station_id: str, days: int = 1) -> list[dict[str, Any]]:
    """Ebb/flood/slack current predictions for a current station."""
    begin, end = _date_range(days)
    data = await _get_json(
        DATAGETTER_URL,
        params={
            "product": "currents_predictions",
            "station": station_id,
            "time_zone": "lst_ldt",
            "units": "english",
            "format": "json",
            "begin_date": begin,
            "end_date": end,
            "interval": "MAX_SLACK",
        },
    )
    return data.get("current_predictions", {}).get("cp", [])
