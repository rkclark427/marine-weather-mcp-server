import pytest

from marine_wx_mcp.sources import windy


@pytest.mark.asyncio
async def test_get_point_forecast_without_key_returns_message_not_exception(monkeypatch):
    monkeypatch.delenv("WINDY_API_KEY", raising=False)

    result = await windy.get_point_forecast(42.6, -87.7)

    assert result["configured"] is False
    assert "not configured" in result["message"]


def test_is_configured_false_when_unset(monkeypatch):
    monkeypatch.delenv("WINDY_API_KEY", raising=False)
    assert windy.is_configured() is False


def test_is_configured_true_when_set(monkeypatch):
    monkeypatch.setenv("WINDY_API_KEY", "test-key")
    assert windy.is_configured() is True
