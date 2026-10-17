"""Tests for the 2026-10-11 multi-region work (Antigua): registry lookup across
regions, timezones, Open-Meteo swell/units/UTC handling, NWS non-coverage
fallback, NHC OFFNT3 zone parsing, threshold scoring, climatology, and the
route outlook end to end with every network source stubbed.

Fixtures named *_synthetic are hand-built to mirror real product layouts
(OFFNT3 per NHC's archived text; Open-Meteo marine per its documented
schema) - not captured live, because the build sandbox had no network.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from marine_wx_mcp import climatology, formatting, registry, scoring, server
from marine_wx_mcp.registry import RegistryError
from marine_wx_mcp.sources import nws, openmeteo

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------- registry

def test_antigua_port_found_without_naming_region():
    port = registry.get_port("english_harbour")
    assert port["region"] == "antigua_barbuda"
    assert port["zones"] == []


def test_lake_michigan_port_still_resolves_and_carries_region():
    port = registry.get_port("kenosha")
    assert port["region"] == "lake_michigan_west"


def test_qualified_name_resolves():
    port = registry.get_port("antigua_barbuda:barbuda_low_bay")
    assert port["name"] == "barbuda_low_bay"


def test_qualified_name_wrong_region_raises():
    with pytest.raises(RegistryError):
        registry.get_port("lake_michigan_west:english_harbour")


def test_ambiguous_name_requires_qualification(monkeypatch):
    fake = {
        "regions": {
            "a": {"timezone": "UTC", "ports": {"dock": {"lat": 0, "lon": 0}}, "routes": {}},
            "b": {"timezone": "UTC", "ports": {"dock": {"lat": 1, "lon": 1}}, "routes": {}},
        }
    }
    monkeypatch.setattr(registry, "load_registry", lambda: fake)
    with pytest.raises(RegistryError, match="qualify"):
        registry.get_port("dock")
    assert registry.get_port("b:dock")["lat"] == 1


def test_route_distance_computed_when_not_stored():
    route = registry.get_route("great_bird_island_barbuda")
    assert route["region"] == "antigua_barbuda"
    assert 25 < route["nm"] < 35  # North Sound to Low Bay is roughly 30 nm


def test_region_for_coords_nearby_and_far():
    assert registry.region_for_coords(17.3, -61.8) == "antigua_barbuda"
    assert registry.region_for_coords(42.5, -87.8) == "lake_michigan_west"
    assert registry.region_for_coords(0.0, 0.0) is None


def test_timezones_per_region():
    assert registry.timezone_for_region("antigua_barbuda") == "America/Antigua"
    assert registry.timezone_for_region("lake_michigan_west") == "America/Chicago"
    assert registry.timezone_for_region(None) == "UTC"


def test_thresholds_carry_a_status_line():
    for key in ("antigua_barbuda", "lake_michigan_west"):
        assert "Kent's limits" in registry.thresholds_for_region(key)["status"]


def test_zones_for_antigua_route_is_empty_not_error():
    assert registry.zones_for_place("great_bird_island_barbuda") == []


# ----------------------------------------------------------------------- scoring

_T = {
    "metrics": {"wind_kt": {"yellow": 20, "red": 27}, "wave_height_ft": {"yellow": 8, "red": 12}},
    "advisories": [
        {"metric": "swell_direction_deg", "from_deg": 300, "to_deg": 30, "min_swell_ft": 3,
         "message": "Northerly swell"}
    ],
    "status": "DRAFT",
}


def test_color_bands_are_upper_bounds():
    band = {"yellow": 20, "red": 27}
    assert scoring.color_for(20, band) == "green"
    assert scoring.color_for(20.1, band) == "yellow"
    assert scoring.color_for(27, band) == "yellow"
    assert scoring.color_for(27.1, band) == "red"
    assert scoring.color_for(None, band) is None


def test_score_rows_uses_window_max_and_names_driver():
    rows = [{"wind_kt": 15, "wave_height_ft": 5}, {"wind_kt": 23, "wave_height_ft": 6}]
    result = scoring.score_rows(rows, _T)
    assert result["categories"]["wind"]["color"] == "yellow"
    assert any("sustained wind" in d for d in result["categories"]["wind"]["drivers"])
    assert result["categories"]["seas"]["color"] == "green"
    assert "overall" not in result  # deliberately no rolled-up grade


def test_clouds_alone_do_not_color_wind_or_seas():
    # The 2026-10-11 live case: 14 kt, 3.5 ft, 100% overcast scored the whole
    # route RED. Each category must now stand on its own.
    t = {"metrics": {"wind_kt": {"yellow": 20, "red": 27}, "gust_kt": {"yellow": 28, "red": 35},
                     "wave_height_ft": {"yellow": 8, "red": 12},
                     "cloud_cover_pct": {"yellow": 60, "red": 90}}}
    rows = [{"wind_kt": 14, "gust_kt": 18, "wave_height_ft": 3.5, "cloud_cover_pct": 100}]
    cats = scoring.score_rows(rows, t)["categories"]
    assert cats["wind"]["color"] == "green"
    assert cats["seas"]["color"] == "green"
    assert cats["clouds"]["color"] == "red"
    text = formatting.format_category_scores({"categories": cats})
    assert text.startswith("Wind **GREEN** (14 kt, gusts 18)")
    assert "Seas **GREEN** (3.5 ft)" in text
    assert "Clouds **RED** (100%)" in text


def test_uncategorized_threshold_metric_gets_its_own_category():
    t = {"metrics": {"rain_mm": {"yellow": 1, "red": 5}}}
    cats = scoring.score_rows([{"rain_mm": 3}], t)["categories"]
    assert cats["rain_mm"]["color"] == "yellow"


def test_missing_metric_is_no_data_not_green():
    result = scoring.score_rows([{"wind_kt": None}], {"metrics": {"wind_kt": {"yellow": 1, "red": 2}}})
    assert result["categories"]["wind"]["color"] is None


def test_north_swell_advisory_wraps_through_north():
    rows = [{"swell_height_ft": 4.0, "swell_direction_deg": 350}]
    assert scoring.score_rows(rows, _T)["advisories"]
    rows = [{"swell_height_ft": 4.0, "swell_direction_deg": 20}]
    assert scoring.score_rows(rows, _T)["advisories"]
    rows = [{"swell_height_ft": 4.0, "swell_direction_deg": 90}]
    assert not scoring.score_rows(rows, _T)["advisories"]
    rows = [{"swell_height_ft": 2.0, "swell_direction_deg": 0}]
    assert not scoring.score_rows(rows, _T)["advisories"]


def test_no_thresholds_reports_status():
    result = scoring.score_rows([{"wind_kt": 10}], None)
    assert result["categories"] == {}
    assert "no thresholds" in result["status"]


def test_merge_hourly_takes_max_across_models_and_keeps_both():
    wind = {
        "timestamps": ["2027-01-12T12:00+00:00"],
        "models": {
            "ecmwf_ifs025": {"wind_speed_10m": [18], "wind_gusts_10m": [24], "wind_direction_10m": [80]},
            "gfs_seamless": {"wind_speed_10m": [21], "wind_gusts_10m": [26], "wind_direction_10m": [85]},
        },
    }
    rows = scoring.merge_hourly(wind, None)
    assert rows[0]["wind_kt"] == 21
    assert rows[0]["gust_kt"] == 26
    assert rows[0]["by_model"]["ecmwf_ifs025"]["wind_kt"] == 18


def test_compass():
    assert scoring.compass(0) == "N"
    assert scoring.compass(359) == "N"
    assert scoring.compass(80) == "E"
    assert scoring.compass(None) == "?"


# -------------------------------------------------------------------- open-meteo

class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._payload


class _Client:
    """Fake httpx.AsyncClient driven by a handler(url, params) -> (payload, status)."""

    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        payload, status = self._handler(url, params or {})
        return _Resp(payload, status)


def _patch(monkeypatch, handler, calls=None):
    def wrapped(url, params):
        if calls is not None:
            calls.append((url, dict(params)))
        return handler(url, params)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(wrapped))


@pytest.mark.asyncio
async def test_marine_forecast_heights_in_feet_and_swell_split(monkeypatch):
    payload = json.loads((FIXTURES / "openmeteo" / "marine_antigua_synthetic.json").read_text())
    calls = []
    _patch(monkeypatch, lambda u, p: (payload, 200), calls)
    result = await openmeteo.get_marine_forecast(17.4, -61.8, 12)

    assert calls[0][1]["timezone"] == "GMT"
    assert calls[0][1]["cell_selection"] == "sea"
    assert result["wave_height"][0] == 1.8                # meters, as returned
    assert result["wave_height_ft"][0] == 5.9             # 1.8 m -> 5.9 ft
    assert result["swell_wave_height_ft"][-1] == 4.9      # 1.5 m
    assert result["timestamps"][0].endswith("+00:00")


@pytest.mark.asyncio
async def test_marine_forecast_retries_without_secondary_swell(monkeypatch):
    payload = json.loads((FIXTURES / "openmeteo" / "marine_antigua_synthetic.json").read_text())
    calls = []

    def handler(url, params):
        if "secondary_swell_wave_height" in params["hourly"]:
            return {"error": True, "reason": "unknown variable"}, 400
        return payload, 200

    _patch(monkeypatch, handler, calls)
    result = await openmeteo.get_marine_forecast(17.4, -61.8, 12)
    assert len(calls) == 2
    assert result["no_data"] is False


@pytest.mark.asyncio
async def test_marine_history_falls_back_to_era5_ocean(monkeypatch):
    def handler(url, params):
        if "models" not in params:
            return {"hourly": {"time": ["2020-01-09T12:00"], "wave_height": [None]}}, 200
        return {
            "hourly": {
                "time": ["2020-01-09T12:00"],
                "wave_height_era5_ocean": [2.0],
                "swell_wave_height_era5_ocean": [1.0],
                "swell_wave_direction_era5_ocean": [10],
                "swell_wave_period_era5_ocean": [12],
            }
        }, 200

    _patch(monkeypatch, handler)
    result = await openmeteo.get_marine_history(17.4, -61.8, "2020-01-09", "2020-01-10")
    assert result["source"] == "era5_ocean"
    assert result["wave_height_ft"] == [6.6]
    assert result["swell_wave_direction"] == [10]


@pytest.mark.asyncio
async def test_marine_history_reports_no_data_honestly(monkeypatch):
    _patch(monkeypatch, lambda u, p: ({"hourly": {"time": ["x"], "wave_height": [None]}}, 200))
    result = await openmeteo.get_marine_history(0, 0, "2020-01-01", "2020-01-02")
    assert result["no_data"] is True


def test_utc_iso_variants():
    assert openmeteo._utc_iso("2027-01-12T12:00") == "2027-01-12T12:00+00:00"
    assert openmeteo._utc_iso("2027-01-12T12:00Z") == "2027-01-12T12:00+00:00"
    assert openmeteo._utc_iso("2027-01-12T12:00-04:00") == "2027-01-12T12:00-04:00"


# ----------------------------------------------------------------------------- NWS

def test_offnt3_parses_by_ugc_zone():
    text = (FIXTURES / "nws" / "off_nt3_synthetic.txt").read_text()
    areas = nws.parse_offshore_text(text)
    key = "AMZ051 - Offshore Waters Leeward Islands"
    assert key in areas
    assert "Seas 5 to 7 ft" in areas[key]["periods"]["TODAY"]


def test_offnt3_zone_filter():
    text = (FIXTURES / "nws" / "off_nt3_synthetic.txt").read_text()
    areas = nws.filter_offshore_areas(nws.parse_offshore_text(text), ["amz051"])
    assert list(areas) == ["AMZ051 - Offshore Waters Leeward Islands"]


def test_opc_offshore_still_uses_bare_name_parser():
    data = json.loads((FIXTURES / "nws" / "off_n01_sample.json").read_text())
    areas = nws.parse_offshore_text(data["productText"])
    assert "Eastport Maine to Cape Cod" in areas


def test_split_high_seas_nhc_section():
    data = json.loads((FIXTURES / "nws" / "hsf_at1_sample.json").read_text())
    text = data["productText"]
    nhc_part = nws.split_high_seas_text(text, "nhc")
    opc_part = nws.split_high_seas_text(text, "opc")
    assert nhc_part.lstrip().startswith("NWS NATIONAL HURRICANE CENTER")
    assert "NATIONAL HURRICANE CENTER" not in opc_part
    assert nws.split_high_seas_text(text, "all") == text


@pytest.mark.asyncio
async def test_point_metadata_null_grid_raises_outside_coverage(monkeypatch):
    async def fake_get(path, params=None):
        return {"properties": {"gridId": None, "gridX": None, "gridY": None}}

    monkeypatch.setattr(nws, "_get", fake_get)
    with pytest.raises(nws.OutsideNWSCoverage):
        await nws.get_point_metadata(17.17, -61.85)


@pytest.mark.asyncio
async def test_point_metadata_404_raises_outside_coverage(monkeypatch):
    async def fake_get(path, params=None):
        raise nws.NWSError("NWS API 404 for /points/17.0,-61.76: Unable to provide data")

    monkeypatch.setattr(nws, "_get", fake_get)
    with pytest.raises(nws.OutsideNWSCoverage):
        await nws.get_point_metadata(17.0, -61.76)


# ---------------------------------------------------------------------- formatting

def test_to_local_naive_is_utc_and_uses_given_zone():
    # 16:00 UTC = 12:00 AST (UTC-4, no DST) - the old code would have shown
    # this in the machine's own zone.
    out = formatting.to_local("2027-01-12T16:00", "America/Antigua")
    assert "12:00 PM" in out and "AST" in out


def test_to_local_default_zone_is_chicago():
    assert "CST" in formatting.to_local("2027-01-12T16:00+00:00")


def test_model_forecast_table_shows_feet_and_swell_direction():
    wind = {
        "timestamps": ["2027-01-12T16:00+00:00"],
        "models": {"ecmwf_ifs025": {"wind_speed_10m": [18.2], "wind_gusts_10m": [24.0],
                                    "wind_direction_10m": [80], "cloud_cover": [40]}},
    }
    marine = {
        "timestamps": ["2027-01-12T16:00+00:00"],
        "wave_height_ft": [5.9], "wave_period": [7.0],
        "swell_wave_height_ft": [3.3], "swell_wave_period": [11.0], "swell_wave_direction": [10],
        "no_data": False,
    }
    out = formatting.format_model_forecast("channel", scoring.merge_hourly(wind, marine), "America/Antigua")
    assert "18/24" in out
    assert "5.9 @7s" in out
    assert "3.3 @11s N" in out
    assert "America/Antigua" in out
    assert "ft" in out


def test_format_registry_shows_antigua_and_thresholds():
    out = formatting.format_registry(registry.dump_registry("antigua_barbuda"))
    assert "America/Antigua" in out
    assert "NT3" in out and "AMZ051" in out
    assert "Kent's limits" in out
    assert "great_bird_island_barbuda" in out


# --------------------------------------------------------------------- climatology

def test_year_windows_respects_lag_and_count():
    wins = climatology.year_windows("01-09", "01-16", 3, today=date(2026, 10, 11))
    assert [w[0].year for w in wins] == [2024, 2025, 2026]
    wins = climatology.year_windows("01-09", "01-16", 2, today=date(2026, 1, 18))
    # 2026's window ended Jan 16, within the ERA5 lag of Jan 18 -> excluded
    assert [w[0].year for w in wins] == [2024, 2025]


def test_year_windows_wrapping_new_year():
    wins = climatology.year_windows("12-28", "01-04", 1, today=date(2026, 10, 11))
    assert wins == [(date(2025, 12, 28), date(2026, 1, 4))]


def test_bad_mmdd_raises():
    with pytest.raises(climatology.ClimatologyError):
        climatology.year_windows("13-01", "01-04", 1)


def test_score_days_groups_by_local_day_and_daylight():
    tz = "America/Antigua"
    rows = []
    for h in range(0, 24):
        local = datetime(2025, 1, 12, h, tzinfo=ZoneInfo(tz))
        rows.append({"time": local.astimezone(timezone.utc).isoformat(),
                     "wind_kt": 30 if h == 3 else 15})  # 30 kt only at 03:00 local (night)
    days = climatology.score_days(rows, tz, _T, 7, 17)
    assert len(days) == 1
    assert days[0]["categories"]["wind"] == "green"  # the night-time spike is outside daylight hours
    assert days[0]["max_wind_kt"] == 15


@pytest.mark.asyncio
async def test_get_climatology_end_to_end_stubbed(monkeypatch):
    async def fake_weather(lat, lon, start, end):
        d0 = date.fromisoformat(start)
        ts, wind = [], []
        for day in range(10):
            for h in range(24):
                t = datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp() + (day * 24 + h) * 3600
                ts.append(datetime.fromtimestamp(t, timezone.utc).isoformat())
                wind.append(24.0 if d0.year == 2025 else 16.0)
        return {"timestamps": ts, "wind_speed_10m": wind, "wind_gusts_10m": [None] * len(ts),
                "wind_direction_10m": [80] * len(ts), "cloud_cover": [30] * len(ts)}

    async def fake_marine(lat, lon, start, end):
        return {"no_data": True, "source": None}

    monkeypatch.setattr(openmeteo, "get_archive_weather", fake_weather)
    monkeypatch.setattr(openmeteo, "get_marine_history", fake_marine)
    monkeypatch.setattr(climatology, "year_windows",
                        lambda s, e, y: [(date(2024, 1, 9), date(2024, 1, 16)), (date(2025, 1, 9), date(2025, 1, 16))])

    result = await climatology.get_climatology(17.4, -61.8, "America/Antigua", "01-09", "01-16", 2, _T)
    assert result["overall"]["days"] == 16
    assert result["overall"]["counts"]["wind"]["green"] == 8
    assert result["overall"]["counts"]["wind"]["yellow"] == 8
    out = formatting.format_climatology("channel", result)
    assert "| Wind | 8 (50%) | 8 (50%) | 0 (0%) |" in out
    assert "| 2025 |" in out


# --------------------------------------------------------------- server helpers

def test_resolve_location_antigua_place_gets_antigua_tz():
    loc = server._resolve_location(None, None, "english_harbour")
    assert loc["tz"] == "America/Antigua"


def test_resolve_location_adhoc_coords_infer_region():
    loc = server._resolve_location(17.4, -61.8, None)
    assert loc["region"] == "antigua_barbuda"
    assert server._resolve_location(0.0, 0.0, None)["tz"] == "UTC"


def test_passage_window_defaults_to_sunrise_and_hour_before_sunset():
    sun = {"events": {"Rise": "2027-01-12T06:41:00-04:00", "Set": "2027-01-12T17:51:00-04:00"}}
    start, end, basis, anchored = server._passage_window(date(2027, 1, 12), "America/Antigua", sun, None, None)
    assert start.isoformat() == "2027-01-12T06:41:00-04:00"
    assert end.isoformat() == "2027-01-12T16:51:00-04:00"
    assert anchored == end
    assert "sunset" in basis


def test_passage_window_explicit_hours():
    start, end, basis, _ = server._passage_window(date(2027, 1, 12), "America/Antigua", None, 6, 15)
    assert (start.hour, end.hour) == (6, 15)
    assert "requested" in basis


@pytest.mark.asyncio
async def test_route_outlook_antigua_end_to_end_stubbed(monkeypatch):
    """Antigua route: no NWS coastal/point calls, offshore AMZ051 included,
    Open-Meteo window conditions scored, north-swell advisory raised, tides
    requested for the departure date, times in America/Antigua."""
    depart = datetime.now(ZoneInfo("America/Antigua")).date()

    async def fake_sun(lat, lon, when, tz="America/Chicago"):
        assert tz == "America/Antigua"
        return {"date": when.isoformat(), "tz_name": tz, "events": {
            "Rise": datetime(when.year, when.month, when.day, 0, 1, tzinfo=ZoneInfo(tz)).isoformat(),
            "Set": datetime(when.year, when.month, when.day, 23, 58, tzinfo=ZoneInfo(tz)).isoformat(),
        }, "moon_events": {}, "moon_phase": {}}

    async def fake_rows(lat, lon, hours):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        rows = []
        for h in range(0, 30):
            t = datetime.fromtimestamp(now.timestamp() + h * 3600, timezone.utc).isoformat()
            rows.append({"time": t, "by_model": {}, "wind_kt": 22, "gust_kt": 29, "wind_dir_deg": 80,
                         "cloud_cover_pct": 40, "wave_height_ft": 7.0, "swell_height_ft": 4.0,
                         "swell_period_s": 12, "swell_direction_deg": 0})
        return rows, None

    offshore_text = (FIXTURES / "nws" / "off_nt3_synthetic.txt").read_text()

    async def fake_offshore(location):
        assert location == "NT3"
        return nws.parse_offshore_text(offshore_text)

    async def forbidden(*a, **k):
        raise AssertionError("NWS coastal/point/NAVCEN call made for a non-US route")

    tide_calls = []

    async def fake_stations(kind):
        return [{"id": "9999", "name": "Barbuda", "lat": 17.6, "lng": -61.85, "greatlakes": False}]

    async def fake_tides(station_id, days=2, start=None):
        tide_calls.append(start)
        return [{"t": "2027-01-12 07:22", "v": "0.9", "type": "H"}]

    async def no_storms():
        return []

    async def fake_water_level(station_id, greatlakes):
        return None

    monkeypatch.setattr(server.usno, "get_sun_times", fake_sun)
    monkeypatch.setattr(server, "_open_meteo_rows", fake_rows)
    monkeypatch.setattr(server.nws, "get_offshore_forecast", fake_offshore)
    monkeypatch.setattr(server.nws, "get_point_metadata", forbidden)
    monkeypatch.setattr(server.nws, "get_active_alerts", forbidden)
    monkeypatch.setattr(server.navcen, "get_discrepancies", forbidden)
    monkeypatch.setattr(server.navcen, "get_light_list", forbidden)
    monkeypatch.setattr(server.coops, "get_stations", fake_stations)
    monkeypatch.setattr(server.coops, "get_tide_predictions", fake_tides)
    monkeypatch.setattr(server.coops, "get_water_level", fake_water_level)
    monkeypatch.setattr(server.nhc, "get_active_storms", no_storms)

    out = await server.marine_route_outlook(
        ["great_bird_island", "antigua_barbuda_channel", "barbuda_low_bay"],
        depart_date=depart.isoformat(),
    )

    assert "America/Antigua" in out
    assert "AMZ051 - Offshore Waters Leeward Islands" in out
    assert "Wind **YELLOW** (22 kt" in out  # 22 kt > 20 (draft yellow), < 27
    assert "Northerly swell" in out        # 4 ft from N >= 3 ft advisory
    assert "Route overall" not in out
    assert "**Route (worst waypoint per category):** Wind **YELLOW**" in out
    assert "/gridpoints/None" not in out
    assert "failed" not in out
    assert tide_calls and all(s == depart for s in tide_calls)


@pytest.mark.asyncio
async def test_point_forecast_falls_back_to_open_meteo(monkeypatch):
    async def outside(lat, lon):
        raise server.nws.OutsideNWSCoverage("outside")

    async def fake_rows(lat, lon, hours):
        return [{"time": "2027-01-12T16:00+00:00", "by_model": {"ecmwf_ifs025": {"wind_kt": 18, "gust_kt": 24}},
                 "wind_kt": 18, "wind_dir_deg": 80}], None

    monkeypatch.setattr(server.nws, "get_point_metadata", outside)
    monkeypatch.setattr(server, "_open_meteo_rows", fake_rows)
    out = await server.marine_get_point_forecast(place="english_harbour")
    assert "Outside NWS gridpoint coverage" in out
    assert "18/24" in out


@pytest.mark.asyncio
async def test_aids_tool_declines_outside_great_lakes():
    out = await server.marine_get_aids_to_navigation(place="english_harbour")
    assert "outside the Great Lakes" in out


# ------------------------------------------------------------ rate limiting

@pytest.mark.asyncio
async def test_openmeteo_get_retries_429_then_succeeds(monkeypatch):
    import httpx
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(openmeteo.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(openmeteo.asyncio, "sleep", fake_sleep)
    assert await openmeteo._get("https://example.test/x", {}) == {"ok": True}
    assert calls["n"] == 3 and slept == [1.0, 1.0]


@pytest.mark.asyncio
async def test_openmeteo_get_gives_up_after_retries(monkeypatch):
    import httpx
    real_client = httpx.AsyncClient
    monkeypatch.setattr(openmeteo.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(429)), **kw))

    async def fake_sleep(s):
        pass

    monkeypatch.setattr(openmeteo.asyncio, "sleep", fake_sleep)
    with pytest.raises(openmeteo.OpenMeteoError):
        await openmeteo._get("https://example.test/x", {})


# ------------------------------------------- offshore periods / buoy time zone

_OFF_PERIODS = {
    "TODAY": "E 10 to 15 kt. Seas 3 to 5 ft.",
    "TONIGHT": "E 10 to 15 kt. Seas 2 to 4 ft.",
    "MON": "E 10 to 15 kt. Seas 2 to 4 ft.",
    "MON NIGHT": "NE to E 10 kt.",
    "TUE": "E 10 to 15 kt. Seas 3 to 5 ft.",
    "TUE NIGHT": "NE to E 10 to 15 kt.",
    "THU": "E 10 to 15 kt.",
}


def test_offshore_periods_follow_departure_date():
    # 2026-10-11 is a Sunday
    today = "2026-10-11"
    chosen, note = formatting.select_periods_for_date(_OFF_PERIODS, today, today)
    assert [k for k, _ in chosen] == ["TODAY", "TONIGHT", "MON"] and note is None
    chosen, _ = formatting.select_periods_for_date(_OFF_PERIODS, "2026-10-13", today)
    assert [k for k, _ in chosen] == ["TUE", "TUE NIGHT"]
    chosen, note = formatting.select_periods_for_date(_OFF_PERIODS, "2026-10-14", today)
    assert chosen == [] and "No WED period" in note      # gap in the product
    chosen, note = formatting.select_periods_for_date(_OFF_PERIODS, "2026-10-19", today)
    assert chosen == [] and "beyond" in note


def test_buoy_station_maps_to_region_timezone():
    assert registry.region_for_station("42060") == "antigua_barbuda"
    assert registry.region_for_station("41043") == "antigua_barbuda"
    assert registry.region_for_station("45186") == "lake_michigan_west"
    assert registry.region_for_station("cnii2") == "lake_michigan_west"  # port station, lower-case
    assert registry.region_for_station("99999") is None


def test_buoy_format_uses_given_zone():
    obs = [{"time_utc": "2026-10-11T18:20:00+00:00", "time_local": "ignored",
            "wind_speed_kt": 13.6, "wind_dir_deg": 80, "wind_gust_kt": 15.6,
            "wave_height_ft": 3.0, "dominant_wave_period_s": 6.0,
            "pressure_hpa": 1014.0, "air_temp_f": 84.2, "water_temp_f": 85.1}]
    out = formatting.format_buoy_obs("42060", obs, "America/Antigua")
    assert "02:20 PM AST" in out
