from pathlib import Path

import pytest

from marine_wx_mcp.sources.ndbc import NDBCError, get_buoy_obs, parse_ndbc_text

FIXTURES = Path(__file__).parent / "fixtures" / "ndbc"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_normal_station():
    obs = parse_ndbc_text(_load("sample_normal.txt"))
    assert len(obs) == 3
    latest = obs[0]
    assert latest["time_utc"] == "2026-07-06T15:00:00+00:00"
    assert latest["time_local"] == "2026-07-06T10:00:00-05:00"
    assert latest["wind_speed_kt"] == pytest.approx(7.0 * 1.9438452, abs=0.05)
    assert latest["wave_height_ft"] == pytest.approx(0.5 * 3.2808399, abs=0.05)
    assert latest["wind_gust_kt"] is None  # "MM" in source


def test_parse_mm_riddled_station_never_crashes():
    obs = parse_ndbc_text(_load("sample_mm.txt"))
    assert len(obs) == 3
    latest = obs[0]
    assert latest["wave_height_ft"] is None
    assert latest["pressure_hpa"] is None
    assert latest["air_temp_f"] is None
    # wind still parses even though everything else is missing
    assert latest["wind_speed_kt"] is not None


def test_parse_truncated_file_skips_bad_rows():
    obs = parse_ndbc_text(_load("sample_truncated.txt"))
    assert len(obs) == 1
    assert obs[0]["time_utc"] == "2026-07-06T15:00:00+00:00"


def test_parse_land_station_without_wave_columns():
    obs = parse_ndbc_text(_load("sample_land_station.txt"))
    assert len(obs) == 2
    assert obs[0]["wave_height_ft"] is None
    assert obs[0]["dominant_wave_period_s"] is None
    assert obs[0]["pressure_hpa"] == pytest.approx(1015.2)


def test_parse_empty_text():
    assert parse_ndbc_text("") == []


@pytest.mark.asyncio
async def test_get_buoy_obs_404_raises_actionable_error(monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 404
        text = ""

        def raise_for_status(self):
            pass

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())

    with pytest.raises(NDBCError, match="KNSW3"):
        await get_buoy_obs("knsw3")
