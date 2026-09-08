"""Regenerate the Dutch rows of
``src/buem/data/reference/num_persons_by_building_type.csv`` from live CBS
queries.

The table itself is generic -- any country can add rows, and the
resolution order in ``buem.config.reference_values.resolve_num_persons``
knows nothing about the Netherlands. This script is the NL-specific half:
it turns
:func:`buem.analysis.netherlands.cbs_household_size.household_size_by_building_type`
into rows for whichever regions buem models, so the shipped figures can be
refreshed when CBS publishes a new year rather than drifting.

Rows for other countries, and any hand-edited row, are preserved
untouched -- only rows whose ``country`` is ``NL`` and whose
``region_code`` this run covers are rewritten.

Usage::

    python scripts/refresh_nl_num_persons.py                 # national + known regions
    python scripts/refresh_nl_num_persons.py --region GM0344 # add another municipality
    python scripts/refresh_nl_num_persons.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

from buem.analysis.netherlands.cbs_household_size import household_size_by_building_type

logger = logging.getLogger(__name__)

TABLE_PATH = Path("src/buem/data/reference/num_persons_by_building_type.csv")

COUNTRY = "NL"
NATIONAL_REGION = "NL01"

# Municipalities buem currently models, with the locality each contains.
DEFAULT_REGIONS: dict[str, str] = {
    "GM0200": "Apeldoorn (contains Loenen)",
    "GM0177": "Raalte (contains Heeten)",
}

FIELDS = ("country", "region_code", "building_type", "num_persons", "source")


def _read_existing(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Return the file's leading comment block and its data rows."""
    comments: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
    for line in lines:
        if line.lstrip().startswith("#"):
            comments.append(line.rstrip("\n"))
        else:
            break
    reader = csv.DictReader(line for line in lines if not line.lstrip().startswith("#"))
    return comments, list(reader)


def build_rows(regions: dict[str, str], period_note: str) -> list[dict[str, str]]:
    """Query CBS and format one row per (region, building type)."""
    rows: list[dict[str, str]] = []
    targets = {NATIONAL_REGION: "national"} | regions
    for region_code, label in targets.items():
        result = household_size_by_building_type(region_code)
        # The table's region column carries "*" for the country-wide
        # default, since resolve_num_persons uses that token rather than
        # CBS's own national code.
        stored_region = "*" if region_code == NATIONAL_REGION else region_code
        for building_type, value in sorted(result.by_building_type.items()):
            note = (
                "Meergezins anchor; CBS publishes no MFH/AB split."
                if building_type in ("MFH", "AB") else ""
            )
            rows.append({
                "country": COUNTRY,
                "region_code": stored_region,
                "building_type": building_type,
                "num_persons": f"{value:.2f}",
                "source": (
                    f"CBS 85035NED + 86064NED + 85140NED, {label}, {period_note}. "
                    f"{note}"
                ).strip(),
            })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region", action="append", default=None,
                        help="Extra CBS RegioS municipality code to include; repeatable.")
    parser.add_argument("--persons-period", default="2024JJ00")
    parser.add_argument("--stock-period", default="2025JJ00")
    parser.add_argument("--table", type=Path, default=TABLE_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the rows that would be written, change nothing.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    regions = dict(DEFAULT_REGIONS)
    for extra in args.region or []:
        regions.setdefault(extra, extra)

    period_note = f"{args.persons_period[:4]}/{args.stock_period[:4]}"
    fresh = build_rows(regions, period_note)

    if not args.table.exists():
        parser.error(f"{args.table} not found -- run from the repository root.")
    comments, existing = _read_existing(args.table)

    # Anything this run did not regenerate survives: other countries, and
    # any NL region not covered here.
    refreshed_keys = {(r["region_code"], r["building_type"]) for r in fresh}
    kept = [
        row for row in existing
        if not (
            row["country"] == COUNTRY
            and (row["region_code"], row["building_type"]) in refreshed_keys
        )
    ]

    for row in fresh:
        print(f"  {row['country']:3s} {row['region_code']:8s} "
              f"{row['building_type']:4s} {row['num_persons']:>6s}")
    if args.dry_run:
        print(f"\n--dry-run: {args.table} unchanged "
              f"({len(kept)} row(s) would be kept, {len(fresh)} rewritten).")
        return 0

    with args.table.open("w", encoding="utf-8", newline="") as handle:
        for line in comments:
            handle.write(line + "\n")
        writer = csv.DictWriter(handle, fieldnames=list(FIELDS))
        writer.writeheader()
        for row in kept + fresh:
            writer.writerow({key: row.get(key, "") for key in FIELDS})

    print(f"\nWrote {args.table}: {len(kept)} kept, {len(fresh)} refreshed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
