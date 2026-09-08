"""
Netherlands TABULA archetype linking: matching each real building to a
TABULA Dutch archetype row, built independently of city2tabula's own
building-to-archetype link (whose reliability could not be verified).

TABULA's published Dutch archetype data itself (``tabula.csv``,
``Code_Country == "NL"``, 135 rows) is used as-is; its ``U_Wall_1``
values, converted to Rc = 1/U, match the Dutch Bouwbesluit's historical
minimum Rc requirements at every checkable period boundary (e.g.
1975-1991: both give 1.30 m²K/W), which independently validates the
dataset. The link built here uses only signals independent of
city2tabula: the real ``construction_year`` (3D BAG
``oorspronkelijkbouwjaar``) and a building_type/neighbour_status derived
by :mod:`buem.buildings.mapping.nl_building_classifier` (real geometry +
RIVM energy-label metadata, replicating CBS's published methodology --
see that module's docstring).

Construction year and energy label play two distinct roles:

1. **Construction year -> era (year class).** The real construction year
   (3D BAG ``oorspronkelijkbouwjaar``) determines the TABULA
   construction-year class, i.e. the as-built envelope geometry and
   construction of that era. This is never overridden: a renovated 1980
   building is still a 1980 building structurally.
2. **Energy label -> refurbishment variant within that era.** TABULA
   provides three variant rows per archetype (``Number_BuildingVariant``
   1/2/3: as-built, standard refurbishment, nZEB refurbishment). A real
   RIVM label materially better than the era's typical label indicates
   the building has been refurbished, and selects variant 2 or 3; the
   variant row's own refurbishment measures then adjust the U-values and
   infiltration rate downstream (see
   :func:`tabula_helpers.apply_refurbishment_measures`). NTA 8800/ISSO
   82.1 associate each construction era with a typical baseline label;
   ``LABEL_TO_YEAR_CLASS`` encodes that correspondence, and the gap
   between the label-implied tier and the year-implied tier is the
   refurbishment signal (see :func:`label_to_refurbishment_variant`).
   Buildings without a real label (the majority) keep the as-built
   variant -- an undetected refurbishment cannot currently be reflected.

The resulting year-class + variant + the classifier's building_type are
looked up against the real bundled Dutch TABULA sheet via
:func:`tabula_helpers.lookup_tabula_archetype` -- the same selection
logic ``LOD2Mapper``/``live_synthesis`` use for Germany. A matched
building's ``tabula_variant_code_id``/``tabula_variant_code`` are set to
that row's own ``id``/``Code_BuildingVariant``, so
``LOD2Mapper.map_building()`` picks up the correct variant (and its
refurbishment measures) with no further linking step.
"""

from __future__ import annotations

import logging

import pandas as pd

from buem.buildings.datasources.bag_use_function import PandUseSummary
from buem.buildings.mapping.nl_building_classifier import classify_all
from buem.buildings.mapping.tabula_helpers import lookup_tabula_archetype

logger = logging.getLogger(__name__)

# TABULA NL's construction-year class boundaries (from the bundled
# tabula.csv): NL.01 <=1964, NL.02 1965-1974, NL.03 1975-1991,
# NL.04 1992-2005, NL.05 2006-2014, NL.06 2015+. These align with the
# real Dutch Bouwbesluit code-change history (1965/1975/1992/2015).
_YEAR_CLASS_BOUNDARIES: list[tuple[int, str]] = [
    (1965, "01"), (1975, "02"), (1992, "03"), (2006, "04"), (2015, "05"),
]
_LATEST_YEAR_CLASS = "06"

# RIVM dominant_label -> the TABULA year-class whose typical as-built
# envelope performance NTA 8800/ISSO 82.1 associates with that label (see
# module docstring). B and A both fall in the source table's single
# "1992-2014" bucket; split across TABULA's finer NL.04/NL.05 boundary
# (B -> the earlier, A -> the later sub-period) to use the extra
# resolution TABULA offers -- that split itself is a convention, not
# independently sourced.
LABEL_TO_YEAR_CLASS: dict[str, str] = {
    "G": "NL.01", "F": "NL.01",
    "E": "NL.02", "D": "NL.02",
    "C": "NL.03",
    "B": "NL.04",
    "A": "NL.05",
    "A+": "NL.06", "A++": "NL.06", "A+++": "NL.06", "A++++": "NL.06", "A+++++": "NL.06",
}

# Gap between the label-implied performance tier and the construction-
# year-implied tier (in year-class steps) -> TABULA refurbishment variant.
# A building performing 1-2 tiers better than its era's typical as-built
# level maps to the standard-refurbishment variant; 3 or more tiers
# better maps to the nZEB-refurbishment variant.
_STANDARD_REFURBISHMENT_MIN_GAP = 1
_NZEB_REFURBISHMENT_MIN_GAP = 3


_EARLIEST_PLAUSIBLE_YEAR = 1000
"""Below this, a ``construction_year`` is a placeholder rather than a
date. BAG's own oldest real Dutch buildings are medieval, so nothing
genuine falls under it, while 0/-1 sentinels are common in derived
datasets."""


def year_to_construction_class(year: float | None) -> str | None:
    """Real construction year -> TABULA ``NL.0X`` class code, or ``None``
    if no year is available -- picking *any* class without a year would
    be a silent guess in either direction (oldest and newest are both
    unjustified); callers should skip TABULA matching for that building
    instead, the same way a missing building_type is already handled.
    Not currently reachable for the bundled Loenen data specifically
    (``construction_year`` is 100% populated from real BAG
    ``oorspronkelijkbouwjaar``), but a real possibility for other regions.
    """
    if year is None or pd.isna(year):
        return None
    try:
        year = int(year)
    except (TypeError, ValueError):
        # An empty string or other unparseable value is missing data, not
        # a year -- treat it the same as None rather than raising out of
        # a whole-region classification run.
        return None
    if year < _EARLIEST_PLAUSIBLE_YEAR:
        # Guards the sentinel case: a 0 (or similar placeholder) would
        # otherwise pass straight through the boundary walk and come back
        # as the oldest, most-uninsulated class, which is a silent
        # fabrication rather than a missing value.
        return None
    for boundary, code in _YEAR_CLASS_BOUNDARIES:
        if year < boundary:
            return f"NL.{code}"
    return f"NL.{_LATEST_YEAR_CLASS}"


def label_to_construction_class(label: str | None) -> str | None:
    """Real RIVM ``dominant_label`` -> equivalent TABULA year-class, or
    ``None`` if the label is missing/unrecognized (caller falls back to
    the year-derived class)."""
    if label is None or pd.isna(label):
        return None
    return LABEL_TO_YEAR_CLASS.get(str(label).strip())


def _class_index(year_class: str) -> int:
    """``"NL.03"`` -> ``3``. Year classes are ordinal (older to newer)."""
    return int(year_class.split(".")[1])


def label_to_refurbishment_variant(
    year_class: str | None, label_class: str | None,
) -> int:
    """TABULA refurbishment variant (1/2/3) implied by the gap between a
    building's era and its measured energy-label performance.

    Parameters
    ----------
    year_class : str or None
        Construction-year-derived class (the building's real era), e.g.
        ``"NL.03"``.
    label_class : str or None
        Energy-label-implied class (the era whose typical as-built
        performance matches the label), or ``None`` when no label exists.

    Returns
    -------
    int
        1 (as-built) when no label is available or the label performs
        at/below the era's typical level; 2 (standard refurbishment) for
        a 1-2 tier improvement; 3 (nZEB refurbishment) for 3+ tiers.
    """
    if year_class is None or label_class is None:
        return 1
    gap = _class_index(label_class) - _class_index(year_class)
    if gap >= _NZEB_REFURBISHMENT_MIN_GAP:
        return 3
    if gap >= _STANDARD_REFURBISHMENT_MIN_GAP:
        return 2
    return 1


# Typical dwelling floor area [m2] by TABULA building type, used only to
# estimate a dwelling count when the registered one is missing or
# implausible. Detached and terraced homes are larger than the ~120 m2
# national average; apartments are smaller.
TYPICAL_DWELLING_AREA_M2: dict[str, float] = {
    "SFH": 150.0,
    "TH": 120.0,
    "MFH": 90.0,
    "AB": 75.0,
}

# Above this floor area per dwelling, the registered count is more likely
# wrong than the building genuinely that large per household. Roughly four
# times the national average dwelling, so it flags missing data rather
# than merely spacious housing.
IMPLAUSIBLE_M2_PER_DWELLING = 500.0


def estimate_dwelling_units(
    floor_area_m2: float, building_type: str | None, recorded_units: float,
) -> float:
    """Estimate how many dwellings a building holds, from its floor area.

    A single BAG *Pand* can legitimately be an entire terrace or apartment
    block housing many households, and the registered
    ``aant_verblijfsobj`` is sometimes absent or counts only part of them.
    Where the registered count implies an impossible dwelling size, this
    derives one from floor area instead.

    The result is never lower than *recorded_units*: a registered count is
    treated as a floor, since the sub-units it does list genuinely exist.
    Buildings whose implied dwelling size is already plausible are
    returned unchanged, so this only ever acts where the data is
    self-evidently wrong.
    """
    if recorded_units <= 0:
        recorded_units = 1.0
    if floor_area_m2 <= 0:
        return recorded_units
    if floor_area_m2 / recorded_units <= IMPLAUSIBLE_M2_PER_DWELLING:
        return recorded_units
    typical = TYPICAL_DWELLING_AREA_M2.get(str(building_type), 120.0)
    return max(recorded_units, float(round(floor_area_m2 / typical)))


def repair_dwelling_counts(buildings_df: pd.DataFrame) -> pd.DataFrame:
    """Add floor-area-derived dwelling counts where the registered ones
    cannot be right.

    Only residential buildings are considered: a warehouse has no
    dwellings, and giving it a derived count would scale occupancy's
    service-building profile by a household multiplier that does not
    exist.

    Returns a copy carrying three columns, so a repaired value can never
    be mistaken for registered data:

    - ``residential_units_recorded`` -- exactly what RIVM registered.
    - ``residential_units_source`` -- ``"rivm"`` or ``"floor_area_estimate"``.
    - ``residential_units`` -- the value everything downstream should use.

    This matters twice over, in opposite directions: the count scales one
    household's occupancy-generated internal gains up to a whole block
    *before* the solve, and divides the whole-building result back down
    for comparison against per-dwelling statistics *after* it.
    """
    result = buildings_df.copy()
    # Start from the registered value, not a previously repaired one, so
    # re-running corrects an earlier repair rather than compounding it.
    if "residential_units_recorded" in result.columns:
        recorded = result["residential_units_recorded"].fillna(1.0)
    elif "residential_units" in result.columns:
        recorded = result["residential_units"].fillna(1.0)
    else:
        recorded = pd.Series(1.0, index=result.index)
    storeys = (
        result["number_of_storeys"].fillna(1.0).clip(lower=1.0)
        if "number_of_storeys" in result.columns
        else pd.Series(1.0, index=result.index)
    )
    floor_area = result.get("area_total_floor", pd.Series(0.0, index=result.index)).fillna(0.0) * storeys

    # Non-residential buildings have no dwellings to count. Estimating one
    # for a warehouse would scale occupancy's ServiceBuildingProfile output
    # by a fictitious household multiplier -- inflating its electricity and
    # internal gains by whatever the floor area happened to divide into.
    is_residential = (
        result["is_residential"].fillna(False).astype(bool)
        if "is_residential" in result.columns
        else pd.Series(True, index=result.index)
    )

    estimated = [
        estimate_dwelling_units(float(a), t, float(u)) if res else float(u)
        for a, t, u, res in zip(
            floor_area,
            result.get("building_type", pd.Series(None, index=result.index)),
            recorded, is_residential, strict=True,
        )
    ]
    result["residential_units_recorded"] = recorded.astype(float)
    result["residential_units"] = [float(e) for e in estimated]
    result["residential_units_source"] = [
        "floor_area_estimate" if e != r else "rivm"
        for e, r in zip(estimated, recorded, strict=True)
    ]

    n_repaired = int((result["residential_units_source"] == "floor_area_estimate").sum())
    if n_repaired:
        logger.info(
            "Estimated dwelling counts from floor area for %d of %d building(s) whose "
            "registered count implied more than %.0f m2 per dwelling.",
            n_repaired, len(result), IMPLAUSIBLE_M2_PER_DWELLING,
        )
    return result


def map_buildings(
    buildings_df: pd.DataFrame,
    nl_tabula_df: pd.DataFrame,
    rivm_labels_df: pd.DataFrame,
    use_by_pand_id: dict[str, PandUseSummary] | None = None,
) -> pd.DataFrame:
    """Full Netherlands archetype-linking pipeline.

    Parameters
    ----------
    buildings_df : pd.DataFrame
        ``cityjson_extractor``'s regenerated ``lod2_building_feature``
        table (must have ``bag_pand_id``, ``construction_year``,
        ``attached_neighbour_id``, ``is_greenhouse_or_warehouse``,
        ``is_glass_roof``).
    nl_tabula_df : pd.DataFrame
        The real bundled Dutch TABULA sheet (``Code_Country == "NL"``).
    rivm_labels_df : pd.DataFrame
        Output of ``rivm_energy_labels.load_labels_for_buildings`` --
        ``bag_pand_id``/``aant_verblijfsobj``/``dominant_label``.
    use_by_pand_id : dict of str to PandUseSummary, optional
        Output of ``bag_use_function.summarize_use_by_pand`` -- each
        building's registered BAG use functions. Without it, buildings
        whose real use is non-residential cannot be told apart from
        dwellings and are classified as houses; see
        :func:`nl_building_classifier.classify_all`.

    Returns
    -------
    pd.DataFrame
        A copy of ``buildings_df`` with ``building_type``,
        ``neighbour_status``, ``is_residential``,
        ``construction_year_class`` (the construction-year-derived era;
        label-derived only when no year exists), ``matched_via_label``
        (whether a real energy label informed the match),
        ``refurbishment_variant`` (1/2/3, see
        :func:`label_to_refurbishment_variant`),
        ``tabula_variant_code_id``, and ``tabula_variant_code``
        populated. Non-residential and
        no-archetype-match buildings keep ``tabula_variant_code_id`` null
        (unchanged from ``cityjson_extractor``'s own output) -- exactly
        as ``LOD2Mapper.map_building()`` already expects (it returns
        ``None`` cleanly for a building with no TABULA match, logging a
        warning, rather than raising).
    """
    units_by_pand_id = dict(zip(rivm_labels_df["bag_pand_id"], rivm_labels_df["aant_verblijfsobj"], strict=False))
    labels_by_pand_id = dict(zip(rivm_labels_df["bag_pand_id"], rivm_labels_df["dominant_label"], strict=False))

    classified = classify_all(buildings_df, units_by_pand_id, use_by_pand_id)

    # Real dwelling-unit count, carried through as its own column (not
    # just consumed internally by classify_all): an MFH/AB
    # building_feature_id is a whole multi-unit building, and downstream
    # per-dwelling comparisons (CBS's gas/electricity figures are per
    # dwelling, not per building) need this to normalize a
    # whole-building simulation result back to a per-unit figure.
    # Defaults to 1.0 (no RIVM match), matching classify_all's own
    # "no data -> treat as single-unit" convention.
    def _real_or_default_units(pid: str) -> float:
        units = units_by_pand_id.get(pid)
        return float(units) if units is not None and units > 0 else 1.0

    classified["residential_units"] = [_real_or_default_units(pid) for pid in classified["bag_pand_id"]]

    year_classes: list[str | None] = []
    matched_via_label: list[bool] = []
    refurbishment_variants: list[int] = []
    tabula_ids: list[float | None] = []
    tabula_codes: list[str | None] = []

    n_matched = 0
    n_missing_year = 0
    for _, row in classified.iterrows():
        if not row["is_residential"]:
            # No TABULA match -- that typology is residential-only -- but
            # the construction era still applies, and the service-building
            # reference table is keyed on it. Leaving it null sends every
            # service building to `_lookup_service_reference`'s
            # oldest-era fallback, so a hall built in 2020 would be
            # modelled with pre-1964 envelope U-values.
            non_residential_class = year_to_construction_class(row.get("construction_year"))
            if non_residential_class is None:
                n_missing_year += 1
            year_classes.append(non_residential_class)
            matched_via_label.append(False)
            refurbishment_variants.append(1)
            tabula_ids.append(None)
            tabula_codes.append(None)
            continue

        label = labels_by_pand_id.get(row["bag_pand_id"])
        label_class = label_to_construction_class(label)
        year_class = year_to_construction_class(row.get("construction_year"))
        if year_class is None:
            # No construction year: the label-implied class is the only
            # signal left; use it as the era with the as-built variant.
            n_missing_year += 1
            year_class = label_class
            variant = 1
        else:
            variant = label_to_refurbishment_variant(year_class, label_class)
        via_label = label_class is not None

        tabula_row = (
            lookup_tabula_archetype(
                row["building_type"], year_class.split(".")[1], "NL",
                sheet=nl_tabula_df, variant_number=variant,
            )
            if year_class is not None else None
        )
        year_classes.append(year_class)
        matched_via_label.append(via_label)
        refurbishment_variants.append(variant)
        if tabula_row is not None:
            tabula_ids.append(float(tabula_row["id"]))
            tabula_codes.append(str(tabula_row["Code_BuildingVariant"]))
            n_matched += 1
        else:
            logger.warning(
                "No TABULA archetype match for %s (building_type=%r year_class=%r)",
                row["bag_pand_id"], row["building_type"], year_class,
            )
            tabula_ids.append(None)
            tabula_codes.append(None)

    result = classified.copy()
    result["construction_year_class"] = year_classes
    result["matched_via_label"] = matched_via_label
    result["refurbishment_variant"] = refurbishment_variants
    result["tabula_variant_code_id"] = tabula_ids
    result["tabula_variant_code"] = tabula_codes

    if n_missing_year:
        # The construction year drives the era for both paths -- the TABULA
        # archetype for a dwelling, the service-building reference row for
        # everything else -- and each path falls back silently when it is
        # absent (to the energy label, and to the oldest era respectively).
        # A region where this fires for more than a handful of buildings
        # has a source-data problem worth fixing upstream, not a modelling
        # result worth trusting, so it is reported rather than swallowed.
        logger.warning(
            "map_buildings: %d/%d buildings have no usable construction_year -- "
            "their construction era is inferred from an energy label where one "
            "exists (residential) or falls back to the oldest reference era "
            "(non-residential). Check the CityJSON source's "
            "oorspronkelijkbouwjaar attribute for these.",
            n_missing_year, len(classified),
        )

    n_residential = int(result["is_residential"].sum())
    logger.info(
        "map_buildings: %d/%d residential buildings matched a TABULA archetype "
        "(%d via a real energy label, %d via construction year)",
        n_matched, n_residential, sum(matched_via_label), n_matched - sum(matched_via_label),
    )
    return result


__all__ = [
    "IMPLAUSIBLE_M2_PER_DWELLING",
    "LABEL_TO_YEAR_CLASS",
    "TYPICAL_DWELLING_AREA_M2",
    "estimate_dwelling_units",
    "label_to_construction_class",
    "map_buildings",
    "repair_dwelling_counts",
    "year_to_construction_class",
]
