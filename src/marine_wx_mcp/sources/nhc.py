"""NHC (National Hurricane Center) active tropical cyclone tracking.

No API key. Build-time verification (2026-07-13):
- https://www.nhc.noaa.gov/CurrentStorms.json is the live feed - covers
  ONLY NHC's area of responsibility (Atlantic and Eastern/Central
  Pacific basins), NOT the Western Pacific or Indian Ocean (those are
  other agencies - e.g. JTWC/Guam, confirmed live that api.weather.gov
  had an active TCP product from PGUM/Guam while NHC's own
  CurrentStorms.json was empty at the same time - different basin,
  different authority). Don't present this tool as global cyclone
  coverage.
- No live storm was active in either basin during verification (mid-
  July, before Atlantic peak season Aug-Oct) - schema confirmed instead
  against NHC's own official sample file
  (nhc.noaa.gov/productexamples/NHC_JSON_Sample.json), which is exactly
  what NHC publishes as its field reference. Re-verify field names
  against a real live storm if one becomes active and something looks
  off.
- Each storm object carries `publicAdvisory`/`forecastDiscussion`/
  `forecastGraphics` sub-objects with a `url` to the human-readable
  product - not fetched/parsed here (only linked), since the .shtml
  page format wasn't verified live. Most other sub-objects
  (`trackCone`, `forecastWindRadiiGIS`, etc.) are GIS shapefile/KMZ
  download links, not useful for a text tool - intentionally not
  surfaced.
- `movementSpeed` is presented in mph per standard NHC public-advisory
  convention (e.g. "moving NW at 9 mph") - the JSON field itself
  carries no unit label, so this is inferred from NHC's well-established
  publication convention, not verified from this specific field.
"""

from __future__ import annotations

from typing import Any

import httpx

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

CLASSIFICATION_NAMES = {
    "TD": "Tropical Depression",
    "TS": "Tropical Storm",
    "HU": "Hurricane",
    "MH": "Major Hurricane",
    "SD": "Subtropical Depression",
    "SS": "Subtropical Storm",
    "EX": "Extratropical Cyclone",
    "PTC": "Post-Tropical Cyclone",
    "LO": "Low",
    "DB": "Disturbance",
    "WV": "Tropical Wave",
}


class NHCError(Exception):
    """Raised for unrecoverable NHC failures with an actionable message."""


async def get_active_storms() -> list[dict[str, Any]]:
    """Active tropical cyclones in NHC's area of responsibility (Atlantic/E&C Pacific).

    Returns a simplified list: [{id, name, classification,
    classification_name, wind_kt, pressure_mb, lat, lon, movement_dir_deg,
    movement_speed_mph, last_update, advisory_url, discussion_url}, ...].
    Empty list means no active storms right now, not a failure.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(CURRENT_STORMS_URL)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise NHCError(
                f"NHC CurrentStorms.json request failed ({exc!r}). Try again shortly."
            ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise NHCError("NHC CurrentStorms.json returned a non-JSON response.") from exc

    storms = []
    for s in data.get("activeStorms", []):
        classification = s.get("classification")
        storms.append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "classification": classification,
                "classification_name": CLASSIFICATION_NAMES.get(classification, classification),
                "wind_kt": s.get("intensity"),
                "pressure_mb": s.get("pressure"),
                "lat": s.get("latitudeNumeric"),
                "lon": s.get("longitudeNumeric"),
                "movement_dir_deg": s.get("movementDir"),
                "movement_speed_mph": s.get("movementSpeed"),
                "last_update": s.get("lastUpdate"),
                "advisory_url": (s.get("publicAdvisory") or {}).get("url"),
                "discussion_url": (s.get("forecastDiscussion") or {}).get("url"),
                "forecast_graphics_url": (s.get("forecastGraphics") or {}).get("url"),
            }
        )
    return storms
