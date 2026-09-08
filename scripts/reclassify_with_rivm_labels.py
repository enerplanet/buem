"""Re-run the real RIVM archetype-linking pipeline for a Netherlands
region, now that the raw ``energielabels_2025.gpkg`` is available.

Some regions' ``lod2_building_feature.csv`` were produced by an earlier
run that only classified buildings (``building_type``/``neighbour_status``/
``is_residential``/``matched_via_label``) without the raw RIVM GeoPackage
in reach -- leaving ``residential_units`` absent entirely and every
building pinned to TABULA's as-built variant, even ones with a real
matched energy label. ``nl_archetype_mapper.map_buildings()`` already
does the *complete* job correctly (see its own docstring); this script is
a thin driver that re-runs it end to end against the real GeoPackage and
rewrites the region's table in place -- no new archetype-linking logic,
same treatment as ``scripts/repair_nl_dwelling_counts.py``'s relationship
to ``repair_dwelling_counts()``.

    python scripts/reclassify_with_rivm_labels.py <data_dir> [--gpkg-path PATH]

``--gpkg-path`` defaults to the ``RIVM_ENERGY_LABELS_GPKG`` environment
variable (see ``.env.example``) -- the file is a machine-local, ~3GB
nationwide export, never bundled with the repo, same treatment as
``WEATHER_DATA_DIR``.

Idempotent: re-running against the same GeoPackage reproduces the same
columns (``map_buildings()`` is a pure function of its three inputs).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pandas as pd

from buem.buildings.datasources.bag_use_function import (
    load_use_functions,
    summarize_use_by_pand,
)
from buem.buildings.datasources.nl_archetype_mapper import map_buildings, repair_dwelling_counts
from buem.buildings.datasources.rivm_energy_labels import load_labels_for_buildings

BAG_USE_FUNCTION_FILENAME = "bag_use_function.csv"

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]

    gpkg_path: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--gpkg-path":
            gpkg_path = args[i + 1]
            i += 2
        else:
            positional.append(args[i])
            i += 1
    if not positional:
        print("ERROR: data_dir is required, e.g. src/buem/data/buildings/netherlands/Heeten", file=sys.stderr)
        return 1
    data_dir = Path(positional[0])

    gpkg_path = gpkg_path or os.environ.get("RIVM_ENERGY_LABELS_GPKG")
    if not gpkg_path:
        print(
            "ERROR: no RIVM GeoPackage path given. Pass --gpkg-path, or set "
            "RIVM_ENERGY_LABELS_GPKG in .env.",
            file=sys.stderr,
        )
        return 1

    buildings_path = data_dir / "lod2_building_feature.csv"
    tabula_path = data_dir / "tabula.csv"
    if not buildings_path.exists():
        print(f"ERROR: {buildings_path} not found", file=sys.stderr)
        return 1

    before = pd.read_csv(buildings_path, na_values=["NULL"], low_memory=False)
    nl_tabula = pd.read_csv(tabula_path, na_values=["NULL"])
    nl_tabula = nl_tabula[nl_tabula["Code_Country"] == "NL"]

    bag_pand_ids = before["bag_pand_id"].dropna().unique().tolist()
    rivm_labels = load_labels_for_buildings(gpkg_path, bag_pand_ids)

    # BAG use functions, when the region's extract has been fetched.
    # Without them non-residential buildings are indistinguishable from
    # dwellings (see nl_building_classifier.classify_all), so a missing
    # extract is worth an explicit warning rather than silent omission.
    use_path = data_dir / BAG_USE_FUNCTION_FILENAME
    use_by_pand_id = None
    if use_path.exists():
        use_by_pand_id = summarize_use_by_pand(load_use_functions(use_path))
    else:
        print(
            f"WARNING: {use_path} not found -- every non-residential building "
            "will be classified as a dwelling. Run "
            f"scripts/fetch_bag_use_functions.py {data_dir} first.",
            file=sys.stderr,
        )

    before_type_counts = before["building_type"].value_counts(dropna=False)
    before_variant_counts = (
        before["refurbishment_variant"].value_counts(dropna=False)
        if "refurbishment_variant" in before.columns
        else None
    )

    classified = map_buildings(before, nl_tabula, rivm_labels, use_by_pand_id)
    after = repair_dwelling_counts(classified)

    after.to_csv(buildings_path, index=False)

    print(f"Wrote {buildings_path}")
    print(f"  buildings:                 {len(after)}")
    print(f"  RIVM GeoPackage matches:   {len(rivm_labels)}/{len(bag_pand_ids)}")
    print(f"  real label matches:       {int(rivm_labels['dominant_label'].notna().sum())}")
    if use_by_pand_id is not None:
        n_service = int(after["service_building_type"].notna().sum())
        print(f"  BAG use-function matches:  {len(use_by_pand_id)}/{len(bag_pand_ids)}")
        print(f"  service buildings:         {n_service}")
    print()
    print("  building_type, before -> after:")
    after_type_counts = after["building_type"].value_counts(dropna=False)
    for btype in sorted(set(before_type_counts.index) | set(after_type_counts.index), key=str):
        print(f"    {btype!s:12s} {before_type_counts.get(btype, 0):5d} -> {after_type_counts.get(btype, 0):5d}")
    print()
    print("  refurbishment_variant, before -> after:")
    after_variant_counts = after["refurbishment_variant"].value_counts(dropna=False)
    variants = set(after_variant_counts.index)
    if before_variant_counts is not None:
        variants |= set(before_variant_counts.index)
    for variant in sorted(variants, key=str):
        before_n = before_variant_counts.get(variant, 0) if before_variant_counts is not None else "n/a"
        print(f"    {variant!s:4s} {before_n!s:>5s} -> {after_variant_counts.get(variant, 0):5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
