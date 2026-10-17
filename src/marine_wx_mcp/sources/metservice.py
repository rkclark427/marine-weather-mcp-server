"""MetService / MetOcean Point Forecast API client (forecast-v2.metoceanapi.com).

Replaces the Windy stub (Windy dropped 2026-10-11: Premium has no API
access and the ~EUR 990/yr Professional licence excludes ECMWF). Free
self-service tier for personal use; key from console.metoceanapi.com.

Key handling: read from the METSERVICE_API_KEY environment variable,
which Claude Desktop sets from the "env" block of this server's entry in
claude_desktop_config.json. Never hard-code it and never log it. Without
a key every call returns a clear "not configured" message - it must never
raise or break tool registration.

Request/response contract (from developer.metservice.com docs, NOT yet
verified against a live response - the build sandbox can't reach the
host; see CLAUDE.md V-14):
  POST {BASE}/point/time, header x-api-key, JSON body
    {"points": [{"lon": .., "lat": ..}], "variables": [...],
     "time": {"from": ISO-Z, "interval": "1h", "repeat": N}, "explain": true}
  Response: "dimensions": {"time": {"data": [ISO...]}, "point": ...},
    "variables": {name: {"units", "data": [...], "noData": [...]}},
    "noDataReasons": {name: code}. data/noData are flat arrays ordered by
    the variable's own "dimensions" list; with one point that is just time.
  Billing: units = requests x variables x points (times are free); the
    x-charge-amount response header reports the cost of each call.
Model choice is automatic per variable (highest-ranked model covering
the point, cycle-locked); "explain": true asks the API to say which
models it used, surfaced as-is.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

BASE_URL = "https://forecast-v2.metoceanapi.com"
API_KEY_ENV = "METSERVICE_API_KEY"
MS_TO_KT = 1.943844
M_TO_FT = 3.28084

NOT_CONFIGURED_MESSAGE = (
    "MetService Point Forecast is not configured (METSERVICE_API_KEY is unset). "
    "Add it to the \"env\" block of the marine_wx entry in claude_desktop_config.json "
    "and fully restart Claude. Meanwhile use marine_get_model_forecast (Open-Meteo)."
)

# Our field name -> MetService variable name (names from the docs' variable list).
VARIABLES: dict[str, str] = {
    "wind_ms": "wind.speed.at-10m",
    "gust_ms": "wind.speed.gust.at-10m",
    "wind_dir_deg": "wind.direction.at-10m",
    "wave_height_m": "wave.height",
    "wave_period_s": "wave.period.peak",
    "wave_dir_deg": "wave.direction.peak",
    "swell_height_m": "wave.height.above-8s",
    "swell_period_s": "wave.period.above-8s.peak",
    "swell_dir_deg": "wave.direction.above-8s.peak",
    "wind_wave_height_m": "wave.height.below-8s",
    "cloud_cover_pct": "cloud.cover",
    "precip_mm_hr": "precipitation.rate",
}


class MetServiceError(Exception):
    """Raised for unrecoverable MetService API failures with an actionable message."""


def is_configured() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


def _headers() -> dict[str, str]:
    return {
        "x-api-key": os.environ.get(API_KEY_ENV, "").strip(),
        "Content-Type": "application/json",
        "accept": "application/json",
    }


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise MetServiceError(
            f"MetService rejected the API key (HTTP {resp.status_code}). Check METSERVICE_API_KEY "
            "in claude_desktop_config.json matches the key in console.metoceanapi.com, then restart Claude."
        )
    if resp.status_code == 429:
        raise MetServiceError(
            "MetService quota or rate limit hit (HTTP 429). Check usage at console.metoceanapi.com; "
            "use marine_get_model_forecast (Open-Meteo) meanwhile."
        )
    if resp.status_code >= 400:
        raise MetServiceError(
            f"MetService request failed (HTTP {resp.status_code}): {resp.text[:300]}. "
            "Use marine_get_model_forecast (Open-Meteo) meanwhile."
        )


def build_request(lat: float, lon: float, hours: int, step_hours: int = 1,
                  start: datetime | None = None) -> dict[str, Any]:
    start = start or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return {
        "points": [{"lon": lon, "lat": lat}],
        "variables": list(VARIABLES.values()),
        "time": {
            "from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval": f"{max(1, step_hours)}h",
            "repeat": max(1, hours // max(1, step_hours)),
        },
        "explain": True,
    }


def _times(payload: dict[str, Any]) -> list[str]:
    tdim = (payload.get("dimensions") or {}).get("time") or {}
    raw = tdim.get("data") or tdim.get("values") or []
    out = []
    for t in raw:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append(dt.astimezone(timezone.utc).isoformat())
    return out


def parse_point_time(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a single-point /point/time response into hourly rows in knots/feet.

    Values flagged in noData become None and the reason is counted, so a
    land-masked or out-of-coverage wave field shows up as "no data", never
    as a zero.
    """
    times = _times(payload)
    variables = payload.get("variables") or {}
    reasons = {v: k for k, v in (payload.get("noDataReasons") or {}).items()}
    series: dict[str, list[Any]] = {}
    missing: dict[str, str] = {}
    for ours, theirs in VARIABLES.items():
        var = variables.get(theirs)
        if not var:
            series[ours] = [None] * len(times)
            missing[ours] = "not returned"
            continue
        data = list(var.get("data") or [])
        flags = list(var.get("noData") or [0] * len(data))
        vals = []
        for i in range(len(times)):
            v = data[i] if i < len(data) else None
            f = flags[i] if i < len(flags) else 0
            if f:
                missing.setdefault(ours, reasons.get(f, str(f)))
                v = None
            vals.append(v)
        series[ours] = vals

    def conv(key: str, factor: float, digits: int) -> list[float | None]:
        return [round(v * factor, digits) if v is not None else None for v in series[key]]

    rows = []
    kt = {k: conv(k, MS_TO_KT, 0) for k in ("wind_ms", "gust_ms")}
    ft = {k: conv(k, M_TO_FT, 1) for k in ("wave_height_m", "swell_height_m", "wind_wave_height_m")}
    for i, ts in enumerate(times):
        rows.append({
            "time": ts,
            "wind_kt": kt["wind_ms"][i],
            "gust_kt": kt["gust_ms"][i],
            "wind_dir_deg": series["wind_dir_deg"][i],
            "wave_height_ft": ft["wave_height_m"][i],
            "wave_period_s": series["wave_period_s"][i],
            "wave_direction_deg": series["wave_dir_deg"][i],
            "swell_height_ft": ft["swell_height_m"][i],
            "swell_period_s": series["swell_period_s"][i],
            "swell_direction_deg": series["swell_dir_deg"][i],
            "wind_wave_height_ft": ft["wind_wave_height_m"][i],
            "cloud_cover_pct": series["cloud_cover_pct"][i],
            "precip_mm_hr": series["precip_mm_hr"][i],
        })
    return {"rows": rows, "missing": missing, "explanation": payload.get("explanation")}


async def get_point_forecast(lat: float, lon: float, hours: int = 48, step_hours: int = 1) -> dict[str, Any]:
    """Point forecast at one location. {"configured": False, "message"} if no key."""
    if not is_configured():
        return {"configured": False, "message": NOT_CONFIGURED_MESSAGE}
    body = build_request(lat, lon, hours, step_hours)
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(f"{BASE_URL}/point/time", json=body, headers=_headers())
        except httpx.HTTPError as exc:
            raise MetServiceError(
                f"Could not reach MetService ({exc!r}). Use marine_get_model_forecast (Open-Meteo) meanwhile."
            ) from exc
    _raise_for(resp)
    result = parse_point_time(resp.json())
    result["configured"] = True
    result["units_charged"] = resp.headers.get("x-charge-amount")
    return result


async def get_models() -> dict[str, Any]:
    """GET /models/ - what this key can see (also a cheap key check)."""
    if not is_configured():
        return {"configured": False, "message": NOT_CONFIGURED_MESSAGE}
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(f"{BASE_URL}/models/", headers=_headers())
        except httpx.HTTPError as exc:
            raise MetServiceError(f"Could not reach MetService ({exc!r}).") from exc
    _raise_for(resp)
    return {"configured": True, "models": resp.json(), "units_charged": resp.headers.get("x-charge-amount")}
