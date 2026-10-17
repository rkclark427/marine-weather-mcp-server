"""FastMCP server init and tool registration for marine_wx_mcp.

Tool implementations delegate to marine_wx_mcp.sources and
marine_wx_mcp.registry; this module wires them up as MCP tools and
handles response_format (markdown vs json) plus turning source
exceptions into actionable string results instead of crashes.
"""

from __future__ import annotations

import asyncio
import math
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from marine_wx_mcp import climatology, formatting, registry, scoring
from marine_wx_mcp.climatology import ClimatologyError
from marine_wx_mcp.registry import RegistryError
from marine_wx_mcp.sources import (
    coast_pilot,
    coops,
    glerl_ice,
    metservice,
    navcen,
    ndbc,
    nhc,
    nws,
    openmeteo,
    usno,
)
from marine_wx_mcp.sources._geo import haversine_nm
from marine_wx_mcp.sources.coast_pilot import CoastPilotError
from marine_wx_mcp.sources.coops import CoopsError
from marine_wx_mcp.sources.glerl_ice import GlerlIceError
from marine_wx_mcp.sources.metservice import MetServiceError
from marine_wx_mcp.sources.navcen import NavcenError
from marine_wx_mcp.sources.ndbc import NDBCError
from marine_wx_mcp.sources.nhc import NHCError
from marine_wx_mcp.sources.nws import NWSError, OutsideNWSCoverage
from marine_wx_mcp.sources.openmeteo import OpenMeteoError
from marine_wx_mcp.sources.usno import USNOError

mcp = FastMCP("marine_wx_mcp")

ResponseFormat = Literal["markdown", "json"]


def _resolve_zones(zone_ids: list[str] | None, place: str | None) -> list[str]:
    if zone_ids:
        return zone_ids
    if place:
        return registry.zones_for_place(place)
    raise RegistryError("Provide either zone_ids or place.")


def _resolve_location(
    lat: float | None, lon: float | None, place: str | None
) -> dict[str, Any]:
    """Resolve place-or-coordinates to {lat, lon, label, region, tz}.

    Registry places carry their region; ad-hoc coordinates inherit the
    nearest region within registry.REGION_MATCH_NM (else UTC). The tz is
    what every tool uses for display - never the machine's local zone.
    """
    if place:
        port = registry.get_port(place)
        region = port["region"]
        return {
            "lat": port["lat"],
            "lon": port["lon"],
            "label": place,
            "region": region,
            "tz": registry.timezone_for_region(region),
        }
    if lat is not None and lon is not None:
        region = registry.region_for_coords(lat, lon)
        return {
            "lat": lat,
            "lon": lon,
            "label": f"{lat},{lon}",
            "region": region,
            "tz": registry.timezone_for_region(region),
        }
    raise RegistryError("Provide either place, or both lat and lon.")


def _resolve_coords(
    lat: float | None, lon: float | None, place: str | None
) -> tuple[float, float, str]:
    loc = _resolve_location(lat, lon, place)
    return loc["lat"], loc["lon"], loc["label"]


def _today(tz_name: str) -> date_cls:
    return datetime.now(ZoneInfo(tz_name)).date()


def _in_great_lakes(lat: float, lon: float) -> bool:
    return (
        GREAT_LAKES_BBOX[0] <= lat <= GREAT_LAKES_BBOX[1]
        and GREAT_LAKES_BBOX[2] <= lon <= GREAT_LAKES_BBOX[3]
    )


async def _open_meteo_rows(lat: float, lon: float, hours: int) -> tuple[list[dict[str, Any]], str | None]:
    """Merged hourly rows (wind + marine) from Open-Meteo, and an optional marine note."""
    wind, marine = await asyncio.gather(
        openmeteo.get_model_forecast(lat, lon, None, hours),
        openmeteo.get_marine_forecast(lat, lon, hours),
        return_exceptions=True,
    )
    if isinstance(wind, Exception):
        raise wind
    note = None
    if isinstance(marine, Exception):
        note = f"Marine (wave) data unavailable: {marine}"
        marine = None
    elif marine.get("no_data"):
        note = marine["no_data_message"]
    return scoring.merge_hourly(wind, marine), note


@mcp.tool()
async def marine_get_alerts(
    zone_ids: list[str] | None = None,
    place: str | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Active marine alerts (Small Craft Advisory, Gale Warning, etc.).

    Provide either explicit zone_ids (e.g. ["LMZ646"]) or a registry
    place/route name to expand to its zones.
    """
    try:
        zones = _resolve_zones(zone_ids, place)
        features = await nws.get_active_alerts(zones)
    except (RegistryError, NWSError) as exc:
        return str(exc)

    if response_format == "json":
        return {"zones": zones, "alerts": features}
    tz = None
    if place and not zone_ids:
        try:
            tz = registry.timezone_for_region(registry.get_port(place)["region"])
        except RegistryError:
            tz = None  # a route name - fall back to the default display zone
    return formatting.format_alerts(zones, features, tz)


@mcp.tool()
async def marine_get_zone_forecast(
    zone_id: str,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Coastal/Great Lakes forecast (wind/waves/weather by period) for one NWS marine zone.

    Works for any US coastal or Great Lakes marine zone nationwide (e.g.
    "LMZ646", "ANZ630") - the office is looked up live from NWS zone
    metadata, not a fixed list. Backed by the Nearshore (Great Lakes) or
    Coastal Waters (elsewhere) text product, not the NWS zone-forecast
    JSON endpoint (which 404s for marine zones). For offshore/high-seas
    zones, use marine_get_offshore_forecast / marine_get_high_seas_forecast
    instead - those aren't covered by this endpoint.
    """
    try:
        office = await nws.get_marine_zone_office(zone_id)
        product_type, zones = await nws.get_marine_zone_forecast(office)
    except NWSError as exc:
        return str(exc)

    zone_data = zones.get(zone_id)
    if response_format == "json":
        return {
            "zone_id": zone_id,
            "office": office,
            "product_type": product_type,
            "forecast": zone_data,
        }
    return formatting.format_zone_forecast(zone_id, zone_data)


@mcp.tool()
async def marine_get_offshore_forecast(
    location: str,
    zones: list[str] | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Offshore Forecast (OFF) - the ~60nm-to-Gulf Stream/shelf-break tier, beyond coastal zones.

    Issued by the Ocean Prediction Center (or Alaska/Hawaii WFOs), not
    tied to a UGC marine zone_id - pass an OPC/WFO location code
    instead (e.g. "N01" for NE US waters). Call with an invalid code to
    get the full list of valid ones back in the error message, or see
    nws.OFFSHORE_LOCATIONS.

    "NT3" is NHC's SW/Tropical N Atlantic + Caribbean product, which
    (unlike OPC's) is split by UGC zone - pass zones=["AMZ051"] (Offshore
    Waters Leeward Islands, covers Antigua/Barbuda) to trim the ~50-zone
    product to just the area you're sailing. The registry lists each
    region's offshore location/zones (marine_list_registry).
    """
    try:
        areas = await nws.get_offshore_forecast(location)
    except NWSError as exc:
        return str(exc)
    if zones:
        filtered = nws.filter_offshore_areas(areas, zones)
        if not filtered:
            return (
                f"No areas matching {', '.join(zones)} in offshore product {location}. "
                f"Areas present: {', '.join(list(areas)[:60])}"
            )
        areas = filtered

    if response_format == "json":
        return {"location": location, "areas": areas}
    return formatting.format_offshore_forecast(location, areas)


@mcp.tool()
async def marine_get_high_seas_forecast(
    area: str,
    section: Literal["all", "opc", "nhc"] = "all",
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """High Seas Forecast (HSF) - ocean-basin-wide, beyond offshore waters.

    Issued by the Ocean Prediction Center/NHC for whole ocean basins
    (METAREAs), e.g. "AT1" (Atlantic), "EP1"/"EP2" (Eastern Pacific),
    "NP" (North Pacific) - see nws.HIGH_SEAS_AREAS for the full list.
    Returned as full text rather than split by area - these forecasts
    aren't zone-subdivided the way coastal/offshore ones are.

    AT1 contains two sections: OPC's North Atlantic (north of 31N) and
    NHC's tropical Atlantic/Caribbean (07N-31N). section="nhc" keeps only
    the latter (what matters for Caribbean sailing); "opc" the former.
    """
    try:
        text = await nws.get_high_seas_forecast(area)
    except NWSError as exc:
        return str(exc)
    text = nws.split_high_seas_text(text, section)

    if response_format == "json":
        return {"area": area, "text": text}
    return f"## High Seas Forecast - {area}\n\n{text}"


@mcp.tool()
async def marine_get_buoy_obs(
    station_id: str,
    hours: int = 3,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Latest NDBC buoy/station observations: wind, waves, pressure, temps.

    Times are shown in the zone of the region whose registry lists the
    station (e.g. AST for 42060/41043, Chicago for Lake Michigan); a
    station not in the registry is shown in UTC.
    """
    try:
        obs = await ndbc.get_buoy_obs(station_id, hours)
    except NDBCError as exc:
        return str(exc)

    tz = registry.timezone_for_region(registry.region_for_station(station_id))
    if response_format == "json":
        return {"station_id": station_id.upper(), "timezone": tz, "observations": obs}
    return formatting.format_buoy_obs(station_id.upper(), obs, tz)


@mcp.tool()
async def marine_get_point_forecast(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    hours: int = 48,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """NWS gridpoint hourly forecast for a coordinate or registry place.

    Includes waveHeight/wavePeriod/waveDirection when the gridpoint
    carries them, merged in from the raw gridpoint (not just the
    narrative hourly endpoint, which omits wave fields).

    Outside NWS coverage (e.g. Antigua) this automatically falls back to
    the Open-Meteo multi-model forecast instead of returning an error.
    """
    try:
        loc = _resolve_location(lat, lon, place)
    except RegistryError as exc:
        return str(exc)
    lat_r, lon_r, label, tz = loc["lat"], loc["lon"], loc["label"], loc["tz"]
    try:
        meta = await nws.get_point_metadata(lat_r, lon_r)
        office, grid_x, grid_y = meta["gridId"], meta["gridX"], meta["gridY"]
        periods = await nws.get_gridpoint_hourly_forecast(office, grid_x, grid_y)
        gridpoint = await nws.get_gridpoint(office, grid_x, grid_y)
    except OutsideNWSCoverage:
        try:
            rows, note = await _open_meteo_rows(lat_r, lon_r, hours)
        except OpenMeteoError as exc:
            return str(exc)
        if response_format == "json":
            return {"location": label, "source": "open-meteo (outside NWS coverage)", "rows": rows}
        return (
            "*Outside NWS gridpoint coverage - showing the Open-Meteo multi-model forecast instead.*\n\n"
            + formatting.format_model_forecast(label, rows, tz, marine_note=note)
        )
    except NWSError as exc:
        return str(exc)

    wave_series = None
    wave_prop = gridpoint.get("waveHeight") or {}
    if wave_prop.get("values"):
        wave_series = formatting.expand_grid_series(wave_prop["values"])

    merged = []
    for period in periods[:hours]:
        start = datetime.fromisoformat(period["startTime"])
        wave_ft = None
        if wave_series is not None:
            raw_m = formatting.value_at(wave_series, start)
            if raw_m is not None:
                wave_ft = round(raw_m * ndbc.M_TO_FEET, 1)
        merged.append(
            {
                "time": period["startTime"],
                "temperature_f": period.get("temperature"),
                "wind_speed": formatting.mph_string_to_knots(period.get("windSpeed", "")),
                "wind_direction": period.get("windDirection"),
                "short_forecast": period.get("shortForecast"),
                "wave_height_ft": wave_ft,
            }
        )

    if response_format == "json":
        return {"location": label, "periods": merged}
    return formatting.format_point_forecast(label, merged, tz)


@mcp.tool()
async def marine_get_forecast_discussion(
    office: str = "MKX",
    section: Literal["marine", "full"] = "marine",
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Latest Area Forecast Discussion (AFD); default extracts the .MARINE section."""
    try:
        text = await nws.get_forecast_discussion(office, section)
    except NWSError as exc:
        return str(exc)

    if response_format == "json":
        return {"office": office, "section": section, "text": text}
    return f"## {office} Area Forecast Discussion ({section})\n\n{text}"


@mcp.tool()
async def marine_get_model_forecast(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    models: list[str] | None = None,
    hours: int = 72,
    step_hours: int = 1,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Open-Meteo multi-model (ECMWF vs GFS) forecast, aligned by timestamp - works worldwide.

    Per model: sustained wind and gusts (kt). Also wind direction, cloud
    cover, rain chance, and seas split into combined / wind-wave / swell
    (height ft, period s, direction FROM) - the swell column is what
    tells a long-period northerly swell apart from local trade-wind chop.
    Times are shown in the place's region timezone. Use step_hours (e.g.
    3) to thin the table for long horizons (hours up to ~384).
    """
    try:
        loc = _resolve_location(lat, lon, place)
        wind = await openmeteo.get_model_forecast(loc["lat"], loc["lon"], models, hours)
    except (RegistryError, OpenMeteoError) as exc:
        return str(exc)
    marine_note = None
    try:
        marine = await openmeteo.get_marine_forecast(loc["lat"], loc["lon"], hours)
        if marine.get("no_data"):
            marine_note = marine["no_data_message"]
    except OpenMeteoError as exc:
        marine, marine_note = None, f"Marine (wave) data unavailable: {exc}"

    rows = scoring.merge_hourly(wind, marine)
    if response_format == "json":
        return {"location": loc["label"], "timezone": loc["tz"], "wind": wind, "marine": marine}
    return formatting.format_model_forecast(loc["label"], rows, loc["tz"], step_hours, marine_note)


@mcp.tool()
async def marine_get_sun_times(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    date: str | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Sunrise/solar transit/sunset, civil twilight, and moon data for one day.

    Source (USNO) only provides civil twilight, not nautical or
    astronomical - fine for harbor-approach visibility planning, not for
    celestial navigation sight timing. Also includes moonrise/set/transit
    and phase (useful for night-passage planning - a full moon vs. new
    moon night matters for visibility). Times are in the place's region
    timezone (e.g. America/Antigua for Antigua registry places). `date`
    defaults to today there; pass an ISO date (YYYY-MM-DD) otherwise.
    """
    try:
        loc = _resolve_location(lat, lon, place)
    except RegistryError as exc:
        return str(exc)
    lat_r, lon_r, label, tz = loc["lat"], loc["lon"], loc["label"], loc["tz"]

    if date:
        try:
            when = date_cls.fromisoformat(date)
        except ValueError:
            return f"Invalid date '{date}' - use ISO format (YYYY-MM-DD)."
    else:
        when = _today(tz)

    try:
        result = await usno.get_sun_times(lat_r, lon_r, when, tz)
    except USNOError as exc:
        return str(exc)

    if response_format == "json":
        return {"location": label, **result}
    return formatting.format_sun_times(label, result, tz)


@mcp.tool()
async def marine_get_tides(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    days: int = 2,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Tide predictions, water level, and/or current predictions near a coordinate/place.

    Works nationwide (NOAA CO-OPS). Automatically reports whichever data
    types have a station within 50nm: tide predictions (high/low) and
    current predictions (ebb/flood/slack) exist only on tidal coasts -
    confirmed zero of either on the Great Lakes; water level (observed)
    exists both on tidal coasts and the Great Lakes (IGLD datum there,
    MLLW elsewhere). Each section names its station and distance so you
    can judge relevance; a data type is omitted (not silently zeroed)
    if nothing is within range.
    """
    try:
        lat_r, lon_r, label = _resolve_coords(lat, lon, place)
    except RegistryError as exc:
        return str(exc)

    try:
        tide_stations = await coops.get_stations("tidepredictions")
        wl_stations = await coops.get_stations("waterlevels")
        curr_stations = await coops.get_stations("currentpredictions")
    except CoopsError as exc:
        return str(exc)

    def _nearby(stations):
        station = coops.find_nearest_station(stations, lat_r, lon_r)
        if station and station["_distance_nm"] <= coops.MAX_STATION_DISTANCE_NM:
            return station
        return None

    nearest_tide = _nearby(tide_stations)
    nearest_wl = _nearby(wl_stations)
    nearest_curr = _nearby(curr_stations)

    tide = water_level = currents = None
    try:
        if nearest_tide:
            predictions = await coops.get_tide_predictions(nearest_tide["id"], days)
            tide = {"station": nearest_tide, "predictions": predictions}
    except CoopsError as exc:
        tide = {"station": nearest_tide, "error": str(exc)}

    try:
        if nearest_wl:
            reading = await coops.get_water_level(nearest_wl["id"], nearest_wl["greatlakes"])
            water_level = {"station": nearest_wl, "reading": reading}
    except CoopsError as exc:
        water_level = {"station": nearest_wl, "error": str(exc)}

    try:
        if nearest_curr:
            predictions = await coops.get_current_predictions(nearest_curr["id"], days)
            currents = {"station": nearest_curr, "predictions": predictions}
    except CoopsError as exc:
        currents = {"station": nearest_curr, "error": str(exc)}

    if response_format == "json":
        return {"location": label, "tide": tide, "water_level": water_level, "currents": currents}
    return formatting.format_tides(label, tide, water_level, currents)


@mcp.tool()
async def marine_get_ice_conditions(
    lake: str | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Current Great Lakes ice cover percentage by lake (NOAA GLERL).

    Whole-lake daily percentages, not location-specific - useful for
    shoulder-season (roughly Dec-Apr) Great Lakes passage planning.
    Outside ice season this legitimately reads 0% everywhere - that's
    real data, not a missing-data failure. Optionally filter to one
    lake (Superior/Michigan/Huron/Erie/Ontario/St_Clair); omit for all
    lakes plus the basin-wide total.
    """
    try:
        result = await glerl_ice.get_latest_ice_cover()
    except GlerlIceError as exc:
        return str(exc)

    if lake:
        if lake not in result["lakes"]:
            return f"Unknown lake '{lake}'. Valid: {', '.join(glerl_ice.LAKES)}."
        result = {**result, "lakes": {lake: result["lakes"][lake]}}

    if response_format == "json":
        return result
    return formatting.format_ice_conditions(result)


@mcp.tool()
async def marine_get_aids_to_navigation(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    radius_nm: float = 10,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Charted aids to navigation (Light List) near a coordinate/place - what should be there.

    Source is NAVCEN's District 09 Light List (covers the whole Great
    Lakes, bulk-downloaded and cached ~6h, then filtered by radius here).
    Light ranges are converted from statute miles (the Light List's native
    unit) to nautical miles. For live "is it actually working" status use
    marine_get_navigation_discrepancies instead.
    """
    try:
        lat_r, lon_r, label = _resolve_coords(lat, lon, place)
    except RegistryError as exc:
        return str(exc)
    if not _in_great_lakes(lat_r, lon_r):
        return (
            f"{label} is outside the Great Lakes - this tool only loads the USCG "
            "District 09 (Great Lakes) Light List. Use the charter company's charts/"
            "cruising guide for aids elsewhere."
        )
    try:
        lights = await navcen.get_light_list("09")
    except NavcenError as exc:
        return str(exc)

    nearby = navcen.filter_by_radius(lights, lat_r, lon_r, radius_nm)

    if response_format == "json":
        return {"location": label, "radius_nm": radius_nm, "aids": nearby}
    return formatting.format_aids_to_navigation(label, radius_nm, nearby)


@mcp.tool()
async def marine_get_navigation_discrepancies(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    radius_nm: float = 10,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Live discrepancies, hazards, temporary changes, and marine events near a place.

    Answers "is that light actually working" rather than just what's
    charted - covers federal/private aid discrepancies (outages, damage),
    temporary changes (e.g. reduced light intensity), hazards to
    navigation, and marine events (regattas, fireworks). Source layers
    are nationwide (not district-scoped), bulk-downloaded and cached
    ~6h, then filtered by radius here.
    """
    try:
        lat_r, lon_r, label = _resolve_coords(lat, lon, place)
        layers = await navcen.get_discrepancies()
    except (RegistryError, NavcenError) as exc:
        return str(exc)

    filtered = {
        key: navcen.filter_by_radius(items, lat_r, lon_r, radius_nm)
        for key, items in layers.items()
    }

    if response_format == "json":
        return {"location": label, "radius_nm": radius_nm, "layers": filtered}
    return formatting.format_navigation_discrepancies(label, radius_nm, filtered)


@mcp.tool()
async def marine_search_coast_pilot(
    query: str,
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    radius_nm: float | None = None,
    volume: int | None = None,
    limit: int = 10,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Full-text search over the U.S. Coast Pilot (all 10 volumes, nationwide).

    Narrative harbor/facility/regulation information NOAA can't put on a
    chart - not weather or live conditions. Works anywhere in the US
    (Atlantic, Gulf, Great Lakes, Pacific, Alaska, Hawaii, Puerto Rico/USVI),
    not just registry places. `place`/`lat`+`lon` are optional - omit them
    for a pure nationwide text search; add radius_nm to additionally
    require a geographic match (drops results with no coordinate signal
    in the text, which is most paragraphs - prefer a specific query over
    a tight radius). Requires a one-time local index build first: run
    `python scripts/ingest_coast_pilot.py`.

    The `harbor_heading` shown on each result is best-effort, not
    authoritative - it's reliable in harbor-by-harbor chapters but can be
    wrong in the regulatory front-matter chapters (1-2) of every volume,
    or briefly thrown off by a mid-chapter regulatory callout (e.g. a
    protected-species notice). The paragraph `text` itself is always the
    real, correctly-matched content regardless of the heading label.
    """
    lat_r = lon_r = None
    label = None
    if place or (lat is not None and lon is not None):
        try:
            lat_r, lon_r, label = _resolve_coords(lat, lon, place)
        except RegistryError as exc:
            return str(exc)
    elif radius_nm is not None:
        return "radius_nm requires place or both lat and lon."

    try:
        results = coast_pilot.search(
            query, limit=limit, volume=volume, lat=lat_r, lon=lon_r, radius_nm=radius_nm
        )
    except CoastPilotError as exc:
        return str(exc)

    if response_format == "json":
        return {"query": query, "location": label, "results": results}
    return formatting.format_coast_pilot_search(query, results)


DEFAULT_CYCLONE_RADIUS_NM = 300


@mcp.tool()
async def marine_get_tropical_cyclones(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    radius_nm: float | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Active tropical cyclones (NHC - Atlantic and Eastern/Central Pacific only).

    Does NOT cover Western Pacific or Indian Ocean typhoons - different
    issuing agencies (JTWC etc.), out of scope. Omit place/lat/lon to
    list every active storm in NHC's basins; add a location to filter
    to storms within radius_nm (default 300nm - tropical cyclone hazards
    extend far beyond the storm center, so this is deliberately generous
    compared to the other radius-based tools). Each storm links to its
    full public advisory and forecast discussion for the real
    trackline/timing detail this summary doesn't include.
    """
    try:
        storms = await nhc.get_active_storms()
    except NHCError as exc:
        return str(exc)

    label = None
    if place or (lat is not None and lon is not None) or radius_nm is not None:
        try:
            lat_r, lon_r, label = _resolve_coords(lat, lon, place)
        except RegistryError as exc:
            return str(exc)
        radius = radius_nm if radius_nm is not None else DEFAULT_CYCLONE_RADIUS_NM
        filtered = []
        for s in storms:
            if s["lat"] is None or s["lon"] is None:
                continue
            distance = haversine_nm(lat_r, lon_r, s["lat"], s["lon"])
            if distance <= radius:
                filtered.append({**s, "_distance_nm": round(distance, 1)})
        storms = sorted(filtered, key=lambda s: s["_distance_nm"])

    if response_format == "json":
        return {"location": label, "storms": storms}
    return formatting.format_tropical_cyclones(label, storms)


GREAT_LAKES_BBOX = (41.0, 49.0, -93.0, -76.0)  # lat_min, lat_max, lon_min, lon_max
OPEN_METEO_MAX_HOURS = 384  # ~16 days, the longest Open-Meteo forecast horizon
DEFAULT_WINDOW_HOURS = (7, 17)  # local, used when sun times are unavailable


def _resolve_waypoint(waypoint: str) -> tuple[float, float, str, list[str]]:
    """A waypoint is either a registry place name or a "lat,lon" string.

    Returns (lat, lon, label, zones) - zones is [] for ad-hoc coordinates
    (no registry zone list to draw on), which the caller uses to decide
    whether zone-based alerts/forecast apply to this waypoint.
    """
    parts = waypoint.split(",")
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
            return lat, lon, waypoint, []
        except ValueError:
            pass
    port = registry.get_port(waypoint)
    return port["lat"], port["lon"], waypoint, list(port.get("zones", []))


def _waypoint_region(waypoint: str, lat: float, lon: float) -> str | None:
    try:
        return registry.get_port(waypoint)["region"]
    except RegistryError:
        return registry.region_for_coords(lat, lon)


def _passage_window(
    when: date_cls,
    tz: str,
    sun_moon: dict[str, Any] | None,
    start_hour: int | None,
    end_hour: int | None,
) -> tuple[datetime, datetime, str, datetime | None]:
    """(start, end, basis, anchored_by) in aware datetimes.

    Default window: sunrise to one hour before sunset (the Moorings/Sunsail
    "moored an hour before sunset" rule, a sensible default anywhere), else
    07:00-17:00 local. Explicit start_hour/end_hour (local) override.
    """
    zone = ZoneInfo(tz)
    events = (sun_moon or {}).get("events", {})
    anchored_by = None
    if "Set" in events:
        anchored_by = datetime.fromisoformat(events["Set"]) - timedelta(hours=1)
    if start_hour is not None or end_hour is not None:
        sh = start_hour if start_hour is not None else DEFAULT_WINDOW_HOURS[0]
        eh = end_hour if end_hour is not None else DEFAULT_WINDOW_HOURS[1]
        start = datetime(when.year, when.month, when.day, sh, tzinfo=zone)
        end = datetime(when.year, when.month, when.day, eh, tzinfo=zone)
        return start, end, f"{sh:02d}:00-{eh:02d}:00 local, as requested", anchored_by
    if "Rise" in events and anchored_by is not None:
        return (
            datetime.fromisoformat(events["Rise"]),
            anchored_by,
            "sunrise to 1 hr before sunset",
            anchored_by,
        )
    sh, eh = DEFAULT_WINDOW_HOURS
    return (
        datetime(when.year, when.month, when.day, sh, tzinfo=zone),
        datetime(when.year, when.month, when.day, eh, tzinfo=zone),
        f"{sh:02d}:00-{eh:02d}:00 local (sun times unavailable)",
        anchored_by,
    )


async def _window_conditions(
    lat: float, lon: float, start: datetime, end: datetime, thresholds: dict[str, Any] | None
) -> dict[str, Any]:
    """Open-Meteo conditions over [start, end], summarized and scored."""
    now = datetime.now(timezone.utc)
    if end < now:
        return {"conditions_note": "passage window is already in the past"}
    hours_needed = math.ceil((end - now).total_seconds() / 3600) + 1
    if hours_needed > OPEN_METEO_MAX_HOURS:
        return {
            "conditions_note": (
                "departure is beyond the ~16-day forecast horizon - use "
                "marine_get_climatology for what this week is typically like"
            )
        }
    try:
        rows, note = await _open_meteo_rows(lat, lon, max(hours_needed, 6))
    except OpenMeteoError as exc:
        return {"conditions_note": str(exc)}
    in_window = [r for r in rows if start <= datetime.fromisoformat(r["time"]) <= end]
    if not in_window:
        return {"conditions_note": "no forecast hours fell inside the passage window"}
    out = {
        "conditions": formatting.summarize_window(in_window),
        "score": scoring.score_rows(in_window, thresholds),
    }
    if note:
        out["marine_note"] = note
    return out


@mcp.tool()
async def marine_route_outlook(
    waypoints: list[str],
    depart_date: str | None = None,
    radius_nm: float = 10,
    window_start_hour: int | None = None,
    window_end_hour: int | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Composite go/no-go passage-planning briefing for a route - works in any registry region.

    Pass an ordered list of waypoints - registry place names (any region,
    e.g. "great_bird_island", "barbuda_low_bay"; qualify as
    "region:name" if ambiguous) and/or "lat,lon" strings. For the
    departure date's passage window (default sunrise to 1 hr before
    sunset, or window_start_hour/window_end_hour local) it gives, per
    waypoint: Open-Meteo conditions (wind range, gusts, seas, swell
    height/period/direction, cloud, rain chance) scored green/yellow/red
    against the region's thresholds (Kent's limits - see
    marine_list_registry), plus directional advisories (e.g. northerly
    swell at an exposed anchorage). Also: NWS alerts + coastal zone
    forecasts for US zones crossed, NWS point forecast (US waters),
    the region's offshore forecast zone (e.g. NHC AMZ051 for Antigua),
    tides for the departure date, USCG aids/discrepancies (US/Great Lakes
    only), tropical cyclones within 300nm, Great Lakes ice, and sun/moon.
    Times are shown in the route's region timezone.

    This assembles and scores the facts - it does not make the go/no-go
    call. Departures beyond ~16 days get no conditions; use
    marine_get_climatology instead.
    """
    resolved = []
    for wp in waypoints:
        try:
            lat, lon, label, zones = _resolve_waypoint(wp)
        except RegistryError as exc:
            return f"Couldn't resolve waypoint '{wp}': {exc}"
        resolved.append(
            {"lat": lat, "lon": lon, "label": label, "zones": zones,
             "region": _waypoint_region(wp, lat, lon)}
        )
    if not resolved:
        return "Provide at least one waypoint."

    region = next((wp["region"] for wp in resolved if wp["region"]), None)
    tz = registry.timezone_for_region(region)
    region_rec = registry.get_region(region) if region else {}
    thresholds = registry.thresholds_for_region(region)
    us_waters = region is None or bool(region_rec.get("nws_offices"))

    if depart_date:
        try:
            when = date_cls.fromisoformat(depart_date)
        except ValueError:
            return f"Invalid depart_date '{depart_date}' - use ISO format (YYYY-MM-DD)."
    else:
        when = _today(tz)

    sun_moon = None
    try:
        first = resolved[0]
        sun_moon = await usno.get_sun_times(first["lat"], first["lon"], when, tz)
    except USNOError:
        sun_moon = None
    win_start, win_end, basis, anchored_by = _passage_window(
        when, tz, sun_moon, window_start_hour, window_end_hour
    )

    unique_zones: list[str] = []
    for wp in resolved:
        for z in wp["zones"]:
            if z not in unique_zones:
                unique_zones.append(z)

    alerts_by_zone: dict[str, list] = {}
    zone_forecasts: dict[str, dict] = {}
    if unique_zones:
        try:
            features = await nws.get_active_alerts(unique_zones)
        except NWSError as exc:
            features = []
            alerts_by_zone["_error"] = str(exc)
        else:
            for f in features:
                for z in f["properties"].get("affectedZones", []):
                    zone_id = z.rsplit("/", 1)[-1]
                    alerts_by_zone.setdefault(zone_id, []).append(f)
        for zone_id in unique_zones:
            try:
                office = await nws.get_marine_zone_office(zone_id)
                _, zones_data = await nws.get_marine_zone_forecast(office)
                zone_forecasts[zone_id] = zones_data.get(zone_id)
            except NWSError as exc:
                zone_forecasts[zone_id] = {"error": str(exc)}

    offshore = None
    if region_rec.get("offshore"):
        off_cfg = region_rec["offshore"]
        try:
            areas = await nws.get_offshore_forecast(off_cfg["location"])
            offshore = {
                "location": off_cfg["location"],
                "areas": nws.filter_offshore_areas(areas, off_cfg.get("zones")),
            }
        except NWSError as exc:
            offshore = {"location": off_cfg["location"], "error": str(exc)}

    conditions = await asyncio.gather(
        *(_window_conditions(wp["lat"], wp["lon"], win_start, win_end, thresholds) for wp in resolved)
    )

    waypoint_details = []
    for wp, cond in zip(resolved, conditions):
        lat, lon, label = wp["lat"], wp["lon"], wp["label"]
        detail: dict[str, Any] = {"label": label, "lat": lat, "lon": lon, **cond}

        if us_waters:
            try:
                meta = await nws.get_point_metadata(lat, lon)
                periods = await nws.get_gridpoint_hourly_forecast(
                    meta["gridId"], meta["gridX"], meta["gridY"]
                )
                detail["forecast"] = [
                    {
                        "time": p["startTime"],
                        "temperature_f": p.get("temperature"),
                        "wind_speed": formatting.mph_string_to_knots(p.get("windSpeed", "")),
                        "wind_direction": p.get("windDirection"),
                        "short_forecast": p.get("shortForecast"),
                    }
                    for p in periods[:6]
                ]
            except OutsideNWSCoverage:
                pass  # expected offshore/outside the US; Open-Meteo conditions cover it
            except NWSError as exc:
                detail["forecast_error"] = str(exc)

            if _in_great_lakes(lat, lon):
                try:
                    lights = await navcen.get_light_list("09")
                    detail["aids_nearby"] = len(navcen.filter_by_radius(lights, lat, lon, radius_nm))
                except NavcenError:
                    detail["aids_nearby"] = None

            try:
                layers = await navcen.get_discrepancies()
                detail["discrepancies"] = {
                    key: navcen.filter_by_radius(items, lat, lon, radius_nm)
                    for key, items in layers.items()
                }
            except NavcenError as exc:
                detail["discrepancies_error"] = str(exc)

        try:
            tide_stations = await coops.get_stations("tidepredictions")
            wl_stations = await coops.get_stations("waterlevels")
            nearest_tide = coops.find_nearest_station(tide_stations, lat, lon)
            nearest_wl = coops.find_nearest_station(wl_stations, lat, lon)
            if nearest_tide and nearest_tide["_distance_nm"] <= coops.MAX_STATION_DISTANCE_NM:
                detail["tide"] = {
                    "station": nearest_tide,
                    "predictions": await coops.get_tide_predictions(
                        nearest_tide["id"], days=2, start=when
                    ),
                }
            if (
                when == _today(tz)
                and nearest_wl
                and nearest_wl["_distance_nm"] <= coops.MAX_STATION_DISTANCE_NM
            ):
                detail["water_level"] = {
                    "station": nearest_wl,
                    "reading": await coops.get_water_level(nearest_wl["id"], nearest_wl["greatlakes"]),
                }
        except CoopsError as exc:
            detail["tide_error"] = str(exc)

        waypoint_details.append(detail)

    try:
        all_storms = await nhc.get_active_storms()
        storms = []
        seen_ids = set()
        for wp in resolved:
            for s in all_storms:
                if s["id"] in seen_ids or s["lat"] is None or s["lon"] is None:
                    continue
                distance = haversine_nm(wp["lat"], wp["lon"], s["lat"], s["lon"])
                if distance <= DEFAULT_CYCLONE_RADIUS_NM:
                    seen_ids.add(s["id"])
                    storms.append({**s, "_distance_nm": round(distance, 1)})
    except NHCError:
        storms = []

    ice = None
    if any(_in_great_lakes(wp["lat"], wp["lon"]) for wp in resolved):
        try:
            ice = await glerl_ice.get_latest_ice_cover()
        except GlerlIceError:
            ice = None

    result = {
        "waypoints": [wp["label"] for wp in resolved],
        "depart_date": when.isoformat(),
        "today": _today(tz).isoformat(),
        "region": region,
        "timezone": tz,
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat(), "basis": basis},
        "must_be_anchored_by": anchored_by.isoformat() if anchored_by else None,
        "thresholds_status": (thresholds or {}).get("status"),
        "zones": unique_zones,
        "alerts_by_zone": alerts_by_zone,
        "zone_forecasts": zone_forecasts,
        "offshore": offshore,
        "waypoint_details": waypoint_details,
        "tropical_cyclones": storms,
        "ice": ice,
        "sun_moon": sun_moon,
    }

    if response_format == "json":
        return result
    return formatting.format_route_outlook(result)


@mcp.tool()
async def marine_get_climatology(
    start_mmdd: str,
    end_mmdd: str,
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    years: int = 10,
    day_start_hour: int = 7,
    day_end_hour: int = 17,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """What a calendar window is typically like at a place, from past years' data.

    For each of the last `years` complete years, pulls the same dates
    (e.g. start_mmdd="01-09", end_mmdd="01-16") from ERA5 reanalysis
    (wind, gusts, direction, cloud) and Open-Meteo marine history (seas,
    swell height/period/direction), keeps daylight sailing hours
    (day_start_hour-day_end_hour, region local time), and scores each day
    green/yellow/red against the region's thresholds (Kent's limits).
    Returns the share of green/yellow/red days, the distribution of daily
    maximum wind/gust/seas/swell/cloud, directional-advisory days (e.g.
    northerly swell), and a per-year table.

    For planning months ahead of any forecast - "how often is the channel
    inside our limits that week" - not a prediction of this year. ERA5 is
    ~25 km and smooths squalls; treat it as the background trade-wind
    regime. Windows may wrap the new year (e.g. "12-28" to "01-04").
    """
    try:
        loc = _resolve_location(lat, lon, place)
    except RegistryError as exc:
        return str(exc)
    if not (0 <= day_start_hour <= 23 and 0 <= day_end_hour <= 23 and day_start_hour <= day_end_hour):
        return "day_start_hour/day_end_hour must be 0-23 with start <= end."
    thresholds = registry.thresholds_for_region(loc["region"])
    try:
        result = await climatology.get_climatology(
            loc["lat"], loc["lon"], loc["tz"], start_mmdd, end_mmdd, years,
            thresholds, day_start_hour, day_end_hour,
        )
    except (ClimatologyError, OpenMeteoError) as exc:
        return str(exc)

    if response_format == "json":
        return {"location": loc["label"], "region": loc["region"], **result}
    return formatting.format_climatology(loc["label"], result)


@mcp.tool()
async def marine_list_registry(
    region: str | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Dump known ports/zones/stations/routes so names can be discovered."""
    try:
        regions = registry.dump_registry(region)
    except RegistryError as exc:
        return str(exc)

    if response_format == "json":
        return regions
    return formatting.format_registry(regions)


@mcp.tool()
async def marine_get_metservice_forecast(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    hours: int = 48,
    step_hours: int = 1,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """MetService/MetOcean Point Forecast - an independent second source to Open-Meteo.

    Wind and gusts (kt), wind direction, seas split into combined /
    wind-wave (<8 s) / swell (>8 s) with periods and directions (FROM),
    cloud cover and rain rate. MetService picks the best model covering
    the point for each variable (it includes ECMWF and GFS) - the reply
    names the models when the API says. Times in the place's region zone.
    Costs ~12 API units per call regardless of hours (units = variables x
    points). Needs METSERVICE_API_KEY; returns a clear message if unset.
    Up to ~240 hours ahead; use step_hours (e.g. 3) to thin long tables.
    """
    try:
        loc = _resolve_location(lat, lon, place)
        result = await metservice.get_point_forecast(loc["lat"], loc["lon"], hours, step_hours)
    except (RegistryError, MetServiceError) as exc:
        return str(exc)
    if not result.get("configured"):
        return result["message"]
    if response_format == "json":
        return {"location": loc["label"], "timezone": loc["tz"], **result}
    return formatting.format_metservice_forecast(loc["label"], result, loc["tz"])


@mcp.tool()
async def marine_metservice_check(response_format: ResponseFormat = "markdown") -> str | dict:
    """Check the MetService API key works and list the models it can see.

    Use after adding or changing METSERVICE_API_KEY. Doesn't fetch a forecast.
    """
    try:
        result = await metservice.get_models()
    except MetServiceError as exc:
        return str(exc)
    if not result.get("configured"):
        return result["message"]
    if response_format == "json":
        return result
    return formatting.format_metservice_models(result)


@mcp.resource("registry://ports/{name}")
def port_resource(name: str) -> dict:
    """A single registry port record (lat/lon, zones, stations, notes)."""
    return registry.get_port(name)
