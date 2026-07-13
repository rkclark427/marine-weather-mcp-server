import json
from pathlib import Path

from marine_wx_mcp.sources.nws import (
    _extract_marine_section,
    parse_off_product,
    parse_zone_forecast_product,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nws"


def test_parse_zone_forecast_product_splits_by_zone():
    data = json.loads((FIXTURES / "nsh_latest_MKX.json").read_text())
    zones = parse_zone_forecast_product(data["productText"])

    assert set(zones) >= {"LMZ643", "LMZ644", "LMZ645", "LMZ646"}
    z = zones["LMZ646"]
    assert z["area"] == "Wind Point WI to Winthrop Harbor IL"
    assert "CDT" in z["issued"]
    assert "REST OF TODAY" in z["periods"]
    assert "knots" in z["periods"]["REST OF TODAY"]


def test_parse_zone_forecast_product_period_text_has_no_stray_newlines():
    data = json.loads((FIXTURES / "nsh_latest_MKX.json").read_text())
    zones = parse_zone_forecast_product(data["productText"])
    for period_text in zones["LMZ645"]["periods"].values():
        assert "\n" not in period_text


def test_extract_marine_section_from_afd():
    data = json.loads((FIXTURES / "afd_latest_MKX.json").read_text())
    marine = _extract_marine_section(data["productText"])

    assert "High pressure" in marine
    assert ".MARINE" not in marine  # header stripped
    assert "Visit us at weather.gov" not in marine  # footer excluded via &&/$$ boundary


def test_parse_zone_forecast_product_empty_text_returns_empty_dict():
    assert parse_zone_forecast_product("") == {}


def test_gridpoint_fixture_has_wave_fields():
    data = json.loads((FIXTURES / "gridpoint_kenosha.json").read_text())
    props = data["properties"]
    for key in ("waveHeight", "wavePeriod", "waveDirection"):
        assert key in props
        assert props[key]["uom"]


def test_parse_off_product_splits_by_bare_name_header():
    data = json.loads((FIXTURES / "off_n01_sample.json").read_text())
    areas = parse_off_product(data["productText"])

    assert "Eastport Maine to Cape Cod" in areas
    periods = areas["Eastport Maine to Cape Cod"]["periods"]
    assert "TODAY" in periods
    assert "kt" in periods["TODAY"]


def test_parse_off_product_multiple_areas_have_independent_periods():
    data = json.loads((FIXTURES / "off_n01_sample.json").read_text())
    areas = parse_off_product(data["productText"])

    assert len(areas) >= 2
    for area in areas.values():
        assert "TODAY" in area["periods"]


def test_parse_off_product_empty_text_returns_empty_dict():
    assert parse_off_product("") == {}
