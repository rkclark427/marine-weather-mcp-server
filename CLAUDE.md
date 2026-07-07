# CLAUDE.md - maintenance brief for marine_wx_mcp

Local stdio MCP server for marine weather data (Lake Michigan v1). Built for
Kent Clark (Windward Clark LLC) per the design brief in the original build
request. This file is for future Claude Code sessions maintaining or
extending it - read it before touching sources/ or registry.json.

## Environment conventions (this machine)

- Venv is always named `.venv`, created with `python -m venv .venv`.
- Always invoke pip as `python -m pip`, never a bare `pip`.
- Windows: `zoneinfo` needs the `tzdata` package (IANA tz data isn't
  shipped with Python on Windows) - it's a declared dependency
  (`tzdata; sys_platform == 'win32'`), don't remove it.
- Claude Desktop does **not** activate virtual environments. The
  `command` in `claude_desktop_config.json` must be the absolute path to
  `.venv\Scripts\python.exe`. A bare `python` will silently run the wrong
  interpreter or fail to start - this is the most common first-launch
  failure mode.

## Required: NWS User-Agent

Every `api.weather.gov` request must send a `User-Agent` naming the app and
a real contact email, or requests get rejected/throttled. Currently set in
`sources/nws.py`:

```python
USER_AGENT = "marine_wx_mcp/0.1 (robert.kent.clark@gmail.com)"
```

If the contact email changes, update it there - it's centralized in one
place by design.

## Verification status (V-items from the build brief)

All live-verified 2026-07-06. Fixtures captured in `tests/fixtures/`.

- **V-1 (zone forecast source)**: CONFIRMED the anticipated fallback is
  required. `/zones/forecast/{zone}/forecast` returns 404 "Marine Forecast
  Not Supported" for every LMZ zone. `marine_get_zone_forecast` uses the
  Nearshore Marine Forecast (NSH) text product instead
  (`nws.get_nearshore_forecast`), parsed by `LMZ###-` header / `$$`
  delimiter. Do not attempt to re-enable the zones/forecast endpoint for
  marine zones - it's a real, permanent API limitation, not a fixture
  artifact.
- **V-2 (station reachability)**: Two registry stations from the original
  seed don't report and were replaced - see per-port `notes` in
  `registry.json` for the reasoning:
  - `KNSW3` (Kenosha Light) is a real, currently-registered NDBC station
    but flagged `met=n` in NDBC's own metadata - no realtime2 feed exists,
    ever. Replaced with `45199` for Kenosha/Racine.
  - `45007` isn't in NDBC's active-station list at all currently. Replaced
    with `45186` (Waukegan) and `45013` (Milwaukee).
  - Chicago now has `CNII2` and `CHII2`, both confirmed live.
  - **Gotcha**: NDBC realtime2 URLs are case-sensitive
    (`realtime2/KNSW3.txt` works, `realtime2/knsw3.txt` 404s even though
    the station ID is case-insensitive everywhere else). `ndbc.py` always
    uppercases before building the URL - don't remove that.
- **V-3 (Open-Meteo Great Lakes wave coverage)**: CONFIRMED real data
  exists (tested 42.6,-87.7: 168/168 hourly values populated, magnitudes
  consistent with the NWS nearshore text forecast). The spec's anticipated
  "unsupported" honest-fallback path is still implemented defensively
  (`openmeteo.get_marine_forecast` sets `no_data`/`no_data_message` if a
  future call ever comes back all-null) but is not expected to trigger for
  this region.
- **V-4 (registry coordinates/zones)**: Confirmed against live NWS
  `/points` lookups. One nuance: harbor-coordinate points resolve to the
  nearest *land* zone via `/points`, not the marine zone - this is
  expected NWS behavior, not a bug. Tools must use the registry's static
  `zones`/`zone_offices` mappings rather than deriving marine zone from
  `/points` live.
- **V-5 (Claude Desktop end-to-end)**: Server registered in
  `claude_desktop_config.json` as `marine_wx`, absolute venv Python path.
  Full MCP stdio handshake (initialize + list_tools + call_tool) verified
  programmatically. **Restarting Claude Desktop and confirming the tools
  appear in the app itself is the one remaining manual step** - do that
  after any change to `server.py`'s tool signatures.
- **R-1 (GLCFS/GLERL)**: not investigated - v2 roadmap item, no action
  taken.
- **V-6 (USNO sun/twilight, added 2026-07-06)**: CONFIRMED endpoint works
  and needs no key, but the `tz`/`dst` params have a non-obvious contract
  - see the gotcha below. `sundata` only carries civil twilight (no
  nautical/astronomical), confirmed live and matches the tool's
  documented caveat.

## Other gotchas worth knowing before editing sources/

- **USNO `rstt/oneday` `tz` param is the STANDARD (non-DST) UTC offset**,
  and `dst=true` adds the daylight hour on top - passing the already-DST
  offset (e.g. `tz=-5` for Chicago in July) with `dst=true` silently
  computes UTC-4, an hour wrong. Confirmed by diffing against the
  tz-less (UTC) response for the same date/coords. `usno._std_offset_and_dst`
  derives both the standard offset and whether DST applies from
  `zoneinfo` for the requested date, rather than hardcoding either -
  don't replace that with a fixed offset even though this project only
  serves one timezone today.

- **NWS gridpoint interval values** (`waveHeight`, `wavePeriod`, etc.) use
  RFC 3339 duration `validTime` strings like
  `"2026-07-06T07:00:00+00:00/PT2H"` - each entry covers a *span*, not an
  instant. `formatting.expand_grid_series` / `value_at` handle this; don't
  try to index these arrays positionally against hourly forecast periods.
- **Open-Meteo multi-model responses** suffix every hourly variable with
  the model name (`wind_speed_10m_ecmwf_ifs025`,
  `wind_speed_10m_gfs_seamless`), sharing one `time` array - not separate
  per-model blocks. `openmeteo._split_model_suffix` handles this.
- **NWS hourly forecast `windSpeed`** comes back as a pre-formatted mph
  string (e.g. `"10 to 15 mph"`), not a number in knots.
  `formatting.mph_string_to_knots` converts it for consistency with the
  rest of the server's knots convention.
- Every source client exception (`NWSError`, `NDBCError`,
  `OpenMeteoError`, `RegistryError`) carries an actionable message naming
  the source and a fallback tool - tool functions in `server.py` catch
  these and return the message as the tool result rather than crashing.
  Keep that pattern when adding tools.

## Package layout

```
src/marine_wx_mcp/
  server.py       - FastMCP init + all @mcp.tool() registrations
  registry.py     - loader/query helpers for registry.json
  registry.json   - ports/zones/stations/routes (Lake Michigan west)
  formatting.py   - markdown rendering + unit conversion helpers
  sources/
    nws.py        - api.weather.gov (alerts, gridpoints, NSH, AFD)
    ndbc.py       - NDBC realtime2 fetch + parser
    openmeteo.py  - Open-Meteo forecast + marine clients
    usno.py       - aa.usno.navy.mil sun/twilight (rstt/oneday)
    windy.py      - Windy Point Forecast v2 stub (inert w/o WINDY_API_KEY)
tests/            - offline unit tests against tests/fixtures/
scripts/smoke.py  - live one-shot smoke test (network required)
```

`marine_wx_mcp.sources` is meant to be importable directly by other
projects (e.g. a future Great Lakes passage-planning tool) - keep it free
of MCP-specific dependencies (that's why `server.py` does all the
`@mcp.tool()` wiring rather than the source modules themselves).

## v2 roadmap (do not build without asking)

1. Windy Point Forecast activation (remember: no ECMWF even when paid).
2. GLCFS/GLERL Great Lakes wave/current model access (R-1, not investigated).
3. `marine_route_outlook(route, depart_time)` - composite go/no-go briefing
   across alerts + zone forecast + point forecast along a registry route.
4. Response caching respecting NWS expiry headers; Caribbean region
   registry (Antigua, Jan 2027); CO-OPS tides/currents.
