"""FastMCP server init and tool registration for marine_wx_mcp.

Tool implementations delegate to marine_wx_mcp.sources and
marine_wx_mcp.registry; this module wires them up as MCP tools and
handles response_format (markdown vs json) plus turning source
exceptions into actionable string results instead of crashes.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from typing import Literal

from mcp.server.fastmcp import FastMCP

from marine_wx_mcp import formatting, registry
from marine_wx_mcp.registry import RegistryError
from marine_wx_mcp.sources import coast_pilot, navcen, ndbc, nws, openmeteo, usno, windy
from marine_wx_mcp.sources.coast_pilot import CoastPilotError
from marine_wx_mcp.sources.navcen import NavcenError
from marine_wx_mcp.sources.ndbc import NDBCError
from marine_wx_mcp.sources.nws import NWSError
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


def _resolve_coords(
    lat: float | None, lon: float | None, place: str | None
) -> tuple[float, float, str]:
    if place:
        port = registry.get_port(place)
        return port["lat"], port["lon"], place
    if lat is not None and lon is not None:
        return lat, lon, f"{lat},{lon}"
    raise RegistryError("Provide either place, or both lat and lon.")


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
    return formatting.format_alerts(zones, features)


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
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Offshore Forecast (OFF) - the ~60nm-to-Gulf Stream/shelf-break tier, beyond coastal zones.

    Issued by the Ocean Prediction Center (or Alaska/Hawaii WFOs), not
    tied to a UGC marine zone_id - pass an OPC/WFO location code
    instead (e.g. "N01" for NE US waters). Call with an invalid code to
    get the full list of valid ones back in the error message, or see
    nws.OFFSHORE_LOCATIONS.
    """
    try:
        areas = await nws.get_offshore_forecast(location)
    except NWSError as exc:
        return str(exc)

    if response_format == "json":
        return {"location": location, "areas": areas}
    return formatting.format_offshore_forecast(location, areas)


@mcp.tool()
async def marine_get_high_seas_forecast(
    area: str,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """High Seas Forecast (HSF) - ocean-basin-wide, beyond offshore waters.

    Issued by the Ocean Prediction Center/NHC for whole ocean basins
    (METAREAs), e.g. "AT1" (Atlantic), "EP1"/"EP2" (Eastern Pacific),
    "NP" (North Pacific) - see nws.HIGH_SEAS_AREAS for the full list.
    Returned as full text rather than split by area - these forecasts
    aren't zone-subdivided the way coastal/offshore ones are.
    """
    try:
        text = await nws.get_high_seas_forecast(area)
    except NWSError as exc:
        return str(exc)

    if response_format == "json":
        return {"area": area, "text": text}
    return f"## High Seas Forecast - {area}\n\n{text}"


@mcp.tool()
async def marine_get_buoy_obs(
    station_id: str,
    hours: int = 3,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Latest NDBC buoy/station observations: wind, waves, pressure, temps."""
    try:
        obs = await ndbc.get_buoy_obs(station_id, hours)
    except NDBCError as exc:
        return str(exc)

    if response_format == "json":
        return {"station_id": station_id.upper(), "observations": obs}
    return formatting.format_buoy_obs(station_id.upper(), obs)


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
    """
    try:
        lat_r, lon_r, label = _resolve_coords(lat, lon, place)
        meta = await nws.get_point_metadata(lat_r, lon_r)
        office, grid_x, grid_y = meta["gridId"], meta["gridX"], meta["gridY"]
        periods = await nws.get_gridpoint_hourly_forecast(office, grid_x, grid_y)
        gridpoint = await nws.get_gridpoint(office, grid_x, grid_y)
    except (RegistryError, NWSError) as exc:
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
    return formatting.format_point_forecast(label, merged)


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
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Open-Meteo multi-model (ECMWF vs GFS) wind comparison, aligned by timestamp.

    Also includes wave height/period/direction when available (verified
    live for Lake Michigan - see sources/openmeteo.py).
    """
    try:
        lat_r, lon_r, label = _resolve_coords(lat, lon, place)
        wind = await openmeteo.get_model_forecast(lat_r, lon_r, models, hours)
        marine = await openmeteo.get_marine_forecast(lat_r, lon_r, hours)
    except (RegistryError, OpenMeteoError) as exc:
        return str(exc)

    if response_format == "json":
        return {"location": label, "wind": wind, "marine": marine}

    md = formatting.format_model_forecast(label, wind)
    if not marine["no_data"]:
        md += "\n\n**Waves (Open-Meteo):** " + ", ".join(
            f"{h}ft" for h in marine["wave_height"][:6] if h is not None
        )
    else:
        md += f"\n\n*{marine['no_data_message']}*"
    return md


@mcp.tool()
async def marine_get_sun_times(
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    date: str | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Sunrise/solar transit/sunset and civil twilight begin/end for one day.

    Source (USNO) only provides civil twilight, not nautical or
    astronomical - fine for harbor-approach visibility planning, not for
    celestial navigation sight timing. `date` defaults to today
    (America/Chicago) if omitted; pass an ISO date (YYYY-MM-DD) otherwise.
    """
    try:
        lat_r, lon_r, label = _resolve_coords(lat, lon, place)
    except RegistryError as exc:
        return str(exc)

    if date:
        try:
            when = date_cls.fromisoformat(date)
        except ValueError:
            return f"Invalid date '{date}' - use ISO format (YYYY-MM-DD)."
    else:
        when = datetime.now(formatting.LOCAL_TZ).date()

    try:
        result = await usno.get_sun_times(lat_r, lon_r, when)
    except USNOError as exc:
        return str(exc)

    if response_format == "json":
        return {"location": label, **result}
    return formatting.format_sun_times(label, result)


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
        lights = await navcen.get_light_list("09")
    except (RegistryError, NavcenError) as exc:
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
async def marine_get_windy_forecast(
    lat: float,
    lon: float,
    model: str = "gfs",
    parameters: list[str] | None = None,
    response_format: ResponseFormat = "markdown",
) -> str | dict:
    """Windy Point Forecast (v2 roadmap item). Returns a clear message if unconfigured.

    Excludes ECMWF (Windy licensing) even when configured - use
    marine_get_model_forecast for ECMWF/GFS comparison instead.
    """
    from marine_wx_mcp.sources.windy import WindyError

    try:
        result = await windy.get_point_forecast(lat, lon, model, parameters)
    except WindyError as exc:
        return str(exc)

    if response_format == "json":
        return result
    if not result.get("configured"):
        return result["message"]
    return f"## Windy point forecast ({model})\n\n```\n{result}\n```"


@mcp.resource("registry://ports/{name}")
def port_resource(name: str) -> dict:
    """A single registry port record (lat/lon, zones, stations, notes)."""
    return registry.get_port(name)
