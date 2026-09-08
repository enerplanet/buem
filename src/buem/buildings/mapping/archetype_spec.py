"""Every per-archetype parameter ``LOD2Mapper`` needs, from whichever
reference source describes the building.

``LOD2Mapper`` used to read these directly off a matched TABULA row,
which meant a building with no TABULA archetype could not be mapped at
all. TABULA is a *residential* typology, so that excluded every
non-residential building outright -- they were skipped before reaching
``AttributeBuilder``, and therefore before occupancy's
``ServiceBuildingProfile`` was ever consulted, even though that routing
already worked.

Collecting the parameters behind one dataclass lets the geometry,
opening-synthesis and element-assembly steps stay identical regardless of
where the thermal description came from: TABULA for residential
buildings, a reference table for service buildings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from buem.buildings.mapping.tabula_helpers import (
    apply_refurbishment_measures,
    safe_series_float,
    select_primary_variant,
)
from buem.config.reference_values import resolve_glazing

logger = logging.getLogger(__name__)

# (column, repr(default)) pairs already reported, so a batch run logs each
# distinct substitution once rather than once per building.
_WARNED_SERVICE_DEFAULTS: set[tuple[str, str]] = set()


@dataclass
class ArchetypeSpec:
    """Thermal and typological description of one building's archetype."""

    # Envelope: U-value [W/m²K] and transmission-reduction factor per component.
    wall_U: float
    wall_b: float
    roof_U: float
    roof_b: float
    floor_U: float
    floor_b: float
    window_U: float
    window_g_gl: float
    door_U: float

    # Opening sizing. Windows are sized from each wall's own area
    # (element_factory.uniform_window_ratios), so only doors and any
    # horizontal (roof) glazing need an archetype-derived figure.
    door_ratio: float
    horizontal_window_area: float

    # Use and construction parameters (ISO 13790 / EN 16798-1 terms).
    n_air_infiltration: float
    n_air_use: float
    c_m: float
    h_room: float
    F_sh_hor: float
    F_sh_vert: float
    F_f: float
    F_w: float
    phi_int: float | None
    q_w_nd: float | None
    design_T_min: float
    F_red_htr: float

    # Identity.
    building_type: str
    construction_period: str
    neighbour_status: str


def spec_from_tabula(
    tabula_row: pd.Series,
    *,
    u_value_overrides: pd.Series | None = None,
    measure_overrides: pd.DataFrame | None = None,
) -> ArchetypeSpec:
    """Build a spec from a matched TABULA archetype row.

    ``u_value_overrides`` is a single already-matched row of the editable
    U-value table, applied *before* refurbishment measures so that added
    insulation compounds with whichever base U-value is in effect.
    ``measure_overrides`` corrects individual refurbishment measures whose
    published performance no longer reflects the real market -- see
    :func:`buem.buildings.mapping.tabula_helpers.apply_refurbishment_measures`.
    """
    wall_U, wall_b = select_primary_variant(tabula_row, "Wall", n_variants=3)
    roof_U, roof_b = select_primary_variant(tabula_row, "Roof", n_variants=2)
    floor_U, floor_b = select_primary_variant(tabula_row, "Floor", n_variants=2)
    window_U = safe_series_float(tabula_row, "U_Window_1", 2.8)
    window_g_gl = safe_series_float(tabula_row, "g_gl_n_Window_1", 0.5)
    door_U = safe_series_float(tabula_row, "U_Door_1", 3.0)

    if u_value_overrides is not None:
        wall_U = float(u_value_overrides["U_Wall"])
        roof_U = float(u_value_overrides["U_Roof"])
        floor_U = float(u_value_overrides["U_Floor"])
        door_U = float(u_value_overrides["U_Door"])
        # U_Window may name a glazing class from glazing_reference.csv
        # instead of carrying a bare number. Naming the class keeps the
        # U- and g-values consistent with each other -- better-insulating
        # glazing also admits less solar gain, and setting only the U
        # would silently keep an unrelated g.
        glazing = resolve_glazing(u_value_overrides["U_Window"])
        if glazing is not None:
            window_U = glazing.u_value
            window_g_gl = glazing.g_value
        else:
            window_U = float(u_value_overrides["U_Window"])

    adjusted = apply_refurbishment_measures(
        tabula_row,
        {"Wall": wall_U, "Roof": roof_U, "Floor": floor_U,
         "Window": window_U, "Door": door_U},
        measure_overrides=measure_overrides,
    )

    a_wall_1 = safe_series_float(tabula_row, "A_Wall_1", 0.0)
    door_ratio = (
        safe_series_float(tabula_row, "A_Door_1", 0.0) / a_wall_1
        if a_wall_1 > 0 else 0.0
    )

    return ArchetypeSpec(
        wall_U=adjusted["Wall"], wall_b=wall_b,
        roof_U=adjusted["Roof"], roof_b=roof_b,
        floor_U=adjusted["Floor"], floor_b=floor_b,
        window_U=adjusted["Window"],
        window_g_gl=window_g_gl,
        door_U=adjusted["Door"],
        door_ratio=door_ratio,
        horizontal_window_area=safe_series_float(tabula_row, "A_Window_Horizontal", 0.0),
        n_air_infiltration=safe_series_float(tabula_row, "n_air_infiltration", 0.5),
        n_air_use=safe_series_float(tabula_row, "n_air_use", 0.5),
        c_m=safe_series_float(tabula_row, "c_m", 165.0, warn=False),
        h_room=safe_series_float(tabula_row, "h_room", 2.5, warn=False),
        F_sh_hor=safe_series_float(tabula_row, "F_sh_hor", 0.8, warn=False),
        F_sh_vert=safe_series_float(tabula_row, "F_sh_vert", 0.75, warn=False),
        F_f=safe_series_float(tabula_row, "F_f", 0.2, warn=False),
        F_w=safe_series_float(tabula_row, "F_w", 1.0, warn=False),
        phi_int=safe_series_float(tabula_row, "phi_int", None),
        q_w_nd=safe_series_float(tabula_row, "q_w_nd", None),
        design_T_min=safe_series_float(tabula_row, "Theta_e", -12.0),
        F_red_htr=safe_series_float(tabula_row, "F_red_htr1", 1.0),
        building_type=str(tabula_row.get("Code_BuildingSizeClass", "SFH")),
        construction_period=str(tabula_row.get("Code_ConstructionYearClass", "")),
        neighbour_status=str(tabula_row.get("Code_AttachedNeighbours", "B_Alone")),
    )


def spec_from_service_reference(reference_row: pd.Series) -> ArchetypeSpec:
    """Build a spec from a row of the non-residential reference table.

    See ``src/buem/data/buildings/netherlands/Loenen/service_building_reference.csv``
    for the table's own provenance notes: the envelope U-values follow the
    same Bouwbesluit construction-year series already cross-checked for
    the residential path (its thermal requirements are not
    residential-specific), while the use parameters -- room height,
    ventilation rate, heating-reduction factor -- differ by building
    category.

    Service buildings carry no party-wall typology, so
    ``neighbour_status`` is always ``B_Alone``: a warehouse or supermarket
    is modelled with its full envelope exposed.
    """
    def _f(column: str, default: float, *, warn: bool = True) -> float:
        """Read a float from the reference row, naming any substitution.

        Same visibility rule as
        :func:`buem.buildings.mapping.tabula_helpers.safe_series_float`: a
        missing column is not an error, but the operator should see in the
        log that a number was assumed rather than read.
        """
        value = reference_row.get(column)
        if value is not None and not pd.isna(value):
            return float(value)
        if warn:
            key = (column, repr(default))
            if key not in _WARNED_SERVICE_DEFAULTS:
                _WARNED_SERVICE_DEFAULTS.add(key)
                logger.warning(
                    "Service-building reference column %r is missing or NaN -- "
                    "substituting the default %r. Further occurrences are not "
                    "repeated.", column, default,
                )
        return default

    return ArchetypeSpec(
        wall_U=_f("U_Wall", 1.0), wall_b=1.0,
        roof_U=_f("U_Roof", 1.0), roof_b=1.0,
        floor_U=_f("U_Floor", 1.0), floor_b=0.5,
        window_U=_f("U_Window", 2.9),
        window_g_gl=_f("g_gl_Window", 0.6),
        door_U=_f("U_Door", 3.0),
        door_ratio=_f("door_to_wall_ratio", 0.02, warn=False),
        horizontal_window_area=0.0,
        n_air_infiltration=_f("n_air_infiltration", 0.3),
        n_air_use=_f("n_air_use", 0.7),
        c_m=_f("c_m", 165.0, warn=False),
        h_room=_f("h_room", 3.0, warn=False),
        # Shading/frame factors are geometry conventions rather than
        # typology data, so they keep the same ISO 13790 defaults the
        # residential path falls back to.
        F_sh_hor=0.8, F_sh_vert=0.75, F_f=0.2, F_w=1.0,
        # Internal gains come from occupancy's ServiceBuildingProfile
        # (per-m² by building category), not from a static archetype
        # figure, so phi_int is deliberately absent here.
        phi_int=None,
        q_w_nd=None,
        design_T_min=_f("design_T_min", -12.0),
        F_red_htr=_f("F_red_htr", 1.0),
        building_type=str(reference_row.get("service_building_type", "")),
        construction_period=str(reference_row.get("construction_year_class", "")),
        neighbour_status="B_Alone",
    )


__all__ = ["ArchetypeSpec", "spec_from_service_reference", "spec_from_tabula"]
