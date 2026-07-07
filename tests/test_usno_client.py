import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from marine_wx_mcp.sources import usno
from marine_wx_mcp.sources.usno import USNOError, _std_offset_and_dst, get_sun_times

FIXTURES = Path(__file__).parent / "fixtures" / "usno"


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, status_code: int, payload):
        self._status_code = status_code
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        return _FakeResponse(self._status_code, self._payload)


def _patch(monkeypatch, fixture_name: str, status_code: int = 200):
    payload = json.loads((FIXTURES / fixture_name).read_text())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(status_code, payload))


def test_std_offset_and_dst_summer():
    std_hours, is_dst = _std_offset_and_dst(date(2026, 7, 6), "America/Chicago")
    assert std_hours == -6
    assert is_dst is True


def test_std_offset_and_dst_winter():
    std_hours, is_dst = _std_offset_and_dst(date(2026, 1, 15), "America/Chicago")
    assert std_hours == -6
    assert is_dst is False


@pytest.mark.asyncio
async def test_get_sun_times_summer(monkeypatch):
    _patch(monkeypatch, "oneday_kenosha_summer.json")

    result = await get_sun_times(42.588, -87.808, date(2026, 7, 6))

    assert result["date"] == "2026-07-06"
    events = result["events"]
    assert set(events) == {
        "Begin Civil Twilight",
        "Rise",
        "Upper Transit",
        "Set",
        "End Civil Twilight",
    }
    # summer -> CDT (-05:00), and rise before transit before set
    assert events["Rise"].endswith("-05:00")
    assert events["Rise"] < events["Upper Transit"] < events["Set"]


@pytest.mark.asyncio
async def test_get_sun_times_winter_uses_cst_offset(monkeypatch):
    _patch(monkeypatch, "oneday_kenosha_winter.json")

    result = await get_sun_times(42.588, -87.808, date(2026, 1, 15))

    assert result["events"]["Rise"].endswith("-06:00")


@pytest.mark.asyncio
async def test_get_sun_times_400_raises_actionable_error(monkeypatch):
    _patch(monkeypatch, "oneday_kenosha_summer.json", status_code=400)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _FakeClient(400, {"error": "Please supply valid coordinates"}),
    )

    with pytest.raises(USNOError, match="valid coordinates"):
        await get_sun_times(999, 999, date(2026, 7, 6))


@pytest.mark.asyncio
async def test_get_sun_times_500_html_error_handled(monkeypatch):
    class _HtmlResponse:
        status_code = 500
        text = "<html><title>500 Internal Server Error</title></html>"

        def json(self):
            raise ValueError("not json")

    class _HtmlClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            return _HtmlResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _HtmlClient())

    with pytest.raises(USNOError, match="500"):
        await get_sun_times(42.588, -87.808, date(2026, 7, 6))
