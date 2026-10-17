"""Loader and query helpers for registry.json (regions/ports/zones/stations/routes).

The registry is the only place port names, zone assignments, and
station IDs are hardcoded - tools resolve `place` arguments through
here rather than deriving marine zones live from the NWS API (see
sources/nws.py docstring for why that live lookup doesn't work for
harbor coordinates).

Multi-region (added 2026-10-11): place names are looked up across every
region, so callers no longer need to know which region a port lives in.
A name that exists in two regions is ambiguous and must be qualified as
"region:name" (e.g. "antigua_barbuda:english_harbour"). Each region
carries its own IANA timezone, which every tool uses for display - a
region-less ad-hoc coordinate inherits the timezone of the nearest
registry region within REGION_MATCH_NM, else falls back to UTC.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from marine_wx_mcp.sources._geo import haversine_nm

_REGISTRY_PATH = Path(__file__).parent / "registry.json"

DEFAULT_REGION = "lake_michigan_west"
FALLBACK_TIMEZONE = "UTC"
# An ad-hoc coordinate within this distance of any port in a region is
# treated as belonging to that region (timezone, thresholds, buoys).
REGION_MATCH_NM = 150


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


def get_region(region_key: str = DEFAULT_REGION) -> dict[str, Any]:
    regions = list_regions()
    if region_key not in regions:
        raise RegistryError(
            f"Unknown region '{region_key}'. Known regions: {', '.join(regions)}"
        )
    return regions[region_key]


def _split_qualified(name: str) -> tuple[str | None, str]:
    """'region:name' -> (region, name); plain 'name' -> (None, name)."""
    for sep in (":", "/"):
        if sep in name:
            region, _, rest = name.partition(sep)
            return _normalize(region), _normalize(rest)
    return None, _normalize(name)


def _find(kind: str, name: str, region_key: str | None) -> tuple[str, str, dict[str, Any]]:
    """Find a port or route by name across regions. Returns (region_key, key, record)."""
    qualified_region, key = _split_qualified(name)
    region_key = qualified_region or region_key
    regions = list_regions()
    if region_key and region_key not in regions:
        raise RegistryError(
            f"Unknown region '{region_key}'. Known regions: {', '.join(regions)}"
        )
    search = [region_key] if region_key else list(regions)

    hits = [(r, regions[r][kind][key]) for r in search if key in regions[r].get(kind, {})]
    if len(hits) == 1:
        r, rec = hits[0]
        return r, key, rec
    if len(hits) > 1:
        raise RegistryError(
            f"'{name}' exists in more than one region ({', '.join(r for r, _ in hits)}) - "
            f"qualify it as 'region:{key}'."
        )
    known = sorted(k for r in search for k in regions[r].get(kind, {}))
    label = "port" if kind == "ports" else "route"
    raise RegistryError(f"Unknown {label} '{name}'. Known {label}s: {', '.join(known)}")


def get_port(name: str, region_key: str | None = None) -> dict[str, Any]:
    region, key, rec = _find("ports", name, region_key)
    return {"name": key, "region": region, **rec}


def get_route(name: str, region_key: str | None = None) -> dict[str, Any]:
    region, key, rec = _find("routes", name, region_key)
    route = {"name": key, "region": region, **rec}
    if "nm" not in route:
        route["nm"] = route_distance_nm(region, route["legs"])
    return route


def route_distance_nm(region_key: str, legs: list[str]) -> float:
    """Straight-line sum of leg distances (not a sailed distance - no tacking)."""
    ports = get_region(region_key)["ports"]
    total = 0.0
    for a, b in zip(legs, legs[1:]):
        pa, pb = ports[a], ports[b]
        total += haversine_nm(pa["lat"], pa["lon"], pb["lat"], pb["lon"])
    return round(total, 1)


def zones_for_place(name: str, region_key: str | None = None) -> list[str]:
    """Zones for a port name, or the union of zones across a route's legs."""
    try:
        port = get_port(name, region_key)
        return list(port.get("zones", []))
    except RegistryError as port_err:
        try:
            route = get_route(name, region_key)
        except RegistryError:
            raise RegistryError(
                f"'{name}' is not a known port or route. Use marine_list_registry to "
                "see valid names."
            ) from port_err
    ports = get_region(route["region"])["ports"]
    zones: list[str] = []
    for leg in route["legs"]:
        for zone in ports[leg].get("zones", []):
            if zone not in zones:
                zones.append(zone)
    return zones


def region_for_coords(lat: float, lon: float, max_nm: float = REGION_MATCH_NM) -> str | None:
    """The region whose nearest port is closest to (lat, lon), if within max_nm."""
    best: tuple[float, str] | None = None
    for region_key, region in list_regions().items():
        for port in region.get("ports", {}).values():
            d = haversine_nm(lat, lon, port["lat"], port["lon"])
            if best is None or d < best[0]:
                best = (d, region_key)
    if best is not None and best[0] <= max_nm:
        return best[1]
    return None


def region_for_station(station_id: str) -> str | None:
    """Region that lists this NDBC station (region "buoys" or a port's "stations")."""
    sid = station_id.strip().upper()
    for region_key, region in list_regions().items():
        if sid in {s.upper() for s in region.get("buoys", [])}:
            return region_key
        for port in region.get("ports", {}).values():
            if sid in {s.upper() for s in port.get("stations", [])}:
                return region_key
    return None


def timezone_for_region(region_key: str | None) -> str:
    if region_key is None:
        return FALLBACK_TIMEZONE
    return get_region(region_key).get("timezone", FALLBACK_TIMEZONE)


def thresholds_for_region(region_key: str | None) -> dict[str, Any] | None:
    if region_key is None:
        return None
    return get_region(region_key).get("thresholds")


def dump_registry(region_key: str | None = None) -> dict[str, Any]:
    """Full registry dump (or a single region) for marine_list_registry."""
    regions = list_regions()
    if region_key is None:
        return regions
    return {region_key: get_region(region_key)}
