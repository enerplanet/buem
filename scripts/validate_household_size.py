"""Cross-check buem's per-dwelling occupancy against two independent
Dutch reference sources, for one or more localities.

Why this exists
---------------
``num_persons`` reaches occupancy's household generator as a single
figure per dwelling, and occupancy v6.0.0 made electricity demand -- and
so the internal gain buem's 5R1C solve receives -- genuinely responsive
to it. A wrong headcount now moves heating demand, where previously it
barely did. Nothing in buem's own pipeline establishes what the right
headcount is, so it has to come from outside.

Two sources are used because neither alone is sufficient:

CBS StatLine (:mod:`buem.analysis.netherlands.cbs_household_size`)
    Resolves occupancy *by dwelling type*, which is what buem needs to
    vary ``num_persons`` across its SFH/TH/MFH/AB archetypes. Its finest
    geography is the municipality, so a village is represented by the
    municipality containing it.

AlleCijfers (``fetch_allecijfers_reference``)
    Resolves the locality itself -- inhabitants, households and dwelling
    stock for the village proper -- but publishes no dwelling-type
    breakdown. It is the only check on whether the municipal figure
    transfers to the village.

Combining them gives a testable prediction: weight the CBS per-type
occupancy by the locality's own dwelling-type mix, as buem classifies
it, and the result should reproduce the locality's observed persons per
dwelling. Agreement supports both the CBS figures and buem's mix;
disagreement localizes the error to whichever of the two the third
quantity -- dwelling stock -- indicts.

Usage::

    python scripts/validate_household_size.py loenen heeten
    python scripts/validate_household_size.py loenen --output-dir results/
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_allecijfers_reference import (  # noqa: E402 -- needs the path insert above
    extract_metrics,
    fetch_page,
)

from buem.analysis.netherlands.cbs_household_size import (  # noqa: E402
    household_size_by_building_type,
)

logger = logging.getLogger(__name__)

# Each locality is a woonplaats inside a municipality; CBS resolves only
# the municipality, so this is the geography the per-type figures come
# from. The data directory holds the classified LOD2 building table whose
# dwelling-type mix the comparison weights by.
LOCALITIES: dict[str, dict[str, str]] = {
    "loenen": {"municipality": "GM0200", "municipality_name": "Apeldoorn", "data_dir": "Loenen"},
    "heeten": {"municipality": "GM0177", "municipality_name": "Raalte", "data_dir": "Heeten"},
}

DATA_ROOT = Path("src/buem/data/buildings/netherlands")

# AlleCijfers metric labels this script reads.
_INHABITANTS = "Inwoners"
_HOUSEHOLDS = "Huishoudens"
_DWELLING_STOCK = "Woningvoorraad"


@dataclass(frozen=True)
class LocalityComparison:
    """One locality's three-way comparison.

    ``predicted_persons_per_dwelling`` is CBS's per-type occupancy
    weighted by buem's own dwelling mix; ``observed_*`` come from
    AlleCijfers. ``stock_ratio`` is buem's dwelling-unit count over the
    official stock, which separates a disagreement caused by wrong
    occupancy figures from one caused by a wrong dwelling count.
    """

    locality: str
    municipality: str
    cbs_by_type: dict[str, float]
    cbs_anchors: dict[str, float]
    mix: pd.DataFrame
    predicted_persons_per_dwelling: float
    observed_persons_per_dwelling: float
    observed_persons_per_household: float
    reference_dwelling_stock: float
    reference_households: float
    reference_inhabitants: float
    buem_dwelling_units: float

    @property
    def stock_ratio(self) -> float:
        return self.buem_dwelling_units / self.reference_dwelling_stock

    @property
    def prediction_error(self) -> float:
        """Predicted over observed persons per dwelling."""
        return self.predicted_persons_per_dwelling / self.observed_persons_per_dwelling


def locality_reference(name: str) -> dict[str, float]:
    """Inhabitants, households and dwelling stock for one woonplaats.

    ``extract_metrics`` has already resolved AlleCijfers' Dutch number
    formatting into its ``value`` field, so that field is read directly;
    running it through ``parse_number`` a second time would strip the
    decimal point it just wrote and inflate every figure tenfold.
    """
    metrics = extract_metrics(fetch_page(name))
    wanted = (_INHABITANTS, _HOUSEHOLDS, _DWELLING_STOCK)
    out: dict[str, float] = {}
    for row in metrics:
        label = row.get("metric", "")
        if label not in wanted or label in out:
            continue
        try:
            out[label] = float(row.get("value", ""))
        except ValueError:
            continue
    missing = set(wanted) - set(out)
    if missing:
        raise ValueError(f"AlleCijfers page for {name!r} is missing {sorted(missing)}.")
    return out


def buem_dwelling_mix(data_dir: str) -> pd.DataFrame:
    """Dwelling units per building type, as buem itself classifies them.

    Counts dwelling *units* rather than buildings: a block carrying
    twenty dwellings contributes twenty households' worth of occupancy,
    which is the quantity a per-dwelling reference figure compares
    against.
    """
    path = DATA_ROOT / data_dir / "lod2_building_feature.csv"
    df = pd.read_csv(path, low_memory=False)
    residential = df[df["is_residential"].astype(bool)]
    units = (
        residential.assign(units=residential["residential_units"].fillna(1.0).clip(lower=1.0))
        .groupby("building_type")["units"]
        .agg(["sum", "count"])
        .rename(columns={"sum": "dwelling_units", "count": "buildings"})
    )
    units["unit_share"] = units["dwelling_units"] / units["dwelling_units"].sum()
    return units


def compare(name: str) -> LocalityComparison:
    """Run the full three-way comparison for one locality."""
    config = LOCALITIES[name]
    cbs = household_size_by_building_type(config["municipality"])
    reference = locality_reference(name)
    mix = buem_dwelling_mix(config["data_dir"])

    # The prediction: CBS per-type occupancy, weighted by buem's own mix.
    predicted = sum(
        float(row.unit_share) * cbs.by_building_type[str(building_type)]
        for building_type, row in mix.iterrows()
        if str(building_type) in cbs.by_building_type
    )

    return LocalityComparison(
        locality=name,
        municipality=config["municipality_name"],
        cbs_by_type=cbs.by_building_type,
        cbs_anchors=cbs.anchors,
        mix=mix,
        predicted_persons_per_dwelling=predicted,
        observed_persons_per_dwelling=reference[_INHABITANTS] / reference[_DWELLING_STOCK],
        observed_persons_per_household=reference[_INHABITANTS] / reference[_HOUSEHOLDS],
        reference_dwelling_stock=reference[_DWELLING_STOCK],
        reference_households=reference[_HOUSEHOLDS],
        reference_inhabitants=reference[_INHABITANTS],
        buem_dwelling_units=float(mix["dwelling_units"].sum()),
    )


def report(result: LocalityComparison) -> str:
    """Human-readable summary of one locality's comparison."""
    by_type = ", ".join(f"{k}={v}" for k, v in sorted(result.cbs_by_type.items()))
    header = f"    {'type':6s} {'buildings':>10s} {'units':>10s} {'share':>8s} {'CBS persons':>12s}"
    lines = [
        f"=== {result.locality.capitalize()} (CBS geography: {result.municipality}) ===",
        "",
        f"  CBS occupants per dwelling by type: {by_type}",
        f"  CBS class anchors                 : {result.cbs_anchors}",
        "",
        "  buem's own dwelling mix:",
        header,
    ]
    for building_type, row in result.mix.iterrows():
        persons = result.cbs_by_type.get(str(building_type))
        shown = "n/a" if persons is None else f"{persons:.3f}"
        lines.append(
            f"    {building_type!s:6s} {int(row.buildings):10d} "
            f"{float(row.dwelling_units):10.0f} {float(row.unit_share):8.1%} {shown:>12s}"
        )
    lines += [
        "",
        f"  predicted persons/dwelling (CBS x buem mix) : {result.predicted_persons_per_dwelling:.3f}",
        f"  observed  persons/dwelling (AlleCijfers)    : {result.observed_persons_per_dwelling:.3f}",
        f"  observed  persons/household (AlleCijfers)   : {result.observed_persons_per_household:.3f}",
        f"  prediction / observation                    : {result.prediction_error:.3f}",
        "",
        f"  dwelling stock, AlleCijfers : {result.reference_dwelling_stock:.0f}",
        (
            f"  dwelling units, buem        : {result.buem_dwelling_units:.0f} "
            f"({result.stock_ratio:.2f}x reference)"
        ),
    ]
    return "\n".join(lines)


def write_csv(results: list[LocalityComparison], output_dir: Path) -> Path:
    """One row per locality, for comparison across localities."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "household_size_validation.csv"
    fields = [
        "locality", "municipality",
        "predicted_persons_per_dwelling", "observed_persons_per_dwelling",
        "observed_persons_per_household", "prediction_error",
        "reference_dwelling_stock", "buem_dwelling_units", "stock_ratio",
        "cbs_SFH", "cbs_TH", "cbs_MFH", "cbs_AB",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow({
                "locality": result.locality,
                "municipality": result.municipality,
                "predicted_persons_per_dwelling": round(result.predicted_persons_per_dwelling, 3),
                "observed_persons_per_dwelling": round(result.observed_persons_per_dwelling, 3),
                "observed_persons_per_household": round(result.observed_persons_per_household, 3),
                "prediction_error": round(result.prediction_error, 3),
                "reference_dwelling_stock": result.reference_dwelling_stock,
                "buem_dwelling_units": result.buem_dwelling_units,
                "stock_ratio": round(result.stock_ratio, 3),
                **{f"cbs_{k}": result.cbs_by_type.get(k) for k in ("SFH", "TH", "MFH", "AB")},
            })
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("localities", nargs="*", default=list(LOCALITIES),
                        help=f"Localities to check (default: all of {', '.join(LOCALITIES)}).")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Write household_size_validation.csv here (default: print only).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    unknown = [name for name in args.localities if name not in LOCALITIES]
    if unknown:
        parser.error(f"Unknown localities: {unknown}. Known: {sorted(LOCALITIES)}.")

    results = [compare(name) for name in args.localities]
    for result in results:
        print()
        print(report(result))
    print()

    if args.output_dir is not None:
        print(f"Wrote {write_csv(results, args.output_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
