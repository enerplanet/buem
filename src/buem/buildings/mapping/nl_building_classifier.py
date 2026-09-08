"""
Netherlands building-type classification -- a from-scratch replacement for
city2tabula's (mistrusted, per the user 2026-08-17: "I do not fully trust
the TABULA and 3D BAG/LOD2 building mapping done by city2tabula") linking
between a real building and a TABULA archetype's ``Code_BuildingSizeClass``
(SFH/TH/MFH/AB) and ``Code_AttachedNeighbours`` (B_Alone/B_N1/B_N2).

Method: replicates CBS's own published ``woningtype`` derivation
methodology (Statistics Netherlands, the national statistics office --
see ``.claude/residential/resolved.md`` for the full research trail,
2026-08-17), rather than inventing a new one or training a classifier:

    "de[rivation] ... is based on a modeling approach where the number of
    connected BAG buildings and the number of BAG residential objects
    with their use function determines the assignment of housing type"
    -- CBS, https://www.cbs.nl/nl-nl/onze-diensten/methoden/
    onderzoeksomschrijvingen/korte-onderzoeksomschrijvingen/woningtype

Both signals CBS describes are already available here without needing
CBS's own (access-gated) microdata product:

- "number of connected BAG buildings" == this session's own geometric
  party-wall coincidence detection (``cityjson_extractor
  .detect_party_walls``), already recorded per building as
  ``attached_neighbour_id`` (semicolon-joined list of neighbouring
  ``bag_pand_id`` values).
- "number of BAG residential objects" == ``aant_verblijfsobj`` from the
  RIVM energy-labels GeoPackage (``energielabels_2025.gpkg`` --
  ``identificatie`` joins exactly to ``bag_pand_id`` once the
  ``NL.IMBAG.Pand.`` prefix is stripped; 99.4% match rate for Loenen,
  confirmed 2026-08-16/17).

CBS's exact category rules (Dutch, quoted from the page above) and their
mapping onto TABULA's coarser SFH/TH/MFH/AB + B_Alone/B_N1/B_N2 scheme
(buem's own judgment call, not from CBS -- CBS's 5 categories don't
correspond 1:1 to TABULA's 4):

======================  ======================================  ==========================
CBS category            CBS rule                                 TABULA mapping
======================  ======================================  ==========================
vrijstaand              0 connections                            SFH, B_Alone
twee-onder-een-kap      1 connection, pair (component size 2)     SFH, B_N1
hoekwoning              1 connection, in a row of 3+               TH, B_N1
tussenwoning            2+ connections (row middle)                TH, B_N2
meergezinswoning        2+ residential units in one Pand           MFH or AB (see below)
======================  ======================================  ==========================

The SFH/TH split for "1 connection" buildings follows TABULA's own
distinction between a semi-detached *pair* (still a single-family
dwelling *form*, structurally close to detached) and a genuine terraced
*row* (an architecturally different typology) -- a corner/end unit of a
3+ row is architecturally a row house even though it only touches one
neighbour, hence TH not SFH.

MFH vs. AB has no CBS or TABULA-published threshold -- buem's own
first-pass heuristic (``MFH_MAX_UNITS``), same framing as
``cfg_attribute.DEFAULT_ARCHETYPE_BY_BUILDING_TYPE``'s own "first-pass
heuristic, not a derivation" disclaimer: revisit with real data if/when
available.

Non-residential handling (2026-08-17, refined 2026-08-18): 3D BAG's own
``b3_kas_warenhuis`` (greenhouse/warehouse) and ``b3_is_glas_dak``
(glasshouse roof) flags (2 + 2 = 4 of Loenen's 3,105 buildings) mark a
building as not a dwelling -- excluded from TABULA *residential*
matching. Checking the flagged buildings' own footprint areas (per the
user, 2026-08-18: "the 4 buildings excluded ... should not be fully
excluded as they can be considered as service buildings and linked to
the occupancy module") before routing them uniformly surfaced a real
split, not assumed: the two ``b3_kas_warenhuis`` buildings are genuinely
large (2,125 m² / 1,718 m²) -- real commercial-scale structures, linked
here to occupancy's ``warehouse`` service-building type (the closest of
its 8 registered types; ``b3_kas_warenhuis`` conflates greenhouse *and*
department store under one flag, both closer to a large low-occupant-
density warehouse than to any other registered type). The two
``b3_is_glas_dak`` buildings are tiny (16 m² / 5 m²) -- too small to be
a real occupied structure at all (most likely a garden greenhouse/
conservatory/shed) -- these remain excluded from *both* residential and
service-building modeling, not force-fit into a type that wouldn't make
physical sense; ``MIN_SERVICE_BUILDING_FOOTPRINT_M2`` is the threshold
that separates the two cases.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import pandas as pd

from buem.buildings.datasources.bag_use_function import PandUseSummary

logger = logging.getLogger(__name__)

# Terminology, because the two BAG levels are easy to conflate and the
# distinction decides how a building is treated here:
#
# *Pand*             a physical structure. Every row this module classifies
#                    is a Pand, so "not in BAG" never applies -- these
#                    records come from BAG in the first place.
# *Verblijfsobject*  an independently addressable usable unit inside a
#                    Pand (a dwelling, a shop unit, an office suite). It
#                    carries the `gebruiksdoel` that says what the space
#                    is used for.
#
# A Pand with no verblijfsobject is therefore a real, registered building
# that contains no separately addressable usable space -- typically a
# garage, shed, barn, industrial hall or utility structure. It is *not*
# an unregistered or non-existent building, and BAG may still record it
# as `Pand in gebruik`. Because the use function lives on the
# verblijfsobject, such a building has no registered use at all, which is
# why it can be excluded from residential classification yet still not be
# typeable as a service building.

MIN_ANCILLARY_FOOTPRINT_M2 = 200.0
"""Above this footprint, a Pand with no verblijfsobject is treated as an
*ancillary structure* -- a real, in-use building whose use no
registration describes.

Below it the same category is dominated by garden sheds and domestic
garages, which carry negligible demand. Above it, it is dominated by
agricultural barns and industrial or storage halls, which do not: they
account for 94,234 m2 of footprint in Loenen and 204,462 m2 in Heeten,
more than everything currently typed as a service building.

**No attempt is made to split them further by activity.** Against 90
buildings labelled by OpenStreetMap as either agricultural
(``farm_auxiliary``/``cowshed``/``barn``/``shed``) or industrial
(``industrial``/``warehouse``/``manufacture``), the interquartile ranges
of every available feature -- footprint area, derived height, roof
complexity, storey count, construction year, roof area -- overlap
between the two classes. Nothing in the available geometry separates a
barn from a hall, so a rule that claimed to would be fitting noise.

That distinction also matters least energetically: both are unheated or
frost-protected rather than conditioned to comfort temperature, and that
single fact dominates their demand far more than which of the two they
are.

Flagging them is deliberately separate from simulating them. Every
service building is modelled through ``occupancy.ServiceBuildingProfile``
and no registered occupancy type describes unconditioned storage;
inventing a synthetic profile in buem instead is precluded by the
architectural rule that occupancy owns all occupant and equipment
behaviour. So these buildings are identified, counted and reported here,
and stay out of the simulated results until occupancy offers a profile
for them."""

MFH_MAX_UNITS = 4
"""First-pass heuristic split between MFH and AB when a Pand has 2+
residential units -- neither CBS nor TABULA publish a threshold for
this; revisit with real data if/when available (see module docstring)."""

MIN_SERVICE_BUILDING_FOOTPRINT_M2 = 50.0
"""Below this footprint, a building flagged non-residential is treated
as too small to be a real occupied structure (garden shed/greenhouse/
conservatory) rather than linked to a service-building type -- see
module docstring. Real Loenen buildings sit well clear of this threshold
on both sides (2,125/1,718 m² real commercial structures vs. 16/5 m²
garden structures), so this is not a finely-tuned cutoff."""

# 3D BAG flag -> occupancy.services_buildings.SERVICE_BUILDING_TYPES id,
# for a large-enough (>= MIN_SERVICE_BUILDING_FOOTPRINT_M2) flagged
# building. Only b3_kas_warenhuis has a real large-building case in
# Loenen today; b3_is_glas_dak's real cases are both tiny (see above),
# but the mapping is defined for either flag in case a future region has
# a large glass-roofed structure (e.g. a real glasshouse/greenhouse
# complex) that should also route to a service type.
_SERVICE_TYPE_BY_FLAG = {
    "is_greenhouse_or_warehouse": "warehouse",
    "is_glass_roof": "warehouse",
}

MIN_HOTEL_FOOTPRINT_M2 = 250.0
"""Below this footprint, a ``logiesfunctie`` building is a recreational
dwelling rather than a hotel.

BAG's ``logiesfunctie`` covers all short-stay accommodation, which in
rural Netherlands is dominated by holiday cabins and recreational homes
(*recreatiewoningen*), not hotels. Real evidence from two villages:
Heeten's 77 ``logiesfunctie`` buildings have a median footprint of 52 m2
and a maximum of 100 m2, Loenen's 16 a median of 70 m2 and a maximum of
111 m2 -- every one of them far below any plausible hotel.

occupancy's ``hotel`` profile describes a staffed, continuously-occupied
building with corridor lighting and commercial laundry. Applying it to a
52 m2 cabin overstates its demand by more than an order of magnitude, so
such buildings are left unmodelled rather than typed as hotels -- the
same treatment ``sportfunctie`` gets, and for the same reason: no
registered occupancy type describes them. A recreational-dwelling
profile is on the list of requests for the occupancy repo.

The threshold sits well clear of both villages' real data on one side
and of a genuine small hotel on the other, so it is a category
separator, not a tuned cutoff."""


# BAG gebruiksdoel -> occupancy.SERVICE_BUILDING_TYPES id.
#
# BAG's use functions and occupancy's service-building registry are two
# different taxonomies -- BAG describes a building's legally registered
# purpose, occupancy describes an occupancy/equipment pattern -- so each
# BAG function maps onto the registered type whose usage profile is
# closest, not onto an exact counterpart:
#
# - winkelfunctie (retail) -> supermarket: occupancy's only grocery/retail
#   profile, and the one whose open-7-days trading hours match a shop.
# - bijeenkomstfunctie (assembly: cafes, bars, community halls, places of
#   worship) -> restaurant: the registered type built around people
#   gathering in a space for extended, largely evening/weekend hours.
# - industriefunctie and overige gebruiksfunctie (storage, garage boxes,
#   utility structures) -> warehouse: low occupant density over a large
#   floor area, the defining feature of that profile.
# - sportfunctie has no close counterpart; a sports hall's intermittent
#   high-occupancy pattern matches none of the eight registered types, so
#   it is deliberately left unmapped rather than forced into one.
_SERVICE_TYPE_BY_BAG_FUNCTION = {
    "winkelfunctie": "supermarket",
    "kantoorfunctie": "office",
    "onderwijsfunctie": "school",
    "gezondheidszorgfunctie": "clinic",
    "logiesfunctie": "hotel",  # size-gated, see MIN_HOTEL_FOOTPRINT_M2
    "bijeenkomstfunctie": "restaurant",
    "industriefunctie": "warehouse",
    "overige gebruiksfunctie": "warehouse",
}


def build_adjacency(buildings_df: pd.DataFrame) -> dict[str, set[str]]:
    """bag_pand_id -> set of directly-adjacent bag_pand_id, from the
    ``attached_neighbour_id`` column (semicolon-joined, as written by
    ``cityjson_extractor``)."""
    graph: dict[str, set[str]] = defaultdict(set)
    for _, row in buildings_df.iterrows():
        pid = row["bag_pand_id"]
        graph.setdefault(pid, set())
        raw = row.get("attached_neighbour_id")
        if pd.isna(raw) or not raw:
            continue
        for neighbour in str(raw).split(";"):
            neighbour = neighbour.strip()
            if not neighbour:
                continue
            graph[pid].add(neighbour)
            graph[neighbour].add(pid)  # adjacency is symmetric even if only recorded one-sided
    return graph


def connected_component_sizes(graph: dict[str, set[str]]) -> dict[str, int]:
    """bag_pand_id -> size of the connected component (row/pair/etc.) it
    belongs to, via a plain BFS over the adjacency graph. An isolated
    building (no neighbours) has component size 1."""
    sizes: dict[str, int] = {}
    seen: set[str] = set()
    for start in graph:
        if start in seen:
            continue
        component: list[str] = []
        queue = [start]
        seen.add(start)
        while queue:
            node = queue.pop()
            component.append(node)
            for neighbour in graph.get(node, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        for node in component:
            sizes[node] = len(component)
    return sizes


def classify_building_type(
    degree: int, component_size: int, n_residential_units: float | None,
) -> tuple[str, str]:
    """Classify one building into (Code_BuildingSizeClass, Code_AttachedNeighbours).

    Parameters
    ----------
    degree : int
        Number of directly-attached neighbouring buildings (0, 1, 2+).
    component_size : int
        Size of the connected group this building belongs to (including
        itself) -- distinguishes a semi-detached *pair* (size 2) from a
        row's corner unit (size 3+, degree 1 either way).
    n_residential_units : float or None
        RIVM's ``aant_verblijfsobj`` for this Pand, or ``None`` if no
        match was found (treated as 1 -- the common case, not "unknown
        multi-unit").

    Returns
    -------
    (building_type, neighbour_status)
        e.g. ``("TH", "B_N1")``.
    """
    units = n_residential_units if n_residential_units and n_residential_units > 0 else 1.0

    if units >= 2:
        building_type = "MFH" if units <= MFH_MAX_UNITS else "AB"
        neighbour_status = "B_Alone" if degree == 0 else ("B_N1" if degree == 1 else "B_N2")
        return building_type, neighbour_status

    if degree == 0:
        return "SFH", "B_Alone"
    if degree == 1:
        return ("SFH", "B_N1") if component_size <= 2 else ("TH", "B_N1")
    return "TH", "B_N2"


def _service_building_type(row: pd.Series, use_summary: PandUseSummary | None = None) -> str | None:
    """Which occupancy service-building type (if any) a non-residential
    building links to.

    Two independent routes, in order of authority:

    1. The building's registered BAG use function, when it has one --
       the building's own legally recorded purpose, mapped through
       ``_SERVICE_TYPE_BY_BAG_FUNCTION``.
    2. 3D BAG's ``is_greenhouse_or_warehouse``/``is_glass_roof``
       geometry flags, for a building with no registered use to read.

    Returns ``None`` when neither route yields a type: a building too
    small to be a real occupied structure (see module docstring), one
    with no registered use and no flag, or one whose only registered
    function has no counterpart among occupancy's eight service types.
    """
    footprint = row.get("footprint_area") or 0.0
    if footprint < MIN_SERVICE_BUILDING_FOOTPRINT_M2:
        return None
    if use_summary is not None and use_summary.is_purely_non_residential:
        # Rank by unit count so a building's dominant registered use wins,
        # skipping functions occupancy has no counterpart for rather than
        # giving up on a building that also has a mappable one.
        for function, _count in sorted(
            use_summary.function_counts.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            service_type = _SERVICE_TYPE_BY_BAG_FUNCTION.get(function)
            if service_type == "hotel" and footprint < MIN_HOTEL_FOOTPRINT_M2:
                # A recreational dwelling, not a hotel -- see
                # MIN_HOTEL_FOOTPRINT_M2. Skip to the next registered
                # function rather than returning a type that would
                # overstate the building by an order of magnitude.
                continue
            if service_type is not None:
                return service_type
    for flag_col, service_type in _SERVICE_TYPE_BY_FLAG.items():
        if bool(row.get(flag_col)):
            return service_type
    return None


def _flag_ancillary_structures(
    result: pd.DataFrame,
    use_by_pand_id: dict[str, PandUseSummary] | None,
) -> pd.Series:
    """Mark the large Pand records that carry no verblijfsobject.

    See ``MIN_ANCILLARY_FOOTPRINT_M2`` for what the class means and why
    it is not subdivided. Without use-function data every building would
    qualify -- the flag depends on knowing a building has no registered
    unit, not merely that none was supplied -- so the whole column is
    False in that case.
    """
    if not use_by_pand_id:
        return pd.Series(False, index=result.index)
    has_unit = result["bag_pand_id"].isin(use_by_pand_id)
    footprint = pd.to_numeric(result["footprint_area"], errors="coerce")
    return (
        ~has_unit
        & ~result["is_residential"].astype(bool)
        & result["service_building_type"].isna()
        & (footprint >= MIN_ANCILLARY_FOOTPRINT_M2)
    )


def classify_all(
    buildings_df: pd.DataFrame,
    units_by_pand_id: dict[str, float],
    use_by_pand_id: dict[str, PandUseSummary] | None = None,
) -> pd.DataFrame:
    """Add ``building_type``/``neighbour_status``/``is_residential``/
    ``service_building_type`` columns to a copy of ``buildings_df``.

    ``use_by_pand_id`` is optional: omitted (or empty), classification
    falls back to the two geometry/unit-count signals below, which
    cannot separate a non-residential building from a dwelling -- every
    shop and office is then classified as a house. Supply it wherever a
    region's BAG use-function extract is available.

    A building flagged non-residential gets ``is_residential=False`` and
    null TABULA type/neighbour_status either way -- never forced into a
    residential archetype. Large enough ones (``>=
    MIN_SERVICE_BUILDING_FOOTPRINT_M2``) additionally get a real
    ``service_building_type`` (one of occupancy's registered service-
    building ids); buildings too small to be a real occupied structure
    get neither -- excluded from both residential and service modeling.

    Three independent signals each mark a building non-residential:

    0. ``use_by_pand_id[pid]`` (BAG ``gebruiksdoel``, via
       ``bag_use_function.summarize_use_by_pand``) showing every unit
       registered under the Pand serving a non-residential purpose.
       This is the only signal that can distinguish a shop, office or
       school from a dwelling: both of the signals below see such a
       building as an ordinary house, since it has a registered unit and
       carries no greenhouse/warehouse geometry flag. A building with
       *both* residential and non-residential units is genuinely
       mixed-use and stays residential -- buem models one use per
       building, so claiming it for the service path would discard its
       dwellings.
    1. 3D BAG's own ``is_greenhouse_or_warehouse``/``is_glass_roof``
       flags. See module docstring for the real Loenen evidence behind
       the size split above.
    2. ``units_by_pand_id[pid]`` (``aant_verblijfsobj``, the RIVM energy-
       labels GeoPackage's own count of registered residential units for
       this Pand) being present but zero or null -- i.e. the Pand *is*
       in RIVM's data, and RIVM records **no residential unit
       registered under it at all**. A Pand entirely absent from
       ``units_by_pand_id`` (no RIVM match either way) is treated as
       unknown, not non-residential -- it still defaults to the
       ambiguous 1-dwelling assumption, only genuinely no-registration
       excludes.

    This second signal was added 2026-08-21 after cross-checking real
    government housing statistics (BAG-derived, via a public aggregator)
    for Loenen and Heeten: buem's residential building counts ran 1.7-2.2x
    the official per-village address counts. Root cause: BAG registers
    every physical structure as its own Pand, including garden sheds,
    garages, and farm outbuildings -- none of these carry
    ``is_greenhouse_or_warehouse``/``is_glass_roof`` (those flags mean
    literal greenhouses/warehouses), so they fell through to the
    residential branch by default, each simulated as a 1-dwelling SFH.
    Checked directly against real Loenen/Heeten data, not assumed: every
    AB/MFH building has a registered unit (100%, as expected -- always
    formally registered), vs. only 37-50% of "SFH"-classified buildings,
    and buildings under 30 m2 footprint have one only ~5% of the time.
    Filtering to "has a registered unit" reproduces the real village
    address counts almost exactly (Loenen 1,443 vs. official 1,424-1,435;
    Heeten 1,570 vs. official 1,568-1,578) -- confirmed a much more
    precise signal than an arbitrary minimum-footprint cutoff.
    """
    graph = build_adjacency(buildings_df)
    sizes = connected_component_sizes(graph)
    use_by_pand_id = use_by_pand_id or {}

    building_types: list[str | None] = []
    neighbour_statuses: list[str | None] = []
    is_residential: list[bool] = []
    service_building_types: list[str | None] = []
    n_no_registered_unit = 0
    n_non_residential_use = 0
    for _, row in buildings_df.iterrows():
        pid = row["bag_pand_id"]
        units = units_by_pand_id.get(pid)
        use_summary = use_by_pand_id.get(pid)
        # A Pand matched in RIVM's data with no residential unit registered
        # under it at all -- distinct from "no RIVM match" (units_by_pand_id
        # has no entry for pid), which stays ambiguous rather than excluded.
        no_registered_unit = pid in units_by_pand_id and (pd.isna(units) or units == 0)
        # A Pand whose every registered unit serves a non-residential
        # purpose. RIVM's unit count cannot see this -- it counts units
        # without describing them, so a shop and a house both read as
        # "one registered unit".
        non_residential_use = use_summary is not None and use_summary.is_purely_non_residential
        non_residential = (
            bool(row.get("is_greenhouse_or_warehouse"))
            or bool(row.get("is_glass_roof"))
            or no_registered_unit
            or non_residential_use
        )
        if non_residential:
            if no_registered_unit:
                n_no_registered_unit += 1
            if non_residential_use:
                n_non_residential_use += 1
            building_types.append(None)
            neighbour_statuses.append(None)
            is_residential.append(False)
            service_building_types.append(_service_building_type(row, use_summary))
            continue
        service_building_types.append(None)
        degree = len(graph.get(pid, ()))
        component_size = sizes.get(pid, 1)
        btype, nstatus = classify_building_type(degree, component_size, units)
        building_types.append(btype)
        neighbour_statuses.append(nstatus)
        is_residential.append(True)

    result = buildings_df.copy()
    result["building_type"] = building_types
    result["neighbour_status"] = neighbour_statuses
    result["is_residential"] = is_residential
    result["service_building_type"] = service_building_types

    result["is_ancillary_structure"] = _flag_ancillary_structures(result, use_by_pand_id)

    n_non_residential = (~result["is_residential"]).sum()
    n_service = result["service_building_type"].notna().sum()
    n_unmodeled = n_non_residential - n_service
    n_ancillary = int(result["is_ancillary_structure"].sum())
    if n_ancillary:
        logger.info(
            "classify_all: %d of the unmodelled buildings are ancillary structures "
            "(>=%.0f m2, registered as a Pand but with no verblijfsobject) covering "
            "%.0f m2 of footprint -- real, in-use, unconditioned or frost-protected "
            "space that no registered use function describes. Flagged, not simulated: "
            "no occupancy profile describes them. See is_ancillary_structure.",
            n_ancillary, MIN_ANCILLARY_FOOTPRINT_M2,
            float(result.loc[result["is_ancillary_structure"], "footprint_area"].sum()),
        )
    if n_non_residential:
        logger.info(
            "classify_all: %d/%d buildings flagged non-residential -- %d linked to a "
            "service_building_type, %d not modeled (too small, or no mappable registered use), "
            "%d excluded for having no registered verblijfsobject (real "
            "structures, but with no addressable unit and so no recorded use), "
            "%d identified by a purely non-residential BAG use function",
            n_non_residential, len(result), n_service, n_unmodeled, n_no_registered_unit,
            n_non_residential_use,
        )
    logger.info("classify_all: building_type distribution: %s",
                result.loc[result["is_residential"], "building_type"].value_counts().to_dict())
    return result


__all__ = [
    "MFH_MAX_UNITS",
    "MIN_ANCILLARY_FOOTPRINT_M2",
    "MIN_HOTEL_FOOTPRINT_M2",
    "MIN_SERVICE_BUILDING_FOOTPRINT_M2",
    "build_adjacency",
    "classify_all",
    "classify_building_type",
    "connected_component_sizes",
]
