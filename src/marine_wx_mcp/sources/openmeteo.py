"""Open-Meteo forecast, marine, and historical clients (free, no API key).

Build-time verification (2026-07-06):
- Multi-model requests suffix each hourly variable with the model name
  (e.g. "wind_speed_10m_ecmwf_ifs025", "wind_speed_10m_gfs_seamless"),
  sharing one "time" array - not separate per-model response blocks.
- The marine endpoint DOES return real, non-null Lake Michigan wave
  data (tested at 42.6,-87.7: all 168 hourly values populated,
  magnitudes consistent with the NWS nearshore forecast text). The
  spec's anticipated "no wave data for this location" fallback is
  kept as a defensive check but is not expected to trigger here.

Changes 2026-10-11 (multi-region / Antigua):
- All requests now use timezone=GMT and every timestamp returned by this
  module is an explicit UTC ISO string ("...+00:00"). Previously the
  client asked for America/Chicago and handed back NAIVE local strings,
  which formatting.to_local then interpreted in the *machine's* local
  zone - correct only by coincidence on Kent's Chicago-time PC, and
  wrong for any other region. Open-Meteo also documents that its
  `timezone` param ignores DST changes inside the requested range, so
  converting ourselves from UTC with zoneinfo is the robust option.
- Marine wave heights are METERS (hourly_units says "m"). The old
  markdown rendering labeled them "ft" - fixed in formatting; this
  module also returns *_ft companions so nothing downstream has to
  remember to convert.
- Marine variables now include the swell / wind-wave partition
  (swell_wave_*, wind_wave_*, secondary_swell_wave_*) - the combined
  wave_height can't distinguish a long-period north swell from local
  trade-wind chop, which is the question that matters for exposed
  anchorages. Secondary swell is only produced by some wave models
  (per Open-Meteo docs); if a request including it is rejected, the
  client retries without it rather than failing the whole call.
- Marine requests pass cell_selection=sea so near-shore harbor
  coordinates resolve to a water grid cell (a land cell has no waves).
- Historical: archive-api.open-meteo.com/v1/archive (ERA5 reanalysis,
  1940-present, 0.25 deg) for wind/gust/cloud, and the marine endpoint
  with start_date/end_date for historical waves. NOT yet live-verified
  for the marine-history path at time of writing (no network in the
  build sandbox) - see get_marine_history for the defensive fallback.
"""

from __future__ import annotations

from typing import Any

import asyncio

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

M_TO_FT = 3.28084

DEFAULT_MODELS = ["ecmwf_ifs025", "gfs_seamless"]
DEFAULT_FORECAST_VARS = [
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "cloud_cover",
    "precipitation",
    "precipitation_probability",
    "cape",
    "temperature_2m",
]
MARINE_VARS = [
    "wave_height",
    "wave_direction",
    "wave_period",
    "wind_wave_height",
    "wind_wave_direction",
    "wind_wave_period",
    "swell_wave_height",
    "swell_wave_direction",
    "swell_wave_period",
]
OPTIONAL_MARINE_VARS = [
    "secondary_swell_wave_height",
    "secondary_swell_wave_direction",
    "secondary_swell_wave_period",
]
HEIGHT_VARS = {
    "wave_height",
    "wind_wave_height",
    "swell_wave_height",
    "secondary_swell_wave_height",
}
ARCHIVE_VARS = ["wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "cloud_cover"]
HISTORY_MARINE_VARS = [
    "wave_height",
    "swell_wave_height",
    "swell_wave_direction",
    "swell_wave_period",
]
# Fallback model id for historical waves if "best match" returns nothing for
# older years. Open-Meteo's docs list "ERA5-Ocean (1940 to present)" by
# display name only; this identifier is a best guess and is tried defensively.
HISTORY_MARINE_FALLBACK_MODEL = "era5_ocean"


class OpenMeteoError(Exception):
    """Raised for unrecoverable Open-Meteo failures with an actionable message."""


RATE_LIMIT_RETRIES = 3          # 429 retries per request
RATE_LIMIT_BACKOFF_S = (2, 5, 10)


async def _get(url: str, params: dict) -> dict:
    """GET with retry on 429 (Open-Meteo's free tier rate-limits bursts).

    Confirmed live 2026-10-11: a 10-year climatology fired ~20 archive and
    marine calls at once and several came back 429. Honors Retry-After
    (capped at 15 s), otherwise backs off 2/5/10 s.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                resp = await client.get(url, params=params)
                if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
                    break
                try:
                    wait = min(float(resp.headers.get("Retry-After", "")), 15.0)
                except ValueError:
                    wait = RATE_LIMIT_BACKOFF_S[attempt]
                await asyncio.sleep(wait)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OpenMeteoError(
                f"Open-Meteo request failed ({exc!r}). Try marine_get_point_forecast "
                "(NWS gridpoint, US waters only) or marine_get_buoy_obs as a fallback."
            ) from exc
        return resp.json()


def _utc_iso(ts: str) -> str:
    """Open-Meteo GMT timestamps are naive ("2026-10-11T12:00") - make them explicit UTC."""
    if ts.endswith("Z"):
        return ts[:-1] + "+00:00"
    tail = ts[10:]
    if "+" in tail or "-" in tail:
        return ts
    return ts + "+00:00"


def _split_model_suffix(var_key: str, base_vars: list[str], models: list[str]) -> tuple[str, str] | None:
    for base in base_vars:
        for model in models:
            if var_key == f"{base}_{model}":
                return base, model
    return None


def _all_null(values: list | None) -> bool:
    return not values or all(v is None for v in values)


def _to_ft(values: list) -> list:
    return [round(v * M_TO_FT, 1) if v is not None else None for v in values]


async def get_model_forecast(
    lat: float,
    lon: float,
    models: list[str] | None = None,
    hours: int = 72,
) -> dict[str, Any]:
    """Multi-model wind/weather comparison, aligned by timestamp.

    Returns {"timestamps": [utc iso...], "units": {var: unit},
    "models": {model: {var: [values]}}}.
    """
    models = models or DEFAULT_MODELS
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(DEFAULT_FORECAST_VARS),
        "models": ",".join(models),
        "wind_speed_unit": "kn",
        "timezone": "GMT",
        "forecast_hours": hours,
    }
    data = await _get(FORECAST_URL, params)
    hourly = data.get("hourly", {})
    units = data.get("hourly_units", {})

    timestamps = [_utc_iso(t) for t in hourly.get("time", [])]
    result: dict[str, Any] = {"timestamps": timestamps, "units": {}, "models": {m: {} for m in models}}

    for key, values in hourly.items():
        if key == "time":
            continue
        split = _split_model_suffix(key, DEFAULT_FORECAST_VARS, models)
        if split is None:
            # Single-model requests come back unsuffixed.
            if len(models) == 1 and key in DEFAULT_FORECAST_VARS:
                result["models"][models[0]][key] = values
                result["units"][key] = units.get(key)
            continue
        base, model = split
        result["models"][model][base] = values
        result["units"][base] = units.get(key)

    return result


def _marine_result(hourly: dict, units: dict) -> dict[str, Any]:
    timestamps = [_utc_iso(t) for t in hourly.get("time", [])]
    result: dict[str, Any] = {"timestamps": timestamps, "units": {}}
    for var in MARINE_VARS + OPTIONAL_MARINE_VARS:
        values = hourly.get(var)
        if values is None:
            continue
        result[var] = values
        result["units"][var] = units.get(var)
        if var in HEIGHT_VARS:
            result[f"{var}_ft"] = _to_ft(values)

    no_data = _all_null(result.get("wave_height"))
    result["no_data"] = no_data
    result["no_data_message"] = (
        "No wave data available for this location from Open-Meteo marine; "
        "use marine_get_point_forecast (NWS gridpoint waveHeight, US waters) or "
        "marine_get_buoy_obs (NDBC buoy) instead."
        if no_data
        else None
    )
    return result


async def get_marine_forecast(lat: float, lon: float, hours: int = 48) -> dict[str, Any]:
    """Hourly combined waves plus the wind-wave / swell partition.

    Heights come back in meters with *_ft companions added. Flags
    no_data honestly if wave_height is all-null.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(MARINE_VARS + OPTIONAL_MARINE_VARS),
        "timezone": "GMT",
        "forecast_hours": hours,
        "cell_selection": "sea",
    }
    try:
        data = await _get(MARINE_URL, params)
    except OpenMeteoError:
        params["hourly"] = ",".join(MARINE_VARS)
        data = await _get(MARINE_URL, params)
    return _marine_result(data.get("hourly", {}), data.get("hourly_units", {}))


async def get_archive_weather(lat: float, lon: float, start: str, end: str) -> dict[str, Any]:
    """ERA5 reanalysis wind/gust/direction/cloud for a past date range (YYYY-MM-DD)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "hourly": ",".join(ARCHIVE_VARS),
        "wind_speed_unit": "kn",
        "timezone": "GMT",
    }
    data = await _get(ARCHIVE_URL, params)
    hourly = data.get("hourly", {})
    return {
        "timestamps": [_utc_iso(t) for t in hourly.get("time", [])],
        **{v: hourly.get(v, []) for v in ARCHIVE_VARS},
    }


async def get_marine_history(lat: float, lon: float, start: str, end: str) -> dict[str, Any]:
    """Historical waves/swell for a past date range via the marine endpoint.

    Tries Open-Meteo's default ("best match") first; if that comes back
    all-null (older years may predate the operational wave models), retries
    with HISTORY_MARINE_FALLBACK_MODEL. Returns {"source": ..., ...} so the
    caller can say which data it actually used, or no_data=True.
    """
    base = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "hourly": ",".join(HISTORY_MARINE_VARS),
        "timezone": "GMT",
        "cell_selection": "sea",
    }
    attempts: list[tuple[str, dict]] = [
        ("best_match", base),
        (HISTORY_MARINE_FALLBACK_MODEL, {**base, "models": HISTORY_MARINE_FALLBACK_MODEL}),
    ]

    last_error: str | None = None
    for source, params in attempts:
        try:
            data = await _get(MARINE_URL, params)
        except OpenMeteoError as exc:
            last_error = str(exc)
            continue
        flat: dict[str, Any] = {}
        for key, values in data.get("hourly", {}).items():
            base_key = key
            suffix = f"_{HISTORY_MARINE_FALLBACK_MODEL}"
            if key.endswith(suffix):
                base_key = key[: -len(suffix)]
            flat[base_key] = values
        if not _all_null(flat.get("wave_height")):
            out: dict[str, Any] = {
                "source": source,
                "timestamps": [_utc_iso(t) for t in flat.get("time", [])],
                "no_data": False,
            }
            for v in HISTORY_MARINE_VARS:
                out[v] = flat.get(v, [])
                if v in HEIGHT_VARS:
                    out[f"{v}_ft"] = _to_ft(out[v])
            return out
    return {"source": None, "no_data": True, "error": last_error}
