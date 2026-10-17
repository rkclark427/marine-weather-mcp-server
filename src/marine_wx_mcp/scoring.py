"""Green/yellow/red scoring of conditions against per-region thresholds,
reported separately for wind, seas and clouds (no overall grade).

Thresholds live in registry.json under each region's "thresholds" key:

    "metrics": {"wind_kt": {"yellow": 20, "red": 27}, ...}

A value ABOVE "yellow" is yellow, ABOVE "red" is red, otherwise green -
i.e. the numbers are the top of the green and yellow bands. Missing data
for a metric is reported as "no data", never silently treated as green.

"advisories" are directional call-outs that don't change the color on
their own (e.g. a northerly swell that makes one anchorage untenable
while the passage itself is fine) - they're surfaced as text.

This module only does arithmetic on numbers it's handed; it makes no
network calls and makes no go/no-go decision - consistent with the rest
of the server, the calling model and the skipper make the call. Region
thresholds are Kent's own limits (adopted 2026-10-11; see each region's
"status" in registry.json).
"""

from __future__ import annotations

from typing import Any

COLORS = ("green", "yellow", "red")
METRIC_LABELS = {
    "wind_kt": "sustained wind (kt)",
    "gust_kt": "gusts (kt)",
    "wave_height_ft": "combined seas (ft)",
    "swell_height_ft": "swell (ft)",
    "cloud_cover_pct": "cloud cover (%)",
}

# Scores are reported per category, never rolled up into one overall grade:
# "green wind, yellow seas, red clouds" says what a single "RED" hides.
# A category's color is the worst of its member metrics.
CATEGORIES = {
    "wind": ("Wind", ("wind_kt", "gust_kt")),
    "seas": ("Seas", ("wave_height_ft", "swell_height_ft")),
    "clouds": ("Clouds", ("cloud_cover_pct",)),
}

_COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def compass(deg: float | None) -> str:
    if deg is None:
        return "?"
    return _COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def color_for(value: float | None, band: dict[str, float]) -> str | None:
    if value is None:
        return None
    if value > band["red"]:
        return "red"
    if value > band["yellow"]:
        return "yellow"
    return "green"


def worst_color(colors: list[str | None]) -> str | None:
    present = [c for c in colors if c]
    if not present:
        return None
    return max(present, key=COLORS.index)


def _in_sector(deg: float, from_deg: float, to_deg: float) -> bool:
    deg %= 360
    if from_deg <= to_deg:
        return from_deg <= deg <= to_deg
    return deg >= from_deg or deg <= to_deg  # wraps through north


def _max(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def score_rows(rows: list[dict[str, Any]], thresholds: dict[str, Any] | None) -> dict[str, Any]:
    """Score a window of hourly rows.

    Each row may carry wind_kt, gust_kt, wave_height_ft, swell_height_ft,
    swell_direction_deg, cloud_cover_pct (None where unavailable). The
    window's value for each metric is its MAX over the rows (conservative).
    Returns {"categories": {cat: {"label", "color", "values", "drivers"}},
    "metrics": {m: {"value", "color", "band"}}, "advisories", "status"}.
    There is deliberately no overall grade - see CATEGORIES. A category's
    color is None when none of its metrics has data.
    """
    if not thresholds:
        return {"categories": {}, "metrics": {}, "advisories": [],
                "status": "no thresholds defined for this region"}

    metrics_out: dict[str, dict[str, Any]] = {}
    for metric, band in thresholds.get("metrics", {}).items():
        value = _max([r.get(metric) for r in rows])
        metrics_out[metric] = {"value": value, "color": color_for(value, band), "band": band}

    categories: dict[str, dict[str, Any]] = {}
    groups = [(key, label, members) for key, (label, members) in CATEGORIES.items()]
    # Any thresholded metric not in a named category gets its own category.
    known = {m for _, _, members in groups for m in members}
    groups += [(m, METRIC_LABELS.get(m, m), (m,)) for m in metrics_out if m not in known]
    for key, label, members in groups:
        present = {m: metrics_out[m] for m in members if m in metrics_out}
        if not present:
            continue
        color = worst_color([v["color"] for v in present.values()])
        categories[key] = {
            "label": label,
            "color": color,
            "values": {m: v["value"] for m, v in present.items()},
            "drivers": [
                f"{METRIC_LABELS.get(m, m)} {v['value']}"
                for m, v in present.items()
                if color and color != "green" and v["color"] == color
            ],
        }

    advisories = []
    for adv in thresholds.get("advisories", []):
        if adv.get("metric") != "swell_direction_deg":
            continue
        hits = [
            r for r in rows
            if r.get("swell_direction_deg") is not None
            and r.get("swell_height_ft") is not None
            and r["swell_height_ft"] >= adv.get("min_swell_ft", 0)
            and _in_sector(r["swell_direction_deg"], adv["from_deg"], adv["to_deg"])
        ]
        if hits:
            peak = max(hits, key=lambda r: r["swell_height_ft"])
            advisories.append(
                f"{adv['message']} (peak {peak['swell_height_ft']} ft from "
                f"{compass(peak['swell_direction_deg'])})"
            )

    return {
        "categories": categories,
        "metrics": metrics_out,
        "advisories": advisories,
        "status": thresholds.get("status", ""),
    }


def merge_hourly(
    wind: dict[str, Any] | None, marine: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Combine Open-Meteo multi-model wind and marine dicts into hourly rows.

    Across models, wind/gust/cloud take the MAX (conservative) and keep the
    per-model values too, so a big model disagreement is visible.
    """
    rows_by_ts: dict[str, dict[str, Any]] = {}
    if wind and wind.get("timestamps"):
        models = wind.get("models", {})
        for i, ts in enumerate(wind["timestamps"]):
            row: dict[str, Any] = {"time": ts, "by_model": {}}
            for name, series in models.items():
                def _v(key, _s=series):
                    vals = _s.get(key, [])
                    return vals[i] if i < len(vals) else None
                row["by_model"][name] = {
                    "wind_kt": _v("wind_speed_10m"),
                    "gust_kt": _v("wind_gusts_10m"),
                    "dir_deg": _v("wind_direction_10m"),
                    "cloud_cover_pct": _v("cloud_cover"),
                    "precip_prob_pct": _v("precipitation_probability"),
                    "cape": _v("cape"),
                }
            per = row["by_model"].values()
            row["wind_kt"] = _max([p["wind_kt"] for p in per])
            row["gust_kt"] = _max([p["gust_kt"] for p in per])
            row["cloud_cover_pct"] = _max([p["cloud_cover_pct"] for p in per])
            row["precip_prob_pct"] = _max([p["precip_prob_pct"] for p in per])
            row["wind_dir_deg"] = next((p["dir_deg"] for p in per if p["dir_deg"] is not None), None)
            rows_by_ts[ts] = row

    if marine and not marine.get("no_data") and marine.get("timestamps"):
        def _m(key, i):
            vals = marine.get(key) or []
            return vals[i] if i < len(vals) else None
        for i, ts in enumerate(marine["timestamps"]):
            row = rows_by_ts.setdefault(ts, {"time": ts, "by_model": {}})
            row["wave_height_ft"] = _m("wave_height_ft", i)
            row["wave_period_s"] = _m("wave_period", i)
            row["wave_direction_deg"] = _m("wave_direction", i)
            row["wind_wave_height_ft"] = _m("wind_wave_height_ft", i)
            row["wind_wave_period_s"] = _m("wind_wave_period", i)
            row["swell_height_ft"] = _m("swell_wave_height_ft", i)
            row["swell_period_s"] = _m("swell_wave_period", i)
            row["swell_direction_deg"] = _m("swell_wave_direction", i)
            row["secondary_swell_height_ft"] = _m("secondary_swell_wave_height_ft", i)
            row["secondary_swell_period_s"] = _m("secondary_swell_wave_period", i)
            row["secondary_swell_direction_deg"] = _m("secondary_swell_wave_direction", i)

    return [rows_by_ts[k] for k in sorted(rows_by_ts)]
