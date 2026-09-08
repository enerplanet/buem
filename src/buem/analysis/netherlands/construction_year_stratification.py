"""
Stratified buem-vs-construction-year/insulation-class analysis of the
buem-vs-CBS heating-demand gap tracked in issue #3.

**What this checks, and why it differs from `validation.py`'s own
comparison**: CBS's 81528NED table has no construction-year dimension
(only ``RegioS``/``Perioden``/``Woningkenmerken``), so there is no
CBS-side per-era figure to compare against directly. What can be
checked instead: (1) whether buem's own simulated heating intensity
(kWh/m², rather than the whole-building kWh `validation.py` uses, which
avoids that module's dwelling-count normalization question entirely)
varies systematically by TABULA construction-year class within a
building type, and (2) whether ``validation.py``'s "first N buildings in
file order" sample selection is representative of the real
construction-year mix in the Apeldoorn dataset, or is skewed toward a
particular era -- a skew would directly inflate or deflate the
group-mean ratio ``validation.py`` reports, independent of any error in
the simulation itself.

Two building types (AB, MFH) have small enough real populations (12 and
28 buildings respectively) that a 5-buildings-per-group sample already
covers most of the population -- a small-N caveat on those two groups'
ratios specifically, independent of any sampling skew. For TH, the real
population is roughly 20% construction class NL.01 (the oldest, most
poorly insulated class), while ``validation.py``'s file-order sample for
TH/B_N1 turned out to be roughly 80% NL.01 -- a genuine sampling skew
toward the highest-heating-demand construction era.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from buem.analysis.netherlands.cbs_household_size import (
    NATIONAL_HOUSEHOLD_SIZE_BY_BUILDING_TYPE,
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
class EraStratum:
    """One (building_type, construction_class) stratum -- population
    share plus one representative building's simulated intensity."""

    building_type: str
    construction_class: str
    u_wall: float | None
    population_count: int
    population_share: float  # of this building_type's total population
    sample_building_id: str | None
    heating_kwh: float | None
    a_ref_m2: float | None
    num_persons_used: float | None = None
    h_tot_kw_k: float | None = None
    tabula_estimate_kwh_m2: float | None = None

    @property
    def intensity_kwh_m2(self) -> float | None:
        if self.heating_kwh is None or not self.a_ref_m2:
            return None
        return self.heating_kwh / self.a_ref_m2

    @property
    def tabula_estimate_kwh(self) -> float | None:
        """TABULA-method estimate as an absolute kWh figure -- the
        estimate's own kWh/m² rate multiplied by this same building's
        real A_ref, rather than derived by dividing buem's own kWh by
        area. A_ref is itself a derived, not directly-measured, quantity,
        so comparing two absolute kWh figures computed against the
        identical A_ref is a fairer comparison than comparing two
        independently-normalized per-m² intensities."""
        if self.tabula_estimate_kwh_m2 is None or self.a_ref_m2 is None:
            return None
        return self.tabula_estimate_kwh_m2 * self.a_ref_m2


# Real CBS figures -- national persons per dwelling in stock, private
# households only, derived in buem.analysis.netherlands.cbs_household_size
# from tables 85035NED (dwelling stock), 86064NED (persons by
# single-/multi-family dwelling) and 85140NED (the five-way dwelling-type
# split). See that module for the derivation and its limits, notably that
# CBS publishes no MFH/AB distinction, so both take the same figure.
#
# Prefer household_size_by_building_type(region_code) over this constant
# when the region is known: municipal figures differ materially from the
# national ones, most of all for apartments.
DEFAULT_NUM_PERSONS_BY_TYPE: dict[str, float] = dict(
    NATIONAL_HOUSEHOLD_SIZE_BY_BUILDING_TYPE
)


def _load_construction_class_lookup(
    data_dir: Path,
) -> tuple[dict[float, str], dict[float, float], dict[float, dict[str, float]]]:
    """id -> Code_ConstructionYearClass, id -> U_Wall_1, id -> a dict of the
    TABULA-method climate/gain parameters needed for
    :func:`tabula_degree_day_kwh_m2` (HeatingDays, Theta_e, theta_i,
    phi_int, F_red_htr1) -- all from tabula.csv."""
    tab = pd.read_csv(data_dir / "tabula.csv")
    class_map = tab.set_index("id")["Code_ConstructionYearClass"].to_dict()
    uwall_map = tab.set_index("id")["U_Wall_1"].to_dict()
    climate_cols = ["HeatingDays", "Theta_e", "theta_i", "phi_int", "F_red_htr1"]
    climate_map = tab.set_index("id")[climate_cols].to_dict(orient="index")
    return class_map, uwall_map, climate_map


def tabula_degree_day_kwh_m2(
    h_tot_kw_k: float,
    a_ref_m2: float,
    *,
    heating_days: float,
    theta_e: float,
    theta_i: float,
    phi_int_w_m2: float,
    f_red_htr: float = 1.0,
    solar_gain_kwh: float = 0.0,
    gain_utilization: float = 0.9,
) -> float:
    """A TABULA-*method*-style annual heating-need estimate (kWh/m²/yr),
    applied to a real building's own conductance -- **not** TABULA's own
    published ``q_h_nd`` figure (the bundled ``tabula.csv`` has no such
    column -- confirmed, see ``.claude/dhw_cooking_heat_handoff.md``).

    Deliberately reuses the same real building envelope buem's own hourly
    dynamic solve used (``h_tot_kw_k`` from that same simulation run) --
    this isolates *methodology* (TABULA's degree-day-style seasonal
    balance vs. buem's hourly dynamic ISO 13790 solve) from *envelope
    data* (real building vs. TABULA's own reference-building geometry),
    which the earlier German-building investigation
    (``tabula_vs_buem_heating_discrepancy.md``) could not cleanly separate.

    Formula: gross transmission+ventilation loss over the heating season,
    minus a simplified internal+solar gain offset (``gain_utilization``
    applied flatly, not TABULA's own gain/loss-ratio-dependent utilization
    factor -- a real simplification, not the exact ISO 13790 method; see
    module docstring)::

        Q_gross = f_red_htr * H_tot * HeatingDays * 24 * (theta_i - Theta_e) / 1000
        Q_gains = gain_utilization * (phi_int_w_m2 * A_ref * HeatingDays * 24 / 1000 + solar_gain_kwh)
        Q_H,nd  = max(0, Q_gross - Q_gains) / A_ref
    """
    if a_ref_m2 <= 0:
        raise ValueError(f"a_ref_m2 must be positive, got {a_ref_m2}")
    q_gross_kwh = f_red_htr * h_tot_kw_k * heating_days * 24.0 * (theta_i - theta_e)
    q_int_gain_kwh = phi_int_w_m2 * a_ref_m2 * heating_days * 24.0 / 1000.0
    q_gains_kwh = gain_utilization * (q_int_gain_kwh + solar_gain_kwh)
    q_h_nd_kwh = max(0.0, q_gross_kwh - q_gains_kwh)
    return q_h_nd_kwh / a_ref_m2


def population_distribution(data_dir: str | Path) -> pd.DataFrame:
    """Real (building_type, construction_class) population counts across
    *every* residential building in the dataset -- not just what
    ``validation.py`` samples. Returns a tidy DataFrame with a
    ``population_share`` column (share within its own building_type)."""
    data_dir = Path(data_dir)
    class_map, _, _ = _load_construction_class_lookup(data_dir)
    bdf = pd.read_csv(data_dir / "lod2_building_feature.csv")
    residential = bdf[bdf["is_residential"] == True].copy()  # noqa: E712
    residential["construction_class"] = residential["tabula_variant_code_id"].map(class_map)
    counts = (
        residential.groupby(["building_type", "construction_class"])
        .size()
        .rename("population_count")
        .reset_index()
    )
    totals = counts.groupby("building_type")["population_count"].transform("sum")
    counts["population_share"] = counts["population_count"] / totals
    return counts


def sample_skew_report(data_dir: str | Path, *, samples_per_group: int = 5) -> str:
    """Compare validation.py's actual "first N in file order" per-
    (building_type, neighbour_status) sample against the real population's
    construction-class mix -- one line per group showing both."""
    data_dir = Path(data_dir)
    class_map, _, _ = _load_construction_class_lookup(data_dir)
    bdf = pd.read_csv(data_dir / "lod2_building_feature.csv")
    residential = bdf[bdf["is_residential"] == True].copy()  # noqa: E712
    residential["construction_class"] = residential["tabula_variant_code_id"].map(class_map)

    lines = [f"{'type':6s} {'neigh':8s} {'sampled (first N)':40s} {'population %':30s}"]
    for (btype, nstatus), grp in residential.groupby(["building_type", "neighbour_status"]):
        sampled = grp.head(samples_per_group)["construction_class"].tolist()
        pop_pct = (
            grp["construction_class"].value_counts(normalize=True).mul(100).round(0).astype(int)
        )
        pop_str = ", ".join(f"{cls}:{pct}%" for cls, pct in sorted(pop_pct.items()))
        lines.append(f"{btype:6s} {nstatus:8s} {sampled!s:40s} {pop_str:30s}")
    return "\n".join(lines)


def _simulate_intensity(
    building: Building,
    weather_df: pd.DataFrame,
    *,
    use_milp: bool,
    num_persons: float | None = None,
    comfort_bounds: tuple[float, float] | None = None,
) -> tuple[float, float, float] | None:
    """Returns (heating_kwh, a_ref_m2, h_tot_kw_k) or None on failure.

    ``num_persons``, when given, overrides whatever the pipeline's own
    default would otherwise resolve to (buem's ``DEFAULT_NUM_PERSONS=4``,
    flat across every building type/size -- see
    ``.claude/dhw_cooking_heat_handoff.md``'s "num_persons realism"
    section for why this override exists).

    ``comfort_bounds``, when given as ``(lb, ub)``, overrides the
    archetype's own TABULA-``theta_i``-derived ``comfortT_lb``/
    ``comfortT_ub`` -- e.g. ``(18.0, 21.0)`` to test a real-world "prebound"
    -style lower/narrower setpoint against the official TABULA assumption
    (20/24 by default). See the same handoff doc's "comfort setpoint
    sensitivity" section.
    """
    from buem.integration.scripts.attribute_builder import AttributeBuilder
    from buem.thermal.model_buem import ModelBUEM

    try:
        attrs = dict(building_attrs_from(building))
        attrs["weather"] = weather_df
        attrs["use_provided_weather"] = True
        if num_persons is not None:
            attrs["num_persons"] = round(num_persons)
        merged = AttributeBuilder(payload_attrs=attrs).build()
        cfg = CfgBuilding(merged).to_cfg_dict()
        if comfort_bounds is not None:
            lb, ub = comfort_bounds
            cfg["comfortT_lb"] = lb
            cfg["comfortT_ub"] = ub
        model = ModelBUEM(cfg)
        model.sim_model(use_milp=use_milp)
        heating_kwh = round(float(pd.Series(model.heating_load).sum()), 2)
        a_ref = float(cfg.get("A_ref") or building.computed_A_ref())
        # Same aggregated conductance buem's own calcDesignHeatLoad() sums --
        # captured post-solve (self.bH is populated by _initEnvelop(), which
        # sim_model() always runs) for the TABULA-method estimate above.
        h_tot = sum(
            model.bH[c].get("Original", 0.0) for c in model.bH if "Original" in model.bH[c]
        )
        return heating_kwh, a_ref, h_tot
    except _PER_BUILDING_ERRORS as exc:
        logger.warning(
            "building_feature_id=%s: simulation failed (%s: %s)",
            building.identity.building_feature_id, type(exc).__name__, exc,
        )
        return None


def run_era_stratification(
    data_dir: str | Path,
    *,
    u_value_overrides_path: str | Path | None = None,
    weather_year: int = DEFAULT_YEAR,
    weather_provider: str = DEFAULT_WEATHER_PROVIDER,
    use_milp: bool = False,
    num_persons_by_type: dict[str, float] | None = None,
    comfort_bounds: tuple[float, float] | None = None,
) -> list[EraStratum]:
    """Simulate one representative building per (building_type,
    construction_class) stratum present in the real data, and report each
    stratum's real population share alongside its simulated intensity
    (kWh/m²) and a TABULA-method estimate -- see module docstring for what
    this reveals and why CBS itself can't be compared against per-stratum.

    ``num_persons_by_type``: e.g. :data:`DEFAULT_NUM_PERSONS_BY_TYPE`, to
    override buem's flat ``num_persons=4`` default with a type-specific
    value. ``None`` (default) leaves the pipeline's own default in place --
    pass it explicitly to test the sensitivity this was built to check.

    ``comfort_bounds``: e.g. ``(18.0, 21.0)``, to override every stratum's
    archetype-derived comfort setpoints uniformly -- see
    :func:`_simulate_intensity`'s docstring.
    """
    data_dir = Path(data_dir)
    class_map, uwall_map, climate_map = _load_construction_class_lookup(data_dir)
    pop = population_distribution(data_dir)

    source = CsvBuildingSource(data_dir)
    u_value_overrides = resolve_envelope_reference(
        u_value_overrides_path, data_dir, country="NL",
    )
    mapper = LOD2Mapper(source, country="NL", u_value_overrides=u_value_overrides)

    from buem.buildings.mapping.geometry_utils import building_lat_lon
    lats, lons = [], []
    for _, row in source.buildings.iterrows():
        result = building_lat_lon(row)
        if result is not None:
            lats.append(result[0])
            lons.append(result[1])
    lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
    weather_df = extract_provider_weather(lat, lon, weather_year, providers=(weather_provider,))[weather_provider]

    bdf = source.buildings
    residential = bdf[bdf["is_residential"] == True]  # noqa: E712

    results: list[EraStratum] = []
    for _, pop_row in pop.iterrows():
        btype = pop_row["building_type"]
        cclass = pop_row["construction_class"]
        candidates = residential[
            (residential["building_type"] == btype)
            & (residential["tabula_variant_code_id"].map(class_map) == cclass)
        ]
        bid = int(candidates.iloc[0]["building_feature_id"]) if len(candidates) else None
        num_persons = (num_persons_by_type or {}).get(btype)
        heating_kwh: float | None = None
        a_ref: float | None = None
        h_tot: float | None = None
        tabula_estimate: float | None = None
        if bid is not None:
            building = mapper.map_building(bid)
            if building is not None:
                sim = _simulate_intensity(
                    building, weather_df, use_milp=use_milp,
                    num_persons=num_persons, comfort_bounds=comfort_bounds,
                )
                if sim is not None:
                    heating_kwh, a_ref, h_tot = sim
        variant_id = candidates.iloc[0]["tabula_variant_code_id"] if len(candidates) else None
        climate = climate_map.get(variant_id) if variant_id is not None else None
        if h_tot is not None and a_ref is not None and climate is not None:
            tabula_estimate = tabula_degree_day_kwh_m2(
                h_tot, a_ref,
                heating_days=climate["HeatingDays"],
                theta_e=climate["Theta_e"],
                theta_i=climate["theta_i"],
                phi_int_w_m2=climate["phi_int"],
                f_red_htr=climate["F_red_htr1"],
            )
        results.append(EraStratum(
            building_type=btype,
            construction_class=cclass,
            u_wall=uwall_map.get(variant_id) if variant_id is not None else None,
            population_count=int(pop_row["population_count"]),
            population_share=float(pop_row["population_share"]),
            sample_building_id=str(bid) if bid is not None else None,
            heating_kwh=heating_kwh,
            a_ref_m2=a_ref,
            num_persons_used=num_persons,
            h_tot_kw_k=h_tot,
            tabula_estimate_kwh_m2=tabula_estimate,
        ))
    return results


def population_weighted_intensity(results: list[EraStratum]) -> dict[str, float]:
    """Population-share-weighted mean intensity (kWh/m²) per
    building_type -- what a *representative* sample (matching the real
    construction-era mix) would average to, vs. an unweighted mean of the
    same strata (what an equal-N-per-era sample would give)."""
    by_type: dict[str, list[EraStratum]] = defaultdict(list)
    for r in results:
        if r.intensity_kwh_m2 is not None:
            by_type[r.building_type].append(r)
    weighted: dict[str, float] = {}
    for btype, strata in by_type.items():
        total_share = sum(s.population_share for s in strata)
        if total_share <= 0:
            continue
        intensity_sum = 0.0
        for s in strata:
            intensity = s.intensity_kwh_m2
            if intensity is None:  # already excluded above; narrows the optional here
                continue
            intensity_sum += intensity * s.population_share
        weighted[btype] = intensity_sum / total_share
    return weighted


def format_era_report(results: list[EraStratum]) -> str:
    """Absolute kWh is the primary comparison column (buem's real
    ``heating_kwh`` vs. the TABULA-method estimate's rate multiplied by
    this same building's real A_ref): A_ref is itself a derived quantity,
    so two absolute kWh figures computed against the identical A_ref
    compare more fairly than two independently-normalized per-m²
    intensities. kWh/m² is kept alongside for readability across
    differently-sized buildings."""
    header = (
        f"{'type':6s} {'era':7s} {'U_wall':>7s} {'pop %':>6s} {'persons':>8s} "
        f"{'A_ref':>7s} {'buem kWh':>10s} {'TABULA-est kWh':>15s} "
        f"{'buem kWh/m2':>12s} {'TABULA-est kWh/m2':>18s}"
    )
    lines = [header]
    for r in sorted(results, key=lambda x: (x.building_type, x.construction_class)):
        u = f"{r.u_wall:.2f}" if r.u_wall is not None else "—"
        persons = f"{r.num_persons_used:.1f}" if r.num_persons_used is not None else "default"
        aref = f"{r.a_ref_m2:.1f}" if r.a_ref_m2 is not None else "—"
        buem_kwh = f"{r.heating_kwh:.0f}" if r.heating_kwh is not None else "—"
        tabula_kwh = f"{r.tabula_estimate_kwh:.0f}" if r.tabula_estimate_kwh is not None else "—"
        intensity = f"{r.intensity_kwh_m2:.1f}" if r.intensity_kwh_m2 is not None else "—"
        tabula = f"{r.tabula_estimate_kwh_m2:.1f}" if r.tabula_estimate_kwh_m2 is not None else "—"
        lines.append(
            f"{r.building_type:6s} {r.construction_class:7s} {u:>7s} "
            f"{r.population_share*100:5.0f}% {persons:>8s} {aref:>7s} "
            f"{buem_kwh:>10s} {tabula_kwh:>15s} {intensity:>12s} {tabula:>18s}"
        )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_NUM_PERSONS_BY_TYPE",
    "EraStratum",
    "format_era_report",
    "population_distribution",
    "population_weighted_intensity",
    "run_era_stratification",
    "sample_skew_report",
    "tabula_degree_day_kwh_m2",
]
