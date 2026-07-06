import pytest

from marine_wx_mcp.registry import (
    RegistryError,
    dump_registry,
    get_port,
    get_route,
    office_for_zone,
    zones_for_place,
)


def test_office_for_zone():
    assert office_for_zone("LMZ646") == "MKX"
    assert office_for_zone("LMZ741") == "LOT"


def test_office_for_zone_unknown_raises():
    with pytest.raises(RegistryError):
        office_for_zone("LMZ999")


def test_get_port_by_exact_name():
    port = get_port("kenosha")
    assert port["zones"] == ["LMZ646"]
    assert "45199" in port["stations"]


def test_get_port_case_and_space_insensitive():
    port = get_port("North Point")
    assert port["name"] == "north_point"


def test_get_port_unknown_raises_with_suggestions():
    with pytest.raises(RegistryError, match="waukegan"):
        get_port("neverland")


def test_get_route_distance():
    route = get_route("milwaukee_waukegan")
    assert route["nm"] == 40


def test_zones_for_place_port():
    assert zones_for_place("kenosha") == ["LMZ646"]


def test_zones_for_place_route_union():
    zones = zones_for_place("kenosha_milwaukee")
    assert "LMZ646" in zones
    assert "LMZ645" in zones


def test_zones_for_place_unknown_raises():
    with pytest.raises(RegistryError):
        zones_for_place("atlantis")


def test_dump_registry_default_region_timezone():
    regions = dump_registry()
    assert regions["lake_michigan_west"]["timezone"] == "America/Chicago"


def test_dump_registry_single_region():
    result = dump_registry("lake_michigan_west")
    assert list(result.keys()) == ["lake_michigan_west"]
