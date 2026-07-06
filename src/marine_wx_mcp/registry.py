"""Loader and query helpers for registry.json (ports/zones/stations/routes).

The registry is the only place port names, zone assignments, and
station IDs are hardcoded - tools resolve `place` arguments through
here rather than deriving marine zones live from the NWS API (see
sources/nws.py docstring for why that live lookup doesn't work for
harbor coordinates).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_REGISTRY_PATH = Path(__file__).parent / "registry.json"


class RegistryError(Exception):
    """Raised when a place/route name can't be resolved in the registry."""


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    with open(_REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _normalize(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def list_regions() -> dict[str, Any]:
    return load_registry()["regions"]


def get_region(region_key: str = "lake_michigan_west") -> dict[str, Any]:
    regions = list_regions()
    if region_key not in regions:
        raise RegistryError(
            f"Unknown region '{region_key}'. Known regions: {', '.join(regions)}"
        )
    return regions[region_key]


def get_port(name: str, region_key: str = "lake_michigan_west") -> dict[str, Any]:
    region = get_region(region_key)
    ports = region["ports"]
    key = _normalize(name)
    if key not in ports:
        raise RegistryError(
            f"Unknown port '{name}'. Known ports: {', '.join(sorted(ports))}"
        )
    return {"name": key, **ports[key]}


def get_route(name: str, region_key: str = "lake_michigan_west") -> dict[str, Any]:
    region = get_region(region_key)
    routes = region["routes"]
    key = _normalize(name)
    if key not in routes:
        raise RegistryError(
            f"Unknown route '{name}'. Known routes: {', '.join(sorted(routes))}"
        )
    return {"name": key, **routes[key]}


def zones_for_place(name: str, region_key: str = "lake_michigan_west") -> list[str]:
    """Zones for a port name, or the union of zones across a route's legs."""
    region = get_region(region_key)
    key = _normalize(name)
    if key in region["ports"]:
        return list(region["ports"][key]["zones"])
    if key in region["routes"]:
        zones: list[str] = []
        for leg in region["routes"][key]["legs"]:
            for zone in region["ports"][leg]["zones"]:
                if zone not in zones:
                    zones.append(zone)
        return zones
    raise RegistryError(
        f"'{name}' is not a known port or route. Use marine_list_registry to "
        "see valid names."
    )


def office_for_zone(zone_id: str, region_key: str = "lake_michigan_west") -> str:
    region = get_region(region_key)
    zone_offices = region.get("zone_offices", {})
    if zone_id not in zone_offices:
        raise RegistryError(
            f"No known NWS office for zone '{zone_id}'. Known zones: "
            f"{', '.join(sorted(zone_offices))}"
        )
    return zone_offices[zone_id]


def dump_registry(region_key: str | None = None) -> dict[str, Any]:
    """Full registry dump (or a single region) for marine_list_registry."""
    regions = list_regions()
    if region_key is None:
        return regions
    return {region_key: get_region(region_key)}
