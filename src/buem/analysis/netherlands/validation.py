"""
buem-vs-CBS validation: run real Netherlands buildings through the full
simulation pipeline, grouped by housing type, and compare simulated
heating demand against CBS's real regional gas-consumption statistics
(``cbs_reference``, converted via ``gas_conversion``).

Mirrors ``buem.analysis.batch``'s own simulation pattern (one weather
fetch shared across all buildings, ``AttributeBuilder``/``CfgBuilding``/
``ModelBUEM`` per building) adapted to a ``CsvBuildingSource`` region
instead of the German Excel workbook, and grouped by
(building_type, neighbour_status) -- the dimension CBS's own housing-type
categories key on (``cbs_reference.BUEM_TYPE_TO_CBS_KEY``) -- rather than
run flat across every building.

**Known, deliberate scope limits, not oversights**:

- One weather fetch for the whole region (its buildings' own mean real
  lat/lon), not per building -- reasonable for a single village/small
  town spanning a few km (matches ``batch.py``'s own single-location
  convention), would need reconsidering for a larger/more spread-out
  region.
- The gas -> useful-heat conversion (``gas_conversion``) rests on a
  national-average space-heating share and a round boiler-efficiency
  assumption -- see that module's own docstring for exactly which parts
  are solid vs. which are the most worth revisiting.
- :func:`run_validation` takes the **first N buildings in file order** per
  group. That order is not random with respect to construction era, and
  the skew is large enough to matter: terraced houses' sample came out
  80% oldest-class against 21% of the real population, inflating their
  reported intensity by roughly 1.9x. Treat sampled runs as a smoke test.
  For a figure to rely on, run every building through
  ``buem.analysis.batch`` and aggregate with :func:`aggregate_parquet`
  (``--from-parquet``), which has no sample to skew.

CLI
---
Sampled smoke test (fast, but see the sampling caveat above)::

    python -m buem.analysis.netherlands.validation \\
        --data-dir src/buem/data/buildings/netherlands/Loenen \\
        --region-code GM0200 --samples-per-type 5

Population-complete, aggregating a finished ``buem.analysis.batch`` run
(no sampling, so no sampling skew)::

    python -m buem.analysis.netherlands.validation \\
        --from-parquet loenen_all.parquet --region-code GM0200
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from buem.analysis.netherlands.cbs_reference import BUEM_TYPE_TO_CBS_KEY, fetch_consumption
from buem.analysis.netherlands.gas_conversion import (
    SPACE_HEATING_SHARE_OF_GAS,
    GasToHeatBreakdown,
    gas_m3_to_useful_heat_kwh,
)
from buem.analysis.provider_comparison import building_attrs_from
from buem.analysis.weather_providers import extract_provider_weather
from buem.buildings.building import Building
from buem.buildings.datasources.csv_source import CsvBuildingSource
from buem.buildings.mapping.lod2_mapper import LOD2Mapper
from buem.config.building_registry import DEFAULT_WEATHER_PROVIDER, DEFAULT_YEAR
from buem.config.cfg_building import CfgBuilding
from buem.config.reference_values import resolve_envelope_reference

logger = logging.getLogger(__name__)

_PER_BUILDING_ERRORS = (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, RuntimeError)


@dataclass
class TypeGroupResult:
    """Simulated + real-reference comparison for one (building_type,
    neighbour_status) group -- one row of the final report.

    Carries **two** parallel comparisons, not one, since 2026-08-18's DHW/
    cooking wiring (``.claude/dhw_cooking_heat_handoff.md``):

    1. The original: ``mean_simulated_heating_kwh`` (space heating only)
       vs. ``cbs_conversion`` (CBS's gas figure with the flat 78% share
       stripped off) -- kept unchanged, still meaningful as "how well does
       buem's space-heating-only output match once DHW/cooking are
       subtracted from CBS's side."
    2. New: ``mean_simulated_total_kwh`` (heating + dhw + cooking, all
       buem-simulated) vs. ``cbs_conversion_full`` (CBS's gas figure
       converted with *no* share stripped -- the real, total
       useful-heat-equivalent of a household's whole gas bill) -- the
       comparison DHW/cooking modeling was actually meant to enable: does
       buem's own total, now that it models more than space heating, land
       closer to CBS's real total without any CBS-side adjustment at all.
    """

    building_type: str
    neighbour_status: str
    cbs_key: str | None
    n_simulated: int
    mean_simulated_heating_kwh: float | None
    mean_simulated_dhw_kwh: float | None
    mean_simulated_cooking_kwh: float | None
    cbs_gas_m3_per_year: float | None
    cbs_conversion: GasToHeatBreakdown | None
    cbs_conversion_full: GasToHeatBreakdown | None
    building_ids: list[str] = field(default_factory=list)

    @property
    def mean_simulated_total_kwh(self) -> float | None:
        """heating + dhw + cooking, treating a missing DHW/cooking
        component as 0 (e.g. a building where occupancy generated no
        cooking_active signal) rather than making the whole total
        ``None`` -- heating alone is still a meaningful lower bound."""
        if self.mean_simulated_heating_kwh is None:
            return None
        return (
            self.mean_simulated_heating_kwh
            + (self.mean_simulated_dhw_kwh or 0.0)
            + (self.mean_simulated_cooking_kwh or 0.0)
        )

    @property
    def ratio_simulated_to_cbs(self) -> float | None:
        """simulated heating / CBS-implied *space-heating-only* useful
        heat (78%-stripped) -- 1.0 means exact agreement; >1 means buem
        simulates more heating than the CBS-derived estimate, <1 means
        less. ``None`` if either side is missing."""
        if self.mean_simulated_heating_kwh is None or self.cbs_conversion is None:
            return None
        if self.cbs_conversion.useful_heat_kwh <= 0:
            return None
        return self.mean_simulated_heating_kwh / self.cbs_conversion.useful_heat_kwh

    @property
    def ratio_total_to_cbs_full(self) -> float | None:
        """(heating + dhw + cooking) / CBS's *unstripped* total useful
        heat -- the comparison DHW/cooking modeling was added to enable.
        ``None`` if either side is missing."""
        total = self.mean_simulated_total_kwh
        if total is None or self.cbs_conversion_full is None:
            return None
        if self.cbs_conversion_full.useful_heat_kwh <= 0:
            return None
        return total / self.cbs_conversion_full.useful_heat_kwh


def _select_buildings_by_group(
    source: CsvBuildingSource, samples_per_group: int,
    *, labeled_only: bool = False,
) -> dict[tuple[str, str], list[int]]:
    """Group residential buildings' real ids by (building_type,
    neighbour_status), capped at ``samples_per_group`` ids per group --
    picked in file order (deterministic, not random), matching
    ``building_selection``'s own "first N that qualify" convention.

    ``labeled_only`` restricts selection to buildings whose TABULA match
    was informed by a real RIVM energy label (``matched_via_label``) --
    the subset whose current envelope performance, including any
    refurbishment, is known rather than inferred from construction year
    alone, and therefore the fairest subset to validate against measured
    consumption statistics.
    """
    bdf = source.buildings
    if "is_residential" not in bdf.columns:
        raise ValueError(
            "buildings table has no 'is_residential' column -- run "
            "nl_archetype_mapper.map_buildings() on this data_dir first."
        )
    residential = bdf[bdf["is_residential"] == True]  # noqa: E712
    if labeled_only:
        if "matched_via_label" not in residential.columns:
            raise ValueError(
                "labeled_only requires a 'matched_via_label' column -- run "
                "nl_archetype_mapper.map_buildings() on this data_dir first."
            )
        residential = residential[residential["matched_via_label"] == True]  # noqa: E712
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for _, row in residential.iterrows():
        btype, nstatus = row.get("building_type"), row.get("neighbour_status")
        if pd.isna(btype) or pd.isna(nstatus):
            continue
        key = (str(btype), str(nstatus))
        if len(groups[key]) >= samples_per_group:
            continue
        groups[key].append(int(row["building_feature_id"]))
    return groups


def _region_center(source: CsvBuildingSource) -> tuple[float, float]:
    """Mean real (lat, lon) across every building with real geometry --
    a single representative point for the region's one shared weather
    fetch (see module docstring)."""
    from buem.buildings.mapping.geometry_utils import region_center_lat_lon

    return region_center_lat_lon(source.buildings)


@dataclass
class _GroupMeans:
    """One group's simulated per-dwelling means, before the CBS reference
    is attached. The intermediate both entry points produce."""

    n: int
    heating: float | None
    dhw: float | None
    cooking: float | None
    ids: list[str] = field(default_factory=list)


def _cbs_results_for_groups(
    group_means: dict[tuple[str, str], _GroupMeans],
    region_code: str,
    period: str,
) -> list[TypeGroupResult]:
    """Attach the real CBS reference figure to each group's simulated means.

    Shared by both entry points -- the inline sampling run and the
    ``--from-parquet`` population run -- so the two cannot drift in how
    they convert or compare.
    """
    cbs_keys_needed = {BUEM_TYPE_TO_CBS_KEY[k] for k in group_means if k in BUEM_TYPE_TO_CBS_KEY}
    cbs_data = fetch_consumption(region_code, period, list(cbs_keys_needed)) if cbs_keys_needed else {}

    results: list[TypeGroupResult] = []
    for (btype, nstatus), means in sorted(group_means.items()):
        cbs_key = BUEM_TYPE_TO_CBS_KEY.get((btype, nstatus))
        cbs_figure = cbs_data.get(cbs_key) if cbs_key else None
        cbs_gas_m3 = cbs_figure.gas_m3_per_year if cbs_figure is not None else None
        conversion = gas_m3_to_useful_heat_kwh(cbs_gas_m3) if cbs_gas_m3 is not None else None
        # space_heating_share=1.0: no CBS-side stripping at all -- the
        # whole gas bill's useful-heat equivalent, comparable against
        # buem's own heating+dhw+cooking total now that it models more
        # than space heating (see TypeGroupResult's docstring).
        conversion_full = (
            gas_m3_to_useful_heat_kwh(cbs_gas_m3, space_heating_share=1.0)
            if cbs_gas_m3 is not None else None
        )
        results.append(TypeGroupResult(
            building_type=btype,
            neighbour_status=nstatus,
            cbs_key=cbs_key,
            n_simulated=means.n,
            mean_simulated_heating_kwh=means.heating,
            mean_simulated_dhw_kwh=means.dhw,
            mean_simulated_cooking_kwh=means.cooking,
            cbs_gas_m3_per_year=cbs_gas_m3,
            cbs_conversion=conversion,
            cbs_conversion_full=conversion_full,
            building_ids=means.ids,
        ))
    return results


# Floor area per dwelling above which the recorded dwelling count is more
# likely wrong than the building is large. An average Dutch dwelling is
# roughly 120 m2, so this is about four times that -- deliberately generous,
# since the point is to catch a missing or default dwelling count on a
# genuinely large block, not to second-guess spacious housing.
IMPLAUSIBLE_M2_PER_DWELLING = 500.0


def dwelling_area_check(df: pd.DataFrame) -> pd.Series:
    """Floor area per dwelling for each row of a batch result frame.

    CBS publishes consumption *per dwelling*, so every comparison divides a
    whole-building result by its ``residential_units``. Where that count is
    missing or wrong, the division silently produces a per-dwelling figure
    that no CBS category could match -- a 19,000 m2 building recorded as
    holding two dwellings yields an implausible 9,500 m2 dwelling. This
    surfaces that rather than letting it pass as a modelling result.
    """
    units = df["residential_units"].fillna(1.0).where(lambda s: s > 0, 1.0)
    return df["A_ref"] / units


def aggregate_parquet(
    parquet_path: str | Path,
    *,
    region_code: str,
    period: str,
    labeled_only: bool = False,
    max_m2_per_dwelling: float | None = None,
) -> list[TypeGroupResult]:
    """Group a completed ``buem.analysis.batch`` run by housing type and
    compare each group against CBS, without simulating anything.

    This is the population-complete counterpart to :func:`run_validation`.
    That function samples ``samples_per_group`` buildings in file order,
    which is not representative of the real construction-era mix -- a
    measured effect, not a theoretical one: terraced houses' sample ran
    80% oldest-class against 21% of the real population, inflating their
    reported intensity by ~1.9x. Aggregating a run that covered every
    building removes that bias by construction, on every dimension at
    once, rather than correcting for it on one.

    Each building's whole-building result is divided by its own
    ``residential_units`` so the group mean is per dwelling, matching what
    CBS publishes. Rows that did not simulate successfully are excluded.

    ``max_m2_per_dwelling`` drops buildings whose implied dwelling size
    exceeds it -- see :func:`dwelling_area_check`. Left unset by default:
    such rows are always *reported*, but excluding them changes the
    headline figure, which is the caller's decision to make rather than a
    silent default.
    """
    df = pd.read_parquet(parquet_path)
    df = df[df["status"] == "ok"]
    if labeled_only:
        if "matched_via_label" not in df.columns:
            raise ValueError(
                f"{parquet_path} has no 'matched_via_label' column -- it predates the "
                "Netherlands columns, or came from the German Excel source."
            )
        df = df[df["matched_via_label"].astype(str).str.lower() == "true"]
    if df.empty:
        raise ValueError(f"{parquet_path} contains no successfully-simulated rows to aggregate.")

    m2_per_dwelling = dwelling_area_check(df)
    implausible = m2_per_dwelling > IMPLAUSIBLE_M2_PER_DWELLING
    if implausible.any():
        logger.warning(
            "%d of %d building(s) imply more than %.0f m2 per dwelling (max %.0f) -- their "
            "recorded residential_units is more likely wrong than the building that large. "
            "Their per-dwelling figures cannot match any CBS category; pass "
            "max_m2_per_dwelling to exclude them.",
            int(implausible.sum()), len(df), IMPLAUSIBLE_M2_PER_DWELLING, m2_per_dwelling.max(),
        )
    if max_m2_per_dwelling is not None:
        dropped = int((m2_per_dwelling > max_m2_per_dwelling).sum())
        df = df[m2_per_dwelling <= max_m2_per_dwelling]
        logger.info("Excluded %d building(s) above %.0f m2/dwelling.", dropped, max_m2_per_dwelling)
        if df.empty:
            raise ValueError(
                f"max_m2_per_dwelling={max_m2_per_dwelling} excluded every row of {parquet_path}."
            )

    units = df["residential_units"].fillna(1.0).where(lambda s: s > 0, 1.0)
    per_dwelling = pd.DataFrame({
        "building_type": df["building_type"],
        "neighbour_status": df["neighbour_status"],
        "building_feature_id": df["building_feature_id"],
        "heating": df["heating_kWh"] / units,
        "dhw": df["dhw_kWh"].fillna(0.0) / units,
        "cooking": df["cooking_gas_kWh"].fillna(0.0) / units,
    }).dropna(subset=["building_type", "neighbour_status", "heating"])

    group_means: dict[tuple[str, str], _GroupMeans] = {}
    for (btype, nstatus), grp in per_dwelling.groupby(["building_type", "neighbour_status"]):
        group_means[(str(btype), str(nstatus))] = _GroupMeans(
            n=len(grp),
            heating=float(grp["heating"].mean()),
            dhw=float(grp["dhw"].mean()),
            cooking=float(grp["cooking"].mean()),
            ids=[str(i) for i in grp["building_feature_id"].tolist()],
        )
    logger.info(
        "Aggregated %d building(s) from %s into %d group(s)%s",
        len(per_dwelling), parquet_path, len(group_means),
        " (label-matched only)" if labeled_only else "",
    )
    return _cbs_results_for_groups(group_means, region_code, period)


def per_building_ratios(
    parquet_path: str | Path,
    *,
    region_code: str,
    period: str,
    max_m2_per_dwelling: float | None = None,
    metric: str = "total",
) -> pd.DataFrame:
    """One row per successfully-simulated *residential* building, carrying
    its own buem/CBS ratio -- the population :func:`aggregate_parquet`
    collapses into a single mean per (building_type, neighbour_status)
    group.

    Needed for any statistic below group level (a population median, or a
    breakdown by construction-year class): CBS's 81528NED table has no
    construction-year dimension (see
    ``construction_year_stratification``'s own docstring for why), so
    every building in a (building_type, neighbour_status) group shares
    that group's one CBS reference figure as its ratio's denominator --
    the per-building numerator is real, the denominator is the group's.

    ``metric`` selects which of ``TypeGroupResult``'s two parallel
    comparisons this reproduces at building level -- the two are not
    interchangeable and give visibly different numbers on the same data:

    - ``"total"`` (default): (heating + dhw + cooking) per dwelling vs.
      CBS's *unstripped* gas-total useful heat -- matches
      ``ratio_total_to_cbs_full``, the count-weighted headline figure.
    - ``"heating_only"``: space heating alone vs. CBS's gas figure with the
      national 78% space-heating share stripped off -- matches
      ``ratio_simulated_to_cbs``, the metric behind the previously-published
      per-building median (e.g. ``docs/source/validation/loenen_cbs.rst``'s
      "0.98"). DHW/cooking is real, modelled output that this metric
      excludes from both sides on purpose, for comparison against results
      computed before DHW/cooking modelling existed.

    The returned frame also carries ``intensity_kwh_m2`` -- the same
    numerator (heating, or heating+dhw+cooking, matching ``metric``)
    divided by the *whole building's* real reference floor area
    (``A_ref``, buem's own computed usable floor area: real LOD2 ground-
    footprint area times storey count, not divided by dwelling count).
    This has no CBS side and needs none: it is what surfaced Heeten's
    real house-size difference from Loenen (see
    ``docs/source/validation/heeten_cbs.rst``'s "Residual gap" section)
    -- ``ratio``/``per_dwelling_kwh`` answer "how does this compare to
    CBS", ``intensity_kwh_m2`` answers "how efficient is buem's own
    envelope model here", a fair like-for-like across regions/eras
    regardless of real house size or dwelling-count data quality.
    """
    if metric not in ("total", "heating_only"):
        raise ValueError(f"metric must be 'total' or 'heating_only', got {metric!r}")

    df = pd.read_parquet(parquet_path)
    df = df[df["status"] == "ok"].copy()

    residential_types = {t for (t, _n) in BUEM_TYPE_TO_CBS_KEY}
    df = df[df["building_type"].isin(residential_types)]
    if df.empty:
        raise ValueError(f"{parquet_path} contains no successfully-simulated residential rows.")

    if max_m2_per_dwelling is not None:
        df = df[dwelling_area_check(df) <= max_m2_per_dwelling]

    units = df["residential_units"].fillna(1.0).where(lambda s: s > 0, 1.0)
    if metric == "total":
        numerator_kwh = df["heating_kWh"] + df["dhw_kWh"].fillna(0.0) + df["cooking_gas_kWh"].fillna(0.0)
        space_heating_share = 1.0
    else:
        numerator_kwh = df["heating_kWh"]
        space_heating_share = SPACE_HEATING_SHARE_OF_GAS
    per_dwelling = numerator_kwh / units

    cbs_keys_needed = {
        BUEM_TYPE_TO_CBS_KEY[(t, n)]
        for t, n in zip(df["building_type"], df["neighbour_status"], strict=True)
        if (t, n) in BUEM_TYPE_TO_CBS_KEY
    }
    cbs_data = fetch_consumption(region_code, period, list(cbs_keys_needed)) if cbs_keys_needed else {}
    cbs_useful_heat = {
        key: gas_m3_to_useful_heat_kwh(fig.gas_m3_per_year, space_heating_share=space_heating_share).useful_heat_kwh
        for key, fig in cbs_data.items()
        if fig.gas_m3_per_year is not None
    }

    def _ratio(btype: str, nstatus: str, simulated: float) -> float | None:
        cbs_key = BUEM_TYPE_TO_CBS_KEY.get((btype, nstatus))
        cbs_val = cbs_useful_heat.get(cbs_key) if cbs_key else None
        if cbs_val is None or cbs_val <= 0:
            return None
        return simulated / cbs_val

    result = pd.DataFrame({
        "building_feature_id": df["building_feature_id"],
        "building_type": df["building_type"],
        "neighbour_status": df["neighbour_status"],
        "construction_year_class": df["construction_year_class"],
        "matched_via_label": df.get("matched_via_label"),
        "residential_units_source": df.get("residential_units_source"),
        "per_dwelling_kwh": per_dwelling,
        "intensity_kwh_m2": numerator_kwh / df["A_ref"],
        "ratio": [
            _ratio(t, n, v)
            for t, n, v in zip(df["building_type"], df["neighbour_status"], per_dwelling, strict=True)
        ],
    })
    return result.dropna(subset=["ratio"])


def stratified_ratio_table(ratios: pd.DataFrame) -> pd.DataFrame:
    """Median/mean/count buem-vs-CBS ratio, *and* buem's own median/mean
    heating intensity (kWh/m2, whole-building real floor area -- see
    :func:`per_building_ratios`'s ``intensity_kwh_m2`` docstring), per
    (building_type, construction_year_class) stratum, from
    :func:`per_building_ratios`'s per-building output.

    The median is the population statistic reported as this comparison's
    headline elsewhere (see ``docs/source/validation/loenen_cbs.rst``) --
    the mean is skewed upward by a long right tail (small-N MFH/AB groups
    with outlier ratios), so a stratum's median is a better read of what a
    typical building in it looks like. The same applies to intensity.

    Intensity has no CBS dependency, so it is a fair way to compare two
    regions/eras directly against each other even where their CBS
    reference or real house size differ -- unlike ``ratio``, which mixes
    buem's own output with whatever CBS/house-size differences exist
    between the strata being compared.
    """
    grouped = ratios.groupby(["building_type", "construction_year_class"])
    table = grouped.agg(
        n=("ratio", "count"),
        median_ratio=("ratio", "median"),
        mean_ratio=("ratio", "mean"),
        median_intensity_kwh_m2=("intensity_kwh_m2", "median"),
        mean_intensity_kwh_m2=("intensity_kwh_m2", "mean"),
    ).reset_index()
    return table.sort_values(["building_type", "construction_year_class"]).reset_index(drop=True)


def service_building_intensity_table(parquet_path: str | Path) -> pd.DataFrame:
    """Simulated annual heating+DHW+cooking intensity (kWh/m2) for
    non-residential buildings, grouped by (service_building_type,
    construction_year_class).

    No CBS comparison exists for these: CBS's 81528NED table is
    residential-dwelling consumption only (see ``TypeGroupResult``'s and
    ``docs/source/validation/loenen_cbs.rst``'s "Service buildings" notes
    -- "they carry no CBS housing-type key, so they do not enter the
    ratios above"). This reports buem's own simulated output on its own
    terms, comparable informally against the residential intensity table
    from the same run.

    ``construction_year_class`` is only ever populated by the residential
    TABULA-era classification stage (``nl_archetype_mapper.map_buildings``)
    -- a service building's source row carries it as null, since
    ``archetype_spec.py``'s non-residential path resolves an era from
    ``service_building_reference.csv`` directly rather than storing one
    back onto the building. Grouping on a null key would silently drop
    every service building (pandas' default groupby behaviour), so null is
    relabelled ``"unknown"`` rather than excluded.
    """
    df = pd.read_parquet(parquet_path)
    df = df[df["status"] == "ok"].copy()

    residential_types = {t for (t, _n) in BUEM_TYPE_TO_CBS_KEY}
    df = df[~df["building_type"].isin(residential_types)]
    if df.empty:
        return pd.DataFrame(columns=[
            "service_building_type", "construction_year_class", "n", "mean_kwh_m2", "median_kwh_m2",
        ])

    total_kwh = df["heating_kWh"] + df["dhw_kWh"].fillna(0.0) + df["cooking_gas_kWh"].fillna(0.0)
    tmp = pd.DataFrame({
        "service_building_type": df["building_type"],
        "construction_year_class": df["construction_year_class"].fillna("unknown"),
        "intensity_kwh_m2": total_kwh / df["A_ref"],
    })
    table = tmp.groupby(["service_building_type", "construction_year_class"])["intensity_kwh_m2"].agg(
        n="count", mean_kwh_m2="mean", median_kwh_m2="median",
    ).reset_index()
    return table.sort_values(["service_building_type", "construction_year_class"]).reset_index(drop=True)


def run_validation(
    data_dir: str | Path,
    *,
    u_value_overrides_path: str | Path | None = None,
    region_code: str,
    period: str,
    samples_per_group: int = 3,
    weather_year: int = DEFAULT_YEAR,
    weather_provider: str = DEFAULT_WEATHER_PROVIDER,
    use_milp: bool = False,
    labeled_only: bool = False,
) -> list[TypeGroupResult]:
    """Run the full buem-vs-CBS comparison for one region.

    Parameters
    ----------
    data_dir : str or Path
        A ``CsvBuildingSource`` directory (e.g.
        ``src/buem/data/buildings/netherlands/Loenen``) already run through
        ``nl_archetype_mapper`` (real ``building_type``/
        ``tabula_variant_code_id`` populated).
    u_value_overrides_path : str or Path, optional
        Path to ``u_value_reference.csv`` (default: ``u_value_reference
        .csv`` inside ``data_dir``, if present).
    region_code, period : str
        CBS ``RegioS``/``Perioden`` codes, e.g. ``"GM0200"``/``"2024JJ00"``.
    samples_per_group : int
        How many buildings to simulate per (building_type,
        neighbour_status) group -- keeps this a smoke-scale comparison,
        not a full-population run (a real compute cost, see module
        docstring).
    labeled_only : bool
        Restrict the sample to buildings with a real RIVM energy label
        (see :func:`_select_buildings_by_group`).

    Returns
    -------
    list of TypeGroupResult
        One entry per (building_type, neighbour_status) group that had
        at least one successfully-simulated building.
    """
    source = CsvBuildingSource(data_dir)
    u_value_overrides = resolve_envelope_reference(
        u_value_overrides_path, data_dir, country="NL",
    )

    # Optional per-region tables, loaded on the same "absent is normal"
    # basis batch.py uses -- so a validation run and a batch run of the
    # same region model identical buildings rather than diverging on
    # whichever reference data each happened to pick up.
    def _optional_table(filename: str) -> pd.DataFrame | None:
        path = Path(data_dir) / filename
        return pd.read_csv(path) if path.exists() else None

    mapper = LOD2Mapper(
        source, country="NL",
        u_value_overrides=u_value_overrides,
        service_building_reference=_optional_table("service_building_reference.csv"),
        refurbishment_measure_overrides=_optional_table("refurbishment_measure_reference.csv"),
    )

    lat, lon = _region_center(source)
    logger.info("Region center for shared weather fetch: (%.4f, %.4f)", lat, lon)
    weather_df = extract_provider_weather(lat, lon, weather_year, providers=(weather_provider,))[weather_provider]

    groups = _select_buildings_by_group(source, samples_per_group, labeled_only=labeled_only)
    logger.info("Selected %d (building_type, neighbour_status) groups, up to %d building(s) each",
                len(groups), samples_per_group)

    group_means: dict[tuple[str, str], _GroupMeans] = {}
    for (btype, nstatus), building_ids in sorted(groups.items()):
        heating_values: list[float] = []
        dhw_values: list[float] = []
        cooking_values: list[float] = []
        simulated_ids: list[str] = []
        for bid in building_ids:
            building = mapper.map_building(bid)
            if building is None:
                continue
            # An MFH/AB building_feature_id is a whole multi-unit building
            # (real Loenen AB rows have 257-756 m2 footprints and 7-19
            # dwellings), matching TABULA's own AB/MFH archetypes, which
            # likewise describe a whole block. The dwelling count is used
            # twice, in opposite directions: scaling one dwelling's
            # occupancy gains up to the whole block before the solve, then
            # dividing the whole-block result back down for comparison
            # against CBS's per-dwelling figures. 1.0 for SFH/TH (already
            # one dwelling per building) makes both a no-op.
            units = source.buildings.loc[
                source.buildings["building_feature_id"] == bid, "residential_units",
            ]
            units_value = float(units.iloc[0]) if len(units) and pd.notna(units.iloc[0]) else 1.0

            energy = _simulate_energy_kwh(
                building, weather_df, use_milp=use_milp, residential_units=units_value,
                region_code=region_code,
            )
            if energy is not None:
                heating_values.append(energy["heating_kwh"] / units_value)
                dhw_values.append(energy["dhw_kwh"] / units_value)
                cooking_values.append(energy["cooking_kwh"] / units_value)
                simulated_ids.append(str(bid))

        group_means[(btype, nstatus)] = _GroupMeans(
            n=len(heating_values),
            heating=(sum(heating_values) / len(heating_values)) if heating_values else None,
            dhw=(sum(dhw_values) / len(dhw_values)) if dhw_values else None,
            cooking=(sum(cooking_values) / len(cooking_values)) if cooking_values else None,
            ids=simulated_ids,
        )

    return _cbs_results_for_groups(group_means, region_code, period)


def _simulate_energy_kwh(
    building: Building, weather_df: pd.DataFrame, *, use_milp: bool,
    residential_units: float = 1.0, region_code: str | None = None,
) -> dict[str, float] | None:
    """Run one building through the same real
    AttributeBuilder/CfgBuilding/ModelBUEM path ``batch.py``/
    ``provider_comparison.py`` use, returning annual
    ``{"heating_kwh", "dhw_kwh", "cooking_kwh"}`` (the latter two 0.0 when
    ``ModelBUEM._addDhwCooking()`` had nothing to compute, e.g. no
    ``cooking_active`` signal for this particular household) or ``None``
    on any per-building failure (logged, not raised -- one bad building
    shouldn't abort the whole comparison).

    ``residential_units`` scales the occupancy-generated internal gains
    from one dwelling up to the whole building for multi-dwelling
    AB/MFH blocks -- see
    ``AttributeBuilder.generate_electricity_profile()``. Injected here
    rather than carried through ``building_attrs_from()``'s v3 GeoJSON
    round-trip, since the v3 request schema has no dwelling-count field.
    """
    from buem.integration.scripts.attribute_builder import AttributeBuilder
    from buem.thermal.model_buem import ModelBUEM

    try:
        attrs = dict(building_attrs_from(building))
        attrs["weather"] = weather_df
        attrs["use_provided_weather"] = True
        attrs["residential_units"] = residential_units
        # Lets AttributeBuilder pick this municipality's own occupants-per-
        # dwelling figures over the national ones, which differ materially
        # for apartments. Same CBS RegioS code the comparison itself uses,
        # so the simulated and reference sides describe one region.
        attrs["region_code"] = region_code
        merged = AttributeBuilder(payload_attrs=attrs).build()
        cfg = CfgBuilding(merged).to_cfg_dict()
        model = ModelBUEM(cfg)
        model.sim_model(use_milp=use_milp)
        return {
            "heating_kwh": round(float(pd.Series(model.heating_load).sum()), 2),
            "dhw_kwh": round(float(model.dhw_kWh.sum()), 2) if model.dhw_kWh is not None else 0.0,
            "cooking_kwh": round(float(model.cooking_gas_kWh.sum()), 2) if model.cooking_gas_kWh is not None else 0.0,
        }
    except _PER_BUILDING_ERRORS as exc:
        logger.warning("building_feature_id=%s: simulation failed (%s: %s)",
                       building.identity.building_feature_id, type(exc).__name__, exc)
        return None


def format_report(results: list[TypeGroupResult]) -> str:
    """Plain-text summary table -- one row per group.

    Two comparisons side by side (see ``TypeGroupResult``'s docstring):
    the original space-heating-only vs. 78%-stripped-CBS columns
    (unchanged), and new total (heating+dhw+cooking) vs. CBS's real
    unstripped-gas-total columns -- the comparison DHW/cooking modeling
    was added to enable.
    """
    lines = [
        (f"{'type':6s} {'neigh':8s} {'n':>3s} "
         f"{'heat kWh':>10s} {'CBS heat':>10s} {'ratio':>6s}  |  "
         f"{'total kWh':>10s} {'CBS full':>10s} {'ratio':>6s}"),
    ]
    for r in results:
        heat_val = f"{r.mean_simulated_heating_kwh:.0f}" if r.mean_simulated_heating_kwh is not None else "—"
        cbs_heat = f"{r.cbs_conversion.useful_heat_kwh:.0f}" if r.cbs_conversion is not None else "—"
        heat_ratio = f"{r.ratio_simulated_to_cbs:.2f}" if r.ratio_simulated_to_cbs is not None else "—"
        total_val = f"{r.mean_simulated_total_kwh:.0f}" if r.mean_simulated_total_kwh is not None else "—"
        cbs_full = f"{r.cbs_conversion_full.useful_heat_kwh:.0f}" if r.cbs_conversion_full is not None else "—"
        total_ratio = f"{r.ratio_total_to_cbs_full:.2f}" if r.ratio_total_to_cbs_full is not None else "—"
        lines.append(
            f"{r.building_type:6s} {r.neighbour_status:8s} {r.n_simulated:3d} "
            f"{heat_val:>10s} {cbs_heat:>10s} {heat_ratio:>6s}  |  "
            f"{total_val:>10s} {cbs_full:>10s} {total_ratio:>6s}"
        )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare buem's simulated heating demand against real CBS gas statistics.")
    parser.add_argument(
        "--data-dir", type=str, default=None,
        help="CsvBuildingSource region directory. Required unless --from-parquet is given.",
    )
    parser.add_argument(
        "--from-parquet", type=str, default=None,
        help="Aggregate a completed `buem.analysis.batch` run instead of simulating. "
             "Covers every building it contains, so no sampling (and no sampling skew) "
             "is involved; --samples-per-type and the weather options do not apply.",
    )
    parser.add_argument("--u-value-overrides", type=str, default=None)
    parser.add_argument("--region-code", type=str, required=True, help='e.g. "GM0200" for Apeldoorn')
    parser.add_argument(
        "--period", type=str, default=None,
        help='CBS "Perioden" code, e.g. "2024JJ00". Defaults to "<weather-year>JJ00" -- '
             "matching CBS's real consumption year to the simulated weather year matters "
             "(Apeldoorn's real gas consumption roughly halved between 2018 and 2024 due to "
             "conservation/insulation/heat-pump trends, so a year mismatch is a real, "
             "non-trivial error source). Pass explicitly to compare mismatched years on purpose.",
    )
    parser.add_argument(
        "--max-m2-per-dwelling", type=float, default=None,
        help="With --from-parquet: exclude buildings implying more floor area per "
             f"dwelling than this (suggested: {IMPLAUSIBLE_M2_PER_DWELLING:.0f}). Such a "
             "building's recorded dwelling count is more likely wrong than the building "
             "that large, and its per-dwelling figure cannot match any CBS category. "
             "Off by default -- these rows are always reported, but excluding them "
             "changes the headline number.",
    )
    parser.add_argument("--samples-per-type", type=int, default=3)
    parser.add_argument("--weather-year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--weather-provider", type=str, default=DEFAULT_WEATHER_PROVIDER)
    parser.add_argument("--use-milp", action="store_true")
    parser.add_argument(
        "--labeled-only", action="store_true",
        help="Restrict to buildings with a real RIVM energy label -- the subset whose "
             "current envelope performance, including refurbishment, is known rather "
             "than inferred from construction year alone. Applies to whichever input "
             "is in use (the sampled run, or the --from-parquet population).",
    )
    return parser


def main(argv: list[str] | None = None) -> list[TypeGroupResult]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    period = args.period if args.period is not None else f"{args.weather_year}JJ00"
    if args.from_parquet:
        results = aggregate_parquet(
            args.from_parquet,
            region_code=args.region_code,
            period=period,
            labeled_only=args.labeled_only,
            max_m2_per_dwelling=args.max_m2_per_dwelling,
        )
    else:
        if not args.data_dir:
            parser.error("--data-dir is required unless --from-parquet is given.")
        if args.max_m2_per_dwelling is not None:
            parser.error("--max-m2-per-dwelling applies only to --from-parquet.")
        results = run_validation(
            args.data_dir,
            u_value_overrides_path=args.u_value_overrides,
            region_code=args.region_code,
            period=period,
            samples_per_group=args.samples_per_type,
            weather_year=args.weather_year,
            weather_provider=args.weather_provider,
            use_milp=args.use_milp,
            labeled_only=args.labeled_only,
        )
    print(format_report(results))
    return results


if __name__ == "__main__":
    main()


__all__ = [
    "IMPLAUSIBLE_M2_PER_DWELLING",
    "TypeGroupResult",
    "aggregate_parquet",
    "dwelling_area_check",
    "format_report",
    "main",
    "per_building_ratios",
    "run_validation",
    "service_building_intensity_table",
    "stratified_ratio_table",
]
