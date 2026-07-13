import pytest

from marine_wx_mcp.registry import RegistryError
from marine_wx_mcp.server import _resolve_waypoint


def test_resolve_waypoint_registry_place():
    lat, lon, label, zones = _resolve_waypoint("kenosha")
    assert (lat, lon) == (42.588, -87.808)
    assert label == "kenosha"
    assert zones == ["LMZ646"]


def test_resolve_waypoint_adhoc_coordinates_have_no_zones():
    lat, lon, label, zones = _resolve_waypoint("43.0,-87.9")
    assert lat == 43.0
    assert lon == -87.9
    assert label == "43.0,-87.9"
    assert zones == []


def test_resolve_waypoint_unknown_place_raises():
    with pytest.raises(RegistryError):
        _resolve_waypoint("atlantis")


def test_resolve_waypoint_malformed_coordinate_string_treated_as_place():
    # "12,34,56" has 3 comma-separated parts, not 2 - falls through to a
    # registry lookup rather than a coordinate parse, and fails there.
    with pytest.raises(RegistryError):
        _resolve_waypoint("12,34,56")
