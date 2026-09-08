import logging
import os
from typing import Any

import pandas as pd

from buem.buildings.mapping.live_synthesis import synthesize_missing_openings

# Pure constants, side-effect-free -- re-exported here unchanged so existing
# importers of cfg_attribute.py see no change. Split out specifically so
# geojson_validator.py can import RESIDENTIAL_BUILDING_TYPES etc. for
# request-structure validation without pulling in this module's own eager
# weather fetch below as a side effect -- see building_registry.py's own
# docstring for the full rationale. Guarded by __all__ below: ruff's F401
# "unused import" check is per-file and cannot see other modules importing
# these names *from here*, so without __all__ an automated lint fix would
# delete the re-exports this file does not also use internally.
from buem.config.building_registry import (
    DEFAULT_ARCHETYPE_BY_BUILDING_TYPE,
    DEFAULT_BUILDING_TYPE,
    DEFAULT_COMFORT_T_LB,
    DEFAULT_COMFORT_T_UB,
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_NUM_PERSONS,
    DEFAULT_WEATHER_PROVIDER,
    DEFAULT_YEAR,
    HOUSEHOLD_EQUIPMENT_TYPES,
    RESIDENTIAL_BUILDING_TYPES,
    SERVICE_FLOOR_AREA_PER_OCCUPANT_M2,
    derive_service_capacity,
)
from buem.config.weather_cache import get_or_fetch_weather
from buem.env import load_env

from .attribute_types import AttributeCategory, AttributeSpec, AttrType

# Explicit public surface -- includes the building_registry.py re-exports
# above (see that import block's own comment for why this exists: without
# it, a per-file "unused import" lint check can't see that other modules
# import a name *from here*, and can silently delete it).
__all__ = [
    "ATTRIBUTE_SPECS",
    "DEFAULT_ARCHETYPE_BY_BUILDING_TYPE",
    "DEFAULT_BUILDING_TYPE",
    "DEFAULT_COMFORT_T_LB",
    "DEFAULT_COMFORT_T_UB",
    "DEFAULT_LATITUDE",
    "DEFAULT_LONGITUDE",
    "DEFAULT_NUM_PERSONS",
    "DEFAULT_WEATHER_PROVIDER",
    "DEFAULT_YEAR",
    "HOUSEHOLD_EQUIPMENT_TYPES",
    "RESIDENTIAL_BUILDING_TYPES",
    "SERVICE_FLOOR_AREA_PER_OCCUPANT_M2",
    "cfg",
    "derive_service_capacity",
]

logger = logging.getLogger(__name__)

load_env()  # ensure BUEM_WEATHER_DATA_DIR/WEATHER_DATA_DIR are set before the fetch below

# occupancy (https://github.com/UU-BUEM/occupancy) is a compulsory
# dependency, same treatment as weather (https://github.com/UU-BUEM/weather)
# -- every Q_ig/elecLoad/occ_nothome/occ_sleeping value comes from its real
# HouseholdProfile/ServiceBuildingProfile generation, so it's imported
# unconditionally like pandas/pvlib.
from occupancy import ElectricityConsumptionProfile, HouseholdProfile, to_buem_profiles  # type: ignore[import]

# Module-level default weather: read by ATTRIBUTE_SPECS["weather"].default and
# by importers that bypass AttributeBuilder's per-building fetch (`buem run`,
# cfg_building.WeatherConfig(None)).
#
# BUEM_WEATHER_FALLBACK=false means a caller always supplies weather per
# request and buem never resolves its own (attribute_builder.
# generate_weather_profile, enerplanet/buem#10). That default is then never
# read, so skip the fetch: only the occupancy profiles below need an index,
# and a plain hourly range serves. With fallback on (standalone installs),
# fetch as before, via WEATHER_API_URL or a local archive
# (BUEM_WEATHER_DATA_DIR / weather's WEATHER_DATA_DIR);
# BUEM_DEFAULT_WEATHER_PROVIDER overrides the provider for this one fetch.
_weather_fallback = os.environ.get(
    "BUEM_WEATHER_FALLBACK", "true"
).strip().lower() not in ("false", "0", "")

if _weather_fallback:
    _default_provider = os.environ.get(
        "BUEM_DEFAULT_WEATHER_PROVIDER", DEFAULT_WEATHER_PROVIDER
    )
    df_weather = get_or_fetch_weather(
        DEFAULT_LATITUDE, DEFAULT_LONGITUDE, DEFAULT_YEAR, _default_provider
    )
    main_index = df_weather.index
    temp_profile = df_weather["T"]
    ghi_profile = df_weather["GHI"]
    dni_profile = df_weather["DNI"]
    dhi_profile = df_weather["DHI"]
else:
    main_index = pd.date_range(f"{DEFAULT_YEAR}-01-01", periods=8760, freq="h")
    temp_profile = ghi_profile = dni_profile = dhi_profile = None

n_hours = len(main_index)

# Generate Q_ig/elecLoad/occ_nothome/occ_sleeping via occupancy's real
# HouseholdProfile + ElectricityConsumptionProfile + to_buem_profiles()
# pipeline -- no fallback (occupancy is compulsory, see module-top import).
# seed left at its dataclass default (None), not a buem-owned constant --
# occupancy v5.0.0's derive_default_seed() makes None itself deterministic
# (same num_persons/year/archetype/region -> same seed every time), so buem
# no longer needs to manufacture and pass its own seed value at all (see
# building_registry.py's comment for the full reasoning).
_household = HouseholdProfile(num_persons=DEFAULT_NUM_PERSONS, year=DEFAULT_YEAR)
_elec = ElectricityConsumptionProfile(occupancy_profile=_household)
_buem_inputs = to_buem_profiles(_elec.to_result())
realistic_elec_load = _buem_inputs["elecLoad"].reindex(main_index, method="nearest", fill_value=0.0)
realistic_elec_load.name = "elecLoad"
q_ig_profile = _buem_inputs["Q_ig"].reindex(main_index, method="nearest", fill_value=0.0)
occ_nothome_profile = _buem_inputs["occ_nothome"].reindex(main_index, method="nearest", fill_value=0.0)
occ_sleeping_profile = _buem_inputs["occ_sleeping"].reindex(main_index, method="nearest", fill_value=0.0)

# Build attribute specs using realistic electricity load
ATTRIBUTE_SPECS: dict[str, AttributeSpec] = {
    "weather": AttributeSpec(
        name="weather",
        category=AttributeCategory.WEATHER,
        type=AttrType.DATAFRAME,
        default=None if temp_profile is None else pd.DataFrame({
            "T": temp_profile,
            "GHI": ghi_profile,
            "DNI": dni_profile,
            "DHI": dhi_profile,
        }, index=main_index),
        doc="Weather DataFrame with columns T, GHI, DNI, DHI indexed by "
            "datetimes. None when BUEM_WEATHER_FALLBACK=false (the caller "
            "always supplies weather per request)."
    ),
    "bldg_tabula_id": AttributeSpec(
        "bldg_tabula_id",
        AttributeCategory.FIXED,
        AttrType.STR,
        "NL.N.MFH.01.Gen",
        doc=(
            "TABULA archetype code (Code_BuildingVariant, e.g. "
            "'DE.N.MFH.03.Gen.ReEx.001.001'). Explicit override for the live "
            "LOD2->LOD3 envelope synthesis (buem.buildings.mapping."
            "live_synthesis) -- tried before the building_type/"
            "construction_period/country match. This module's own default "
            "('NL.N.MFH.01.Gen') does not match any row in the bundled "
            "(Germany-only) reference sheet, so it exercises the "
            "building_type/construction_period/country fallback instead --"
            " that's expected, not a bug."
        ),
    ),
    "costdatapath": AttributeSpec(
        "costdatapath",
        AttributeCategory.FIXED,
        AttrType.STR,
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "data", "default_2016.xlsx")
        ),
    ),
    "refurbishment": AttributeSpec("refurbishment", AttributeCategory.BOOLEAN, AttrType.BOOL, False, doc="Deprecated: refurbishment decisions not used in parameterized model"),
    "force_refurbishment": AttributeSpec("force_refurbishment", AttributeCategory.BOOLEAN, AttrType.BOOL, False, doc="Deprecated"),
    "occControl": AttributeSpec("occControl", AttributeCategory.BOOLEAN, AttrType.BOOL, False, doc="Deprecated"),
    "nightReduction": AttributeSpec("nightReduction", AttributeCategory.BOOLEAN, AttrType.BOOL, False, doc="Deprecated"),
    "capControl": AttributeSpec("capControl", AttributeCategory.BOOLEAN, AttrType.BOOL, False, doc="Deprecated"),
    "elecLoad": AttributeSpec("elecLoad", AttributeCategory.FIXED, AttrType.SERIES,
                              default=realistic_elec_load,  # Use occupancy-based calculation
                              doc="Electric internal load profile from occupancy simulation (pd.Series)"),
    "Q_ig": AttributeSpec(
        "Q_ig",
        AttributeCategory.FIXED,
        AttrType.SERIES,
        default=q_ig_profile,
        doc="Internal gains profile (pd.Series), kW. From occupancy.to_buem_profiles().",
    ),
    "occ_nothome": AttributeSpec("occ_nothome", AttributeCategory.FIXED, AttrType.SERIES,
                                 default=occ_nothome_profile,
                                 doc="Occupancy away profile (fraction not home). From occupancy.to_buem_profiles()."),
    "occ_sleeping": AttributeSpec("occ_sleeping", AttributeCategory.FIXED, AttrType.SERIES,
                                  default=occ_sleeping_profile,
                                  doc="Sleeping occupancy profile (fraction asleep). From occupancy.to_buem_profiles()."),
    "dhw_liters": AttributeSpec(
        "dhw_liters", AttributeCategory.FIXED, AttrType.SERIES,
        default=None,
        doc="Hourly domestic-hot-water draw volume (liters, pd.Series). "
            "From occupancy.generate_dhw_draws()['dhw_liters_total'] -- "
            "residential only (occupancy's DHW model isn't wired to service "
            "buildings yet). Optional: None means ModelBUEM._addDhwCooking() "
            "won't compute dhw_kWh, not an error -- unlike elecLoad/Q_ig/"
            "occ_nothome/occ_sleeping above, this is not a required key. "
            "default=None (not an eagerly-generated series like the four "
            "above) so the module-level demo cfg isn't forced to pay DHW "
            "generation's cost/randomness at import time; a real request "
            "via AttributeBuilder always supplies a real one. See "
            "buem.thermal.dhw_cooking.",
    ),
    "cooking_active": AttributeSpec(
        "cooking_active", AttributeCategory.FIXED, AttrType.SERIES,
        default=None,
        doc="Hourly boolean/0-1 series (pd.Series) flagging when a "
            "household's electric-kitchen equipment was active. From "
            "occupancy.to_buem_profiles()'s optional 5th key. Optional, "
            "same None-means-not-computed convention as dhw_liters above. "
            "Used only as a fallback timing signal now that cooking_kwh "
            "below carries occupancy's own per-appliance energy.",
    ),
    "dhw_kwh": AttributeSpec(
        "dhw_kwh", AttributeCategory.FIXED, AttrType.SERIES,
        default=None,
        doc="Hourly domestic-hot-water energy (kWh, pd.Series), priced "
            "per fixture. occupancy resolves basin, kitchen-sink, shower "
            "and bath draws separately and each is delivered at its own "
            "temperature, so this is more accurate than applying one "
            "blended delta-T to dhw_liters above -- which is what "
            "ModelBUEM falls back to when this is absent. Optional, same "
            "None-means-not-computed convention as dhw_liters.",
    ),
    "cooking_kwh": AttributeSpec(
        "cooking_kwh", AttributeCategory.FIXED, AttrType.SERIES,
        default=None,
        doc="Hourly cooking energy (kWh, pd.Series), generated by "
            "occupancy's own stochastic appliance model restricted to its "
            "'kitchen' equipment category (hob, oven, microwave, kettle and "
            "small cooking appliances). Both when a household cooks and how "
            "much each event draws therefore come from occupancy's "
            "per-appliance draws and that household's own ownership of each "
            "appliance -- not from a fixed annual figure, and not from the "
            "building's heating demand. Optional, same "
            "None-means-not-computed convention as dhw_liters above.",
    ),
    "cooking_carrier": AttributeSpec(
        "cooking_carrier", AttributeCategory.FIXED, AttrType.STR, "electric",
        doc="Energy carrier the household cooks with: 'electric' (default), "
            "'gas' or 'none'. occupancy models cooking appliances "
            "electrically, so their energy already sits inside elecLoad. "
            "'electric' leaves it there and reports no separate cooking "
            "term, which is correct for the induction/electric hobs that "
            "now dominate the Dutch stock and cannot double-count. 'gas' "
            "moves that same energy out of elecLoad and reports it as "
            "cooking_gas_kWh instead, for a household whose hob really is "
            "gas. 'none' drops the cooking term without moving anything. "
            "See AttributeBuilder.generate_electricity_profile() and "
            "ModelBUEM._addDhwCooking().",
    ),
    "setback_profile": AttributeSpec(
        "setback_profile", AttributeCategory.FIXED, AttrType.STR, None,
        doc="Name of a heating-setpoint setback profile from "
            "data/reference/setback_profiles.csv ('night_only', "
            "'night_and_away', 'deep'), turning comfortT_lb into an hourly "
            "schedule driven by occ_nothome/occ_sleeping -- ISO 13790 "
            "section 13 intermittency for the hourly method. None (the "
            "default) and 'none' both mean a constant setpoint. Off by "
            "default on purpose: buem's 18-21 degC band already represents "
            "observed behaviour rather than a standardized calculation "
            "setpoint, so a setback on top double-counts it. Intended for "
            "scenario work, not validation runs. See buem.config.setback.",
    ),
    "include_dhw": AttributeSpec(
        "include_dhw", AttributeCategory.FIXED, AttrType.BOOL, True,
        doc="Whether to report domestic-hot-water energy alongside the "
            "space-heating result. False suppresses dhw_kWh without "
            "affecting the 5R1C solve, which never consumes it. Useful "
            "where DHW is met by a separate system whose demand the caller "
            "accounts for itself.",
    ),
    "latitude": AttributeSpec("latitude", AttributeCategory.FIXED, AttrType.FLOAT, DEFAULT_LATITUDE),
    "longitude": AttributeSpec("longitude", AttributeCategory.FIXED, AttrType.FLOAT, DEFAULT_LONGITUDE),
    # New structured component tree: component-level U (same for all elements) + element list
    "components": AttributeSpec(
        "components",
        AttributeCategory.OTHER,
        AttrType.OBJECT,
        default={
            # Geometry represents a realistic ~100 m2 Dutch residential building
            # footprint (TABULA-style wall/roof/floor proportions).
            #
            # Windows/Doors/Ventilation are deliberately *absent* here -- they
            # are synthesized internally by CfgBuilding.to_cfg_dict() from this
            # Walls geometry (buem.buildings.mapping.live_synthesis), the same
            # way a real EnerPlanET request that omits LOD3 detail is handled
            # (see CLAUDE.md "LOD3 envelope synthesis is internal to buem").
            # This demo previously hand-picked Windows/Doors/Ventilation values
            # that did not follow buildings.rst's documented sizing rules;
            # letting the real synthesis path fill them in keeps this default
            # an honest example of what buem actually computes, not a
            # hand-tuned stand-in.
            #
            # Wall areas below are GROSS (full wall footprint, not yet net of
            # window/door area) -- synthesis shrinks them to net opaque area
            # itself; do not also hand-subtract window/door area here.
            # Wall_1 (south, az=180) carries most solar gain; Wall_2 (north+east+west
            # combined, modelled north-facing az=0) has near-zero solar contribution
            # but accounts for the full N/E/W envelope conductance.
            # pvlib tilt convention: 0=horizontal-up, 90=vertical, 180=horizontal-down.
            "Walls": {
                "U": 1.61,
                "b_transmission": 1.0,
                "elements": [
                    {"id": "Wall_1", "area": 53.0, "azimuth": 180.0, "tilt": 90.0},  # South facade (gross)
                    {"id": "Wall_2", "area": 80.0, "azimuth":   0.0, "tilt": 90.0},  # N+E+W combined (gross), north-facing = minimal solar
                ],
            },
            "Roof": {
                "U": 1.54,
                "elements": [
                    {"id": "Roof_1", "area": 60.0, "azimuth": 180.0, "tilt": 30.0},  # Pitched roof: 50 m2 footprint / cos(30)
                ],
            },
            "Floor": {"U": 1.72, "elements": [{"id": "Floor_1", "area": 50.0, "azimuth": 0.0, "tilt": 180.0}]},  # Ground floor footprint; tilt 180=downward, no solar
        },
        doc=(
            "Structured component tree. Component-level 'U' applies to all "
            "elements; elements list carries per-surface geometry and area. "
            "Windows/Doors/Ventilation are synthesized internally from Walls "
            "by CfgBuilding.to_cfg_dict() when the caller omits them -- see "
            "buem.buildings.mapping.live_synthesis; supply them explicitly "
            "here (non-empty 'elements') to opt out of synthesis for a "
            "given component."
        ),
    ),
    "A_ref": AttributeSpec("A_ref", AttributeCategory.FIXED, AttrType.FLOAT, 100.0),  # Realistic reference floor area
    "h_room": AttributeSpec("h_room", AttributeCategory.FIXED, AttrType.FLOAT, 2.5),
    "n_air_infiltration": AttributeSpec("n_air_infiltration", AttributeCategory.FIXED, AttrType.FLOAT, 0.5),
    "n_air_use": AttributeSpec("n_air_use", AttributeCategory.FIXED, AttrType.FLOAT, 0.5),
    "design_T_min": AttributeSpec("design_T_min", AttributeCategory.FIXED, AttrType.FLOAT, -12.0),
    "onlyEnergyInvest": AttributeSpec("onlyEnergyInvest", AttributeCategory.BOOLEAN, AttrType.BOOL, False),
    "g_gl_n_Window": AttributeSpec("g_gl_n_Window", AttributeCategory.FIXED, AttrType.FLOAT, 0.5),
    "thermalClass": AttributeSpec("thermalClass", AttributeCategory.FIXED, AttrType.STR, "medium"),
    "c_m": AttributeSpec(
        "c_m",
        AttributeCategory.FIXED,
        AttrType.FLOAT,
        175.0,
        doc="Specific thermal capacity of building mass [kJ/m²K]. ISO 13790 medium class midpoint: (137.5+212.5)/2=175.",
    ),
    "comfortT_lb": AttributeSpec(
        "comfortT_lb", AttributeCategory.FIXED, AttrType.FLOAT, DEFAULT_COMFORT_T_LB,
        doc=(
            "Heating setpoint / lower comfort bound [degC]. Scalar, or a "
            "pd.Series aligned to the weather index for a real setback "
            "schedule. See building_registry.DEFAULT_COMFORT_T_LB."
        ),
    ),
    "comfortT_ub": AttributeSpec(
        "comfortT_ub", AttributeCategory.FIXED, AttrType.FLOAT, DEFAULT_COMFORT_T_UB,
        doc=(
            "Cooling setpoint / upper comfort bound [degC]. Scalar, or a "
            "pd.Series aligned to the weather index. See "
            "building_registry.DEFAULT_COMFORT_T_UB."
        ),
    ),
    "F_sh_vert": AttributeSpec("F_sh_vert", AttributeCategory.FIXED, AttrType.FLOAT, 0.75),  # Realistic shading for Netherlands
    "F_sh_hor": AttributeSpec("F_sh_hor", AttributeCategory.FIXED, AttrType.FLOAT, 0.80),  # Realistic shading for Netherlands
    "F_f": AttributeSpec("F_f", AttributeCategory.FIXED, AttrType.FLOAT, 0.2),
    "F_w": AttributeSpec("F_w", AttributeCategory.FIXED, AttrType.FLOAT, 1.0),
    "F_red_htr": AttributeSpec(
        "F_red_htr",
        AttributeCategory.FIXED,
        AttrType.FLOAT,
        1.0,
        doc="Intermittent heating reduction factor (ISO 13790 §13.2.2). TABULA F_red_htr1: 0.95 (AB/MFH), 0.90 (SFH/TH). 1.0 = no reduction.",
    ),
    "ventControl": AttributeSpec("ventControl", AttributeCategory.BOOLEAN, AttrType.BOOL, False),
    "control": AttributeSpec("control", AttributeCategory.BOOLEAN, AttrType.BOOL, False),
    "building_type": AttributeSpec(
        "building_type",
        AttributeCategory.FIXED,
        AttrType.STR,
        DEFAULT_BUILDING_TYPE,
        doc=(
            "TABULA residential size class (SFH/MFH/TH/AB, see "
            "RESIDENTIAL_BUILDING_TYPES) or one of occupancy's service-building "
            "type ids (supermarket/office/restaurant/school/hotel/bakery/"
            "warehouse/clinic). Selects HouseholdProfile vs. ServiceBuildingProfile "
            "in AttributeBuilder.generate_electricity_profile(); also one of the "
            "TABULA archetype match keys for live LOD2->LOD3 envelope synthesis "
            "(buem.buildings.mapping.live_synthesis, via CfgBuilding.to_cfg_dict())."
        ),
    ),
    "construction_period": AttributeSpec(
        "construction_period",
        AttributeCategory.FIXED,
        AttrType.STR,
        "",
        doc=(
            "TABULA construction-year class (e.g. 'DE.04', or the bare '04' "
            "form EnerPlanET's v3/v4 send -- see CLAUDE.md 'v2 vs v3/v4 "
            "request formats'). Already forwarded end-to-end from a real v3 "
            "request's building.construction_period by "
            "geojson_validator.py::_convert_v3_to_v2(); registered here so "
            "CfgBuilding retains it (attributes not in ATTRIBUTE_SPECS are "
            "dropped by CfgBuilding._build_internal_cfg). Used, together "
            "with building_type/country, to resolve a TABULA archetype for "
            "live envelope synthesis -- see bldg_tabula_id and "
            "tabula_helpers.lookup_tabula_archetype. Empty string (no "
            "match) falls back to documented safe-default ratios."
        ),
    ),
    "country": AttributeSpec(
        "country",
        AttributeCategory.FIXED,
        AttrType.STR,
        "NL",
        doc=(
            "ISO 3166-1 alpha-2 country code. Same forwarding/registration "
            "rationale as construction_period above. The bundled TABULA "
            "reference sheet (tabula_building_child_features.xlsx) "
            "currently only covers 'DE', so this module's own 'NL' default "
            "deliberately exercises live_synthesis's safe-default-ratio "
            "fallback rather than a real TABULA match -- expected, not a bug."
        ),
    ),
    "capacity": AttributeSpec(
        "capacity",
        AttributeCategory.FIXED,
        AttrType.INT,
        None,
        doc=(
            "Service-building capacity (e.g. seats, beds, staff) passed to "
            "occupancy.ServiceBuildingProfile. Ignored for residential "
            "building_type values, where num_persons applies instead. "
            "None uses the service type's own capacity_default."
        ),
    ),
    "num_persons": AttributeSpec(
        "num_persons", AttributeCategory.FIXED, AttrType.INT, None,
        doc=(
            "Occupants per dwelling, for occupancy profile generation. "
            "For a multi-dwelling building (see residential_units) this is "
            "the size of one household, not the building's total occupancy. "
            "None (the default) resolves a real per-building-type figure "
            "from data/reference/num_persons_by_building_type.csv, keyed on "
            "building_type plus country/region_code where those are known, "
            "falling back to building_registry.DEFAULT_NUM_PERSONS. An "
            "explicit caller value always wins. Ignored for "
            "service-building types, where capacity applies instead."
        ),
    ),
    "region_code": AttributeSpec(
        "region_code", AttributeCategory.FIXED, AttrType.STR, None,
        doc=(
            "Statistical region identifier, interpreted by the country's "
            "own scheme rather than by buem -- a CBS 'RegioS' municipality "
            "code such as 'GM0200' for the Netherlands. Used only to select "
            "a region-specific row from "
            "data/reference/num_persons_by_building_type.csv; an unknown "
            "code falls back to the country-wide row, so supplying a wrong "
            "one degrades rather than fails."
        ),
    ),
    "window_to_wall_ratio": AttributeSpec(
        "window_to_wall_ratio", AttributeCategory.FIXED, AttrType.FLOAT, None,
        doc=(
            "Fraction of each exposed wall's area that is glazed, in "
            "[0, 1). Applied uniformly to every wall; each synthesized "
            "window inherits its host wall's own azimuth and tilt, so "
            "orientation follows the real geometry. Used only when the "
            "request supplies no explicit Windows component. None "
            "(default) uses building_registry.DEFAULT_WINDOW_TO_WALL_RATIO. "
            "An out-of-range value raises rather than silently reverting "
            "to the default."
        ),
    ),
    "residential_units": AttributeSpec(
        "residential_units", AttributeCategory.FIXED, AttrType.FLOAT, 1.0,
        doc=(
            "Number of dwellings in this building. 1.0 for single-dwelling "
            "houses (SFH/TH); the real dwelling count for apartment blocks "
            "and multi-family buildings (AB/MFH), where one building_id "
            "represents the whole block. Scales the occupancy-generated "
            "Q_ig/elecLoad from one dwelling up to the whole building, "
            "matching the whole-block envelope the 5R1C solve uses. See "
            "AttributeBuilder.generate_electricity_profile()."
        ),
    ),
    "archetype": AttributeSpec(
        "archetype",
        AttributeCategory.FIXED,
        AttrType.STR,
        None,
        doc=(
            "occupancy household archetype (generic/working_couple/"
            "family_with_children/retired_single/student_shared) passed to "
            "occupancy.HouseholdProfile. Residential only -- ignored for "
            "service-building types. None falls back to "
            "DEFAULT_ARCHETYPE_BY_BUILDING_TYPE.get(building_type, 'generic')."
        ),
    ),
    "equipment": AttributeSpec(
        "equipment",
        AttributeCategory.OTHER,
        AttrType.OBJECT,
        None,
        doc=(
            "Optional per-item household-equipment inclusion/exclusion map: "
            "{equipment_id: bool, ...}, where each equipment_id is one of "
            "HOUSEHOLD_EQUIPMENT_TYPES (occupancy's 29 registered "
            "appliances). true guarantees the item is treated as owned "
            "(overrides its normal ownership-probability draw); false "
            "guarantees it's excluded entirely; an omitted id uses "
            "occupancy's own archetype-adjusted default for that item. "
            "Residential building_type only -- ignored (with a logged "
            "warning) for service-building types, since "
            "occupancy.ServiceBuildingProfile has no per-item equipment "
            "selection. None (default) uses occupancy's own default "
            "equipment set."
        ),
    ),
    "year": AttributeSpec("year", AttributeCategory.FIXED, AttrType.INT, DEFAULT_YEAR, doc="Default year for profile generation"),
    "seed": AttributeSpec(
        "seed",
        AttributeCategory.FIXED,
        AttrType.INT,
        None,
        doc=(
            "RNG seed for occupancy profile generation. Internal-only -- "
            "not part of the EnerPlanET request contract (deliberately "
            "absent from the v3/v4 request schemas and not forwarded by "
            "_convert_v3_to_v2()). Default is None: buem does not "
            "manufacture or own a default seed value. None flows straight "
            "through to occupancy's HouseholdProfile/"
            "ElectricityConsumptionProfile/ServiceBuildingProfile/"
            "generate_dhw_draws(), each of which derives its own "
            "deterministic seed from the profile's own construction "
            "inputs (occupancy.core.seed.derive_default_seed) when "
            "seed=None -- the same building always resolves to the same "
            "seed and therefore the same profile, with no buem-side "
            "bookkeeping required. Still overridable for direct "
            "AttributeBuilder calls or tests wanting one specific, "
            "caller-chosen value."
        ),
    ),
    "use_provided_elecLoad": AttributeSpec(
        "use_provided_elecLoad",
        AttributeCategory.BOOLEAN,
        AttrType.BOOL,
        False,
        doc=(
            "If true, substitute the provided elecLoad series for the "
            "occupancy-generated one via occupancy.to_buem_profiles(elec_load=...) "
            "-- Q_ig/occ_nothome/occ_sleeping still come from a real "
            "HouseholdProfile/ServiceBuildingProfile generation, only "
            "elecLoad itself is overridden. Does not skip the occupancy "
            "call entirely -- that would also lose "
            "Q_ig/occ_nothome/occ_sleeping."
        ),
    ),
    "weather_provider": AttributeSpec(
        "weather_provider", AttributeCategory.FIXED, AttrType.STR, DEFAULT_WEATHER_PROVIDER,
        doc="Weather source for the per-location fetch: 'merra-2' (default), 'era5-land', or 'cosmo-rea6'."
    ),
    "use_provided_weather": AttributeSpec(
        "use_provided_weather", AttributeCategory.BOOLEAN, AttrType.BOOL, False,
        doc="If true, keep the provided weather DataFrame instead of dynamically fetching one for (latitude, longitude, year)."
    ),
}

# Legacy default cfg dict (keeps existing API for other modules)
cfg: dict[str, Any] = {spec.name: spec.default for spec in ATTRIBUTE_SPECS.values()}
# Ensure the DataFrame is the actual DataFrame object (already set in spec defaults)
cfg["weather"] = ATTRIBUTE_SPECS["weather"].default

# ATTRIBUTE_SPECS["components"].default above deliberately carries only
# Walls/Roof/Floor (see its own comment) -- CfgBuilding.to_cfg_dict() is the
# usual place Windows/Doors/Ventilation get synthesized (buem.buildings.
# mapping.live_synthesis), but some callers (e.g. ModelBUEM in tests/
# buem.main's demo path) use this legacy `cfg` dict directly, bypassing
# CfgBuilding entirely -- model_buem.py treats "Ventilation" as a required
# component and would raise if it were simply absent. Apply the same
# synthesis here once at import time so `cfg`/DEFAULT_CFG stays a complete,
# directly-runnable config exactly like before this change, and idempotent
# to call again from CfgBuilding.to_cfg_dict() when supplied through it.
cfg["components"] = synthesize_missing_openings(
    cfg["components"],
    building_type=cfg.get("building_type"),
    construction_period=cfg.get("construction_period"),
    country=cfg.get("country"),
    bldg_tabula_id=cfg.get("bldg_tabula_id"),
)
