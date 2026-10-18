# marine_wx_mcp

> **Not for navigation.** This is a personal, hobby-grade planning aid that
> aggregates third-party forecasts and observations. Data may be late,
> incomplete, wrong, or misread by the model that consumes it, and the
> go/no-go colors are one sailor's personal limits, not safety guidance.
> Always check official sources (NWS/NOAA marine forecasts, Notices to
> Mariners, VHF weather), use current official charts, and rely on your own
> seamanship. Provided as-is with no warranty - see `LICENSE`
> (GPL-3.0, sections 15-16).

Local MCP server giving Claude direct, structured access to marine weather
and navigation data - nationwide. Every tool accepts a coordinate, NDBC
station ID, or NWS zone/location code directly; the registry adds a
`place` shorthand for named ports in two regions today - Lake Michigan
west (Waukegan–Kenosha–Racine–Milwaukee–Chicago) and Antigua & Barbuda -
and can grow by adding a region to `registry.json`. Outside NWS coverage
(e.g. the Caribbean) forecasts come from Open-Meteo automatically. Built to replace
unreliable web-fetch/cached-snippet lookups with live/local structured
data instead.

## Install

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Always use `python -m pip`, not a bare `pip`, so packages land in the venv
you think they're landing in.

## Run the tests

```
.venv\Scripts\python.exe -m pytest tests/ -v
```

Tests run offline against fixtures in `tests/fixtures/` - no network needed.

## Live smoke test

```
.venv\Scripts\python.exe scripts\smoke.py
```

Hits every real data source once and prints formatted output for a quick
eyeball check. Requires network access.

## Coast Pilot search setup (one-time)

`marine_search_coast_pilot` needs a local search index that isn't built
automatically - run once (or whenever you want fresh data):

```
.venv\Scripts\python.exe scripts\ingest_coast_pilot.py
```

Downloads all 10 U.S. Coast Pilot volumes (~77MB total, nationwide -
Atlantic, Gulf, Great Lakes, Pacific, Alaska, Hawaii, Puerto Rico/USVI),
parses them, and writes a SQLite+FTS5 index to
`~/.marine_wx_mcp_cache/coast_pilot/coast_pilot.db`. Takes several
minutes; requires network access. Every run is a full rebuild - see the
script's docstring before passing specific volume numbers to it.

## Claude Desktop registration

Add to `%APPDATA%\Claude\claude_desktop_config.json` under `mcpServers`:

```json
"marine_wx": {
  "command": "C:\\ABSOLUTE\\PATH\\TO\\weather-mcp-server\\.venv\\Scripts\\python.exe",
  "args": ["-m", "marine_wx_mcp"]
}
```

**The `command` must be the venv's Python by absolute path.** Claude Desktop
does not activate virtual environments, so a bare `python` or `python.exe`
will run the wrong interpreter (or none) and the server will silently fail
to start. Restart Claude Desktop after editing the config.

To enable the MetService/MetOcean Point Forecast tools (free personal tier,
key from console.metoceanapi.com), add an `env` block to the `marine_wx`
entry, next to `command` and `args`:
```json
"env": { "METSERVICE_API_KEY": "your-key-here" }
```
Also add `"NWS_CONTACT": "you@example.com"` to the same `env` block. NWS asks
every API client to identify a contact; without it the project URL is sent.
then fully quit and reopen Claude. Without it, `marine_get_metservice_forecast`
and `marine_metservice_check` just return a "not configured" message - they
never crash tool registration. Keep the key out of the repo.

## Companion skill: passage briefings

`skills/marine-passage-briefing/SKILL.md` tells the agent *how* to use these
tools for a passage: which tools to call by lead time, how to cross-check
sources, and how to report go/no-go per category (never one overall grade).
It uses the open Agent Skills `SKILL.md` format.

- Claude Code: copy the folder to `~/.claude/skills/` (all projects) or
  `.claude/skills/` (one project).
- Claude apps: zip the `marine-passage-briefing` folder and upload it as a
  custom skill in Settings.
- Other agents that read `SKILL.md` skills: see that agent's docs for its
  skills directory.

The skill only changes how the agent works; the MCP server above must still
be installed and registered.

## Regions, time zones and go/no-go thresholds

Each region in `registry.json` carries its own `timezone` (all output is
shown in the place's local time), forecast-discussion office, offshore
forecast zone, reference buoys, and `thresholds` - green/yellow/red bands
for sustained wind, gusts, seas and (Antigua) cloud cover, plus
directional advisories such as northerly swell at Barbuda's Low Bay.
**The thresholds are drafts** - edit the numbers in `registry.json` to
your own limits (Lake Michigan and open-ocean limits are deliberately
separate). `marine_route_outlook` scores each waypoint over the passage
window against them; `marine_get_climatology` scores the same calendar
week across past years ("how often is mid-January inside our limits?").

## Tools

See [CLAUDE.md](CLAUDE.md) for the full tool list, data source gotchas, and
verification status. Quick summary:

- `marine_get_alerts` - active marine alerts (Small Craft Advisory, etc.)
- `marine_get_zone_forecast` - coastal/Great Lakes wind/wave forecast for any US marine zone (nationwide)
- `marine_get_offshore_forecast` - Offshore Forecast (OFF), the ~60nm-and-beyond tier
- `marine_get_high_seas_forecast` - High Seas Forecast (HSF), ocean-basin-wide
- `marine_get_buoy_obs` - latest NDBC buoy/station observations
- `marine_get_point_forecast` - NWS gridpoint hourly forecast for a coordinate/place
- `marine_get_forecast_discussion` - latest Area Forecast Discussion (.MARINE section)
- `marine_get_model_forecast` - Open-Meteo ECMWF vs GFS wind comparison
- `marine_get_sun_times` - sunrise/transit/sunset, civil twilight, and moon data for a coordinate/place/date
- `marine_get_tides` - tide predictions, water level, and/or current predictions near a coordinate/place (nationwide)
- `marine_get_aids_to_navigation` - charted Light List entries near a coordinate/place
- `marine_get_navigation_discrepancies` - live aid outages, hazards, temporary changes, marine events near a coordinate/place
- `marine_search_coast_pilot` - full-text search over all 10 U.S. Coast Pilot volumes (nationwide; needs one-time ingest, see above)
- `marine_get_tropical_cyclones` - active tropical cyclones (NHC - Atlantic/Eastern-Central Pacific only)
- `marine_get_ice_conditions` - current Great Lakes ice cover percentage by lake
- `marine_route_outlook` - composite go/no-go passage-planning briefing for a route (alerts + forecast + navigation + tides + storms + ice + sun/moon, all in one call)
- `marine_list_registry` - dump known ports/zones/stations/routes
- `marine_get_metservice_forecast` - MetService/MetOcean point forecast, an independent second source (needs `METSERVICE_API_KEY`)
- `marine_metservice_check` - confirm the MetService key works and list the models it can see

All tools are read-only and accept `response_format: "markdown" | "json"`.

## Troubleshooting

- **Tools don't show up in Claude Desktop**: check the `command` path is
  absolute and points at `.venv\Scripts\python.exe`, then fully restart
  Desktop (not just close the window).
- **A tool returns an error message instead of data**: that's by design -
  every source failure returns actionable text naming the source and a
  fallback tool to try, rather than crashing. Read the message.
- **NDBC station returns "no data"**: some registered stations don't
  actually report (see CLAUDE.md V-2 notes) - try a nearby one from
  `marine_list_registry`.
