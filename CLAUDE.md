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
  documented caveat. Moon data added 2026-07-13: the same response
  already carried `moondata`/`curphase`/`fracillum`/`closestphase` that
  were being discarded - confirmed live that `closestphase` can fall on
  a different calendar day than the requested date (own
  day/month/year fields, not `when`) and that `moondata` legitimately
  has 0-3 entries depending on the day (moonrise/set don't occur every
  calendar day since moonrise shifts ~50min later daily) - not a
  parsing failure if `moon_events` comes back sparse.
- **V-7 (NAVCEN aids to navigation, added 2026-07-12)**: CONFIRMED both
  the Light List and the live discrepancy/hazard layers work and need no
  key. Real field names captured live (differ in places from the initial
  description - e.g. discrepancy layers use `LLNR`/`ATON_UID` where the
  Light List uses `LIGHT_LIST_NUMBER`/`AID_UID`; join on the UID field,
  not the LLNR, if ever cross-referencing the two). Confirmed the
  statute-mile range unit and the pagination-wraparound gotcha below live
  - both matched the tip-off given for this build, not assumed. **Also
  worth recording**: verification was blocked for a while not by
  anything about the API, but by the dev machine's Surfshark VPN - its
  DNS resolver failed on `navcen.uscg.gov` and (transiently)
  `aa.usno.navy.mil` while resolving everything else fine. Disconnecting
  the VPN fixed it immediately. If a future session hits inexplicable
  DNS failures on just one or two domains, check for an active VPN
  before assuming the domain itself is down.
- **V-8 (Coast Pilot national ingest, added 2026-07-13)**: CONFIRMED no
  structured API exists - the "xml2html" tool on NOAA's site is a PDF/ZIP
  download hub despite the name, not HTML/XML rendering. Confirmed all
  10 volumes' combined-book PDFs (~77MB total), confirmed the geographic
  coverage of each (see `coast_pilot.VOLUME_COVERAGE`), and confirmed the
  numbered-paragraph convention and running-header chapter tagging hold
  identically in two widely separated volumes (Great Lakes/Vol 6 and
  Eastport ME/Vol 1) before generalizing the parser to all ten - not
  assumed from one sample. Text extraction quality is uneven: front-matter
  preface pages come out with spurious mid-word spaces (a kerning
  artifact), but the harbor-entry body text that actually matters
  extracts cleanly. User decided (2026-07-13): ingest all 10 volumes in
  one pass (not staged by sector - the ~77MB total made staging pointless),
  manual re-ingest on demand (no scheduled auto-refresh). Live full-series
  ingest run 2026-07-13: 82,639 paragraphs across all 10 volumes, ~40%
  with an explicit or inherited coordinate. One transient network failure
  hit volume 5 mid-run on an earlier attempt (silently dropped from the
  index since every ingest is a full rebuild) - `fetch_volume_pages` now
  retries 3x with a 5s backoff before giving up, and `main` prints a
  loud `*** WARNING ***` naming any volume that still failed after
  retries rather than silently reporting success on a partial index.
  **Known, accepted limitation**: `harbor_heading` attribution is
  unreliable in every volume's regulatory front-matter (Chapters 1-2 -
  confirmed live the top offenders like "Exemptions", "Note", "Current"
  are exclusively/mostly Ch1-2 content, not harbor names) and can be
  briefly knocked off by a mid-chapter regulatory callout (confirmed:
  Eastport's own customs paragraph in Vol 1 Ch 4 got attributed to a
  preceding "North Atlantic Right Whales" protected-species notice
  instead). This was investigated across three ingest iterations (fixed
  two real bugs - heading state leaking across chapter boundaries, and
  short all-caps PDF-extraction noise like "NNN" passing as a heading -
  each confirmed live with before/after row counts) before concluding
  the remainder needs font/layout-aware PDF parsing to fix properly,
  which plain-text extraction can't give us. **This does not affect
  search quality** - `marine_search_coast_pilot` matches against
  paragraph `text`, not `harbor_heading`, and every spot-check (Eastport
  customs, Kenosha, Waukegan, Honolulu, San Francisco) returned the
  correct paragraph regardless of a wrong heading label. Don't try to
  "fix" `harbor_heading` further without a real reason - it's cosmetic on
  the primary use case.
- **V-9 (nationwide marine zone/offshore/high-seas forecasts, added
  2026-07-13)**: CONFIRMED `/zones/marine/{zoneId}` returns the
  responsible office live for any US coastal/Great Lakes marine zone
  (`awipsLocationIdentifier`/`cwa`) - this replaced the old hardcoded
  `registry.office_for_zone`/`zone_offices` table, which is now deleted
  entirely (`marine_get_zone_forecast` works for any zone nationwide,
  not just the 6 in the registry - confirmed live against `ANZ630`,
  Chesapeake Bay, which was never in the registry). Confirmed CWF
  (Coastal Waters Forecast, everywhere outside the Great Lakes) uses
  the byte-for-byte identical text format to NSH, so the existing
  parser needed no changes, just a rename
  (`parse_nsh_product`→`parse_zone_forecast_product`) and an NSH→CWF
  fallback. Confirmed live that NSH-issuing offices (exactly 11: APX,
  BUF, CLE, DLH, DTX, GRB, GRR, IWX, LOT, MKX, MQT) and CWF-issuing
  offices are mutually exclusive sets, so trying NSH first and falling
  back to CWF never double-fetches.
  Confirmed offshore (OFF) and high-seas (HSF) forecasts are a
  genuinely different tier: issued by national centers under special
  location codes (e.g. `N01`, `AT1`), not local WFO office codes -
  and confirmed live that a marine zone's own metadata does NOT
  reliably resolve to these codes (offshore zone `AMZ040`'s
  `awipsLocationIdentifier` is `NH2`, which isn't even in OFF's own
  valid-locations list) - so `marine_get_offshore_forecast`/
  `marine_get_high_seas_forecast` take the location/area code directly
  rather than trying to force a `zone_id` parameter that doesn't
  cleanly exist at this tier. Confirmed OFF has no UGC zone codes or
  `$$` delimiters at all (bare Title Case name line immediately
  followed by `.PERIOD...` entries, no terminator) - needed a new
  parser (`parse_off_product`). HSF is returned as full text, not
  parsed into sub-areas - confirmed it's an ocean-basin-wide narrative
  (ALL CAPS, embedded lat/lon polygons, ice/tropical-cyclone caveats)
  with no zone-style subdivision to parse against.
- **V-10 (CO-OPS tides/currents, added 2026-07-13)**: CONFIRMED no key
  needed, and confirmed live that Lake Michigan has zero tide-prediction
  and zero current-prediction stations (no astronomical tide on the
  Great Lakes - both `tidepredictions` and `currentpredictions` station
  lists return 0 results in a Lake Michigan bounding box) but does have
  `waterlevels` stations (Milwaukee, Calumet Harbor) - designed
  `marine_get_tides` to report whichever of the three product types has
  a station within 50nm rather than assuming all three apply everywhere.
  Confirmed the three station lists are disjoint (not one list with
  capability flags) and datum must be picked from the station's own
  `greatlakes` boolean (IGLD there, MLLW on tidal coasts) - requesting
  tide predictions against a Great Lakes station returns a clean
  `{"error": {"message": "Great Lakes stations don't have Predictions
  data."}}`, not a crash or a misleadingly-empty result. Also confirmed
  live that `date=today&range=<hours>` does NOT reliably extend
  `interval=hilo` tide predictions past the current day (range=48 only
  returned today's ~4 events) - switched to explicit
  `begin_date`/`end_date` which does give the correct multi-day window.
- **V-11 (NHC tropical cyclones, added 2026-07-13)**: CONFIRMED no key
  needed for `CurrentStorms.json`. No storm was active in either Atlantic
  or Eastern/Central Pacific during verification (mid-July, before Aug-
  Oct peak season) - schema confirmed instead against NHC's own official
  sample file (`nhc.noaa.gov/productexamples/NHC_JSON_Sample.json`),
  which is exactly what NHC publishes as its field reference; one real
  field name mismatch was caught this way (`classification: "PTC"` for
  post-tropical, not the "PT" first guessed) before it could ship wrong.
  **Confirmed live NHC's feed only covers its own area of responsibility**
  - api.weather.gov had an active `TCP` (Public Tropical Cyclone
  Advisory) product from `PGUM` (Guam) for a Western Pacific system at
  the same moment NHC's `CurrentStorms.json` was completely empty -
  different basin, different issuing agency (JTWC-adjacent, not NHC) -
  `marine_get_tropical_cyclones` documents this scope limit rather than
  implying global coverage. `movementSpeed` is presented as mph per
  NHC's well-established public-advisory convention; the JSON field
  itself carries no unit label, so this is inferred from known NHC
  publication style, not verified from this specific field.
- **V-12 (Great Lakes ice cover, added 2026-07-13)**: CONFIRMED no key
  needed. The obvious-by-name candidate dataset on GLERL's ERDDAP,
  `GL_Ice_Cover` ("Great Lakes Ice Cover Dataset"), looked right in
  `/info/` metadata but its actual griddap query endpoint returned a
  consistent 404 "Currently unknown datasetID" on retry - confirmed
  genuinely down, not a URL mistake, since a different dataset
  (`glerlIce`) answered normally on the same server at the same moment.
  Switched to `glerlIce` ("Great Lakes Ice Concentration, Daily"),
  which turned out to be a better fit anyway: simple daily whole-lake
  percentages (`tabledap`, one row per day, a column per lake), not a
  spatial grid needing lat/lon subsetting. Confirmed the actual values
  are real/climatologically sane, not placeholders - Feb 2026 showed
  Erie ~94-95% (shallow, freezes fastest) vs. Michigan ~30-36% (deep,
  freezes least), matching known Great Lakes ice behavior. Confirmed
  live that outside ice season every lake legitimately reads 0%
  (checked the latest available row, 2026-05-16, post-season) - the
  tool says so explicitly rather than presenting zeros as a possible
  data failure.
- **marine_route_outlook (added 2026-07-13)**: not a new data source -
  orchestrates the existing tools (alerts, zone forecast, point
  forecast, aids/discrepancies, tides/water level, tropical cyclones,
  ice, sun/moon) into one composite passage-planning briefing, per
  Kent's stated primary use case for this server ("Claude Desktop
  helping prepare for a crossing or itinerary"). Accepts a mix of
  registry place names and ad-hoc `"lat,lon"` waypoints in one call
  (`_resolve_waypoint` in `server.py`) - ad-hoc coordinates skip
  zone-based alerts/forecast (no registry zone to resolve there) but
  still get everything coordinate-based. Verified live with a mixed
  registry+coordinate route and confirmed the zone-skip behaves
  correctly (only the registry waypoint's zone appears in output).
  Deliberately does NOT make the go/no-go judgment itself - assembles
  the raw facts and leaves the actual reasoning to the calling model,
  consistent with every other tool in this server. Found and fixed one
  real bug while verifying live: the ice section used `if total:` which
  treats a legitimate 0% (off-season) as falsy and silently drops the
  line - changed to `if total is not None:` and added a regression test
  (`test_format_route_outlook_shows_zero_percent_ice_not_blank`) since
  this class of bug is easy to reintroduce and easy to miss by eye.

## Other gotchas worth knowing before editing sources/

- **Coast Pilot has a real two-level heading structure that isn't
  obvious from a quick sample.** Standard subsection labels ("Channels",
  "Wharves", "Small-craft facilities", etc.) recur under every harbor in
  every volume - confirmed "Channels" alone appears under 100+ different
  harbors in Chapter 11. `coast_pilot.parse_pages` tracks harbor_heading
  and subsection_heading separately and only resets/inherits coordinates
  at the harbor level (`_SUBSECTION_LABEL_PREFIXES`); collapsing these
  into one flat heading (an earlier version of this code did) loses the
  harbor attribution for every paragraph after the first subsection
  marker - verified live this affected roughly 2/3 of all paragraphs in
  the sample chapter (coordinate coverage went from 33% to 69% after the
  fix).
- **Not every Coast Pilot paragraph states its own coordinates** - most
  harbor-facility paragraphs don't repeat the harbor's position. Inherited
  coordinates propagate forward only while `harbor_heading` stays the
  same string, specifically to avoid falsely attributing one harbor's
  position to the next harbor's "Channels"/"Coast Guard"/etc. paragraphs
  just because those labels recur.
- **Coast Pilot ingestion is a one-time bulk job, not a live source
  client** - unlike every other `sources/` module, `coast_pilot.py` never
  makes a network request at query time. `scripts/ingest_coast_pilot.py`
  does the fetching/parsing and writes to
  `~/.marine_wx_mcp_cache/coast_pilot/coast_pilot.db` (SQLite + FTS5);
  `marine_search_coast_pilot` only ever reads that file and raises an
  actionable `CoastPilotError` naming the ingest script if it doesn't
  exist yet. Every ingest run is a full rebuild - there's no incremental
  per-volume refresh, so re-running with a subset of volumes silently
  drops the others (documented in the script's own docstring; don't
  "fix" this by adding partial-refresh logic without checking whether
  it's actually wanted first).

- **NAVCEN GeoJSON files paginate via `properties.exceededTransferLimit`,
  not via HTTP status.** Each `{layer}_{N}.geojson` page is capped at
  1000 features by the underlying ArcGIS export; confirmed live that
  requesting a page number past the real last one returns HTTP 200 with
  a full, valid (but duplicate/wrapped) FeatureCollection rather than a
  404 or empty result - `navcen._fetch_all_pages` stops based on the
  flag, never by probing until failure. Don't hardcode a fixed page
  count even though District 09's Light List happens to be 5 pages
  today (4697 records) and the discrepancy layers 1-3 pages each.
- **NAVCEN Light List `LIGHT_NOM_RANGE_UNIT` is "STAT MI"** (statute
  miles), not nautical miles, unlike every other distance in this
  server. `navcen.light_range_nm` converts; don't consume
  `LIGHT_NOM_RANGE` raw.
- **NAVCEN Light List (`lightListD{district}`) is district-scoped**
  (District 09 = the whole Great Lakes, not just Lake Michigan) but the
  **discrepancy/hazard/change/event layers are nationwide** single
  datasets with no district split - both need client-side radius
  filtering (`navcen.filter_by_radius`, haversine) since neither
  supports a server-side geographic query.
- NAVCEN data is bulk-downloaded and cached to disk at
  `~/.marine_wx_mcp_cache/navcen/` with a 6h TTL
  (`navcen.CACHE_TTL_SECONDS`), not fetched live per tool call - the
  Light List alone is ~9MB across 5 pages. Delete the cache dir to force
  a refresh sooner; don't reduce the TTL to make testing more convenient
  without reverting it.

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
  `OpenMeteoError`, `RegistryError`, `USNOError`, `NavcenError`,
  `CoastPilotError`, `CoopsError`, `NHCError`, `GlerlIceError`) carries
  an actionable message naming the source and a fallback tool - tool
  functions in `server.py` catch these and return the message as the
  tool result rather than crashing. Keep that pattern when adding tools.

## Package layout

```
src/marine_wx_mcp/
  server.py       - FastMCP init + all @mcp.tool() registrations
  registry.py     - loader/query helpers for registry.json
  registry.json   - ports/zones/stations/routes (Lake Michigan west)
  formatting.py   - markdown rendering + unit conversion helpers
  sources/
    nws.py         - api.weather.gov (alerts, gridpoints, NSH/CWF, OFF, HSF, AFD)
    ndbc.py        - NDBC realtime2 fetch + parser
    openmeteo.py   - Open-Meteo forecast + marine clients
    usno.py        - aa.usno.navy.mil sun/twilight + moon (rstt/oneday)
    navcen.py      - navcen.uscg.gov MSI Light List + discrepancy layers (cached)
    coast_pilot.py - local SQLite/FTS5 index over all 10 Coast Pilot volumes (no live API)
    coops.py       - CO-OPS tides/water levels/currents (station lists cached 24h)
    nhc.py         - NHC active tropical cyclones (Atlantic/E&C Pacific only)
    glerl_ice.py   - GLERL daily Great Lakes ice cover by lake (ERDDAP tabledap)
    _geo.py        - shared haversine helper (navcen.py, coast_pilot.py, coops.py, server.py)
    windy.py       - Windy Point Forecast v2 stub (inert w/o WINDY_API_KEY)
tests/                        - offline unit tests against tests/fixtures/
scripts/smoke.py              - live one-shot smoke test (network required)
scripts/ingest_coast_pilot.py - one-time/manual Coast Pilot ingest (network required, ~77MB)
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
