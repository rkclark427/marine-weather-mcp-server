import json
from pathlib import Path

import httpx
import pytest

from marine_wx_mcp.sources import navcen
from marine_wx_mcp.sources.navcen import filter_by_radius, light_range_nm

FIXTURES = Path(__file__).parent / "fixtures" / "navcen"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())["features"]


def test_light_range_nm_converts_statute_miles():
    props = {"LIGHT_NOM_RANGE": 6, "LIGHT_NOM_RANGE_UNIT": "STAT MI"}
    assert light_range_nm(props) == pytest.approx(5.2, abs=0.05)


def test_light_range_nm_leaves_nm_unconverted():
    props = {"LIGHT_NOM_RANGE": 6, "LIGHT_NOM_RANGE_UNIT": "NM"}
    assert light_range_nm(props) == 6


def test_light_range_nm_missing_value_returns_none():
    assert light_range_nm({"LIGHT_NOM_RANGE": None, "LIGHT_NOM_RANGE_UNIT": None}) is None


def test_filter_by_radius_kenosha_light_list():
    features = _load("light_list_sample.json")
    near = filter_by_radius(features, 42.588, -87.808, 5)

    assert len(near) > 0
    names = [f["properties"]["NAME"] for f in near]
    assert "Kenosha Light" in names
    # sorted nearest-first
    assert near[0]["_distance_nm"] <= near[-1]["_distance_nm"]


def test_filter_by_radius_excludes_far_features():
    features = _load("light_list_sample.json")
    near = filter_by_radius(features, 42.588, -87.808, 0.01)
    assert all(f["_distance_nm"] <= 0.01 for f in near)


def test_filter_by_radius_skips_features_without_coords():
    features = [{"type": "Feature", "properties": {"NAME": "no coords"}, "geometry": None}]
    assert filter_by_radius(features, 42.588, -87.808, 100) == []


def test_disc_fed_aid_fixture_has_discrepancy_status():
    features = _load("disc_fed_aid_sample.json")
    assert features
    for f in features:
        assert "DISCREP_STATUS" in f["properties"]
        assert f["properties"]["ATON_GROUP"] == "DISCREPANCIES - FEDERAL AIDS"


def test_disc_pri_aid_fixture_has_discrepancy_status():
    features = _load("disc_pri_aid_sample.json")
    assert features
    for f in features:
        assert f["properties"]["ATON_GROUP"] == "DISCREPANCIES - PRIVATE AIDS"


def test_haz_nav_fixture_schema():
    features = _load("haz_nav_sample.json")
    assert features
    for f in features:
        p = f["properties"]
        assert "TITLE" in p
        assert "SUB_CATEGORY" in p


def test_mar_event_fixture_schema():
    features = _load("mar_event_sample.json")
    assert features
    for f in features:
        p = f["properties"]
        assert p["SUB_CATEGORY"] == "Marine Events"


def _page(features: list[dict], exceeded: bool) -> dict:
    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {"exceededTransferLimit": True} if exceeded else {},
    }


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakePaginatedClient:
    """Serves 2 pages then a 3rd empty/non-exceeded page, like the real API."""

    def __init__(self):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        self.calls += 1
        if url.endswith("_1.geojson"):
            return _FakeResponse(_page([{"id": 1}], exceeded=True))
        if url.endswith("_2.geojson"):
            return _FakeResponse(_page([{"id": 2}], exceeded=False))
        # A page beyond the real last page - real API wraps and re-serves
        # earlier data rather than 404ing; the paginator must never reach
        # here because it should have stopped after page 2.
        return _FakeResponse(_page([{"id": 1}], exceeded=True))


@pytest.mark.asyncio
async def test_fetch_all_pages_stops_when_exceeded_transfer_limit_false(monkeypatch):
    fake_client = _FakePaginatedClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_client)

    features = await navcen._fetch_all_pages("someLayer")

    assert features == [{"id": 1}, {"id": 2}]
    assert fake_client.calls == 2


@pytest.mark.asyncio
async def test_load_or_fetch_uses_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(navcen, "CACHE_DIR", tmp_path)

    call_count = 0

    async def fake_fetch_all_pages(layer):
        nonlocal call_count
        call_count += 1
        return [{"id": layer}]

    monkeypatch.setattr(navcen, "_fetch_all_pages", fake_fetch_all_pages)

    first = await navcen._load_or_fetch("test_key", "testLayer")
    second = await navcen._load_or_fetch("test_key", "testLayer")

    assert first == second == [{"id": "testLayer"}]
    assert call_count == 1  # second call hit the cache, not the network


@pytest.mark.asyncio
async def test_load_or_fetch_refetches_when_cache_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(navcen, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(navcen, "CACHE_TTL_SECONDS", 0)

    call_count = 0

    async def fake_fetch_all_pages(layer):
        nonlocal call_count
        call_count += 1
        return [{"call": call_count}]

    monkeypatch.setattr(navcen, "_fetch_all_pages", fake_fetch_all_pages)

    await navcen._load_or_fetch("test_key", "testLayer")
    await navcen._load_or_fetch("test_key", "testLayer")

    assert call_count == 2  # TTL=0 means every call is a miss
