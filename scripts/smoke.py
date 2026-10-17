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
    await _run(
        "marine_get_zone_forecast (ANZ630, Chesapeake Bay - not in registry, "
        "proves nationwide CWF fallback)",
        server.marine_get_zone_forecast("ANZ630"),
    )
    await _run("marine_get_offshore_forecast (N01)", server.marine_get_offshore_forecast("N01"))
    await _run("marine_get_high_seas_forecast (AT1)", server.marine_get_high_seas_forecast("AT1"))
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
    await _run(
        "marine_get_sun_times (place=kenosha)",
        server.marine_get_sun_times(place="kenosha"),
    )
    await _run(
        "marine_get_tides (place=kenosha, Great Lakes - water level only)",
        server.marine_get_tides(place="kenosha"),
    )
    await _run(
        "marine_get_tides (Eastport ME - tide + water level + currents)",
        server.marine_get_tides(lat=44.9, lon=-66.98),
    )
    await _run(
        "marine_get_aids_to_navigation (place=kenosha, 5nm)",
        server.marine_get_aids_to_navigation(place="kenosha", radius_nm=5),
    )
    await _run(
        "marine_get_navigation_discrepancies (place=racine, 15nm)",
        server.marine_get_navigation_discrepancies(place="racine", radius_nm=15),
    )
    await _run(
        "marine_search_coast_pilot (Kenosha fuel - requires prior "
        "scripts/ingest_coast_pilot.py run)",
        server.marine_search_coast_pilot("Kenosha fuel", limit=3),
    )
    await _run(
        "marine_get_tropical_cyclones (all active, Atlantic/E&C Pacific)",
        server.marine_get_tropical_cyclones(),
    )
    await _run(
        "marine_get_ice_conditions (all lakes)",
        server.marine_get_ice_conditions(),
    )
    await _run(
        "marine_route_outlook (waukegan -> north_point -> kenosha)",
        server.marine_route_outlook(["waukegan", "north_point", "kenosha"]),
    )
    # --- Antigua / non-US region (added 2026-10-11) ---
    await _run(
        "marine_get_offshore_forecast (NT3, zone AMZ051 Leeward Islands)",
        server.marine_get_offshore_forecast("NT3", zones=["AMZ051"]),
    )
    await _run(
        "marine_get_high_seas_forecast (AT1, NHC section only)",
        server.marine_get_high_seas_forecast("AT1", section="nhc"),
    )
    await _run(
        "marine_get_model_forecast (antigua_barbuda_channel - swell split, AST times)",
        server.marine_get_model_forecast(place="antigua_barbuda_channel", hours=12, step_hours=3),
    )
    await _run(
        "marine_get_point_forecast (english_harbour - should fall back to Open-Meteo)",
        server.marine_get_point_forecast(place="english_harbour", hours=6),
    )
    await _run(
        "marine_get_sun_times (barbuda_low_bay, 2027-01-12 - expect ~06:41/17:51 AST)",
        server.marine_get_sun_times(place="barbuda_low_bay", date="2027-01-12"),
    )
    await _run(
        "marine_route_outlook (great_bird_island -> channel -> barbuda_low_bay)",
        server.marine_route_outlook(["great_bird_island", "antigua_barbuda_channel", "barbuda_low_bay"]),
    )
    await _run(
        "marine_get_climatology (channel, 01-09..01-16, 3 years)",
        server.marine_get_climatology("01-09", "01-16", place="antigua_barbuda_channel", years=3),
    )
    await _run("marine_list_registry", server.marine_list_registry())
    await _run(
        "marine_metservice_check (no key -> should be a message, not a crash)",
        server.marine_metservice_check(),
    )
    await _run(
        "marine_get_metservice_forecast (antigua_barbuda_channel, 24h @3h)",
        server.marine_get_metservice_forecast(place="antigua_barbuda_channel", hours=24, step_hours=3),
    )
    print("\nSmoke test complete.")


if __name__ == "__main__":
    asyncio.run(main())
