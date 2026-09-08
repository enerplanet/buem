"""
Pure, side-effect-free building-type/archetype/equipment/location constants
shared across buem's config, integration, and request-validation layers.

Deliberately split out of ``cfg_attribute.py``: that module performs a
real, eager ``weather.get_point_weather()`` fetch at import time (its
module-level ``df_weather = get_or_fetch_weather(...)`` backing the demo/
example-house default), so importing anything from it -- even a pure
constant -- pulls in that fetch as a side effect. This module holds
nothing that touches weather or occupancy, so ``geojson_validator.py``
-- which validates request *structure* and must not require weather
archives to be configured just to check a ``building_type`` value --
imports from here instead.

``cfg_attribute.py`` re-exports these same names unchanged (see its own
import block), so existing importers are unaffected by the split.
"""
from __future__ import annotations

# Defaults for the built-in household electricity/internal-gains profile
# (cfg_attribute.py's module-level demo) and the "num_persons"/"year"
# AttributeSpec defaults.
DEFAULT_NUM_PERSONS = 4
DEFAULT_YEAR = 2018

# No DEFAULT_SEED constant here by design. occupancy's `derive_default_seed()`
# (occupancy.core.seed) makes `seed=None` itself deterministic -- the same
# building inputs (num_persons/capacity, year, archetype, region) always
# resolve to the same seed -- so buem does not need to manufacture or pass
# its own seed value. `ATTRIBUTE_SPECS["seed"]`'s default is `None`; a
# caller can still pass an explicit int via `merged_attrs["seed"]` for a
# specific, literal value (e.g. in tests), but nothing in buem does so by
# default.

# Default indoor comfort dead-band [degC]. Single source of truth for
# ThermalProperties' own dataclass defaults, the matching AttributeSpec
# defaults, and LOD2Mapper's mapped buildings.
#
# These represent *observed occupant behavior*, not a standardized
# calculation setpoint. TABULA's own `theta_i` (a constant 20 degC across
# every Dutch archetype) is the setpoint its reference calculation
# assumes, but real households heat to a lower average indoor temperature
# and do not hold it around the clock -- a well-documented divergence
# between calculated and metered consumption. Simulating the standardized
# setpoint therefore systematically overestimates real demand, so buem's
# default is the more realistic band. Pass explicit comfortT_lb/comfortT_ub
# (scalars or per-timestep Series) to model a specific building's own
# setpoints or a real setback schedule.
DEFAULT_COMFORT_T_LB = 18.0
DEFAULT_COMFORT_T_UB = 21.0

# Default window-to-wall area ratio, applied to each exposed wall when a
# request supplies no explicit window geometry. Each synthesized window
# inherits its host wall's own azimuth and tilt.
#
# Deliberately independent of TABULA's A_Window_<Direction> columns.
# TABULA expresses reference glazing per compass direction, but for the
# Dutch typology every archetype places its entire window area on
# East/West with North and South at exactly zero -- an abstract
# front/back-facade convention rather than a claim about orientation.
# Applying those figures by real compass direction gave a genuinely
# south-facing wall no glazing at all and collapsed whole-building
# window-to-wall ratio to a few percent. Sizing windows directly from
# each wall's own area avoids that mismatch entirely and needs no
# assumption about how a reference building happened to be oriented.
#
# Raising this ratio increases both solar gain and transmission loss.
# In a heating-dominated maritime climate the loss term dominates over a
# full year (window U-values are several times wall U-values, and winter
# solar is weak), so a higher ratio raises annual heating demand even
# though it also raises solar gain.
DEFAULT_WINDOW_TO_WALL_RATIO = 0.5

# Default location/provider for cfg_attribute.py's module-level weather
# default and the "latitude"/"longitude"/"weather_provider" AttributeSpec
# defaults -- a single source of truth so the two can't drift apart.
DEFAULT_LATITUDE = 52.0
DEFAULT_LONGITUDE = 5.0
# merra-2 rather than era5-land: era5-land's archive fails at this default
# cell/year (an unrepaired accumulation-boundary artifact in its source
# data), while merra-2 is complete and physically sane there.
DEFAULT_WEATHER_PROVIDER = "merra-2"

# TABULA residential building-size classes (see BuildingIdentity.building_type,
# src/buem/buildings/building.py). Anything outside this set is routed to
# occupancy's ServiceBuildingProfile instead of HouseholdProfile -- see
# AttributeBuilder.generate_electricity_profile(). occupancy's own service
# types are deliberately not hand-copied here at runtime;
# occupancy.SERVICE_BUILDING_TYPES (a stable top-level export) is the
# single source of truth for that side, maintained in the occupancy repo.
# tests/test_building_types.py::test_v4_building_type_enum_matches_occupancy
# is a drift guard: it fails CI if versions/v4/'s static schema enum (which,
# unlike this runtime set, genuinely is a hand-copied snapshot) falls out of
# sync with occupancy's actual registry.
RESIDENTIAL_BUILDING_TYPES = frozenset({"SFH", "MFH", "TH", "AB"})
DEFAULT_BUILDING_TYPE = "MFH"

# building_type -> occupancy household archetype, used by
# AttributeBuilder.generate_electricity_profile() as a fallback only when
# the caller doesn't supply an explicit "archetype" attribute. This is a
# first-pass heuristic, not a derivation: TABULA's SFH/MFH/TH/AB describe
# building *form* (attachment/size class), while occupancy's archetypes
# (occupancy.HOUSEHOLD_ARCHETYPES) describe household *composition* --
# there is no reliable 1:1 mapping between the two. The caller-supplied
# num_persons remains the dominant, more reliable signal; this table only
# picks a plausible default occupancy *schedule shape* when nothing more
# specific is known. Revisit with real occupancy-survey data if available.
DEFAULT_ARCHETYPE_BY_BUILDING_TYPE: dict[str, str] = {
    "SFH": "family_with_children",  # detached houses skew toward families in TABULA's own survey basis
    "TH": "working_couple",  # terraced houses skew toward smaller working households
    "MFH": "generic",  # multi-family buildings house a wide mix of composition -- no single default fits
    "AB": "generic",  # apartment blocks: same reasoning as MFH
}

# Floor area per occupant [m2/person] by occupancy service-building type,
# used to size a service building's `capacity` from its real floor area
# (see derive_service_capacity).
#
# occupancy's ServiceBuildingProfile takes an occupant capacity, not a
# floor area, and falls back to its type's `capacity_default` when none
# is given. That default describes a *typical full-size* building of the
# type, so applying it unscaled gives every building of a type the same
# occupancy regardless of size -- a 50 m2 holiday cabin and a 3,000 m2
# hotel both modelled as 120 guests. The resulting internal gains are
# large enough to invert the thermal result entirely (heating demand
# collapses to zero, cooling demand explodes), so capacity has to track
# floor area.
#
# Densities follow EN 16798-1 / ASHRAE 62.1 default occupant densities
# for the equivalent use categories, taken at whole-building scale
# (including circulation, storage and back-of-house) rather than at the
# occupied-room scale those tables also list. Cross-check: multiplying
# each density by occupancy's own capacity_default recovers a plausible
# reference building size for every type (office 600 m2, school 1,500 m2,
# restaurant 180 m2, supermarket 800 m2, hotel 2,400 m2, warehouse
# 1,500 m2, clinic 750 m2, bakery 250 m2) -- so a building at its type's
# reference size reproduces occupancy's default capacity, and only
# buildings away from that size are rescaled.
SERVICE_FLOOR_AREA_PER_OCCUPANT_M2: dict[str, float] = {
    "office": 15.0,
    "school": 5.0,
    "restaurant": 3.0,
    "supermarket": 10.0,
    "hotel": 20.0,
    "warehouse": 100.0,
    "clinic": 15.0,
    "bakery": 10.0,
}


def derive_service_capacity(building_type: str, floor_area_m2: float | None) -> int | None:
    """Occupant capacity for a service building of ``floor_area_m2``.

    Returns ``None`` when the type has no published density or the floor
    area is unusable, letting occupancy apply its own
    ``capacity_default`` rather than inventing a number.

    The result is floored at 1: a building small enough to round to zero
    occupants is still a building someone enters, and a zero capacity is
    rejected by ``ServiceBuildingProfile`` itself.
    """
    density = SERVICE_FLOOR_AREA_PER_OCCUPANT_M2.get(building_type)
    if density is None or not floor_area_m2 or floor_area_m2 <= 0:
        return None
    return max(1, round(float(floor_area_m2) / density))


# Household equipment ids, hand-copied from occupancy's
# households/data/equipment.json (29 items). Unlike
# RESIDENTIAL_BUILDING_TYPES's service-type counterpart
# (occupancy.SERVICE_BUILDING_TYPES), occupancy exposes no stable
# top-level export for its equipment registry, so this set can drift out
# of sync if an item is added/renamed/removed there. Used only to
# validate the optional "equipment" attribute's include/exclude item ids
# before they reach occupancy.ElectricityConsumptionProfile.
HOUSEHOLD_EQUIPMENT_TYPES = frozenset({
    "chest_freezer", "fridge_freezer", "refrigerator", "upright_freezer",
    "answer_machine", "cassette_cd_player", "clock", "cordless_telephone",
    "hi_fi", "iron", "vacuum", "fax", "personal_computer", "printer",
    "tv_1", "tv_2", "tv_3", "vcr_dvd", "tv_receiver_box", "hob", "oven",
    "microwave", "kettle", "small_cooking_group", "dish_washer",
    "tumble_dryer", "washing_machine", "washer_dryer", "lighting",
})
