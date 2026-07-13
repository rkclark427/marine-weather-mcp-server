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

from marine_wx_mcp.sources.coops import MAX_STATION_DISTANCE_NM
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


_MOON_EVENT_ORDER = ["Rise", "Upper Transit", "Set"]


def format_sun_times(place_or_coords: str, result: dict[str, Any]) -> str:
    events = result["events"]
    if not events:
        return f"No sun/twilight data returned for {place_or_coords} on {result['date']}."

    lines = [f"## Sun & moon - {place_or_coords} ({result['date']})\n", "### Sun"]
    for phen in _SUN_EVENT_ORDER:
        if phen in events:
            lines.append(f"- **{phen}:** {to_local(events[phen])}")
    lines.append(
        "\n*Civil twilight only - not nautical or astronomical twilight; "
        "not suitable for celestial navigation sight timing.*"
    )

    moon_events = result.get("moon_events") or {}
    moon_phase = result.get("moon_phase") or {}
    lines.append("\n### Moon")
    if moon_phase.get("current_phase"):
        line = f"- **Phase:** {moon_phase['current_phase']}"
        if moon_phase.get("fraction_illuminated"):
            line += f" ({moon_phase['fraction_illuminated']} illuminated)"
        lines.append(line)
    closest = moon_phase.get("closest_phase")
    if closest and closest.get("time"):
        lines.append(f"- **Next {closest['phase']}:** {to_local(closest['time'])}")
    for phen in _MOON_EVENT_ORDER:
        if phen in moon_events:
            lines.append(f"- **Moon {phen}:** {to_local(moon_events[phen])}")
    if not moon_events:
        lines.append("- *No moonrise or moonset today (can happen - the moon rises "
                      "~50 min later each day).*")

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


def format_tides(
    place_or_coords: str,
    tide: dict[str, Any] | None,
    water_level: dict[str, Any] | None,
    currents: dict[str, Any] | None,
) -> str:
    if not (tide or water_level or currents):
        return (
            f"No tide, water level, or current station within "
            f"{MAX_STATION_DISTANCE_NM}nm of {place_or_coords}."
        )

    lines = [f"## Tides & currents - {place_or_coords}\n"]

    if tide:
        st = tide["station"]
        lines.append(f"### Tide predictions - {st['name']} ({st['_distance_nm']}nm), MLLW datum")
        if "error" in tide:
            lines.append(f"*{tide['error']}*")
        else:
            for p in tide["predictions"]:
                kind = "High" if p["type"] == "H" else "Low"
                lines.append(f"- **{kind}** {p['t']}: {p['v']} ft")
        lines.append("")

    if water_level:
        st = water_level["station"]
        datum = "IGLD" if st.get("greatlakes") else "MLLW"
        lines.append(f"### Water level - {st['name']} ({st['_distance_nm']}nm), {datum} datum")
        reading = water_level.get("reading")
        if "error" in water_level:
            lines.append(f"*{water_level['error']}*")
        elif reading:
            lines.append(f"- **{reading['t']}:** {reading['v']} ft")
        else:
            lines.append("- No recent reading available.")
        lines.append("")

    if currents:
        st = currents["station"]
        lines.append(f"### Current predictions - {st['name']} ({st['_distance_nm']}nm)")
        if "error" in currents:
            lines.append(f"*{currents['error']}*")
        else:
            for p in currents["predictions"]:
                kind = p["Type"].capitalize()
                velocity = p.get("Velocity_Major")
                vel_str = f", {abs(velocity)} kt" if velocity else ""
                lines.append(f"- **{kind}** {p['Time']}{vel_str}")
        lines.append("")

    return "\n".join(lines)


def format_tropical_cyclones(place_or_coords: str | None, storms: list[dict[str, Any]]) -> str:
    if not storms:
        scope = f" within range of {place_or_coords}" if place_or_coords else ""
        return (
            f"No active tropical cyclones{scope} in NHC's area of responsibility "
            "(Atlantic, Eastern/Central Pacific). Doesn't cover Western Pacific or "
            "Indian Ocean typhoons - different issuing agencies."
        )

    title = f"Tropical cyclones near {place_or_coords}" if place_or_coords else "Active tropical cyclones"
    lines = [f"## {title}\n"]
    for s in storms:
        distance = f" - {s['_distance_nm']}nm away" if "_distance_nm" in s else ""
        lines.append(f"### {s['name']} - {s['classification_name']}{distance}")
        lines.append(
            f"- **Winds:** {s['wind_kt']} kt, **Pressure:** {s['pressure_mb']} mb"
        )
        lines.append(f"- **Position:** {s['lat']}, {s['lon']}")
        if s.get("movement_dir_deg") is not None:
            lines.append(
                f"- **Movement:** {s['movement_dir_deg']}° at {s['movement_speed_mph']} mph"
            )
        lines.append(f"- **Last update:** {s['last_update']}")
        if s.get("advisory_url"):
            lines.append(f"- [Public advisory]({s['advisory_url']})")
        if s.get("discussion_url"):
            lines.append(f"- [Forecast discussion]({s['discussion_url']})")
        lines.append("")
    return "\n".join(lines)


def format_ice_conditions(result: dict[str, Any]) -> str:
    date_str = result["date"].split("T")[0]
    lines = [f"## Great Lakes ice cover - {date_str}\n"]
    for lake, pct in result["lakes"].items():
        lines.append(f"- **{lake.replace('_', ' ')}:** {pct}%")
    if "total" in result and len(result["lakes"]) > 1:
        lines.append(f"- **Great Lakes total:** {result['total']}%")
    if all(v == 0 for v in result["lakes"].values()):
        lines.append("\n*0% across the board likely means it's outside ice season "
                      "(roughly Dec-Apr), not missing data.*")
    return "\n".join(lines)


def format_route_outlook(result: dict[str, Any]) -> str:
    lines = [
        f"# Route outlook: {' -> '.join(result['waypoints'])}",
        f"*Departing {result['depart_date']}*\n",
    ]

    lines.append("## Alerts")
    alerts_by_zone = result["alerts_by_zone"]
    if alerts_by_zone.get("_error"):
        lines.append(f"*{alerts_by_zone['_error']}*")
    else:
        any_alert = False
        for zone_id, features in alerts_by_zone.items():
            for f in features:
                any_alert = True
                p = f["properties"]
                lines.append(f"- **{p.get('event')}** ({zone_id}): {p.get('headline', '')}")
        if not any_alert:
            lines.append(
                "No active alerts." if result["zones"] else "No registry zones to check (all ad-hoc waypoints)."
            )
    lines.append("")

    if result["zones"]:
        lines.append("## Zone forecasts")
        for zone_id in result["zones"]:
            zdata = result["zone_forecasts"].get(zone_id)
            if not zdata:
                lines.append(f"- **{zone_id}:** no forecast text found.")
            elif "error" in zdata:
                lines.append(f"- **{zone_id}:** {zdata['error']}")
            else:
                lines.append(f"### {zone_id} - {zdata['area']}")
                for period, text in list(zdata["periods"].items())[:2]:
                    lines.append(f"- **{period}:** {text}")
        lines.append("")

    lines.append("## Waypoints")
    for d in result["waypoint_details"]:
        lines.append(f"### {d['label']}")
        if d.get("forecast"):
            p = d["forecast"][0]
            when = to_local(p["time"])
            lines.append(
                f"- **Now-ish ({when}):** {p.get('temperature_f', '?')}°F, "
                f"wind {p.get('wind_direction', '')} {p.get('wind_speed', '')}, "
                f"{p.get('short_forecast', '')}"
            )
        elif d.get("forecast_error"):
            lines.append(f"- *Forecast: {d['forecast_error']}*")

        if d.get("aids_nearby") is not None:
            lines.append(f"- **Charted aids within range:** {d['aids_nearby']}")

        discrepancies = d.get("discrepancies")
        if discrepancies:
            total = sum(len(v) for v in discrepancies.values())
            if total:
                parts = [f"{len(v)} {k.replace('_', ' ')}" for k, v in discrepancies.items() if v]
                lines.append(f"- **Navigation discrepancies:** {', '.join(parts)}")
            else:
                lines.append("- **Navigation discrepancies:** none nearby")
        elif d.get("discrepancies_error"):
            lines.append(f"- *Discrepancies: {d['discrepancies_error']}*")

        if d.get("tide"):
            st = d["tide"]["station"]
            next_events = d["tide"]["predictions"][:2]
            summary = "; ".join(
                f"{'High' if p['type'] == 'H' else 'Low'} {p['t']} ({p['v']}ft)"
                for p in next_events
            )
            lines.append(f"- **Tide ({st['name']}, {st['_distance_nm']}nm):** {summary}")
        if d.get("water_level"):
            st = d["water_level"]["station"]
            reading = d["water_level"].get("reading")
            if reading:
                lines.append(
                    f"- **Water level ({st['name']}, {st['_distance_nm']}nm):** "
                    f"{reading['v']} ft as of {reading['t']}"
                )
        if d.get("tide_error"):
            lines.append(f"- *Tides: {d['tide_error']}*")
        lines.append("")

    if result["tropical_cyclones"]:
        lines.append("## Tropical cyclones within range")
        for s in result["tropical_cyclones"]:
            lines.append(
                f"- **{s['name']}** ({s['classification_name']}) - "
                f"{s['_distance_nm']}nm away, {s['wind_kt']} kt"
            )
        lines.append("")

    if result.get("ice"):
        ice = result["ice"]
        date_str = ice["date"].split("T")[0]
        total = ice.get("total")
        lines.append(f"## Great Lakes ice ({date_str})")
        if total is not None:
            lines.append(f"- Basin-wide total: {total}%")
        lines.append("")

    if result.get("sun_moon"):
        sm = result["sun_moon"]
        events = sm["events"]
        lines.append(f"## Sun ({result['waypoints'][0]}, {sm['date']})")
        for phen in ("Rise", "Set"):
            if phen in events:
                lines.append(f"- **{phen}:** {to_local(events[phen])}")
        lines.append("")

    return "\n".join(lines)
