"""
Live-path LOD2 → LOD3 envelope synthesis.

Bridges the documented, TABULA-ratio-based window/door/ventilation sizing
logic in :mod:`element_factory`/:mod:`tabula_helpers` (originally written
for :class:`~buem.buildings.mapping.lod2_mapper.LOD2Mapper`'s offline
Excel/PostgreSQL batch pipeline) to the live request-handling path
(:class:`buem.config.cfg_building.CfgBuilding`), which receives wall
geometry from an EnerPlanET API request, buem's own module-level config
default, or a caller-supplied ``components`` dict directly -- with no
attached LOD2 surface table to detect party walls or a numeric FK to
resolve a TABULA row.

EnerPlanET's own UI deliberately doesn't ask general users to fill in
window/door/ventilation detail (see ``CLAUDE.md``): a request may supply
these explicitly, but when it doesn't, buem must compute them internally
from whatever wall geometry it does have, using the same rules documented
in ``docs/source/modules/buildings.rst`` -- not silently leave them empty.
This module is that internal computation for the live path:

- "Shared wall" detection falls back to each wall's own ``b_transmission``
  (``0`` → party wall) instead of cross-building ``surface_feature_id``
  matching, which needs a full LOD2 surface table this path doesn't have
  (see buildings.rst "Party (Shared) Walls").
- The TABULA archetype is resolved from ``building_type`` +
  ``construction_period`` + ``country`` (already forwarded end-to-end from
  a real v3 API request, see ``CLAUDE.md`` "v2 vs v3/v4 request formats")
  or an explicit ``bldg_tabula_id``, via
  :func:`tabula_helpers.lookup_tabula_archetype`, falling back to
  documented safe-default ratios when no archetype matches -- see
  buildings.rst "Missing TABULA values use safe defaults".
- Never overrides an explicitly-supplied, non-empty ``Windows``/``Doors``/
  ``Ventilation`` component -- EnerPlanET "can provide [it]... but does not
  have to".
"""

from __future__ import annotations

import logging
from typing import Any

from buem.buildings.mapping.element_factory import (
    WallInfo,
    identify_front_back,
    synthesize_openings,
    uniform_window_ratios,
)
from buem.buildings.mapping.tabula_helpers import (
    lookup_tabula_archetype,
    safe_series_float,
)
from buem.config.reference_values import glazing_by_nearest_u

logger = logging.getLogger(__name__)

# ── documented safe-default fallback ratios ──────────────────────────────────
#
# Used only when no TABULA archetype can be resolved for the caller's
# building_type/construction_period/country (e.g. a country the bundled
# reference sheet doesn't cover, or a construction_period given as a literal
# year-range like "1965-1974" instead of TABULA's class-code format). NOT
# derived from TABULA data -- a first-pass heuristic, not a substitute for a
# real archetype match, same treatment as buildings.rst's existing "Missing
# TABULA values use safe defaults" table (n_air_infiltration/c_m/etc.), which
# this extends. Windows measurably affect heat loss/gain even at a modest
# share of envelope area, so the fallback always synthesizes something
# physically plausible rather than leaving these components empty.
#
# Window sizing no longer differs between the archetype-matched and
# fallback paths: both use building_registry.DEFAULT_WINDOW_TO_WALL_RATIO
# via element_factory.uniform_window_ratios(). Only the door ratio still
# needs a fallback, giving a ~2 m2 door on a typical ~40 m2 front wall.
# How far a window U must move from the archetype's own before the glazing
# counts as replaced. Deliberately far above float noise: a U-value that has
# travelled through JSON can return as 2.7999999523162842 against a stored
# 2.8, and treating that as a replacement would substitute a class-derived
# transmittance for the archetype's correct one on every as-built building.
# Far below any real measure, the smallest of which moves the U by 0.5.
_U_UNCHANGED_TOLERANCE = 0.01

FALLBACK_DOOR_RATIO = 0.05
FALLBACK_WINDOW_U = 2.8
FALLBACK_WINDOW_G_GL = 0.5
FALLBACK_DOOR_U = 3.0
FALLBACK_N_AIR_USE = 0.5

_OPENING_TYPES = ("window", "door", "ventilation")
_COMPONENT_KEY_BY_TYPE = {"window": "Windows", "door": "Doors", "ventilation": "Ventilation"}


def _is_empty_component(components: dict[str, Any], key: str) -> bool:
    comp = components.get(key)
    if not isinstance(comp, dict):
        return True
    return not comp.get("elements")


def _flatten_element(el) -> dict[str, Any]:
    """Convert a synthesized ``EnvelopeElement`` to the flat dict shape used
    by ``components.<Group>.elements[]`` -- the same shape
    ``geojson_validator.py::_convert_v3_to_v2`` and ``cfg_attribute.py``'s
    demo default already use (plain floats, not v3's ``{value, unit}``
    wrapping -- ``EnvelopeElement.to_element_dict()`` is not reused here
    because it targets that different, v3-wrapped shape).
    """
    if el.element_type == "ventilation":
        d: dict[str, Any] = {"id": el.id}
        if el.air_changes is not None:
            d["air_changes"] = round(el.air_changes, 4)
        return d
    d = {
        "id": el.id,
        "area": round(el.area, 4),
        "azimuth": round(el.azimuth, 2),
        "tilt": round(el.tilt, 2),
    }
    if el.surface is not None:
        d["surface"] = el.surface
    return d


def synthesize_missing_openings(
    components: dict[str, Any],
    *,
    building_type: str | None,
    construction_period: str | None,
    country: str | None,
    bldg_tabula_id: str | None = None,
    window_to_wall_ratio: float | None = None,
    window_U: float | None = None,
    window_g_gl: float | None = None,
    door_U: float | None = None,
) -> dict[str, Any]:
    """Fill in missing Windows/Doors/Ventilation from Walls geometry.

    Only synthesizes component groups the caller left empty/absent; any
    explicitly-supplied non-empty group is returned unchanged. Returns a
    new dict -- does not mutate ``components`` in place.

    ``window_U``/``window_g_gl``/``door_U`` override the resolved archetype
    row and the module fallbacks for the synthesized openings. A caller that
    knows the real construction should not be given a reference value in its
    place; window U-value in particular moves annual demand substantially.
    ``None`` leaves each to the archetype or fallback as before.

    When *all three* of Windows/Doors/Ventilation are missing (the expected
    case: a caller that supplied wall/roof/floor geometry only), the
    synthesized openings are also subtracted from each wall's own area, so
    the envelope doesn't double-count opaque wall area and window/door area
    over the same physical surface (matching
    ``LOD2Mapper``'s own wall-building step, which always uses each wall's
    net opaque area). When only *some* of the three are missing, ``Walls``
    is left untouched -- there is no reliable way to tell how much wall
    area a partially-supplied request already accounted for.
    """
    walls_comp = components.get("Walls")
    if not isinstance(walls_comp, dict):
        # Nothing to synthesize from -- leave components untouched.
        return components
    wall_elements = walls_comp.get("elements")
    if not wall_elements:
        return components

    missing = [
        _COMPONENT_KEY_BY_TYPE[t] for t in _OPENING_TYPES
        if _is_empty_component(components, _COMPONENT_KEY_BY_TYPE[t])
    ]
    if not missing:
        return components

    wall_default_b = float(walls_comp.get("b_transmission", 1.0))
    walls: list[WallInfo] = []
    for idx, elem in enumerate(wall_elements, start=1):
        b_transmission = float(elem.get("b_transmission", wall_default_b))
        walls.append(WallInfo(
            wall_id=str(elem.get("id", f"wall_{idx}")),
            surface_feature_id=-1,  # not applicable outside the LOD2 batch pipeline
            area=float(elem.get("area", 0.0)),
            azimuth=float(elem.get("azimuth", 0.0)) % 360.0,
            is_shared=(b_transmission == 0.0),
        ))

    exposed = [w for w in walls if not w.is_shared]
    front_wall, back_wall = identify_front_back(exposed)

    # Captured before the branch below reassigns these names from the
    # resolved archetype row.
    caller_window_U, caller_window_g_gl, caller_door_U = window_U, window_g_gl, door_U

    tabula_row = None
    if building_type:
        tabula_row = lookup_tabula_archetype(
            building_type, construction_period, country, bldg_tabula_id=bldg_tabula_id,
        )

    if tabula_row is not None:
        a_wall_1 = safe_series_float(tabula_row, "A_Wall_1", 0.0)
        # Windows are sized from each wall's own area rather than from
        # TABULA's per-direction window columns, so both this path and
        # LOD2Mapper's use one orientation-independent rule -- see
        # element_factory.uniform_window_ratios().
        window_ratios = uniform_window_ratios(window_to_wall_ratio)
        door_ratio = (
            safe_series_float(tabula_row, "A_Door_1", 0.0) / a_wall_1 if a_wall_1 > 0 else 0.0
        )
        window_U = safe_series_float(tabula_row, "U_Window_1", FALLBACK_WINDOW_U)
        window_g_gl = safe_series_float(tabula_row, "g_gl_n_Window_1", FALLBACK_WINDOW_G_GL)
        door_U = safe_series_float(tabula_row, "U_Door_1", FALLBACK_DOOR_U)
        n_air_use = safe_series_float(tabula_row, "n_air_use", FALLBACK_N_AIR_USE)
        horizontal = safe_series_float(tabula_row, "A_Window_Horizontal", 0.0)
        logger.info(
            "Synthesizing missing envelope component(s) %s from TABULA archetype %s "
            "(building_type=%r construction_period=%r country=%r)",
            missing, tabula_row.get("Code_BuildingVariant"),
            building_type, construction_period, country,
        )
    else:
        window_ratios = uniform_window_ratios(window_to_wall_ratio)
        door_ratio = FALLBACK_DOOR_RATIO
        window_U, window_g_gl = FALLBACK_WINDOW_U, FALLBACK_WINDOW_G_GL
        door_U, n_air_use, horizontal = FALLBACK_DOOR_U, FALLBACK_N_AIR_USE, 0.0
        logger.warning(
            "No TABULA archetype resolved for building_type=%r construction_period=%r "
            "country=%r -- synthesizing missing envelope component(s) %s from buem's "
            "documented safe-default ratios, not real TABULA data (see buildings.rst "
            "'Missing TABULA values use safe defaults').",
            building_type, construction_period, country, missing,
        )

    # Applied to both branches at once: a caller-supplied value wins over
    # the archetype row and over the fallbacks alike.
    if caller_window_U is not None:
        window_U = float(caller_window_U)
    if caller_window_g_gl is not None:
        window_g_gl = float(caller_window_g_gl)
    if caller_door_U is not None:
        door_U = float(caller_door_U)

    # Transmittance follows the U-value: a U and a g that do not belong to
    # the same glazing describe a window that does not exist. TABULA records
    # the as-built glazing's own transmittance and keeps it when a measure
    # replaces the glazing, so a refurbished window U arrives paired with a
    # transmittance no glazing achieves at that U. Re-derive it from the
    # glazing class nearest the U actually in force (enerplanet/buem#26).
    #
    # Only when the U has moved away from the archetype's own, which is what
    # identifies replaced glazing, and never over a caller's explicit value.
    # An unchanged U keeps the archetype's transmittance, which is the
    # correct one at the as-built state and is paired with that U by
    # construction.
    if tabula_row is not None and caller_window_g_gl is None:
        archetype_window_U = safe_series_float(tabula_row, "U_Window_1", None)
        if archetype_window_U and abs(window_U - archetype_window_U) > _U_UNCHANGED_TOLERANCE:
            spec = glazing_by_nearest_u(window_U)
            logger.info(
                "Window U %.2f differs from archetype's %.2f, so the glazing was "
                "replaced: taking transmittance %.2f from class %s rather than the "
                "archetype's %.2f",
                window_U, archetype_window_U, spec.g_value, spec.glazing_type, window_g_gl,
            )
            window_g_gl = spec.g_value

    opening_elements = synthesize_openings(
        exposed, front_wall, back_wall,
        window_ratios=window_ratios,
        door_ratio=door_ratio,
        window_U=window_U,
        window_g_gl=window_g_gl,
        door_U=door_U,
        n_air_use=n_air_use,
        horizontal_window_area=horizontal,
    )

    result = dict(components)

    grouped: dict[str, list[dict[str, Any]]] = {"Windows": [], "Doors": [], "Ventilation": []}
    for el in opening_elements:
        grouped[_COMPONENT_KEY_BY_TYPE[el.element_type]].append(_flatten_element(el))

    for comp_key, comp_U, comp_g_gl in (
        ("Windows", window_U, window_g_gl),
        ("Doors", door_U, None),
        ("Ventilation", None, None),
    ):
        if comp_key not in missing:
            continue
        comp: dict[str, Any] = {"elements": grouped[comp_key]}
        if comp_U is not None:
            comp["U"] = comp_U
        if comp_g_gl is not None:
            comp["g_gl"] = comp_g_gl
        result[comp_key] = comp

    # Shrink wall areas to opaque net area -- only when the full opening set
    # was synthesized (see docstring: partial overrides leave Walls alone).
    # ``walls`` was built from ``wall_elements`` via enumerate() above, so
    # the two lists share order/length -- zip rather than re-match by id,
    # which would break for walls without an explicit "id".
    if set(missing) == {"Windows", "Doors", "Ventilation"}:
        new_wall_elements = [
            {**elem, "area": round(w.net_area, 4)}
            for elem, w in zip(wall_elements, walls)
        ]
        result["Walls"] = {**walls_comp, "elements": new_wall_elements}

    return result


def _parent_lookup(components: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map element id -> element dict across Walls and Roof components --
    the two component types a Window/Door's parent surface can reference
    (v3's ``envelope_element.parent_id`` description: "A window is
    embedded in a wall (or roof for a skylight); a door is embedded in a
    wall")."""
    lookup: dict[str, dict[str, Any]] = {}
    for comp_key in ("Walls", "Roof"):
        comp = components.get(comp_key)
        if not isinstance(comp, dict):
            continue
        for elem in comp.get("elements") or []:
            elem_id = elem.get("id")
            if elem_id is not None:
                lookup[str(elem_id)] = elem
    return lookup


def normalize_opening_azimuths(components: dict[str, Any]) -> dict[str, Any]:
    """Force Windows/Doors to inherit their parent surface's azimuth and
    tilt whenever they reference one via ``surface`` (buem's internal name
    for v3's ``parent_id`` -- see ``geojson_validator.py::
    _convert_v3_to_v2``).

    A window or door is physically embedded in its host wall (or roof, for
    a skylight) and cannot face a different direction or slope than that
    surface -- azimuth/tilt supplied independently on the opening are not
    physically meaningful once a parent is known. A caller-supplied
    mismatch is silently *corrected* here (logged, not rejected), since
    the parent surface's own azimuth/tilt is the physically authoritative
    value, and rejecting an otherwise-valid request over a redundant,
    derivable field would be needlessly strict.

    Internally-synthesized openings (:mod:`element_factory`) already
    inherit their parent wall's azimuth/tilt by construction, so this is a
    no-op for them -- it only has an effect on explicitly caller-supplied
    Windows/Doors that carry a ``surface`` reference.

    Ventilation is intentionally excluded: buildings.rst notes the ISO
    13790 model uses only air change rates for ventilation, not physical
    opening azimuth/tilt, and internally-synthesized ventilation elements
    do not carry those fields at all (see ``_flatten_element``).

    Elements with no ``surface`` reference, or whose reference doesn't
    resolve to a known Wall/Roof element id, are left untouched -- nothing
    to normalize against (e.g. a standalone opening with no declared
    parent).
    """
    parents = _parent_lookup(components)
    if not parents:
        return components

    result = dict(components)
    for comp_key in ("Windows", "Doors"):
        comp = result.get(comp_key)
        if not isinstance(comp, dict) or not comp.get("elements"):
            continue
        new_elements: list[dict[str, Any]] = []
        changed = False
        for elem in comp["elements"]:
            parent = parents.get(str(elem.get("surface", "")))
            if parent is None:
                new_elements.append(elem)
                continue
            parent_azimuth = float(parent.get("azimuth", 0.0)) % 360.0
            parent_tilt = float(parent.get("tilt", 90.0))
            new_elem = dict(elem)
            if float(new_elem.get("azimuth", parent_azimuth)) % 360.0 != parent_azimuth:
                logger.warning(
                    "%s element %r azimuth %s does not match its parent "
                    "surface %r azimuth %s -- correcting to the parent's "
                    "value (a window/door cannot face a different "
                    "direction than the surface it is embedded in).",
                    comp_key, elem.get("id"), elem.get("azimuth"),
                    elem.get("surface"), parent_azimuth,
                )
                new_elem["azimuth"] = parent_azimuth
                changed = True
            if float(new_elem.get("tilt", parent_tilt)) != parent_tilt:
                new_elem["tilt"] = parent_tilt
                changed = True
            new_elements.append(new_elem)
        if changed:
            result[comp_key] = {**comp, "elements": new_elements}
    return result
