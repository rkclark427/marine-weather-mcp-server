"""Open-Meteo forecast + marine clients (free, no API key).

Build-time verification (2026-07-06):
- Multi-model requests suffix each hourly variable with the model name
  (e.g. "wind_speed_10m_ecmwf_ifs025", "wind_speed_10m_gfs_seamless"),
  sharing one "time" array - not separate per-model response blocks.
- The marine endpoint DOES return real, non-null Lake Michigan wave
  data (tested at 42.6,-87.7: all 168 hourly values populated,
  magnitudes consistent with the NWS nearshore forecast text). The
  spec's anticipated "no wave data for this location" fallback is
  kept as a defensive check but is not expected to trigger here.
"""

from __future__ import annotations

from typing import Any

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

DEFAULT_MODELS = ["ecmwf_ifs025", "gfs_seamless"]
DEFAULT_FORECAST_VARS = [
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "precipitation",
    "precipitation_probability",
    "cape",
    "temperature_2m",
]
MARINE_VARS = ["wave_height", "wave_direction", "wave_period"]


class OpenMeteoError(Exception):
    """Raised for unrecoverable Open-Meteo failures with an actionable message."""


async def _get(url: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OpenMeteoError(
                f"Open-Meteo request failed ({exc!r}). Try marine_get_point_forecast "
                "(NWS gridpoint) as a fallback."
            ) from exc
        return resp.json()


def _split_model_suffix(var_key: str, base_vars: list[str], models: list[str]) -> tuple[str, str] | None:
    for base in base_vars:
        for model in models:
            if var_key == f"{base}_{model}":
                return base, model
    return None


async def get_model_forecast(
    lat: float,
    lon: float,
    models: list[str] | None = None,
    hours: int = 72,
) -> dict[str, Any]:
    """Multi-model wind/weather comparison, aligned by timestamp.

    Returns {"timestamps": [...], "units": {var: unit}, "models": {model: {var: [values]}}}.
    """
    models = models or DEFAULT_MODELS
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(DEFAULT_FORECAST_VARS),
        "models": ",".join(models),
        "wind_speed_unit": "kn",
        "timezone": "America/Chicago",
        "forecast_hours": hours,
    }
    data = await _get(FORECAST_URL, params)
    hourly = data.get("hourly", {})
    units = data.get("hourly_units", {})

    timestamps = hourly.get("time", [])
    result: dict[str, Any] = {"timestamps": timestamps, "units": {}, "models": {m: {} for m in models}}

    for key, values in hourly.items():
        if key == "time":
            continue
        split = _split_model_suffix(key, DEFAULT_FORECAST_VARS, models)
        if split is None:
            continue
        base, model = split
        result["models"][model][base] = values
        result["units"][base] = units.get(key)

    return result


async def get_marine_forecast(lat: float, lon: float, hours: int = 48) -> dict[str, Any]:
    """Hourly wave height/direction/period. Flags no_data honestly if all-null.

    See module docstring: Great Lakes coverage was verified live and is
    expected to be populated, but this still checks rather than assuming.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(MARINE_VARS),
        "timezone": "America/Chicago",
        "forecast_hours": hours,
    }
    data = await _get(MARINE_URL, params)
    hourly = data.get("hourly", {})
    units = data.get("hourly_units", {})
    timestamps = hourly.get("time", [])

    wave_height = hourly.get("wave_height", [])
    no_data = len(wave_height) == 0 or all(v is None for v in wave_height)

    return {
        "timestamps": timestamps,
        "units": {v: units.get(v) for v in MARINE_VARS},
        "wave_height": hourly.get("wave_height", []),
        "wave_direction": hourly.get("wave_direction", []),
        "wave_period": hourly.get("wave_period", []),
        "no_data": no_data,
        "no_data_message": (
            "No wave data available for this location from Open-Meteo marine; "
            "use marine_get_point_forecast (NWS gridpoint waveHeight) or "
            "marine_get_buoy_obs (NDBC buoy) instead."
            if no_data
            else None
        ),
    }
