import json
from pathlib import Path

import httpx
import pytest

from marine_wx_mcp.sources import coops
from marine_wx_mcp.sources.coops import CoopsError, find_nearest_station

FIXTURES = Path(__file__).parent / "fixtures" / "coops"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _load_stations() -> list[dict]:
    return json.loads((FIXTURES / "stations_sample.json").read_text())


def test_find_nearest_station_picks_closest():
    stations = _load_stations()
    nearest = find_nearest_station(stations, 42.588, -87.808)  # Kenosha
    assert nearest["name"] == "Milwaukee"
    assert nearest["_distance_nm"] < 30


def test_find_nearest_station_empty_list_returns_none():
    assert find_nearest_station([], 42.588, -87.808) is None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        return _FakeResponse(self._payload)


def _patch(monkeypatch, fixture_name: str):
    payload = json.loads((FIXTURES / fixture_name).read_text())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(payload))
    return payload


@pytest.mark.asyncio
async def test_get_tide_predictions_parses_hilo_events(monkeypatch):
    _patch(monkeypatch, "tide_predictions_eastport.json")

    predictions = await coops.get_tide_predictions("8410140", days=2)

    assert len(predictions) == 11
    assert predictions[0]["type"] == "L"
    assert predictions[1]["type"] == "H"
    # Eastport (Bay of Fundy) has a huge tidal range - sanity check the data is real
    assert float(predictions[1]["v"]) > 15


@pytest.mark.asyncio
async def test_get_water_level_returns_latest_reading(monkeypatch):
    _patch(monkeypatch, "water_level_milwaukee.json")

    reading = await coops.get_water_level("9087057", greatlakes=True)

    assert reading is not None
    assert float(reading["v"]) > 0


@pytest.mark.asyncio
async def test_get_current_predictions_parses_ebb_flood_slack(monkeypatch):
    _patch(monkeypatch, "current_predictions_smyrna.json")

    predictions = await coops.get_current_predictions("ACT4241", days=1)

    assert len(predictions) > 0
    types = {p["Type"] for p in predictions}
    assert types & {"ebb", "flood", "slack"}


@pytest.mark.asyncio
async def test_great_lakes_station_predictions_raises_actionable_error(monkeypatch):
    _patch(monkeypatch, "error_greatlakes_predictions.json")

    with pytest.raises(CoopsError, match="Great Lakes"):
        await coops.get_tide_predictions("9087057", days=1)


@pytest.mark.asyncio
async def test_invalid_station_type_raises(monkeypatch):
    with pytest.raises(CoopsError, match="Unknown station type"):
        await coops.get_stations("not_a_real_type")


@pytest.fixture
def temp_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(coops, "CACHE_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_get_stations_uses_disk_cache(monkeypatch, temp_cache):
    call_count = 0
    stations_payload = {"stations": _load_stations()}

    async def fake_get_json(url, params=None):
        nonlocal call_count
        call_count += 1
        return stations_payload

    monkeypatch.setattr(coops, "_get_json", fake_get_json)

    first = await coops.get_stations("waterlevels")
    second = await coops.get_stations("waterlevels")

    assert len(first) == len(second) == 3
    assert call_count == 1  # second call hit the cache
