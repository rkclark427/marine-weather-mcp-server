"""MetService Point Forecast client - offline tests.

The response layout is built from developer.metservice.com's documented
example (dimensions/time, variables/{data,noData}, noDataReasons); it has
NOT been checked against a live response yet - see CLAUDE.md V-14.
"""

from datetime import datetime, timezone

import pytest

from marine_wx_mcp import formatting
from marine_wx_mcp.sources import metservice

TIMES = ["2026-10-11T12:00:00Z", "2026-10-11T13:00:00Z"]


def _payload(**overrides):
    def var(vals, flags=None, units="unknown"):
        return {"units": units, "dimensions": ["time", "point"], "data": vals,
                "noData": flags or [0] * len(vals)}
    variables = {
        "wind.speed.at-10m": var([7.2, 8.0], units="meter/second"),
        "wind.speed.gust.at-10m": var([9.3, 10.1], units="meter/second"),
        "wind.direction.at-10m": var([90, 75]),
        "wave.height": var([1.1, 1.2], units="meter"),
        "wave.period.peak": var([7, 7.5]),
        "wave.direction.peak": var([80, 85]),
        "wave.height.above-8s": var([0.6, None], [0, 2], units="meter"),
        "wave.period.above-8s.peak": var([10, None], [0, 2]),
        "wave.direction.above-8s.peak": var([10, None], [0, 2]),
        "wave.height.below-8s": var([0.8, 0.9], units="meter"),
        "cloud.cover": var([40, 100]),
        "precipitation.rate": var([0, 1.2]),
    }
    variables.update(overrides)
    return {
        "dimensions": {"point": {"type": "point", "data": [{"lon": -61.8, "lat": 17.4}]},
                       "time": {"type": "time", "data": TIMES}},
        "noDataReasons": {"GOOD": 0, "MASK_LAND": 1, "GAP": 2},
        "variables": variables,
    }


def test_build_request_matches_documented_shape():
    body = metservice.build_request(17.4, -61.8, 24, 3, start=datetime(2026, 10, 11, 12, tzinfo=timezone.utc))
    assert body["points"] == [{"lon": -61.8, "lat": 17.4}]
    assert body["time"] == {"from": "2026-10-11T12:00:00Z", "interval": "3h", "repeat": 8}
    assert "wind.speed.at-10m" in body["variables"]
    assert "wave.height.above-8s" in body["variables"]


def test_parse_converts_units_and_masks_no_data():
    out = metservice.parse_point_time(_payload())
    r0, r1 = out["rows"]
    assert r0["time"] == "2026-10-11T12:00:00+00:00"
    assert r0["wind_kt"] == 14 and r0["gust_kt"] == 18          # 7.2 / 9.3 m/s
    assert r0["wave_height_ft"] == 3.6                            # 1.1 m
    assert r0["swell_height_ft"] == 2.0 and r0["swell_period_s"] == 10
    assert r1["swell_height_ft"] is None                          # flagged GAP, not zero
    assert out["missing"]["swell_height_m"] == "GAP"


def test_missing_variable_is_reported_not_crash():
    p = _payload()
    del p["variables"]["cloud.cover"]
    out = metservice.parse_point_time(p)
    assert out["rows"][0]["cloud_cover_pct"] is None
    assert out["missing"]["cloud_cover_pct"] == "not returned"


@pytest.mark.asyncio
async def test_no_key_returns_message_not_error(monkeypatch):
    monkeypatch.delenv("METSERVICE_API_KEY", raising=False)
    assert metservice.is_configured() is False
    r = await metservice.get_point_forecast(17.4, -61.8)
    assert r["configured"] is False and "METSERVICE_API_KEY" in r["message"]
    r = await metservice.get_models()
    assert r["configured"] is False


def test_blank_key_counts_as_unset(monkeypatch):
    monkeypatch.setenv("METSERVICE_API_KEY", "   ")
    assert metservice.is_configured() is False


def test_format_table_in_region_time():
    out = metservice.parse_point_time(_payload())
    out["units_charged"] = "12"
    text = formatting.format_metservice_forecast("antigua_barbuda_channel", out, "America/Antigua")
    assert "| 14/18 | E |" in text
    assert "3.6 @7s" in text
    assert "2.0 @10s N" in text
    assert "swell_height_m (GAP)" in text
    assert "API units charged: 12" in text
    assert "08:00" in text   # 12:00Z shown as 08:00 AST
