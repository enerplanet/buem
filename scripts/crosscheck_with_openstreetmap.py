"""Cross-check a region's building classification against OpenStreetMap.

Why this is a *check* and not a classification source
----------------------------------------------------
OSM's geometric coverage of Dutch buildings is essentially complete (it
imports BAG footprints), but its ``building=*`` tag is left at the
generic ``yes`` for most rural structures. Measured for Loenen: 3,049 of
3,105 buildings match an OSM footprint (98%), yet only ~5-15% of the
buildings that BAG leaves undescribed carry a specific OSM tag.

That makes OSM a poor primary signal -- classifying from it would leave
most buildings untyped -- but a genuinely independent second opinion on
the buildings that *are* tagged, since it is surveyed by people on the
ground rather than derived from the same BAG registration buem already
reads. It catches two error classes that BAG-only validation cannot:

- a building buem calls residential that OSM tags ``retail``/
  ``industrial`` (a missed service building, or a mixed-use one);
- a building buem types as a service building that OSM tags ``house``
  (a likely false positive).

It is also the only readily available evidence for what the Pand records
with no registered verblijfsobject actually are -- OSM's
``farm_auxiliary``, ``cowshed``, ``barn`` and ``shed`` tags describe
exactly the agricultural structures BAG declines to characterise.

Usage::

    python scripts/crosscheck_with_openstreetmap.py Loenen
    python scripts/crosscheck_with_openstreetmap.py Loenen Heeten --output-dir results

Writes ``<region>_osm_crosscheck.csv`` (one row per building, with the
matched OSM tag) and prints an agreement summary.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import pandas as pd

from buem.buildings.datasources.bag_use_function import (
    load_use_functions,
    summarize_use_by_pand,
)
from buem.buildings.mapping.geometry_utils import wkb_point_to_lat_lon

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "buem/1.0 (building-energy model; research use)"

MATCH_RADIUS_M = 25.0
"""How close an OSM footprint centre must be to a BAG Pand centroid to
count as the same building. Both derive from the same footprints, so
real matches are metres apart; the tolerance absorbs the difference
between a polygon centroid and Overpass' bounding-box centre for
irregular shapes."""

_GRID_DEG = 0.001
"""Spatial-hash cell size, ~80 m at this latitude -- comfortably larger
than MATCH_RADIUS_M so a 3x3 neighbourhood always contains the nearest
candidate."""

RESIDENTIAL_OSM_TAGS = frozenset({
    "house", "detached", "semidetached_house", "apartments", "bungalow",
    "terrace", "residential", "farm", "static_caravan",
})

NON_RESIDENTIAL_OSM_TAGS = frozenset({
    "industrial", "retail", "commercial", "office", "school", "church",
    "civic", "farm_auxiliary", "cowshed", "barn", "warehouse", "hotel",
    "supermarket", "service", "public", "hospital", "kindergarten",
    "sports_hall", "greenhouse", "chapel", "government", "manufacture",
})

GENERIC_OSM_TAG = "yes"
"""OSM's untyped default. Present on the majority of rural buildings, so
it is excluded from every agreement statistic -- counting it would
inflate coverage without adding information."""


def _bbox_wgs84(buildings_df: pd.DataFrame) -> tuple[float, float, float, float]:
    """(south, west, north, east) covering the region's centroids."""
    lats, lons = [], []
    for geom in buildings_df["building_centroid_geom"]:
        if not isinstance(geom, str):
            continue
        try:
            lat, lon = wkb_point_to_lat_lon(geom)
        except (ValueError, IndexError):
            continue
        lats.append(lat)
        lons.append(lon)
    if not lats:
        raise ValueError("no decodable building centroids -- cannot build a bbox")
    margin = 0.002  # ~200 m, so edge buildings keep their OSM neighbours
    return (min(lats) - margin, min(lons) - margin, max(lats) + margin, max(lons) + margin)


def fetch_osm_buildings(bbox: tuple[float, float, float, float],
                        retries: int = 3) -> list[dict[str, object]]:
    """Every tagged building way in ``bbox``, with its centre point."""
    south, west, north, east = bbox
    query = (
        "[out:json][timeout:180];"
        f'(way["building"]({south:.4f},{west:.4f},{north:.4f},{east:.4f}););'
        "out tags center;"
    )
    payload = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(OVERPASS_URL, data=payload, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                return json.load(response).get("elements", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                raise
            # Overpass rate-limits aggressively; back off rather than hammer it.
            logger.warning("Overpass request failed (%s), retrying", type(exc).__name__)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("unreachable")


def _build_index(elements: list[dict[str, object]]) -> dict[tuple[float, float], list]:
    grid: dict[tuple[float, float], list] = {}
    for element in elements:
        centre = element.get("center")
        if not centre:
            continue
        lat, lon = centre["lat"], centre["lon"]
        key = (round(lat, 3), round(lon, 3))
        grid.setdefault(key, []).append((lat, lon, element.get("tags", {})))
    return grid


def _nearest_tags(grid: dict, lat: float, lon: float) -> dict | None:
    best, best_distance = None, math.inf
    metres_per_lon = 111320 * math.cos(math.radians(lat))
    for d_lat in (-_GRID_DEG, 0.0, _GRID_DEG):
        for d_lon in (-_GRID_DEG, 0.0, _GRID_DEG):
            cell = grid.get((round(lat + d_lat, 3), round(lon + d_lon, 3)))
            if not cell:
                continue
            for o_lat, o_lon, tags in cell:
                distance = math.hypot((o_lon - lon) * metres_per_lon, (o_lat - lat) * 110540)
                if distance < best_distance:
                    best_distance, best = distance, tags
    return best if best_distance <= MATCH_RADIUS_M else None


def crosscheck(region_dir: Path) -> pd.DataFrame:
    """Attach each building's nearest OSM ``building`` tag."""
    buildings = pd.read_csv(region_dir / "lod2_building_feature.csv", low_memory=False)
    bbox = _bbox_wgs84(buildings)
    logger.info("querying Overpass for bbox %.4f,%.4f,%.4f,%.4f", *bbox)
    elements = fetch_osm_buildings(bbox)
    logger.info("OSM returned %d building ways", len(elements))
    grid = _build_index(elements)

    tags: list[dict | None] = []
    for geom in buildings["building_centroid_geom"]:
        if not isinstance(geom, str):
            tags.append(None)
            continue
        try:
            lat, lon = wkb_point_to_lat_lon(geom)
        except (ValueError, IndexError):
            tags.append(None)
            continue
        tags.append(_nearest_tags(grid, lat, lon))

    buildings["osm_building"] = [t.get("building") if t else None for t in tags]
    buildings["osm_name"] = [t.get("name") if t else None for t in tags]
    buildings["osm_amenity"] = [t.get("amenity") if t else None for t in tags]
    buildings["osm_shop"] = [t.get("shop") if t else None for t in tags]
    return buildings


def report(region: str, df: pd.DataFrame, use_by_pand_id: dict) -> None:
    matched = df["osm_building"].notna()
    specific = matched & (df["osm_building"] != GENERIC_OSM_TAG)
    print(f"\n=== {region} ===")
    print(f"  buildings                 : {len(df)}")
    print(f"  matched an OSM footprint  : {matched.sum()} ({100 * matched.sum() / len(df):.0f}%)")
    print(f"  with a specific OSM tag   : {specific.sum()} ({100 * specific.sum() / len(df):.0f}%)")

    residential = df[(df["is_residential"] == True) & specific]  # noqa: E712 - pandas mask
    service = df[df["service_building_type"].notna() & specific]
    agree_r = residential["osm_building"].isin(RESIDENTIAL_OSM_TAGS).sum()
    agree_s = service["osm_building"].isin(NON_RESIDENTIAL_OSM_TAGS).sum()
    print("\n  agreement where OSM is specific:")
    if len(residential):
        print(f"    residential : {agree_r}/{len(residential)} ({100 * agree_r / len(residential):.0f}%)")
    if len(service):
        print(f"    service     : {agree_s}/{len(service)} ({100 * agree_s / len(service):.0f}%)")

    misses = residential[residential["osm_building"].isin(NON_RESIDENTIAL_OSM_TAGS)]
    false_pos = service[service["osm_building"].isin(RESIDENTIAL_OSM_TAGS)]
    print(f"\n  buem residential, OSM non-residential (possible misses): {len(misses)}")
    if len(misses):
        print("    " + ", ".join(f"{k} {v}" for k, v in Counter(misses["osm_building"]).most_common(8)))
    print(f"  buem service, OSM residential (possible false positives): {len(false_pos)}")
    if len(false_pos):
        print("    " + ", ".join(f"{k} {v}" for k, v in Counter(false_pos["service_building_type"]).most_common(8)))

    # The buildings BAG leaves undescribed -- the reason OSM is worth querying.
    no_unit = df[~df["bag_pand_id"].isin(use_by_pand_id)]
    no_unit = no_unit[(no_unit["is_residential"] != True) & no_unit["service_building_type"].isna()]  # noqa: E712
    print(f"\n  Pand records with no registered verblijfsobject: {len(no_unit)}")
    for low, high, label in ((0, 50, "<50 m2"), (50, 200, "50-200"), (200, math.inf, ">=200")):
        band = no_unit[(no_unit["footprint_area"] >= low) & (no_unit["footprint_area"] < high)]
        if band.empty:
            continue
        counts = Counter(t for t in band["osm_building"] if t and t != GENERIC_OSM_TAG)
        described = sum(counts.values())
        detail = ", ".join(f"{k} {v}" for k, v in counts.most_common(6)) or "nothing specific"
        print(f"    {label:8s} n={len(band):5d}  OSM-described {described:4d} "
              f"({100 * described / len(band):3.0f}%)  ->  {detail}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("regions", nargs="+", help="Region directory names under src/buem/data/buildings/netherlands/")
    parser.add_argument("--data-root", default="src/buem/data/buildings/netherlands")
    parser.add_argument("--output-dir", default="results")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ok = False
    for region in args.regions:
        region_dir = Path(args.data_root) / region
        if not (region_dir / "lod2_building_feature.csv").exists():
            print(f"ERROR: {region_dir} has no lod2_building_feature.csv", file=sys.stderr)
            continue
        use_path = region_dir / "bag_use_function.csv"
        use_by_pand_id = (
            summarize_use_by_pand(load_use_functions(use_path)) if use_path.exists() else {}
        )
        df = crosscheck(region_dir)
        report(region, df, use_by_pand_id)
        out = output_dir / f"{region.lower()}_osm_crosscheck.csv"
        df[[
            "building_feature_id", "bag_pand_id", "footprint_area", "construction_year",
            "is_residential", "building_type", "service_building_type",
            "osm_building", "osm_name", "osm_amenity", "osm_shop",
        ]].to_csv(out, index=False)
        logger.info("wrote %s", out)
        ok = True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
