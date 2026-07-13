"""NAVCEN MSI (Maritime Safety Information) client - aids to navigation.

Static Light List (what should be there) and live discrepancy/hazard
layers (what's actually wrong right now), both served as GeoJSON with
no API key. Build-time verification (2026-07-06):

- Files are paginated ArcGIS exports capped at 1000 features each,
  named `{layer}_{N}.geojson` starting at N=1. The top-level
  `properties.exceededTransferLimit` flag tells you whether another
  page exists - **do not** treat the chunk count advertised on the
  navcen.uscg.gov/msi HTML page as authoritative and don't guess
  further chunk numbers past what exceededTransferLimit says: verified
  live that requesting N one past the true last page returns HTTP 200
  with a full, valid FeatureCollection that just duplicates an earlier
  page (not a 404, not empty) - so paginate by checking the flag, not
  by fetching until you see an error.
- The Light List (`lightListD09_N.geojson`) is District-09-scoped
  (District 09 covers the whole Great Lakes, not just Lake Michigan);
  confirmed D09 has 5 pages (4697 records) as of 2026-07-06. The
  discrepancy/hazard/change/event layers (discFedAid, discPriAid,
  tmpChange, hazNav, marEvent) are NATIONWIDE, not district-scoped -
  confirmed discFedAid has 3 pages, discPriAid 3 pages, and
  tmpChange/hazNav/marEvent are each a single page. All need bbox/
  radius filtering client-side; there's no server-side geographic
  query on these static exports.
- **Unit gotcha, confirmed**: `LIGHT_NOM_RANGE_UNIT` on Light List
  entries is "STAT MI" (statute miles), not nautical miles, unlike
  every other distance/speed figure in this server. Must convert.
- discFedAid/discPriAid/tmpChange share one schema (ATON_GROUP, LLNR,
  NAME, *_STATUS, ATON_UID, AID_TYPE, WATERWAY_NAME, DECIMAL_LAT/LON).
  hazNav/marEvent share a different schema (MSI_UID, TITLE, STATUS,
  MSI_GROUP, SUB_CATEGORY, TYPE, BEGIN_DATE/END_DATE, DESCRIPTION,
  DECIMAL_LAT/LON). Both keyed off DECIMAL_LATITUDE/DECIMAL_LONGITUDE
  for filtering.
- Files are large (Light List D09 alone is ~9MB across 5 pages) - per
  the build brief this wants bulk-download-and-cache with a refresh
  interval, not a live fetch per tool call. Cached to disk under
  ~/.marine_wx_mcp_cache/navcen/ with a TTL; see _load_or_fetch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from marine_wx_mcp.sources._geo import haversine_nm

BASE_URL = "https://navcen.uscg.gov/sites/default/files/msi"
CACHE_DIR = Path.home() / ".marine_wx_mcp_cache" / "navcen"
CACHE_TTL_SECONDS = 6 * 3600
MAX_PAGES = 10  # safety cap; real datasets top out at 5 pages (see module docstring)

STAT_MI_TO_NM = 0.868976


class NavcenError(Exception):
    """Raised for unrecoverable NAVCEN MSI failures with an actionable message."""


async def _fetch_page(client: httpx.AsyncClient, layer: str, page: int) -> dict:
    resp = await client.get(f"{BASE_URL}/{layer}_{page}.geojson")
    resp.raise_for_status()
    return resp.json()


async def _fetch_all_pages(layer: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                data = await _fetch_page(client, layer, page)
            except httpx.HTTPError as exc:
                if page == 1:
                    raise NavcenError(
                        f"NAVCEN MSI request for layer '{layer}' failed ({exc!r})."
                    ) from exc
                break
            features.extend(data.get("features", []))
            if not data.get("properties", {}).get("exceededTransferLimit"):
                break
    return features


def _cache_path(cache_key: str) -> Path:
    return CACHE_DIR / f"{cache_key}.json"


async def _load_or_fetch(cache_key: str, layer: str) -> list[dict[str, Any]]:
    path = _cache_path(cache_key)
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            with open(path, encoding="utf-8") as f:
                return json.load(f)

    features = await _fetch_all_pages(layer)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(features, f)
    return features


async def get_light_list(district: str = "09") -> list[dict[str, Any]]:
    """All Light List entries for a Coast Guard district (unfiltered, cached)."""
    return await _load_or_fetch(f"lightList_d{district}", f"lightListD{district}")


async def get_discrepancies() -> dict[str, list[dict[str, Any]]]:
    """Live discrepancy/hazard/change/event layers (unfiltered, cached).

    Returns {"federal_aid": [...], "private_aid": [...], "temporary_change": [...],
    "hazard": [...], "marine_event": [...]}.
    """
    return {
        "federal_aid": await _load_or_fetch("disc_fed_aid", "discFedAid"),
        "private_aid": await _load_or_fetch("disc_pri_aid", "discPriAid"),
        "temporary_change": await _load_or_fetch("tmp_change", "tmpChange"),
        "hazard": await _load_or_fetch("haz_nav", "hazNav"),
        "marine_event": await _load_or_fetch("mar_event", "marEvent"),
    }


def filter_by_radius(
    features: list[dict[str, Any]], lat: float, lon: float, radius_nm: float
) -> list[dict[str, Any]]:
    """Features (each with DECIMAL_LATITUDE/DECIMAL_LONGITUDE properties) within radius_nm."""
    results = []
    for feature in features:
        props = feature["properties"]
        flat, flon = props.get("DECIMAL_LATITUDE"), props.get("DECIMAL_LONGITUDE")
        if flat is None or flon is None:
            continue
        distance = haversine_nm(lat, lon, flat, flon)
        if distance <= radius_nm:
            results.append({**feature, "_distance_nm": round(distance, 1)})
    return sorted(results, key=lambda f: f["_distance_nm"])


def light_range_nm(properties: dict[str, Any]) -> float | None:
    """LIGHT_NOM_RANGE converted to nautical miles, honoring LIGHT_NOM_RANGE_UNIT."""
    value = properties.get("LIGHT_NOM_RANGE")
    if value is None:
        return None
    unit = (properties.get("LIGHT_NOM_RANGE_UNIT") or "").upper()
    if "STAT" in unit:
        return round(value * STAT_MI_TO_NM, 1)
    return value
