"""Fetch a region's BAG use functions (``gebruiksdoel``) from PDOK.

``buem.buildings.datasources.bag_use_function`` needs one row per
verblijfsobject for the buildings in a region. That attribute is not
present in either of the region's existing inputs -- 3D BAG's CityJSON
carries Pand-level attributes only, and the RIVM energy-labels export
counts units without describing them -- so it is fetched here from the
national BAG WFS service published by PDOK (the Dutch public geo-data
platform) and cached as a per-region CSV.

The fetch is a bounding-box query derived from the region's own building
centroids, then joined back to the region's Pand ids: a bbox is the
filter this WFS supports natively, and any unit it returns from outside
the region is simply dropped by the join.

Usage::

    python scripts/fetch_bag_use_functions.py src/buem/data/buildings/netherlands/Loenen

Writes ``bag_use_function.csv`` into that directory, alongside the
region's other reference tables. Idempotent: re-running overwrites the
extract with a fresh fetch of the same query.
"""
from __future__ import annotations

import argparse
import json
import logging
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

WFS_URL = "https://service.pdok.nl/lv/bag/wfs/v2_0"
RD_NEW_EPSG = "EPSG:28992"
"""Amersfoort / RD New, the Dutch national grid the building centroids
already use -- querying in it avoids a reprojection round-trip."""

PAGE_SIZE = 1000
BBOX_MARGIN_M = 50.0
"""Padding on the centroid-derived bounding box. A Pand's centroid and
its units' address points are not the same location, so a box drawn
tightly around the centroids can clip units belonging to buildings at
the edge of the region."""

OUTPUT_FILENAME = "bag_use_function.csv"
_BAG_PREFIX = "NL.IMBAG.Pand."
_FIELDS = ("identificatie", "pandidentificatie", "gebruiksdoel",
           "oppervlakte", "status", "bouwjaar", "woonplaats")


def _decode_point_xy(hex_wkb: str) -> tuple[float, float] | None:
    """Decode a hex-encoded EWKB POINT to its raw (x, y).

    Deliberately not reusing ``geometry_utils.wkb_point_to_lat_lon``:
    that helper reprojects to WGS84, while the WFS query wants the
    untransformed RD New coordinates.
    """
    if not isinstance(hex_wkb, str) or len(hex_wkb) < 42:
        return None
    try:
        raw = bytes.fromhex(hex_wkb)
    except ValueError:
        return None
    endian = "<" if raw[0] == 1 else ">"
    geom_type = struct.unpack(endian + "I", raw[1:5])[0]
    offset = 5
    if geom_type & 0x20000000:  # SRID flag present
        offset += 4
    x, y = struct.unpack(endian + "dd", raw[offset:offset + 16])
    return x, y


def region_bbox(buildings_df: pd.DataFrame) -> tuple[float, float, float, float]:
    """Bounding box of a region's building centroids, in RD New metres."""
    points = [p for p in (_decode_point_xy(g) for g in buildings_df["building_centroid_geom"]) if p]
    if not points:
        raise ValueError("no decodable building_centroid_geom values -- cannot derive a bbox")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) - BBOX_MARGIN_M, min(ys) - BBOX_MARGIN_M,
            max(xs) + BBOX_MARGIN_M, max(ys) + BBOX_MARGIN_M)


def fetch_verblijfsobjecten(bbox: tuple[float, float, float, float],
                            retries: int = 3) -> list[dict[str, object]]:
    """Page through every verblijfsobject whose point falls in ``bbox``."""
    minx, miny, maxx, maxy = bbox
    bbox_param = f"{minx:.0f},{miny:.0f},{maxx:.0f},{maxy:.0f},{RD_NEW_EPSG}"
    records: list[dict[str, object]] = []
    start_index = 0
    while True:
        query = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "bag:verblijfsobject", "outputFormat": "application/json",
            "srsName": RD_NEW_EPSG, "bbox": bbox_param,
            "count": str(PAGE_SIZE), "startIndex": str(start_index),
        }
        url = f"{WFS_URL}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"User-Agent": "buem/1.0"})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == retries - 1:
                    raise
                logger.warning("PDOK request failed (%s), retrying", type(exc).__name__)
                time.sleep(3 * (attempt + 1))

        features = payload.get("features", [])
        for feature in features:
            properties = feature.get("properties", {})
            records.append({field: properties.get(field) for field in _FIELDS})
        logger.info("fetched %d verblijfsobjecten (total %d)", len(features), len(records))
        if len(features) < PAGE_SIZE:
            return records
        start_index += PAGE_SIZE


def build_extract(buildings_df: pd.DataFrame, records: list[dict[str, object]]) -> pd.DataFrame:
    """Join fetched units onto the region's Pand ids.

    The WFS returns raw 16-digit Pand ids, while the region's own column
    carries the prefixed ``NL.IMBAG.Pand.<digits>`` form; the prefixed
    form is restored so the extract joins directly to every other
    per-building table.
    """
    stripped_to_full = {
        str(pid).removeprefix(_BAG_PREFIX): str(pid)
        for pid in buildings_df["bag_pand_id"].dropna()
    }
    rows = []
    for record in records:
        full_id = stripped_to_full.get(str(record.get("pandidentificatie")))
        if full_id is None:
            continue  # a unit from outside the region's own buildings
        rows.append({
            "bag_pand_id": full_id,
            "verblijfsobject_id": record.get("identificatie"),
            "gebruiksdoel": record.get("gebruiksdoel"),
            "oppervlakte": record.get("oppervlakte"),
            "status": record.get("status"),
            "bouwjaar": record.get("bouwjaar"),
            "woonplaats": record.get("woonplaats"),
        })
    extract = pd.DataFrame(rows, columns=[
        "bag_pand_id", "verblijfsobject_id", "gebruiksdoel",
        "oppervlakte", "status", "bouwjaar", "woonplaats",
    ])
    logger.info(
        "build_extract: %d/%d fetched units belong to this region's %d buildings (%d matched)",
        len(extract), len(records), len(stripped_to_full), extract["bag_pand_id"].nunique(),
    )
    return extract


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", help="Region directory, e.g. src/buem/data/buildings/netherlands/Loenen")
    parser.add_argument("--output", default=None, help=f"Output path (default: <data_dir>/{OUTPUT_FILENAME})")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    buildings_path = data_dir / "lod2_building_feature.csv"
    if not buildings_path.exists():
        print(f"ERROR: {buildings_path} not found", file=sys.stderr)
        return 1

    buildings_df = pd.read_csv(buildings_path, low_memory=False)
    bbox = region_bbox(buildings_df)
    logger.info("region bbox (RD New): %.0f,%.0f,%.0f,%.0f", *bbox)

    records = fetch_verblijfsobjecten(bbox)
    extract = build_extract(buildings_df, records)

    output_path = Path(args.output) if args.output else data_dir / OUTPUT_FILENAME
    extract.to_csv(output_path, index=False)
    logger.info("wrote %d rows to %s", len(extract), output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
