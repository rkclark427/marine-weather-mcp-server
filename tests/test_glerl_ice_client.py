import json
from pathlib import Path

import httpx
import pytest

from marine_wx_mcp.sources.glerl_ice import GlerlIceError, get_latest_ice_cover

FIXTURES = Path(__file__).parent / "fixtures" / "glerl_ice"


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

    async def get(self, url, headers=None):
        return _FakeResponse(self._payload)


def _patch(monkeypatch, fixture_name: str):
    payload = json.loads((FIXTURES / fixture_name).read_text())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(payload))


@pytest.mark.asyncio
async def test_get_latest_ice_cover_off_season(monkeypatch):
    _patch(monkeypatch, "latest.json")

    result = await get_latest_ice_cover()

    assert result["date"] == "2026-05-16T00:00:00Z"
    assert result["total"] == 0
    assert all(v == 0 for v in result["lakes"].values())


@pytest.mark.asyncio
async def test_get_latest_ice_cover_mid_winter_values(monkeypatch):
    _patch(monkeypatch, "winter_range.json")

    result = await get_latest_ice_cover()

    # fixture's first row (Feb 1) since get_latest_ice_cover takes rows[0]
    assert result["date"] == "2026-02-01T00:00:00Z"
    assert result["lakes"]["Erie"] > 90  # shallow lake, freezes fastest
    assert result["lakes"]["St_Clair"] == 100
    assert result["lakes"]["Michigan"] < result["lakes"]["Erie"]


@pytest.mark.asyncio
async def test_get_latest_ice_cover_empty_response_raises(monkeypatch):
    payload = {"table": {"columnNames": [], "rows": []}}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(payload))

    with pytest.raises(GlerlIceError, match="no ice cover data"):
        await get_latest_ice_cover()


@pytest.mark.asyncio
async def test_get_latest_ice_cover_http_error_raises(monkeypatch):
    class _FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FailingClient())

    with pytest.raises(GlerlIceError):
        await get_latest_ice_cover()
