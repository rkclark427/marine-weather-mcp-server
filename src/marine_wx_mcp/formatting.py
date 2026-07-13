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

from marine_wx_mcp.sources.navcen import light_range_nm

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
            f"No marine zone forecast text found for zone {zone_id} in the latest "
            "product (it may not be covered by this office's NSH/CWF product)."
        )
    lines = [
        f"## {zone_id} - {zone_data['area']}",
        f"*Issued: {zone_data['issued']}*\n",
    ]
    for period, text in zone_data["periods"].items():
        lines.append(f"**{period}:** {text}")
    return "\n".join(lines)


def format_offshore_forecast(location: str, areas: dict[str, dict[str, Any]]) -> str:
    if not areas:
        return f"No offshore forecast areas found for location {location}."
    lines = [f"## Offshore Forecast - {location}\n"]
    for name, data in areas.items():
        lines.append(f"### {name}")
        for period, text in data["periods"].items():
            lines.append(f"**{period}:** {text}")
        lines.append("")
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


_SUN_EVENT_ORDER = [
    "Begin Civil Twilight",
    "Rise",
    "Upper Transit",
    "Set",
    "End Civil Twilight",
]


def format_sun_times(place_or_coords: str, result: dict[str, Any]) -> str:
    events = result["events"]
    if not events:
        return f"No sun/twilight data returned for {place_or_coords} on {result['date']}."

    lines = [f"## Sun & civil twilight - {place_or_coords} ({result['date']})\n"]
    for phen in _SUN_EVENT_ORDER:
        if phen in events:
            lines.append(f"- **{phen}:** {to_local(events[phen])}")
    lines.append(
        "\n*Civil twilight only - not nautical or astronomical twilight; "
        "not suitable for celestial navigation sight timing.*"
    )
    return "\n".join(lines)


def _epoch_ms_to_local(epoch_ms: int | None) -> str:
    if epoch_ms is None:
        return "unknown"
    return datetime.fromtimestamp(epoch_ms / 1000, tz=LOCAL_TZ).strftime("%a %Y-%m-%d %I:%M %p %Z")


def format_aids_to_navigation(place_or_coords: str, radius_nm: float, aids: list[dict[str, Any]]) -> str:
    if not aids:
        return f"No charted aids to navigation within {radius_nm}nm of {place_or_coords}."

    lines = [f"## Aids to navigation within {radius_nm}nm of {place_or_coords}\n"]
    for aid in aids:
        p = aid["properties"]
        bits = [f"**{p.get('NAME', 'Unnamed')}** ({aid['_distance_nm']}nm)"]
        if p.get("LIGHT_CHAR"):
            bits.append(p["LIGHT_CHAR"])
        rng = light_range_nm(p)
        if rng is not None:
            bits.append(f"range {rng}nm")
        if p.get("COLOR"):
            bits.append(f"color {p['COLOR']}")
        if p.get("STRUCTURE_TYPE"):
            bits.append(p["STRUCTURE_TYPE"])
        if p.get("SEASON_FROM") or p.get("SEASON_TO"):
            bits.append(f"seasonal ({p.get('SEASON_FROM', '?')}-{p.get('SEASON_TO', '?')})")
        if p.get("INACTIVE") == "1":
            bits.append("**INACTIVE**")
        line = " - ".join(bits)
        if p.get("REMARK"):
            line += f"\n  *{p['REMARK']}*"
        lines.append(line)
    return "\n".join(lines)


_DISCREPANCY_LAYER_LABELS = {
    "federal_aid": "Federal aid discrepancies",
    "private_aid": "Private aid discrepancies",
    "temporary_change": "Temporary changes",
    "hazard": "Hazards to navigation",
    "marine_event": "Marine events",
}


def format_navigation_discrepancies(
    place_or_coords: str, radius_nm: float, layers: dict[str, list[dict[str, Any]]]
) -> str:
    total = sum(len(v) for v in layers.values())
    if total == 0:
        return (
            f"No navigation discrepancies, hazards, or marine events reported "
            f"within {radius_nm}nm of {place_or_coords}."
        )

    lines = [f"## Navigation discrepancies/hazards within {radius_nm}nm of {place_or_coords}\n"]
    for key in ("federal_aid", "private_aid", "temporary_change", "hazard", "marine_event"):
        items = layers.get(key, [])
        if not items:
            continue
        lines.append(f"### {_DISCREPANCY_LAYER_LABELS[key]}")
        for item in items:
            p = item["properties"]
            if key in ("hazard", "marine_event"):
                until = _epoch_ms_to_local(p.get("END_DATE"))
                lines.append(
                    f"- **{p.get('TYPE', p.get('TITLE', 'Untitled'))}** "
                    f"({item['_distance_nm']}nm) - {p.get('WATERWAY_NAME', '')} - "
                    f"status: {p.get('STATUS', 'unknown')} - through {until}"
                )
            else:
                status = p.get("DISCREP_STATUS") or p.get("TC_STATUS") or "unknown"
                reported = _epoch_ms_to_local(p.get("CREATE_DATE"))
                lines.append(
                    f"- **{p.get('NAME', 'Unnamed')}** ({item['_distance_nm']}nm) - "
                    f"{status} - {p.get('WATERWAY_NAME', '')} - reported {reported}"
                )
        lines.append("")
    return "\n".join(lines)


def format_coast_pilot_search(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return (
            f'No Coast Pilot results for "{query}". Try a different term, or run '
            "scripts/ingest_coast_pilot.py if the index hasn't been built yet."
        )

    lines = [f'## Coast Pilot search: "{query}"\n']
    for r in results:
        place = r["harbor_heading"] or "(unattributed)"
        if r["subsection_heading"]:
            place += f" - {r['subsection_heading']}"
        cite = f"Vol. {r['volume']}"
        if r.get("chapter") is not None:
            cite += f", Ch. {r['chapter']}"
        cite += f" ({r['paragraph_number']})"
        distance = f" - {r['_distance_nm']}nm away" if "_distance_nm" in r else ""
        lines.append(f"**{place}** [{cite}]{distance}")
        lines.append(r["text"])
        lines.append("")
    return "\n".join(lines)
