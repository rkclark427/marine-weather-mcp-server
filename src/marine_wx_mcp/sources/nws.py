"""api.weather.gov client.

Gotchas discovered during build-time verification (2026-07-06):
- /zones/forecast/{zone}/forecast returns 404 "Marine Forecast Not
  Supported" for every LMZ zone tried. Marine zone forecasts must
  come from a text product instead (Nearshore/Coastal Waters Forecast -
  get_marine_zone_forecast below).
- /points/{lat},{lon} resolves to the nearest LAND zone, even for
  harbor coordinates sitting right on the shoreline. It is only
  useful for grid metadata (gridId/gridX/gridY), not marine zone
  lookup.
- Gridpoint values (waveHeight, wavePeriod, etc.) use RFC 3339
  duration validTime intervals like "2026-07-06T07:00:00+00:00/PT2H",
  not point timestamps - each entry covers a span, not an instant.

Gotchas discovered generalizing marine zone forecasts nationwide
(2026-07-13):
- /zones/marine/{zoneId} returns the responsible office directly
  (awipsLocationIdentifier/cwa) for ANY marine zone - no need to
  hardcode a zone->office table. get_marine_zone_office uses this.
- Coastal marine zone text products come in two product types with
  IDENTICAL formatting (UGC "ZONE###-" header, "$$"-delimited blocks,
  ".PERIOD...text" entries) - Nearshore Marine Forecast (NSH), issued
  by exactly 11 Great Lakes offices (confirmed live: APX, BUF, CLE,
  DLH, DTX, GRB, GRR, IWX, LOT, MKX, MQT), and Coastal Waters Forecast
  (CWF) issued by every other coastal office nationwide - confirmed
  these sets are mutually exclusive. get_marine_zone_forecast tries NSH
  then falls back to CWF, reusing the same parser for both.
- Offshore (OFF, >~60nm to the Gulf Stream/shelf break) and High Seas
  (HSF, ocean basins) forecasts are a genuinely different tier, issued
  by national centers (Ocean Prediction Center/NHC-TAFB) under special
  location codes (e.g. "N01", "AT1"), NOT local WFO office codes - and
  a marine zone's own /zones/marine metadata does NOT reliably map to
  these codes (confirmed live: offshore zone AMZ040's
  awipsLocationIdentifier is "NH2", not a location code that appears in
  OFF's own /products/types/OFF/locations list). Callers must supply
  the OPC/TAFB location code directly rather than a UGC zone_id - see
  get_offshore_forecast/get_high_seas_forecast.
- OFF products have no UGC zone codes or "$$" delimiters at all -
  sub-areas are just a bare Title Case name line immediately followed
  by ".PERIOD..." entries, confirmed live and consistent through a full
  product with no explicit terminator (parse_off_product).
- HSF products are ocean-basin-wide narratives (ALL CAPS, embedded
  lat/lon polygon boundaries, ice/tropical cyclone caveats) with no
  zone-style subdivision at all - returned as cleaned full text rather
  than parsed into sub-areas.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

BASE_URL = "https://api.weather.gov"
# NWS asks for a User-Agent naming the app plus a way to reach its operator.
# Set NWS_CONTACT (an email address) in the marine_wx env block of
# claude_desktop_config.json; without it the project URL is sent instead, so
# nobody else's install ever reports the author's address.
DEFAULT_CONTACT = "https://github.com/rkclark427/marine-weather-mcp-server"
USER_AGENT = f"marine_wx_mcp/0.1 ({os.environ.get('NWS_CONTACT') or DEFAULT_CONTACT})"


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


async def get_marine_zone_office(zone_id: str) -> str:
    """The WFO office code responsible for any NWS marine zone (coastal/Great Lakes).

    Works nationwide via live zone metadata - no hardcoded zone list.
    Does NOT resolve offshore/high-seas codes (see module docstring).
    """
    data = await _get(f"/zones/marine/{zone_id}")
    props = data["properties"]
    office = props.get("awipsLocationIdentifier") or (props.get("cwa") or [None])[0]
    if not office:
        raise NWSError(
            f"No responsible office found for marine zone '{zone_id}'. It may be an "
            "offshore/high-seas zone - try marine_get_offshore_forecast or "
            "marine_get_high_seas_forecast instead."
        )
    return office


class OutsideNWSCoverage(NWSError):
    """The coordinate has no NWS gridpoint (outside US forecast coverage).

    Callers should fall back to Open-Meteo rather than surfacing this as a
    failure - it's the expected answer for e.g. Antigua, not an outage.
    """


async def get_point_metadata(lat: float, lon: float) -> dict[str, Any]:
    """Grid metadata for a coordinate: gridId/gridX/gridY, land forecastZone, city.

    Raises OutsideNWSCoverage when the point has no NWS grid - confirmed live
    2026-10-11 two ways: /points 404s ("Unable to provide data for requested
    point") for Antigua, and some open-water points return 200 with null
    gridId/gridX/gridY, which previously produced a garbled
    /gridpoints/None/None,None request downstream.
    """
    try:
        data = await _get(f"/points/{lat},{lon}")
    except NWSError as exc:
        if "404" in str(exc):
            raise OutsideNWSCoverage(
                f"No NWS gridpoint forecast for {lat},{lon} (outside NWS coverage). "
                "Use marine_get_model_forecast (Open-Meteo) instead."
            ) from exc
        raise
    props = data["properties"]
    if not props.get("gridId") or props.get("gridX") is None or props.get("gridY") is None:
        raise OutsideNWSCoverage(
            f"No NWS gridpoint forecast for {lat},{lon} (point has no forecast grid). "
            "Use marine_get_model_forecast (Open-Meteo) instead."
        )
    return props


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


def parse_zone_forecast_product(text: str) -> dict[str, dict[str, Any]]:
    """Parse a coastal marine zone text product (NSH or CWF - identical format)
    into per-zone periods.

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


async def get_marine_zone_forecast(office: str) -> tuple[str, dict[str, dict[str, Any]]]:
    """Latest coastal marine zone forecast for an office, parsed by zone.

    Tries NSH (Great Lakes) first, falls back to CWF (everywhere else) -
    the two product types are mutually exclusive by office, see module
    docstring. Returns (product_type_used, {zone_id: {...}}). This is
    the zone forecast tool's real data source - see the module docstring
    for why /zones/forecast/{zone}/forecast can't be used directly.
    """
    for product_type in ("NSH", "CWF"):
        product_id = await _latest_product_id(product_type, office)
        if product_id is not None:
            text = await _product_text(product_id)
            return product_type, parse_zone_forecast_product(text)
    raise NWSError(
        f"No coastal marine zone forecast (NSH or CWF) found for office {office}. "
        "Try the NWS forecast discussion (.MARINE section) as a fallback."
    )


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


# Offshore Forecast (OFF) locations confirmed live 2026-07-13. AER/AFG/AJK/
# ALU/HFO are Alaska/Hawaii WFOs; N01-N15/NT1-NT5/PZ5-PZ8 are Ocean
# Prediction Center NAVTEX-style area codes with no human-readable name in
# the API itself (the product text names its own coverage area).
OFFSHORE_LOCATIONS = [
    "AER", "AFG", "AJK", "ALU", "HFO",
    "N01", "N02", "N03", "N04", "N05", "N06", "N07", "N08", "N09", "N10",
    "N11", "N12", "N13", "N14", "N15",
    "NT1", "NT2", "NT3", "NT4", "NT5",
    "PZ5", "PZ6", "PZ7", "PZ8",
]

# High Seas Forecast (HSF) area codes confirmed live 2026-07-13 - ocean
# basins (METAREAs), not WFO offices: Atlantic 1/2, Eastern Pacific 1/2,
# Eastern Pacific Islands, North Pacific.
HIGH_SEAS_AREAS = ["AT1", "AT2", "EP1", "EP2", "EPI", "NP"]

_OFF_AREA_HEADER_RE = re.compile(
    r"^([A-Z][A-Za-z0-9 ,\-'/]{2,80})\s*$\n+(?=\.[A-Z])", re.MULTILINE
)


def parse_off_product(text: str) -> dict[str, dict[str, Any]]:
    """Parse an Offshore Forecast (OFF) product into named sub-area -> periods.

    Unlike coastal zone products, OFF has no UGC zone codes or "$$"
    delimiters - sub-areas are just a bare name line immediately
    followed by ".PERIOD..." entries, confirmed live (see module
    docstring). Returns {area_name: {"periods": {name: text}}}.
    """
    matches = list(_OFF_AREA_HEADER_RE.finditer(text))
    areas: dict[str, dict[str, Any]] = {}
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        periods = {
            pname.strip(): " ".join(body.split())
            for pname, body in _PERIOD_RE.findall(body)
        }
        areas[name] = {"periods": periods}
    return areas


async def get_offshore_forecast(location: str) -> dict[str, dict[str, Any]]:
    """Latest Offshore Forecast (OFF) for an OPC/WFO location code, parsed by sub-area.

    `location` is an Ocean Prediction Center area code or Alaska/Hawaii
    WFO code (see OFFSHORE_LOCATIONS), not a UGC marine zone_id - see
    module docstring for why zone_id doesn't reliably map to these.
    """
    product_id = await _latest_product_id("OFF", location)
    if product_id is None:
        raise NWSError(
            f"No Offshore Forecast (OFF) found for location '{location}'. Valid "
            f"locations: {', '.join(OFFSHORE_LOCATIONS)}."
        )
    text = await _product_text(product_id)
    return parse_offshore_text(text)


def parse_offshore_text(text: str) -> dict[str, dict[str, Any]]:
    """Parse either offshore product style.

    OPC offshore products (N01 etc.) use bare Title Case area headers with no
    UGC codes (parse_off_product). NHC's Caribbean/tropical Atlantic product
    (location NT3, AWIPS OFFNT3) instead uses UGC zone blocks ("AMZ051-...$$")
    in the same layout as CWF - confirmed 2026-10-11 against NHC's archived
    OFFNT3 text; the OPC-only parser returned {} for it, which is why NT3
    came back "No offshore forecast areas found". Areas from the UGC style
    are keyed "AMZ051 - Offshore Waters Leeward Islands".
    """
    if _ZONE_BLOCK_RE.search(text) and "$$" in text:
        zones = parse_zone_forecast_product(text)
        if zones:
            return {
                f"{zone_id} - {z['area']}" if z["area"] else zone_id: {
                    "periods": z["periods"],
                    "zone_id": zone_id,
                    "issued": z["issued"],
                }
                for zone_id, z in zones.items()
            }
    return parse_off_product(text)


def filter_offshore_areas(
    areas: dict[str, dict[str, Any]], zones: list[str] | None
) -> dict[str, dict[str, Any]]:
    """Keep only areas whose zone_id (or name) matches one of `zones`."""
    if not zones:
        return areas
    wanted = {z.upper() for z in zones}
    return {
        name: a
        for name, a in areas.items()
        if (a.get("zone_id") or "").upper() in wanted or name.upper() in wanted
    }


async def get_high_seas_forecast(area: str) -> str:
    """Latest High Seas Forecast (HSF) full text for an ocean basin/METAREA code.

    Returned as cleaned full text rather than parsed into sub-areas -
    HSF products are basin-wide narratives with no zone-style
    subdivision (see module docstring).
    """
    product_id = await _latest_product_id("HSF", area)
    if product_id is None:
        raise NWSError(
            f"No High Seas Forecast (HSF) found for area '{area}'. Valid areas: "
            f"{', '.join(HIGH_SEAS_AREAS)}."
        )
    return await _product_text(product_id)


_NHC_HSF_MARKER = "NATIONAL HURRICANE CENTER"


def split_high_seas_text(text: str, section: str = "all") -> str:
    """Trim an HSF product to one issuing center's section.

    AT1 carries two sections in one product: OPC's North Atlantic (north of
    31N) and NHC's tropical Atlantic/Caribbean (07N-31N). For a Caribbean
    passage only the NHC section matters. `section` is "all", "opc" or "nhc";
    if the marker isn't found the full text is returned unchanged.
    """
    if section == "all":
        return text
    idx = text.find(_NHC_HSF_MARKER)
    if idx == -1:
        return text
    # Back up to the start of the line containing the marker.
    line_start = text.rfind("\n", 0, idx) + 1
    if section == "nhc":
        return text[line_start:]
    return text[:line_start]
