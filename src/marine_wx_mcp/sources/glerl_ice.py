"""NOAA GLERL Great Lakes daily ice cover (ERDDAP tabledap) - no API key.

Build-time verification (2026-07-13):
- Dataset ID `glerlIce` on GLERL's ERDDAP (apps.glerl.noaa.gov/erddap) is
  simple daily whole-lake percentages (time, Superior, Michigan, Huron,
  Erie, Ontario, St_Clair, GL_Total - all percent), not a spatial grid -
  no lat/lon subsetting needed or possible, just a time filter.
- A DIFFERENT-looking dataset in the same catalog, `GL_Ice_Cover`
  ("Great Lakes Ice Cover Dataset"), looked like a better match by name
  and showed up fine in /info/ metadata, but its actual griddap query
  endpoint returned 404 "Currently unknown datasetID" consistently on
  retry - confirmed genuinely down (not a transient blip: `glerlIce`
  answered normally on the same server at the same moment) rather than
  a wrong URL. Don't switch back to it without re-verifying it's live.
- Confirmed live the data is real and climatologically sane: Feb 2026
  showed Erie ~94-95% (shallow, freezes fastest, matches known Great
  Lakes ice climatology), St_Clair 100% (shallow connecting channel),
  Michigan ~30-36% (deep, freezes least) - not placeholder/dummy values.
- Ice season is genuinely Dec-Apr; outside that window (confirmed live
  for 2026-05-16, the latest available row at verification time) every
  lake legitimately reads 0% - that's real data, not a missing-data
  gap, and the tool should say so rather than implying failure.
"""

from __future__ import annotations

from typing import Any

import httpx

TABLEDAP_URL = "https://apps.glerl.noaa.gov/erddap/tabledap/glerlIce.json"
LAKES = ["Superior", "Michigan", "Huron", "Erie", "Ontario", "St_Clair"]
COLUMNS = ["time", *LAKES, "GL_Total"]


class GlerlIceError(Exception):
    """Raised for unrecoverable GLERL ice-data failures with an actionable message."""


async def _fetch(query_suffix: str) -> list[dict[str, Any]]:
    """`query_suffix` is appended verbatim after the column list, e.g.
    '&orderByMax("time")' or '&time>=2026-02-01&time<=2026-02-05'.
    ERDDAP mixes constraint (key=value) and function-call
    (orderByMax("time")) syntax in the same query string, so callers
    build the exact suffix rather than passing a key/value dict.
    """
    url = f"{TABLEDAP_URL}?{','.join(COLUMNS)}{query_suffix}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise GlerlIceError(
                f"GLERL ice data request failed ({exc!r}). Try again shortly."
            ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise GlerlIceError("GLERL ice data returned a non-JSON response.") from exc

    table = data.get("table", {})
    cols = table.get("columnNames", [])
    return [dict(zip(cols, row)) for row in table.get("rows", [])]


async def get_latest_ice_cover() -> dict[str, Any]:
    """Most recent day's ice cover percentage for every lake, plus basin-wide total."""
    rows = await _fetch('&orderByMax(%22time%22)')
    if not rows:
        raise GlerlIceError("GLERL returned no ice cover data.")
    row = rows[0]
    return {
        "date": row["time"],
        "lakes": {lake: row.get(lake) for lake in LAKES},
        "total": row.get("GL_Total"),
    }
