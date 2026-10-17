import json
from pathlib import Path

import httpx
import pytest

from marine_wx_mcp.sources import openmeteo

FIXTURES = Path(__file__).parent / "fixtures" / "openmeteo"


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        return _FakeResponse(self._payload)


def _patch_client(monkeypatch, fixture_name: str):
    payload = json.loads((FIXTURES / fixture_name).read_text())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(payload))
    return payload


@pytest.mark.asyncio
async def test_get_model_forecast_aligns_models_by_timestamp(monkeypatch):
    _patch_client(monkeypatch, "multimodel_forecast.json")

    result = await openmeteo.get_model_forecast(
        42.6, -87.7, models=["ecmwf_ifs025", "gfs_seamless"], hours=6
    )

    # Requested in GMT since 2026-10-11 and returned as explicit UTC.
    assert result["timestamps"][:1] == ["2026-07-06T11:00+00:00"]
    assert "wind_speed_10m" in result["models"]["ecmwf_ifs025"]
    assert "wind_speed_10m" in result["models"]["gfs_seamless"]
    assert result["units"]["wind_speed_10m"] == "kn"
    # both models' arrays are the same length as the shared timestamp array
    assert len(result["models"]["ecmwf_ifs025"]["wind_speed_10m"]) == len(result["timestamps"])


@pytest.mark.asyncio
async def test_get_marine_forecast_reports_real_data(monkeypatch):
    _patch_client(monkeypatch, "marine_lake_michigan.json")

    result = await openmeteo.get_marine_forecast(42.6, -87.7, hours=48)

    assert result["no_data"] is False
    assert result["no_data_message"] is None
    assert all(v is not None for v in result["wave_height"])


@pytest.mark.asyncio
async def test_get_marine_forecast_flags_all_null_honestly(monkeypatch):
    payload = {
        "hourly_units": {"wave_height": "m", "wave_direction": "°", "wave_period": "s"},
        "hourly": {
            "time": ["2026-07-06T00:00", "2026-07-06T01:00"],
            "wave_height": [None, None],
            "wave_direction": [None, None],
            "wave_period": [None, None],
        },
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(payload))

    result = await openmeteo.get_marine_forecast(0.0, 0.0)

    assert result["no_data"] is True
    assert "marine_get_point_forecast" in result["no_data_message"]
