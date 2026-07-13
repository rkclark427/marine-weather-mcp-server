from marine_wx_mcp.formatting import format_route_outlook

_BASE_RESULT = {
    "waypoints": ["kenosha"],
    "depart_date": "2026-07-13",
    "zones": [],
    "alerts_by_zone": {},
    "zone_forecasts": {},
    "waypoint_details": [],
    "tropical_cyclones": [],
    "ice": None,
    "sun_moon": None,
}


def test_format_route_outlook_shows_zero_percent_ice_not_blank():
    # Regression: `if total:` treated 0% (falsy) as "no data" and hid the
    # line entirely - off-season Great Lakes ice is legitimately 0%.
    result = {
        **_BASE_RESULT,
        "ice": {"date": "2026-05-16T00:00:00Z", "lakes": {"Michigan": 0}, "total": 0},
    }

    output = format_route_outlook(result)

    assert "Basin-wide total: 0%" in output


def test_format_route_outlook_shows_nonzero_ice():
    result = {
        **_BASE_RESULT,
        "ice": {"date": "2026-02-01T00:00:00Z", "lakes": {"Michigan": 36.57}, "total": 54.61},
    }

    output = format_route_outlook(result)

    assert "Basin-wide total: 54.61%" in output


def test_format_route_outlook_no_alerts_message():
    result = {**_BASE_RESULT, "zones": ["LMZ646"], "alerts_by_zone": {}}

    output = format_route_outlook(result)

    assert "No active alerts." in output


def test_format_route_outlook_lists_alert_events():
    result = {
        **_BASE_RESULT,
        "zones": ["LMZ646"],
        "alerts_by_zone": {
            "LMZ646": [
                {"properties": {"event": "Small Craft Advisory", "headline": "SCA until 10 PM"}}
            ]
        },
    }

    output = format_route_outlook(result)

    assert "Small Craft Advisory" in output
    assert "SCA until 10 PM" in output


def test_format_route_outlook_waypoint_with_no_discrepancies():
    result = {
        **_BASE_RESULT,
        "waypoint_details": [
            {
                "label": "kenosha",
                "lat": 42.588,
                "lon": -87.808,
                "discrepancies": {"federal_aid": [], "private_aid": []},
            }
        ],
    }

    output = format_route_outlook(result)

    assert "Navigation discrepancies:** none nearby" in output
