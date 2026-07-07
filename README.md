# marine_wx_mcp

Local MCP server giving Claude direct, structured access to marine weather
data for Lake Michigan (Waukegan–Kenosha–Racine–Milwaukee–Chicago) sailing
passage planning. Built to replace unreliable web-fetch/cached-snippet
lookups with live calls to api.weather.gov, NDBC, and Open-Meteo.

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

To enable the optional Windy Point Forecast tool later, add:
```json
"env": { "WINDY_API_KEY": "..." }
```
Without it, `marine_get_windy_forecast` just returns a "not configured"
message - it never crashes tool registration.

## Tools

See [CLAUDE.md](CLAUDE.md) for the full tool list, data source gotchas, and
verification status. Quick summary:

- `marine_get_alerts` - active marine alerts (Small Craft Advisory, etc.)
- `marine_get_zone_forecast` - nearshore wind/wave forecast by LMZ zone
- `marine_get_buoy_obs` - latest NDBC buoy/station observations
- `marine_get_point_forecast` - NWS gridpoint hourly forecast for a coordinate/place
- `marine_get_forecast_discussion` - latest Area Forecast Discussion (.MARINE section)
- `marine_get_model_forecast` - Open-Meteo ECMWF vs GFS wind comparison
- `marine_get_sun_times` - sunrise/transit/sunset + civil twilight for a coordinate/place/date
- `marine_list_registry` - dump known ports/zones/stations/routes
- `marine_get_windy_forecast` - Windy Point Forecast (v2 roadmap, inert without a key)

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
