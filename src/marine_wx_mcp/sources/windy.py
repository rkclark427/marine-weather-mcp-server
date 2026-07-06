"""Windy Point Forecast API v2 client - v2 roadmap item, stubbed in v1.

Verified 2026-07-06 (per spec): Windy Premium does not include API
access; the Point Forecast API is a separate product, excludes ECMWF
for licensing reasons, and its free trial is development-only per ToS.
Inert without WINDY_API_KEY - must never raise or crash tool
registration when the key is unset.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

POINT_FORECAST_URL = "https://api.windy.com/api/point-forecast/v2"

NOT_CONFIGURED_MESSAGE = (
    "Windy Point Forecast is not configured (WINDY_API_KEY is unset). "
    "This is an optional v2 data source - use marine_get_model_forecast "
    "(Open-Meteo) for multi-model wind comparison instead. To enable, set "
    "WINDY_API_KEY in the server's Claude Desktop config env block."
)


class WindyError(Exception):
    """Raised for unrecoverable Windy API failures with an actionable message."""


def is_configured() -> bool:
    return bool(os.environ.get("WINDY_API_KEY"))


async def get_point_forecast(
    lat: float,
    lon: float,
    model: str = "gfs",
    parameters: list[str] | None = None,
) -> dict[str, Any]:
    """Point forecast from Windy. Returns a not-configured message if no key is set."""
    api_key = os.environ.get("WINDY_API_KEY")
    if not api_key:
        return {"configured": False, "message": NOT_CONFIGURED_MESSAGE}

    parameters = parameters or ["wind", "waves"]
    body = {
        "lat": lat,
        "lon": lon,
        "model": model,
        "parameters": parameters,
        "levels": ["surface"],
        "key": api_key,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(POINT_FORECAST_URL, json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WindyError(
                f"Windy Point Forecast request failed ({exc!r}). Try "
                "marine_get_model_forecast (Open-Meteo) instead."
            ) from exc
        data = resp.json()
        data["configured"] = True
        return data
