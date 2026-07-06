"""api.weather.gov client.

Gotchas discovered during build-time verification (2026-07-06):
- /zones/forecast/{zone}/forecast returns 404 "Marine Forecast Not
  Supported" for every LMZ zone tried. Marine zone forecasts must
  come from the Nearshore Marine Forecast (NSH) text product instead
  (get_nearshore_forecast below).
- /points/{lat},{lon} resolves to the nearest LAND zone, even for
  harbor coordinates sitting right on the shoreline. It is only
  useful for grid metadata (gridId/gridX/gridY), not marine zone
  lookup - use the registry's static zone list for that.
- Gridpoint values (waveHeight, wavePeriod, etc.) use RFC 3339
  duration validTime intervals like "2026-07-06T07:00:00+00:00/PT2H",
  not point timestamps - each entry covers a span, not an instant.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

BASE_URL = "https://api.weather.gov"
USER_AGENT = "marine_wx_mcp/0.1 (robert.kent.clark@gmail.com)"


class NWSError(Exception):
    """Raised for unrecoverable api.weather.gov failures.

    Message is written to be shown directly to the model/user - it
    names the source and, where applicable, a fallback.
    """


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=15.0,
    )


async def _get(path: str, params: dict | None = None) -> dict:
    async with _client() as client:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await client.get(path, params=params)
                if resp.status_code >= 500:
                    last_exc = NWSError(
                        f"NWS API returned {resp.status_code} for {path}; "
                        "retrying once before giving up."
                    )
                    continue
                if resp.status_code == 404:
                    detail = resp.json().get("detail", "not found")
                    raise NWSError(f"NWS API 404 for {path}: {detail}")
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                last_exc = NWSError(
                    f"NWS API request to {path} failed ({exc!r}); "
                    "api.weather.gov may be down. Try again shortly, or "
                    "fall back to NDBC buoy obs / Open-Meteo model forecast."
                )
        assert last_exc is not None
        raise last_exc


async def get_active_alerts(zone_ids: list[str]) -> list[dict[str, Any]]:
    """Active alerts for one or more marine zones. Returns GeoJSON features."""
    data = await _get("/alerts/active", params={"zone": ",".join(zone_ids)})
    return data.get("features", [])


async def get_point_metadata(lat: float, lon: float) -> dict[str, Any]:
    """Grid metadata for a coordinate: gridId/gridX/gridY, land forecastZone, city."""
    data = await _get(f"/points/{lat},{lon}")
    return data["properties"]


async def get_gridpoint(office: str, grid_x: int, grid_y: int) -> dict[str, Any]:
    """Raw gridded values for an office/grid cell, including waveHeight etc."""
    data = await _get(f"/gridpoints/{office}/{grid_x},{grid_y}")
    return data["properties"]


async def get_gridpoint_hourly_forecast(
    office: str, grid_x: int, grid_y: int
) -> list[dict[str, Any]]:
    """Hourly forecast periods for an office/grid cell."""
    data = await _get(f"/gridpoints/{office}/{grid_x},{grid_y}/forecast/hourly")
    return data["properties"]["periods"]


async def _latest_product_id(product_type: str, office: str) -> str | None:
    data = await _get(f"/products/types/{product_type}/locations/{office}")
    graph = data.get("@graph", [])
    if not graph:
        return None
    return graph[0]["id"]


async def _product_text(product_id: str) -> str:
    data = await _get(f"/products/{product_id}")
    return data["productText"]


_ZONE_BLOCK_RE = re.compile(r"^([A-Z]{2,3}\d{3})-", re.MULTILINE)
_PERIOD_RE = re.compile(
    r"\.([A-Z][A-Z0-9 /]*?)\.\.\.\s*(.*?)(?=\n\.[A-Z][A-Z0-9 /]*?\.\.\.|\Z)",
    re.DOTALL,
)


def parse_nsh_product(text: str) -> dict[str, dict[str, Any]]:
    """Parse a Nearshore Marine Forecast text product into per-zone periods.

    Returns {zone_id: {"area": str, "issued": str, "periods": {name: text}}}.
    """
    zones: dict[str, dict[str, Any]] = {}
    blocks = text.split("$$")
    for block in blocks:
        match = _ZONE_BLOCK_RE.search(block)
        if not match:
            continue
        zone_id = match.group(1)
        # After the "LMZ643-" match, the block reads:
        #   062200-                              (UGC purge/expiration code)
        #   Sheboygan to Port Washington WI-      (area name)
        #   1110 AM CDT Mon Jul 6 2026            (issuance time)
        lines = block[match.end():].strip().splitlines()
        area = lines[1].rstrip("-").strip() if len(lines) > 1 else ""
        issued = lines[2].strip() if len(lines) > 2 else ""
        periods = {
            name.strip(): " ".join(body.split())
            for name, body in _PERIOD_RE.findall(block)
        }
        zones[zone_id] = {"area": area, "issued": issued, "periods": periods}
    return zones


async def get_nearshore_forecast(office: str) -> dict[str, dict[str, Any]]:
    """Latest Nearshore Marine Forecast (NSH) for an office, parsed by zone.

    This is the marine zone forecast tool's real data source - see the
    module docstring for why /zones/forecast/{zone}/forecast can't be
    used directly.
    """
    product_id = await _latest_product_id("NSH", office)
    if product_id is None:
        raise NWSError(
            f"No Nearshore Marine Forecast (NSH) product found for office {office}. "
            "Try the NWS forecast discussion (.MARINE section) as a fallback."
        )
    text = await _product_text(product_id)
    return parse_nsh_product(text)


def _extract_marine_section(text: str) -> str:
    match = re.search(r"^\.MARINE\.\.\.(.*?)(?=^\$\$|\Z)", text, re.DOTALL | re.MULTILINE)
    if not match:
        return text
    return match.group(1).strip()


async def get_forecast_discussion(office: str, section: str = "marine") -> str:
    """Latest Area Forecast Discussion (AFD) text, optionally trimmed to .MARINE."""
    product_id = await _latest_product_id("AFD", office)
    if product_id is None:
        raise NWSError(f"No Area Forecast Discussion (AFD) found for office {office}.")
    text = await _product_text(product_id)
    if section == "marine":
        return _extract_marine_section(text)
    return text
