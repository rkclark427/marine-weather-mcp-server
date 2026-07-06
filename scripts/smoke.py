"""Live smoke test - hits each real data source once and prints formatted output.

Run with the project's venv: .venv\\Scripts\\python.exe scripts\\smoke.py
Requires network access; not part of the pytest suite (which runs offline
against fixtures).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from marine_wx_mcp import server


async def _run(label: str, coro) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001 - smoke test wants to see any failure
        print(f"FAILED: {exc!r}")
        return
    text = str(result)
    print(text[:1500] + ("... [truncated]" if len(text) > 1500 else ""))


async def main() -> None:
    await _run("marine_get_alerts (place=kenosha)", server.marine_get_alerts(place="kenosha"))
    await _run("marine_get_zone_forecast (LMZ646)", server.marine_get_zone_forecast("LMZ646"))
    await _run("marine_get_buoy_obs (45199)", server.marine_get_buoy_obs("45199", hours=3))
    await _run(
        "marine_get_point_forecast (place=kenosha)",
        server.marine_get_point_forecast(place="kenosha", hours=6),
    )
    await _run(
        "marine_get_forecast_discussion (MKX, marine)",
        server.marine_get_forecast_discussion(),
    )
    await _run(
        "marine_get_model_forecast (place=kenosha)",
        server.marine_get_model_forecast(place="kenosha", hours=6),
    )
    await _run("marine_list_registry", server.marine_list_registry())
    await _run(
        "marine_get_windy_forecast (no key -> should be a message, not a crash)",
        server.marine_get_windy_forecast(42.6, -87.7),
    )
    print("\nSmoke test complete.")


if __name__ == "__main__":
    asyncio.run(main())
