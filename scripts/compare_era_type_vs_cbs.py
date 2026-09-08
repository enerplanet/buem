"""Compare a completed ``buem.analysis.batch`` run against real CBS
consumption, resolved by construction era *and* building type.

Why by era
----------
buem picks a building's U-values primarily from its construction-year
class, so an aggregate comparison against CBS cannot tell a wrong
envelope for 1960s stock apart from a wrong one for 1990s stock. Table
85140NED publishes measured gas and electricity against both axes, giving
every (type, era) cell buem simulates a real counterpart -- see
:mod:`buem.analysis.netherlands.cbs_era_reference`.

Two independent caveats the output repeats, because both matter when
reading it:

*Geography.* The era-resolved CBS table is national. A municipality's own
figures exist only without era detail (table 81528NED, used by
``validation.py``). Neither table supersedes the other.

*Year.* Dutch gas use fell sharply over this period -- 2024 heating runs
0.69-0.80x its 2019 level -- so the CBS year must match the simulated
weather year or the comparison measures the weather, not the model.
85140NED starts at 2019, so a 2018 weather run is compared against 2019
and carries a one-year offset.

Usage::

    python scripts/compare_era_type_vs_cbs.py results/loenen_gm0200.parquet
    python scripts/compare_era_type_vs_cbs.py results/x.parquet --by-refurbishment
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from buem.analysis.netherlands.cbs_era_reference import ERA_LABELS, fetch_consumption_by_era

logger = logging.getLogger(__name__)

# Above this floor area per dwelling the recorded dwelling count is more
# likely wrong than the building that large, so the per-dwelling
# normalization -- and every ratio built on it -- is meaningless. Mirrors
# validation.py's own IMPLAUSIBLE_M2_PER_DWELLING.
IMPLAUSIBLE_M2_PER_DWELLING = 500.0


def load_results(path: Path, *, max_m2_per_dwelling: float | None) -> pd.DataFrame:
    """Read a batch parquet, keep successful residential rows, and
    normalize whole-building energy down to per-dwelling."""
    df = pd.read_parquet(path)
    total = len(df)
    df = df[df["status"] == "ok"].copy()
    df = df[df["building_type"].isin(["SFH", "TH", "MFH", "AB"])]

    units = df["residential_units"].fillna(1.0).where(lambda s: s > 0, 1.0)
    df["m2_per_dwelling"] = df["A_ref"] / units
    dropped = 0
    if max_m2_per_dwelling is not None:
        keep = df["m2_per_dwelling"] <= max_m2_per_dwelling
        dropped = int((~keep).sum())
        df = df[keep]
        units = units[keep]

    for column in ("heating_kWh", "elec_kWh", "dhw_kWh", "cooking_gas_kWh"):
        if column in df:
            df[column + "_per_dwelling"] = df[column] / units

    logger.info("%s: %d row(s), %d ok residential, %d dropped as implausible",
                path.name, total, len(df) + dropped, dropped)
    return df


def build_table(df: pd.DataFrame, cbs: dict, *, group_extra: str | None = None) -> pd.DataFrame:
    """One row per (building type, era[, extra]), with buem and CBS side
    by side."""
    keys = ["building_type", "construction_year_class"]
    if group_extra:
        keys.append(group_extra)

    grouped = df.groupby(keys).agg(
        n=("building_feature_id", "count"),
        buem_heat=("heating_kWh_per_dwelling", "mean"),
        buem_elec=("elec_kWh_per_dwelling", "mean"),
        buem_dhw=("dhw_kWh_per_dwelling", "mean"),
    ).reset_index()

    rows = []
    for _, row in grouped.iterrows():
        era = row["construction_year_class"]
        ref = cbs.get((row["building_type"], era))
        if ref is None:
            continue
        entry = {
            "type": row["building_type"],
            "era": era,
            "years": ERA_LABELS.get(era, era),
            "n": int(row["n"]),
            "buem_heat": round(float(row["buem_heat"]), 0),
            "cbs_heat": ref.useful_heat_kwh_per_year,
            "heat_ratio": round(float(row["buem_heat"]) / ref.useful_heat_kwh_per_year, 2),
            "buem_elec": round(float(row["buem_elec"]), 0),
            "cbs_elec": ref.electricity_kwh_per_year,
            "elec_ratio": round(float(row["buem_elec"]) / ref.electricity_kwh_per_year, 2),
        }
        if group_extra:
            entry[group_extra] = row[group_extra]
        rows.append(entry)

    table = pd.DataFrame(rows)
    if table.empty:
        return table
    order = {"SFH": 0, "TH": 1, "MFH": 2, "AB": 3}
    return table.sort_values(
        by=["type", "era"], key=lambda s: s.map(order) if s.name == "type" else s,
    ).reset_index(drop=True)


def weighted_summary(table: pd.DataFrame, df: pd.DataFrame) -> str:
    """Building-count-weighted mean ratios -- the figure to quote, since
    an unweighted mean gives a twelve-building group the same weight as a
    two-thousand-building one."""
    if table.empty:
        return "  (no comparable cells)"
    n = table["n"].sum()
    heat = (table["buem_heat"] * table["n"]).sum() / (table["cbs_heat"] * table["n"]).sum()
    elec = (table["buem_elec"] * table["n"]).sum() / (table["cbs_elec"] * table["n"]).sum()
    median = float((df["heating_kWh_per_dwelling"]).median())
    return (
        f"  buildings compared      : {n}\n"
        f"  count-weighted heat ratio: {heat:.2f}\n"
        f"  count-weighted elec ratio: {elec:.2f}\n"
        f"  median buem heat/dwelling: {median:,.0f} kWh"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("parquet", type=Path, help="Completed batch run.")
    parser.add_argument("--period", default="2019JJ00",
                        help="CBS Perioden code; match it to the simulated weather year.")
    parser.add_argument("--by-refurbishment", action="store_true",
                        help="Split each cell by TABULA refurbishment variant, to compare "
                             "as-built U-values against refurbished ones.")
    parser.add_argument("--max-m2-per-dwelling", type=float, default=IMPLAUSIBLE_M2_PER_DWELLING,
                        help="Exclude buildings implying more than this per dwelling. 0 disables.")
    parser.add_argument("--csv", type=Path, default=None, help="Also write the table here.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cap = args.max_m2_per_dwelling if args.max_m2_per_dwelling > 0 else None
    df = load_results(args.parquet, max_m2_per_dwelling=cap)
    cbs = fetch_consumption_by_era(args.period)

    extra = "refurbishment_variant" if args.by_refurbishment else None
    table = build_table(df, cbs, group_extra=extra)

    print()
    print(f"buem vs CBS {args.period[:4]} (national, era-resolved), per dwelling per year")
    print("=" * 78)
    if table.empty:
        print("  no comparable (type, era) cells")
    else:
        print(table.to_string(index=False))
    print()
    print(weighted_summary(table, df))
    print()
    print("  CBS side is national (85140NED has no regional breakdown);")
    print(f"  CBS year is {args.period[:4]} against the run's own weather year.")

    if args.csv is not None and not table.empty:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(args.csv, index=False)
        print(f"\nWrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
