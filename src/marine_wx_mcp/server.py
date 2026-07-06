"""FastMCP server init and tool registration for marine_wx_mcp.

Tool implementations delegate to marine_wx_mcp.sources and
marine_wx_mcp.registry; this module wires them up as MCP tools and
handles response_format (markdown vs json) plus turning source
exceptions into actionable string results instead of crashes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from mcp.server.fastmcp import FastMCP

from marine_wx_mcp import formatting, registry
from marine_wx_mcp.registry import RegistryError
from marine_wx_mcp.sources import ndbc, nws, openmeteo, windy
from marine_wx_mcp.sources.ndbc import NDBCError
from marine_wx_mcp.sources.nws import NWSError
from marine_wx_mcp.sources.openmeteo import OpenMeteoError

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
    """Nearshore forecast (wind/waves/weather by period) for one LMZ zone.

    Backed by the Nearshore Marine Forecast (NSH) text product, not the
    NWS zone-forecast JSON endpoint (which 404s for marine zones).
    """
    try:
        office = registry.office_for_zone(zone_id)
        zones = await nws.get_nearshore_forecast(office)
    except (RegistryError, NWSError) as exc:
        return str(exc)

    zone_data = zones.get(zone_id)
    if response_format == "json":
        return {"zone_id": zone_id, "office": office, "forecast": zone_data}
    return formatting.format_zone_forecast(zone_id, zone_data)


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
