"""Markdown formatting helpers for tool outputs.

Every tool supports response_format "markdown" (compact, human units,
local time) or "json" (structured passthrough). This module only
covers the markdown side - json mode returns source data almost as-is.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Chicago")

_DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def to_local(iso_time: str) -> str:
    dt = datetime.fromisoformat(iso_time)
    return dt.astimezone(LOCAL_TZ).strftime("%a %Y-%m-%d %I:%M %p %Z")


def format_alerts(zone_ids: list[str], features: list[dict[str, Any]]) -> str:
    if not features:
        return f"No active alerts for {', '.join(zone_ids)}."

    lines = [f"## Active marine alerts for {', '.join(zone_ids)}\n"]
    for feature in features:
        p = feature["properties"]
        effective = to_local(p["effective"]) if p.get("effective") else "unknown"
        expires = to_local(p["expires"]) if p.get("expires") else "unknown"
        lines.append(f"### {p.get('event', 'Alert')} ({p.get('severity', 'Unknown')})")
        lines.append(f"- **Zones:** {p.get('areaDesc', 'n/a')}")
        lines.append(f"- **Effective:** {effective}")
        lines.append(f"- **Expires:** {expires}")
        lines.append(f"- {p.get('headline', '')}")
        lines.append("")
    return "\n".join(lines)


def format_zone_forecast(zone_id: str, zone_data: dict[str, Any] | None) -> str:
    if zone_data is None:
        return (
            f"No nearshore forecast text found for zone {zone_id} in the latest "
            "product (it may not be covered by this office's NSH product)."
        )
    lines = [
        f"## {zone_id} - {zone_data['area']}",
        f"*Issued: {zone_data['issued']}*\n",
    ]
    for period, text in zone_data["periods"].items():
        lines.append(f"**{period}:** {text}")
    return "\n".join(lines)


def format_buoy_obs(station_id: str, obs: list[dict[str, Any]]) -> str:
    if not obs:
        return (
            f"No recent observations for station {station_id}. It may not be "
            "reporting right now - check marine_list_registry notes for a "
            "nearby alternate, or try marine_get_point_forecast instead."
        )
    lines = [f"## {station_id} - latest observations\n"]
    for o in obs:
        parts = [f"**{o['time_local']}**"]
        if o["wind_speed_kt"] is not None:
            wind_dir = o.get("wind_dir_deg")
            wind_dir_str = str(int(wind_dir)) if wind_dir is not None else "?"
            wind = f"Wind {wind_dir_str}° at {o['wind_speed_kt']} kt"
            if o.get("wind_gust_kt") is not None:
                wind += f" (gust {o['wind_gust_kt']} kt)"
            parts.append(wind)
        if o["wave_height_ft"] is not None:
            wave = f"Waves {o['wave_height_ft']} ft"
            if o.get("dominant_wave_period_s") is not None:
                wave += f" @ {o['dominant_wave_period_s']}s"
            parts.append(wave)
        if o["pressure_hpa"] is not None:
            parts.append(f"Pressure {o['pressure_hpa']} hPa")
        if o["air_temp_f"] is not None:
            parts.append(f"Air {o['air_temp_f']}°F")
        if o["water_temp_f"] is not None:
            parts.append(f"Water {o['water_temp_f']}°F")
        lines.append(" - ".join(parts))
    return "\n".join(lines)


def _parse_duration_hours(duration: str) -> float:
    match = _DURATION_RE.match(duration)
    if not match:
        return 0.0
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return days * 24 + hours + minutes / 60


def expand_grid_series(entries: list[dict[str, Any]]) -> list[tuple[datetime, datetime, Any]]:
    """Expand NWS gridpoint validTime interval entries into (start, end, value)."""
    spans = []
    for entry in entries:
        start_str, duration = entry["validTime"].split("/")
        start = datetime.fromisoformat(start_str)
        end = start + timedelta(hours=_parse_duration_hours(duration))
        spans.append((start, end, entry["value"]))
    return spans


def value_at(spans: list[tuple[datetime, datetime, Any]], when: datetime) -> Any:
    for start, end, value in spans:
        if start <= when < end:
            return value
    return None


_MPH_RE = re.compile(r"(\d+)\s*mph")
MPH_TO_KT = 0.868976


def mph_string_to_knots(text: str) -> str:
    """Convert an NWS windSpeed string ("10 to 15 mph") to knots ("9 to 13 kt")."""
    if not text:
        return text

    def _convert(match: re.Match) -> str:
        return str(round(int(match.group(1)) * MPH_TO_KT))

    converted = _MPH_RE.sub(_convert, text)
    return converted.replace("mph", "kt")


def format_point_forecast(place_or_coords: str, periods: list[dict[str, Any]]) -> str:
    if not periods:
        return f"No forecast periods returned for {place_or_coords}."
    lines = [f"## Point forecast - {place_or_coords}\n"]
    for p in periods:
        when = to_local(p["time"])
        bits = [f"**{when}**", f"{p.get('temperature_f', '?')}°F"]
        if p.get("wind_speed"):
            bits.append(f"wind {p.get('wind_direction', '')} {p['wind_speed']}".strip())
        if p.get("wave_height_ft") is not None:
            bits.append(f"waves {p['wave_height_ft']} ft")
        if p.get("short_forecast"):
            bits.append(p["short_forecast"])
        lines.append(" - ".join(bits))
    return "\n".join(lines)


def format_model_forecast(place_or_coords: str, result: dict[str, Any]) -> str:
    timestamps = result["timestamps"]
    if not timestamps:
        return f"No model forecast data returned for {place_or_coords}."
    models = list(result["models"])
    lines = [f"## Multi-model wind forecast - {place_or_coords}", f"Models: {', '.join(models)}\n"]
    lines.append("| Time (local) | " + " | ".join(f"{m} wind (kt)" for m in models) + " |")
    lines.append("|---" * (len(models) + 1) + "|")
    for i, ts in enumerate(timestamps):
        when = to_local(ts + ":00" if len(ts) == 16 else ts)
        row = [when]
        for m in models:
            values = result["models"][m].get("wind_speed_10m", [])
            row.append(str(values[i]) if i < len(values) else "-")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def format_registry(regions: dict[str, Any]) -> str:
    lines = ["## Registry\n"]
    for region_key, region in regions.items():
        lines.append(f"### {region_key} ({region['timezone']})")
        lines.append(f"NWS offices: {', '.join(region['nws_offices'])}\n")
        lines.append("**Ports:**")
        for name, port in region["ports"].items():
            lines.append(
                f"- `{name}` ({port['lat']}, {port['lon']}) - zones: "
                f"{', '.join(port['zones'])} - stations: {', '.join(port['stations']) or 'none'}"
            )
        lines.append("\n**Routes:**")
        for name, route in region["routes"].items():
            lines.append(f"- `{name}`: {' -> '.join(route['legs'])} ({route['nm']} nm)")
    return "\n".join(lines)
