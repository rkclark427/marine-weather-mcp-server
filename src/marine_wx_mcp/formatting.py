"""Markdown formatting helpers for tool outputs.

Every tool supports response_format "markdown" (compact, human units,
local time) or "json" (structured passthrough). This module only
covers the markdown side - json mode returns source data almost as-is.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marine_wx_mcp.registry import route_distance_nm
from marine_wx_mcp.scoring import CATEGORIES, METRIC_LABELS, compass, worst_color
from marine_wx_mcp.sources.coops import MAX_STATION_DISTANCE_NM
from marine_wx_mcp.sources.navcen import light_range_nm

LOCAL_TZ = ZoneInfo("America/Chicago")  # default display zone (Lake Michigan home waters)


_DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def _tz(tz: ZoneInfo | str | None) -> ZoneInfo:
    if tz is None:
        return LOCAL_TZ
    if isinstance(tz, str):
        return ZoneInfo(tz)
    return tz


def to_local(iso_time: str, tz: ZoneInfo | str | None = None) -> str:
    """Render an ISO timestamp in `tz` (default America/Chicago).

    A NAIVE timestamp is treated as UTC. (Before 2026-10-11 naive Open-Meteo
    strings were Chicago wall time and were silently interpreted in the
    machine's own zone; Open-Meteo is now always requested in GMT - see
    sources/openmeteo.py.)
    """
    dt = datetime.fromisoformat(iso_time)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(_tz(tz)).strftime("%a %Y-%m-%d %I:%M %p %Z")


def to_local_short(iso_time: str, tz: ZoneInfo | str | None = None) -> str:
    dt = datetime.fromisoformat(iso_time)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(_tz(tz)).strftime("%a %m-%d %H:%M")


def format_alerts(zone_ids: list[str], features: list[dict[str, Any]], tz: ZoneInfo | str | None = None) -> str:
    if not features:
        return f"No active alerts for {', '.join(zone_ids)}."

    lines = [f"## Active marine alerts for {', '.join(zone_ids)}\n"]
    for feature in features:
        p = feature["properties"]
        effective = to_local(p["effective"], tz) if p.get("effective") else "unknown"
        expires = to_local(p["expires"], tz) if p.get("expires") else "unknown"
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


def format_buoy_obs(station_id: str, obs: list[dict[str, Any]], tz: ZoneInfo | str | None = None) -> str:
    if not obs:
        return (
            f"No recent observations for station {station_id}. It may not be "
            "reporting right now - check marine_list_registry notes for a "
            "nearby alternate, or try marine_get_point_forecast instead."
        )
    lines = [f"## {station_id} - latest observations\n"]
    for o in obs:
        when = to_local(o["time_utc"], tz) if tz else o["time_local"]
        parts = [f"**{when}**"]
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


def format_point_forecast(
    place_or_coords: str, periods: list[dict[str, Any]], tz: ZoneInfo | str | None = None
) -> str:
    if not periods:
        return f"No forecast periods returned for {place_or_coords}."
    lines = [f"## Point forecast - {place_or_coords}\n"]
    for p in periods:
        when = to_local(p["time"], tz)
        bits = [f"**{when}**", f"{p.get('temperature_f', '?')}°F"]
        if p.get("wind_speed"):
            bits.append(f"wind {p.get('wind_direction', '')} {p['wind_speed']}".strip())
        if p.get("wave_height_ft") is not None:
            bits.append(f"waves {p['wave_height_ft']} ft")
        if p.get("short_forecast"):
            bits.append(p["short_forecast"])
        lines.append(" - ".join(bits))
    return "\n".join(lines)


def _num(v: Any, digits: int = 0) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}" if digits else str(round(v))


def _ht_period(height: Any, period: Any) -> str:
    if height is None:
        return "-"
    return f"{height:.1f}" + (f" @{period:.0f}s" if period is not None else "")


def format_model_forecast(
    place_or_coords: str,
    rows: list[dict[str, Any]],
    tz: ZoneInfo | str | None = None,
    step_hours: int = 1,
    marine_note: str | None = None,
) -> str:
    """Hourly table from scoring.merge_hourly rows: per-model wind/gust, direction,
    cloud, rain chance, combined seas, wind-wave and swell (heights in FEET)."""
    if not rows:
        return f"No model forecast data returned for {place_or_coords}."
    zone = _tz(tz)
    models = list(rows[0].get("by_model", {}))
    tz_label = datetime.now(zone).strftime("%Z")
    has_secondary = any(r.get("secondary_swell_height_ft") is not None for r in rows)

    lines = [
        f"## Multi-model forecast - {place_or_coords}",
        f"Models: {', '.join(models) or 'n/a'} - times {zone.key} ({tz_label}) - "
        "wind/gust kt, heights ft, directions are FROM\n",
    ]
    header = [f"Time ({tz_label})"]
    header += [f"{m} wind/gust" for m in models]
    header += ["Dir", "Cloud %", "Rain %", "Seas ft", "Wind-wave ft", "Swell ft (from)"]
    if has_secondary:
        header.append("2nd swell ft (from)")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    for row in rows[:: max(1, step_hours)]:
        cells = [to_local_short(row["time"], zone)]
        for m in models:
            bm = row["by_model"].get(m, {})
            cells.append(f"{_num(bm.get('wind_kt'))}/{_num(bm.get('gust_kt'))}")
        cells.append(compass(row.get("wind_dir_deg")) if row.get("wind_dir_deg") is not None else "-")
        cells.append(_num(row.get("cloud_cover_pct")))
        cells.append(_num(row.get("precip_prob_pct")))
        cells.append(_ht_period(row.get("wave_height_ft"), row.get("wave_period_s")))
        cells.append(_ht_period(row.get("wind_wave_height_ft"), row.get("wind_wave_period_s")))
        swell = _ht_period(row.get("swell_height_ft"), row.get("swell_period_s"))
        if row.get("swell_direction_deg") is not None:
            swell += f" {compass(row['swell_direction_deg'])}"
        cells.append(swell)
        if has_secondary:
            sec = _ht_period(row.get("secondary_swell_height_ft"), row.get("secondary_swell_period_s"))
            if row.get("secondary_swell_direction_deg") is not None:
                sec += f" {compass(row['secondary_swell_direction_deg'])}"
            cells.append(sec)
        lines.append("| " + " | ".join(cells) + " |")

    if marine_note:
        lines.append(f"\n*{marine_note}*")
    return "\n".join(lines)


def format_thresholds(thresholds: dict[str, Any] | None) -> list[str]:
    if not thresholds:
        return ["- *No go/no-go thresholds defined.*"]
    lines = [f"- *Status: {thresholds.get('status', '')}*"]
    for metric, band in thresholds.get("metrics", {}).items():
        lines.append(
            f"- {METRIC_LABELS.get(metric, metric)}: green <= {band['yellow']}, "
            f"yellow <= {band['red']}, red above"
        )
    for adv in thresholds.get("advisories", []):
        lines.append(f"- Advisory: {adv.get('message')}")
    return lines


def format_registry(regions: dict[str, Any]) -> str:
    lines = ["## Registry\n"]
    for region_key, region in regions.items():
        lines.append(f"### {region_key} ({region['timezone']})")
        if region.get("nws_offices"):
            lines.append(f"NWS offices: {', '.join(region['nws_offices'])}")
        if region.get("afd_office"):
            lines.append(f"Forecast discussion office: {region['afd_office']}")
        if region.get("offshore"):
            off = region["offshore"]
            lines.append(f"Offshore forecast: {off['location']} zones {', '.join(off.get('zones', []))}")
        if region.get("buoys"):
            lines.append(f"Buoys: {', '.join(region['buoys'])}")
        lines.append("\n**Ports:**")
        for name, port in region["ports"].items():
            lines.append(
                f"- `{name}` ({port['lat']}, {port['lon']}) - zones: "
                f"{', '.join(port.get('zones', [])) or 'none'} - stations: "
                f"{', '.join(port.get('stations', [])) or 'none'}"
            )
        lines.append("\n**Routes:**")
        for name, route in region.get("routes", {}).items():
            nm = route.get("nm") or route_distance_nm(region_key, route["legs"])
            lines.append(f"- `{name}`: {' -> '.join(route['legs'])} ({nm} nm)")
        lines.append("\n**Go/no-go thresholds:**")
        lines.extend(format_thresholds(region.get("thresholds")))
        lines.append("")
    return "\n".join(lines)


_SUN_EVENT_ORDER = [
    "Begin Civil Twilight",
    "Rise",
    "Upper Transit",
    "Set",
    "End Civil Twilight",
]


_MOON_EVENT_ORDER = ["Rise", "Upper Transit", "Set"]


def format_sun_times(place_or_coords: str, result: dict[str, Any], tz: ZoneInfo | str | None = None) -> str:
    tz = tz or result.get("tz_name")
    events = result["events"]
    if not events:
        return f"No sun/twilight data returned for {place_or_coords} on {result['date']}."

    lines = [f"## Sun & moon - {place_or_coords} ({result['date']})\n", "### Sun"]
    for phen in _SUN_EVENT_ORDER:
        if phen in events:
            lines.append(f"- **{phen}:** {to_local(events[phen], tz)}")
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
        lines.append(f"- **Next {closest['phase']}:** {to_local(closest['time'], tz)}")
    for phen in _MOON_EVENT_ORDER:
        if phen in moon_events:
            lines.append(f"- **Moon {phen}:** {to_local(moon_events[phen], tz)}")
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
        lines.append(
            f"### Tide predictions - {st['name']} ({st['_distance_nm']}nm), MLLW datum, "
            "station-local time"
        )
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


def _range(values: list[Any]) -> tuple[Any, Any]:
    present = [v for v in values if v is not None]
    if not present:
        return None, None
    return min(present), max(present)


def summarize_window(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Min/max of the key passage-planning numbers over a window of hourly rows."""
    w_lo, w_hi = _range([r.get("wind_kt") for r in rows])
    _, g_hi = _range([r.get("gust_kt") for r in rows])
    dirs = [r.get("wind_dir_deg") for r in rows if r.get("wind_dir_deg") is not None]
    _, seas_hi = _range([r.get("wave_height_ft") for r in rows])
    swell_rows = [r for r in rows if r.get("swell_height_ft") is not None]
    peak_swell = max(swell_rows, key=lambda r: r["swell_height_ft"]) if swell_rows else None
    _, cloud_hi = _range([r.get("cloud_cover_pct") for r in rows])
    cloud_lo, _ = _range([r.get("cloud_cover_pct") for r in rows])
    _, rain_hi = _range([r.get("precip_prob_pct") for r in rows])
    return {
        "hours": len(rows),
        "wind_kt": [w_lo, w_hi],
        "gust_kt_max": g_hi,
        "wind_from": [c for c, _ in Counter(compass(d) for d in dirs).most_common(3)],
        "seas_ft_max": seas_hi,
        "swell_peak": (
            {
                "height_ft": peak_swell["swell_height_ft"],
                "period_s": peak_swell.get("swell_period_s"),
                "from": compass(peak_swell.get("swell_direction_deg")),
            }
            if peak_swell
            else None
        ),
        "cloud_pct": [cloud_lo, cloud_hi],
        "rain_prob_max": rain_hi,
    }


def _format_window_summary(summary: dict[str, Any]) -> str:
    bits = []
    lo, hi = summary["wind_kt"]
    if hi is not None:
        wind = f"wind {_num(hi)} kt" if _num(lo) == _num(hi) else f"wind {_num(lo)}-{_num(hi)} kt"
        if summary.get("gust_kt_max") is not None:
            wind += f" (gusts {_num(summary['gust_kt_max'])})"
        if summary.get("wind_from"):
            wind += f" from {'/'.join(summary['wind_from'][:3])}"
        bits.append(wind)
    if summary.get("seas_ft_max") is not None:
        bits.append(f"seas to {summary['seas_ft_max']:.1f} ft")
    sp = summary.get("swell_peak")
    if sp:
        swell = f"swell to {sp['height_ft']:.1f} ft"
        if sp.get("period_s") is not None:
            swell += f" @{sp['period_s']:.0f}s"
        swell += f" from {sp['from']}"
        bits.append(swell)
    clo, chi = summary.get("cloud_pct", [None, None])
    if chi is not None:
        bits.append(f"cloud {_num(chi)}%" if _num(clo) == _num(chi) else f"cloud {_num(clo)}-{_num(chi)}%")
    if summary.get("rain_prob_max") is not None:
        bits.append(f"rain chance up to {_num(summary['rain_prob_max'])}%")
    return ", ".join(bits) if bits else "no data"


_COLOR_BADGE = {"green": "GREEN", "yellow": "YELLOW", "red": "RED", None: "NO DATA"}

_VALUE_FMT = {
    "wind_kt": "{} kt",
    "gust_kt": "gusts {}",
    "wave_height_ft": "{} ft",
    "swell_height_ft": "swell {} ft",
    "cloud_cover_pct": "{}%",
}


def _fmt_value(metric: str, value: Any) -> str:
    if isinstance(value, float):
        value = round(value) if metric in ("wind_kt", "gust_kt", "cloud_cover_pct") else round(value, 1)
    return _VALUE_FMT.get(metric, "{}").format(value)


def format_category_scores(score: dict[str, Any]) -> str:
    """'Wind **GREEN** (14 kt, gusts 18) · Seas **YELLOW** (6.2 ft, swell 4.1 ft) · ...'"""
    parts = []
    for cat in score.get("categories", {}).values():
        vals = ", ".join(_fmt_value(m, v) for m, v in cat["values"].items() if v is not None)
        parts.append(f"{cat['label']} **{_COLOR_BADGE[cat['color']]}**" + (f" ({vals})" if vals else ""))
    return " · ".join(parts)


_WEEKDAY_ABBR = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def select_periods_for_date(
    periods: dict[str, str], depart: str, today: str | None
) -> tuple[list[tuple[str, str]], str | None]:
    """Pick the text-forecast periods covering `depart` (ISO date).

    NWS/NHC text products label periods TODAY / TONIGHT / then weekday
    names (MON, MON NIGHT, TUE ...). Departing today -> the first three
    periods. Later -> that weekday's day + night periods. Returns
    (periods, note); note explains a miss (beyond the product's range).
    """
    items = list(periods.items())
    if not today or depart <= today:
        return items[:3], None
    abbr = _WEEKDAY_ABBR[date.fromisoformat(depart).weekday()]
    days_out = (date.fromisoformat(depart) - date.fromisoformat(today)).days
    if days_out >= 7:
        return [], "Departure is a week or more out - beyond the text forecast's range."
    picked = [
        (k, v) for k, v in items
        if k.upper() in (abbr, f"{abbr} NIGHT") or k.upper().startswith(f"{abbr} ")
    ]
    if not picked:
        return [], f"No {abbr} period in this product yet (it covers {items[-1][0] if items else 'nothing'} at most)."
    return picked, None


def format_route_outlook(result: dict[str, Any]) -> str:
    tz = result.get("timezone")
    lines = [
        f"# Route outlook: {' -> '.join(result['waypoints'])}",
        f"*Departing {result['depart_date']}"
        + (f" - times shown in {tz}" if tz else "")
        + (f" - region {result['region']}" if result.get("region") else "")
        + "*\n",
    ]

    window = result.get("window")
    if window:
        lines.append(
            f"**Passage window:** {to_local(window['start'], tz)} to {to_local(window['end'], tz)} "
            f"({window['basis']})\n"
        )

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
                "No active alerts."
                if result["zones"]
                else "No NWS coastal zones on this route (ad-hoc waypoints or non-US waters) - "
                "check the offshore forecast below and the regional forecast discussion."
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

    offshore = result.get("offshore")
    if offshore:
        lines.append(f"## Offshore forecast ({offshore.get('location')})")
        if offshore.get("error"):
            lines.append(f"*{offshore['error']}*")
        for name, area in (offshore.get("areas") or {}).items():
            lines.append(f"### {name}")
            chosen, note = select_periods_for_date(
                area["periods"], result["depart_date"], result.get("today")
            )
            for period, text in chosen:
                lines.append(f"- **{period}:** {text}")
            if note:
                lines.append(f"*{note}*")
        lines.append("")

    scored = [d["score"] for d in result["waypoint_details"] if d.get("score", {}).get("categories")]
    if scored:
        lines.append("## Go/no-go scoring")
        lines.append("*Scored separately by category - there is no single overall grade.*")
        route_parts = []
        cat_keys = list(dict.fromkeys(k for s in scored for k in s["categories"]))
        for key in cat_keys:
            label = next(s["categories"][key]["label"] for s in scored if key in s["categories"])
            color = worst_color([s["categories"].get(key, {}).get("color") for s in scored])
            route_parts.append(f"{label} **{_COLOR_BADGE[color]}**")
        lines.append(f"**Route (worst waypoint per category):** {' · '.join(route_parts)}")
        status = result.get("thresholds_status")
        if status:
            lines.append(f"*Thresholds: {status}*")
        lines.append("")

    lines.append("## Waypoints")
    for d in result["waypoint_details"]:
        lines.append(f"### {d['label']}")
        if d.get("conditions"):
            lines.append(f"- **Window conditions (Open-Meteo):** {_format_window_summary(d['conditions'])}")
        elif d.get("conditions_note"):
            lines.append(f"- *Window conditions: {d['conditions_note']}*")
        score = d.get("score")
        if score and score.get("categories"):
            lines.append(f"- **Score:** {format_category_scores(score)}")
            for adv in score.get("advisories", []):
                lines.append(f"- **Advisory:** {adv}")

        if d.get("forecast"):
            p = d["forecast"][0]
            when = to_local(p["time"], tz)
            lines.append(
                f"- **NWS now-ish ({when}):** {p.get('temperature_f', '?')}°F, "
                f"wind {p.get('wind_direction', '')} {p.get('wind_speed', '')}, "
                f"{p.get('short_forecast', '')}"
            )
        elif d.get("forecast_error"):
            lines.append(f"- *NWS forecast: {d['forecast_error']}*")

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
            lines.append(
                f"- **Tide ({st['name']}, {st['_distance_nm']}nm, station-local time):** {summary}"
            )
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
                lines.append(f"- **{phen}:** {to_local(events[phen], tz)}")
        if result.get("must_be_anchored_by"):
            lines.append(
                f"- **Anchored by (1 hr before sunset, charter rule):** "
                f"{to_local(result['must_be_anchored_by'], tz)}"
            )
        lines.append("")

    return "\n".join(lines)


def format_climatology(label: str, result: dict[str, Any]) -> str:
    overall = result["overall"]
    counts = overall["counts"]
    total = overall["days"] or 1
    labels_by_cat = {k: v[0] for k, v in CATEGORIES.items()}
    years = result["years"]
    h0, h1 = result["daylight_hours"]

    def pct(n: int) -> str:
        return f"{n} ({round(100 * n / total)}%)"

    lines = [
        f"## Climatology - {label}, {result['window']} ({years[0]}-{years[-1]}, {len(years)} years)",
        f"*Daylight sailing hours {h0:02d}:00-{h1:02d}:00 {result['timezone']}; each day scored by its "
        "worst hour against the region thresholds, separately for each category.*",
    ]
    if result.get("thresholds_status"):
        lines.append(f"*Thresholds: {result['thresholds_status']}*")
    lines.append("")
    lines.append(f"**{overall['days']} days, by category:**\n")
    lines.append("| Category | Green | Yellow | Red | No data |")
    lines.append("|---|---|---|---|---|")
    for key, c in counts.items():
        lines.append(
            f"| {labels_by_cat.get(key, METRIC_LABELS.get(key, key))} | {pct(c['green'])} | "
            f"{pct(c['yellow'])} | {pct(c['red'])} | {c['no_data'] or '-'} |"
        )
    if overall.get("advisory_days"):
        lines.append(f"**Days with a directional advisory (e.g. northerly swell):** {overall['advisory_days']}")
    lines.append("")

    labels = {
        "max_wind_kt": "Daily max sustained wind (kt)",
        "max_gust_kt": "Daily max gust (kt)",
        "max_seas_ft": "Daily max combined seas (ft)",
        "max_swell_ft": "Daily max swell (ft)",
        "max_cloud_pct": "Daily max cloud cover (%)",
    }
    lines.append("| Measure | Median | 90th pct | Max |")
    lines.append("|---|---|---|---|")
    for key, name in labels.items():
        d = overall["daily_max_distribution"][key]
        if d["n"]:
            lines.append(f"| {name} | {d['median']} | {d['p90']} | {d['max']} |")
    lines.append("")

    cat_cols = list(counts)
    lines.append(
        "| Year | "
        + " | ".join(f"{labels_by_cat.get(k, k)} G/Y/R" for k in cat_cols)
        + " | Max wind | Max gust | Max seas | Max swell | Wave data |"
    )
    lines.append("|---|" + "---|" * (len(cat_cols) + 5))
    for y in result["per_year"]:
        s = y["summary"]
        c = s["counts"]
        dist = s["daily_max_distribution"]
        note = y.get("wave_source") or ("error" if y.get("marine_error") else "none")
        if y.get("weather_error"):
            note += "; wind error"
        lines.append(
            f"| {y['start'][:4]} | "
            + "".join(
                f"{c.get(k, {}).get('green', 0)}/{c.get(k, {}).get('yellow', 0)}/{c.get(k, {}).get('red', 0)} | "
                for k in cat_cols
            )
            + f"{dist['max_wind_kt']['max'] if dist['max_wind_kt']['max'] is not None else '-'} | "
            f"{dist['max_gust_kt']['max'] if dist['max_gust_kt']['max'] is not None else '-'} | "
            f"{dist['max_seas_ft']['max'] if dist['max_seas_ft']['max'] is not None else '-'} | "
            f"{dist['max_swell_ft']['max'] if dist['max_swell_ft']['max'] is not None else '-'} | "
            f"{note} |"
        )
    lines.append(
        "\n*Source: ERA5 reanalysis (~25 km) via Open-Meteo archive, plus Open-Meteo marine history. "
        "Reanalysis smooths squalls and local acceleration, and its gusts are modelled - read this as "
        "the typical background regime, not peak conditions.*"
    )
    return "\n".join(lines)


def format_metservice_forecast(label: str, result: dict[str, Any], tz: str | None) -> str:
    rows = result.get("rows") or []
    lines = [
        f"## MetService point forecast - {label}",
        f"*Times {tz or 'UTC'} - wind/gust kt - heights ft - directions are FROM - "
        "swell = waves over 8 s, wind-wave = under 8 s*",
        "",
        "| Time | Wind/gust | Dir | Seas ft | Wind-wave ft | Swell ft (from) | Cloud % | Rain mm/h |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        seas = "-" if r["wave_height_ft"] is None else (
            f"{r['wave_height_ft']}" + (f" @{_num(r['wave_period_s'])}s" if r["wave_period_s"] is not None else ""))
        swell = "-" if r["swell_height_ft"] is None else (
            f"{r['swell_height_ft']}"
            + (f" @{_num(r['swell_period_s'])}s" if r["swell_period_s"] is not None else "")
            + (f" {compass(r['swell_direction_deg'])}" if r["swell_direction_deg"] is not None else ""))
        lines.append(
            f"| {to_local_short(r['time'], tz)} | {_num(r['wind_kt'])}/{_num(r['gust_kt'])} | "
            f"{compass(r['wind_dir_deg'])} | {seas} | "
            f"{'-' if r['wind_wave_height_ft'] is None else r['wind_wave_height_ft']} | {swell} | "
            f"{_num(r['cloud_cover_pct'])} | {_num(r['precip_mm_hr'], 1)} |"
        )
    if not rows:
        lines.append("| (no time steps returned) |||||||| ")
    missing = result.get("missing") or {}
    if missing:
        lines.append("")
        lines.append("*No data for: " + ", ".join(f"{k} ({v})" for k, v in missing.items()) + "*")
    expl = result.get("explanation")
    if expl:
        import json as _json
        text = _json.dumps(expl)
        lines.append("")
        lines.append(f"*Models used (as reported by MetService):* `{text[:600]}{'...' if len(text) > 600 else ''}`")
    if result.get("units_charged"):
        lines.append(f"*API units charged: {result['units_charged']}*")
    return "\n".join(lines)


def format_metservice_models(result: dict[str, Any]) -> str:
    models = result.get("models")
    lines = ["## MetService API key OK"]
    if isinstance(models, dict):
        names = list(models.keys())
    elif isinstance(models, list):
        names = [m.get("name", m.get("id", str(m))) if isinstance(m, dict) else str(m) for m in models]
    else:
        names = [str(models)]
    lines.append(f"{len(names)} models visible to this key:")
    lines.extend(f"- {n}" for n in names)
    if result.get("units_charged"):
        lines.append(f"\n*API units charged: {result['units_charged']}*")
    return "\n".join(lines)
