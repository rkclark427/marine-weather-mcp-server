import json
from pathlib import Path

import httpx
import pytest

from marine_wx_mcp.sources import nhc
from marine_wx_mcp.sources.nhc import NHCError, get_active_storms

FIXTURES = Path(__file__).parent / "fixtures" / "nhc"


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

    async def get(self, url):
        return _FakeResponse(self._payload)


def _patch(monkeypatch, fixture_name: str):
    payload = json.loads((FIXTURES / fixture_name).read_text())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(payload))


@pytest.mark.asyncio
async def test_get_active_storms_empty(monkeypatch):
    _patch(monkeypatch, "current_storms_empty.json")

    storms = await get_active_storms()

    assert storms == []


@pytest.mark.asyncio
async def test_get_active_storms_parses_sample_fields(monkeypatch):
    _patch(monkeypatch, "current_storms_sample.json")

    storms = await get_active_storms()

    assert len(storms) == 4
    gert = storms[0]
    assert gert["name"] == "Gert"
    assert gert["classification"] == "TD"
    assert gert["classification_name"] == "Tropical Depression"
    assert gert["wind_kt"] == "25"
    assert gert["lat"] == 17.1
    assert gert["lon"] == -58.4
    assert gert["advisory_url"].endswith(".shtml")


@pytest.mark.asyncio
async def test_get_active_storms_handles_unknown_classification(monkeypatch):
    _patch(monkeypatch, "current_storms_sample.json")

    storms = await get_active_storms()

    hilary = next(s for s in storms if s["name"] == "Hilary")
    assert hilary["classification"] == "PTC"
    assert hilary["classification_name"] == "Post-Tropical Cyclone"


@pytest.mark.asyncio
async def test_get_active_storms_http_error_raises_nhcerror(monkeypatch):
    class _FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FailingClient())

    with pytest.raises(NHCError):
        await get_active_storms()
