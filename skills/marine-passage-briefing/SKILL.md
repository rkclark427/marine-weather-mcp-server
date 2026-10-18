---
name: marine-passage-briefing
description: Use when planning a sail, passage, crossing, anchorage move, or charter day and the marine_wx MCP tools are available - builds a sourced go/no-go briefing scored per category (wind, seas, clouds) against the region's limits. Also for "is this week usually sailable" questions months ahead.
---

# Marine passage briefing (marine_wx)

Turn the `marine_wx` MCP server's data into a passage briefing a skipper can
act on. The tools assemble and score facts; **you** weigh them and say what
they mean - but the skipper makes the call. Never present the briefing as a
substitute for official forecasts, charts, or seamanship.

If the `marine_*` tools are not available, say so and stop - do not
improvise forecasts from memory or web snippets.

## 1. Pin down the passage

- Waypoints: call `marine_list_registry` to map place names to registry
  names (qualify as `region:name` if ambiguous) and to see the region's
  `thresholds`. Ad-hoc points are fine as `"lat,lon"` strings.
- Departure date and window. Default window is sunrise to 1 hour before
  sunset (be moored/anchored an hour before dark). Ask only if the date is
  genuinely unknown; otherwise state the assumption and continue.
- Who is aboard: guests, or crew only. This changes how yellow is read
  (see step 4).

## 2. Pick the path by lead time

| Departure is... | Use |
|---|---|
| Today to ~16 days out | `marine_route_outlook` (the core of the briefing) |
| More than ~16 days out | `marine_get_climatology` for the date window - typical conditions, not a forecast. Say so plainly. |

## 3. Build the briefing (forecast range)

1. **`marine_route_outlook`** with the ordered waypoints, `depart_date`, and
   window hours if non-default. This is the backbone: per-waypoint scores,
   alerts, zone/offshore forecasts, tides, nav discrepancies, tropical
   cyclones, ice, sun/moon.
2. **Second opinion on wind and seas.** Call `marine_get_model_forecast`
   (ECMWF vs GFS) for the most exposed waypoint. If `METSERVICE_API_KEY` is
   configured, also call `marine_get_metservice_forecast` for that point
   (costs ~12 API units; one call per briefing is plenty).
   - MetService has **no wave data on the Great Lakes** - use it for wind
     and cloud there, and rely on Open-Meteo/NWS for lake waves.
   - MetService "swell" means energy above 8 s; Open-Meteo's swell is the
     wave model's own partition and can be 4-5 s. Do not compare the two
     swell columns one-to-one.
3. **Ground truth near departure** (today/tomorrow only): `marine_get_buoy_obs`
   for the nearest reference buoy - are observations running above or below
   the forecast?
4. **When models disagree or something looks off**, read the human
   forecaster: `marine_get_forecast_discussion` (US offices). For US
   harbor-entry questions, `marine_search_coast_pilot`.
5. Anything flagged (active alerts, a tropical cyclone within range, an
   aid-to-navigation outage on the route, ice) goes at the **top** of the
   briefing, not buried.

## 4. Scoring rules - follow exactly

- **Report each category separately: Wind, Seas, Clouds** (Clouds only where
  the region scores it - e.g. reef-reading in Barbuda). Format per waypoint:
  `Wind GREEN (14 kt, gusts 18) · Seas YELLOW (5 ft, 9 s NE swell) · Clouds RED (100%)`.
- **Never roll the categories up into one overall color or grade.** A red
  on clouds is not a red on wind, and the skipper wants to see which limit
  is binding.
- The route line shows the worst waypoint per category, with its name.
- The thresholds in `registry.json` are the boat owner's own limits. Do not
  substitute your own numbers or soften them. If the region has no
  thresholds, say the conditions are unscored rather than inventing limits.
- **Operating convention** (default - the owner may change it):
  green = fine with guests; yellow = crew only; red = no-go.
  If guests are aboard, any yellow in Wind or Seas means "not with guests"
  - say that explicitly.
- Directional advisories (e.g. northerly swell into an exposed anchorage)
  are reported alongside the scores even when the colors are green.

## 5. What to deliver

Keep it tight - a skipper reads this on a phone at the dock.

1. **Flags first** - alerts, storms, nav outages, or "none".
2. **Per-category route summary** and per-waypoint lines (format above).
3. **Source agreement** - one or two lines: do the models/MetService/buoys
   agree? Name the outlier and by how much.
4. **Timing** - if conditions improve or deteriorate inside the window,
   say when (e.g. "gusts drop below 22 kt after 1300 local").
5. **Your read** - what the numbers suggest (e.g. "a crew-only day; with
   guests, wait for Thursday"). Frame it as a reading of the data, not a
   decision.
6. **Freshness** - model run / issuance times for the main sources, and the
   time zone used (the route's region zone).
7. Close with: *Not for navigation - confirm with official marine forecasts
   and VHF weather before departing.*

## Long-range (climatology) briefings

For `marine_get_climatology`: give the share of green/yellow/red days per
category for the window, the typical daily max wind/gust/seas, and
advisory-day counts. State the caveats the tool reports - ERA5 is ~25 km and
smooths squalls (background trade-wind regime only), and older years may lack
swell partition, so swell-advisory counts can run low. This is "how often is
it inside our limits that week", never a prediction for this year.

## Don'ts

- Don't make the go/no-go decision for the skipper or phrase a forecast as
  certainty.
- Don't present an overall grade.
- Don't fill gaps with remembered weather; if a source failed, say which one
  and what the briefing is missing.
- Don't convert or reinterpret units the tools already resolved (knots,
  feet, region-local time).
