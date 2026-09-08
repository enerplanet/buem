"""
Build complete building attributes by merging payload, database, and defaults.
Generate weather and electricity profiles, and align timeseries indices.
"""
import logging
import math
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pandas as pd

# occupancy (https://github.com/UU-BUEM/occupancy) is a compulsory
# dependency, same treatment as weather -- imported unconditionally like
# pandas/pvlib.
from occupancy import (  # type: ignore[import]
    ElectricityConsumptionProfile,
    HouseholdProfile,
    ServiceBuildingProfile,
    generate_dhw_draws,
    to_buem_profiles,
)

from buem.config.building_registry import DEFAULT_NUM_PERSONS
from buem.config.cfg_attribute import (
    ATTRIBUTE_SPECS,
    DEFAULT_ARCHETYPE_BY_BUILDING_TYPE,
    HOUSEHOLD_EQUIPMENT_TYPES,
    RESIDENTIAL_BUILDING_TYPES,
    derive_service_capacity,
)
from buem.config.reference_values import resolve_num_persons
from buem.config.validator import validate_cfg
from buem.config.weather_cache import get_or_fetch_weather
from buem.thermal import dhw_cooking

logger = logging.getLogger(__name__)


def _reindex_or_raise(series: pd.Series, target_index: pd.DatetimeIndex, name: str) -> pd.Series:
    """Reindex a profile onto the weather index without silently zero-filling
    gaps -- a real misalignment (e.g. a year/timezone mismatch between the
    occupancy profile and weather) should surface as an error, not a
    plausible-looking zero internal-gains/electricity result for those hours.

    ``method="nearest"`` alone would happily match e.g. a 2019 profile onto a
    2018 index (nearest always finds *some* label), which is exactly the
    silent-wrong-data failure mode this guards against -- a ``tolerance`` is
    required so a genuinely out-of-range timestamp reindexes to NaN instead.
    Half an hour assumes this repo's consistently-hourly resolution.
    """
    series_tz = series.index.tz if isinstance(series.index, pd.DatetimeIndex) else None
    target_tz = target_index.tz
    if series_tz != target_tz:
        series = series.tz_localize(target_tz) if series_tz is None else series.tz_convert(target_tz)
    aligned = series.reindex(target_index, method="nearest", tolerance=pd.Timedelta(minutes=30))
    missing = aligned.index[aligned.isna()]
    # weather-serve's /v1/weather/point exports N+1 boundary timestamps for
    # a year -- index[0] is the year's first hour, index[-1] is the first
    # instant of the *next* year, closing the final bin (see weather's
    # point.py). An occupancy-derived series only ever covers real hours
    # (N points), so that trailing boundary point is expected to miss --
    # it marks the end of the data, not a genuinely missing hour. Carry
    # the last real value forward for it rather than raising.
    if len(missing) == 1 and missing[0] == target_index[-1]:
        aligned.iloc[-1] = aligned.iloc[-2]
    if aligned.isna().any():
        n_missing = int(aligned.isna().sum())
        raise ValueError(
            f"{name} could not be aligned to the weather timeseries at "
            f"{n_missing} of {len(target_index)} timestep(s) -- refusing to "
            "silently zero-fill. Check that the occupancy profile covers the "
            "same year/timezone as the weather data."
        )
    return aligned

# How close to a whole number an occupancy figure must be before the
# second household generation is skipped as not worth its cost. At 0.01
# of a person the blend would shift gains by well under a percent.
_OCCUPANCY_BLEND_TOLERANCE = 0.01

# Blended across two household sizes; the rest of to_buem_profiles()'s
# keys are left as the lower household produced them. occ_nothome and
# occ_sleeping are dimensionless presence fractions rather than
# magnitudes, and cooking_active is a boolean activity flag that a linear
# blend would turn into a meaningless fraction of a flag.
_BLENDED_PROFILE_KEYS = ("Q_ig", "elecLoad")

# occupancy's equipment category covering hob, oven, microwave, kettle and
# small cooking appliances -- the set whose energy buem reports as cooking.
_COOKING_EQUIPMENT_CATEGORY = "kitchen"


def _bracket_occupancy(num_persons: float) -> tuple[int, int, float]:
    """Split a mean household size into the two integer sizes that bracket
    it, plus the weight of the upper one.

    ``occupancy.HouseholdProfile`` models a specific household with a whole
    number of occupants, but a per-building-type figure is a population
    mean and is normally fractional. Generating the two neighbouring sizes
    and blending their outputs reproduces that mean, where rounding to one
    integer would not: 2.4 and 2.6 occupants would otherwise collapse onto
    2 and 3, a 50% step across a 0.2 difference.

    Returns ``(lower, upper, upper_weight)``. ``upper_weight`` is 0.0 for a
    whole number, which is the signal to skip the second generation
    entirely -- so the common integer case costs exactly what it did
    before.
    """
    lower = math.floor(num_persons)
    weight = num_persons - lower
    if weight < _OCCUPANCY_BLEND_TOLERANCE:
        return max(1, lower), max(1, lower), 0.0
    if weight > 1.0 - _OCCUPANCY_BLEND_TOLERANCE:
        return lower + 1, lower + 1, 0.0
    return max(1, lower), max(1, lower) + 1, weight


def _blend_series(
    lower: pd.Series, upper: pd.Series, upper_weight: float,
) -> pd.Series:
    """Linear blend of two occupancy-derived series, preserving the name
    and index of the lower one."""
    blended = lower * (1.0 - upper_weight) + upper * upper_weight
    return blended.rename(lower.name)


def _scale_out_annual(series: pd.Series, remove_kwh: float) -> pd.Series:
    """Remove ``remove_kwh`` from a series' annual total by scaling it
    uniformly, preserving its shape and never producing a negative hour.

    Subtracting the cooking series hour by hour would be wrong here. The
    kitchen-only draws are an *independent* realization of occupancy's
    stochastic model rather than a decomposition of the full run (see
    :func:`_generate_cooking_energy`), so in individual hours cooking can
    exceed the very series it is being removed from; clipping the result
    at zero then silently discards a large share of the energy -- measured
    at roughly half of it on a real household. Scaling gets the annual
    total exactly right, which is what a carrier split or a gain
    correction actually needs, and gives up only the hour-level placement
    of a term that was never hour-aligned to begin with.
    """
    total = float(series.sum())
    if total <= 0:
        return series
    factor = max(0.0, 1.0 - remove_kwh / total)
    return (series * factor).rename(series.name)


def _generate_cooking_energy(
    household: HouseholdProfile,
    elec_gen: ElectricityConsumptionProfile,
    seed: int | None,
) -> pd.Series | None:
    """Hourly cooking energy (kWh) from occupancy's own appliance model.

    Re-runs occupancy's stochastic generator over this household's
    ``kitchen`` equipment alone -- hob, oven, microwave, kettle and small
    cooking appliances -- so the result carries both *when* the household
    cooks and *how much* each event draws, from the same per-appliance
    draws and ownership probabilities that produced its electricity
    profile. This replaces deriving cooking energy from a fixed annual
    figure, or from the building's own simulated heating demand, neither
    of which responds to what the household actually does.

    The kitchen-only draws are an independent realization rather than an
    exact decomposition of ``elec_gen``'s own run: occupancy consumes its
    random stream per appliance, so restricting the table shifts it.
    Statistically equivalent, and deterministic for a given seed, but the
    two are not additive to the last kWh.

    Returns ``None`` if this household owns no cooking appliances at all,
    which the caller treats as "not computed" rather than as zero.
    """
    kitchen = {
        name: spec
        for name, spec in elec_gen.get_equipment_table().items()
        if getattr(spec, "category", None) == _COOKING_EQUIPMENT_CATEGORY
    }
    if not kitchen:
        return None
    profile = ElectricityConsumptionProfile(
        household, equipment=kitchen, seed=seed,
    ).to_result().profile
    return profile["total_power_kwh"].rename("cooking_kwh")


def _resolve_equipment_table(
    household: HouseholdProfile, seed: int | None, equipment_spec: Any
) -> dict[str, Any] | None:
    """Build a filtered occupancy equipment table from the optional
    ``equipment`` attribute: a per-item boolean map, e.g.
    ``{"washing_machine": True, "oven": False}``.

    ``True`` forces that item to be treated as owned -- sets its
    ``ownership_probability`` to 1.0, guaranteeing inclusion rather than
    just making it more likely (occupancy still gates on ``probability >=
    1.0`` in ``_owned_by_name()``, confirmed against occupancy's real
    source). ``False`` omits the item from the returned table entirely,
    guaranteeing exclusion regardless of ``enabled``/``ownership_probability``
    (an item absent from the dict never reaches
    ``ElectricityConsumptionProfile.generate()``'s iteration at all). An id
    not mentioned in ``equipment_spec`` is left exactly as occupancy's own
    default produces it for this household.

    Deliberately reads the *archetype-adjusted* base table via a throwaway
    ``ElectricityConsumptionProfile(household, seed=seed)
    .get_equipment_table()`` (a real public method) rather than the raw
    ``occupancy.households.electricity.default_equipment_table()`` --
    ``ElectricityConsumptionProfile.__post_init__`` applies the household's
    ``archetype.equipment_overrides`` on top of the raw default table, and
    reaching for the raw table directly would silently lose that per-archetype
    tuning for every unmentioned item.

    Returns ``None`` (occupancy's own default equipment set, unchanged) when
    no selector is supplied. Raises ``ValueError`` for a malformed selector
    or an unrecognized/non-boolean item value, naming the offending value(s),
    rather than failing inside occupancy with a less legible error.
    """
    if not equipment_spec:
        return None
    if not isinstance(equipment_spec, dict):
        # ValueError deliberately, matching the other two malformed-input
        # branches below (unrecognized id / non-bool value) -- a consistent
        # exception type across all three lets callers catch one type for
        # "malformed equipment input", not three. TRY004 would suggest
        # TypeError here; tests/test_equipment_selection.py::
        # test_resolve_equipment_table_non_dict_raises asserts ValueError.
        raise ValueError(  # noqa: TRY004
            "equipment must be a dict of {equipment_id: bool}, "
            f"got {type(equipment_spec).__name__}."
        )
    unknown = sorted(set(equipment_spec) - HOUSEHOLD_EQUIPMENT_TYPES)
    if unknown:
        raise ValueError(
            f"equipment contains unrecognized id(s) {unknown} -- expected "
            f"a subset of {sorted(HOUSEHOLD_EQUIPMENT_TYPES)}."
        )
    non_bool = {k: v for k, v in equipment_spec.items() if not isinstance(v, bool)}
    if non_bool:
        raise ValueError(f"equipment values must be true/false, got {non_bool!r}.")

    base_table = ElectricityConsumptionProfile(household, seed=seed).get_equipment_table()
    result: dict[str, Any] = {}
    for key, spec in base_table.items():
        if key not in equipment_spec:
            result[key] = spec
            continue
        if equipment_spec[key]:
            result[key] = replace(spec, ownership_probability=1.0)
        # False -> forced exclusion: simply omit from the returned table.
    return result


# Attributes that identify *which building* is being modeled -- there is no
# safe generic default for these (unlike thermal-class-type assumptions), so
# they must be explicitly supplied via payload_attrs or db_fetcher rather than
# silently falling back to ATTRIBUTE_SPECS' generic example-house defaults.
REQUIRED_FROM_CALLER: tuple[str, ...] = ("latitude", "longitude", "components", "A_ref")


class AttributeBuilder:
    """
    Merge building attributes from multiple sources and generate derived profiles.

    Precedence: payload > database > defaults (cfg_attribute.py)
    """

    def __init__(
        self,
        payload_attrs: dict[str, Any],
        building_id: str | None = None,
        db_fetcher: Callable[[str], dict[str, Any]] | None = None,
    ):
        """
        Initialize attribute builder.

        Parameters
        ----------
        payload_attrs : Dict[str, Any]
            Attributes from incoming API payload (building_attributes section).
        building_id : str, optional
            Building identifier for database lookup.
        db_fetcher : Callable, optional
            Function to fetch additional attributes by building_id.
        """
        self.payload_attrs = payload_attrs
        self.building_id = building_id
        self.db_fetcher = db_fetcher
        self.merged_attrs: dict[str, Any] = {}
        self._provided_keys: set[str] = set()

    def build(self) -> dict[str, Any]:
        """
        Build complete attribute dictionary.

        Returns
        -------
        Dict[str, Any]
            Complete building attributes ready for CfgBuilding.

        Raises
        ------
        ValueError
            If required attributes missing or validation fails.
        """
        # Step 1: Merge sources (payload > db > defaults)
        self.merge_sources()

        # Step 2: Refuse to silently model the generic example house in place
        # of a real building the caller forgot to fully specify.
        missing_required = [k for k in REQUIRED_FROM_CALLER if k not in self._provided_keys]
        if missing_required:
            raise ValueError(
                f"Missing required building attributes (not supplied via payload "
                f"or database): {missing_required}. These identify the specific "
                "building being modeled and are not safe to default silently."
            )

        # Step 3: Fetch a location-specific weather DataFrame (unless opted out)
        self.generate_weather_profile()

        # Step 4: Generate electricity profile (unless opted out)
        self.generate_electricity_profile()

        # Step 5: Align timeseries indices to weather year
        self.align_timeseries()

        # Step 6: Validate complete config
        issues = validate_cfg(self.merged_attrs)
        if issues:
            raise ValueError(f"Attribute validation failed: {'; '.join(issues)}")

        return self.merged_attrs
    
    def merge_sources(self):
        """Merge payload, database, and defaults with correct precedence."""
        # Start with defaults
        self.merged_attrs = {
            spec.name: spec.default
            for spec in ATTRIBUTE_SPECS.values()
        }

        # Overlay database values (if available)
        if self.db_fetcher and self.building_id:
            try:
                db_attrs = self.db_fetcher(self.building_id) or {}
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                # A db_fetcher was explicitly wired for a specific building_id --
                # if it fails, that building's real data is missing. Silently
                # continuing with the generic example-house defaults would model
                # the wrong building without any signal that anything went
                # wrong, so raise instead.
                raise RuntimeError(
                    f"db_fetcher failed for building_id={self.building_id!r}; "
                    "refusing to silently continue with generic building "
                    "defaults for a specific building lookup."
                ) from exc
            self.merged_attrs.update(db_attrs)
            self._provided_keys.update(db_attrs.keys())

        # Overlay payload (highest priority)
        self.merged_attrs.update(self.payload_attrs)
        self._provided_keys.update(self.payload_attrs.keys())
    
    def generate_weather_profile(self):
        """Fetch a location-specific weather DataFrame via the (compulsory)
        weather package, unless opted out. A fetch that fails for the
        requested location/year (no processed archive, bad response, etc.)
        always raises -- there is no fallback, since substituting any other
        location's weather (real or not) would silently model the wrong
        building.

        BUEM_WEATHER_FALLBACK (default: true) gates this fetch itself, for
        deployments where something upstream (an Orchestrator) always
        supplies weather and a missing block should fail loudly instead of
        buem silently resolving its own -- see enerplanet/buem#10. Standalone
        buem installs that want buem to resolve weather itself leave this
        unset."""
        if bool(self.merged_attrs.get("use_provided_weather", False)):
            return  # Keep the provided/merged weather DataFrame as-is

        if os.environ.get("BUEM_WEATHER_FALLBACK", "true").strip().lower() in ("false", "0", ""):
            raise RuntimeError(
                "buem.weather is required and BUEM_WEATHER_FALLBACK=false -- "
                "this deployment does not resolve its own weather. The caller "
                "must supply buem.weather.profile (a file path) or set "
                "BUEM_WEATHER_FALLBACK=true to allow buem's own per-location fetch."
            )

        lat = float(self.merged_attrs.get("latitude", ATTRIBUTE_SPECS["latitude"].default))
        lon = float(self.merged_attrs.get("longitude", ATTRIBUTE_SPECS["longitude"].default))
        year = int(self.merged_attrs.get("year", ATTRIBUTE_SPECS["year"].default))
        provider = self.merged_attrs.get("weather_provider", ATTRIBUTE_SPECS["weather_provider"].default)

        try:
            self.merged_attrs["weather"] = get_or_fetch_weather(lat, lon, year, provider)
        except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"Weather fetch failed for the requested building location "
                f"(lat={lat}, lon={lon}, year={year}, provider={provider!r})."
            ) from exc

    def _resolve_num_persons(self, building_type: str) -> float:
        """Mean occupants per dwelling for this building.

        An explicit caller-supplied ``num_persons`` always wins. Otherwise
        the figure comes from
        ``data/reference/num_persons_by_building_type.csv``, keyed on this
        building's own type plus its ``country``/``region_code`` where the
        request carries them, so a Dutch terraced house and a Dutch
        apartment no longer receive the same household size. The flat
        ``DEFAULT_NUM_PERSONS`` remains the last resort for a building type
        that table does not cover.

        Returned as a float, deliberately: a real household size is a
        population mean, and rounding it here would quantize the whole
        reference table onto a handful of integers -- making a 2.4-to-2.6
        edit either a no-op or a 50% jump. :func:`_bracket_occupancy` and
        :meth:`generate_electricity_profile` preserve the fraction
        instead.
        """
        explicit = self.merged_attrs.get("num_persons")
        if explicit is not None:
            # Explicit cast: a string from a JSON payload would otherwise
            # reach HouseholdProfile and raise an unrelated-looking
            # TypeError there.
            return max(1.0, float(explicit))
        resolved = resolve_num_persons(
            building_type,
            country=self.merged_attrs.get("country"),
            region_code=self.merged_attrs.get("region_code"),
            default=float(DEFAULT_NUM_PERSONS),
        )
        logger.debug(
            "Resolved num_persons=%s for building_type=%r country=%r "
            "region_code=%r", resolved, building_type,
            self.merged_attrs.get("country"), self.merged_attrs.get("region_code"),
        )
        # resolve_num_persons only returns None when its own default is
        # None; a real float default is always passed above.
        return max(1.0, float(resolved if resolved is not None else DEFAULT_NUM_PERSONS))

    def _generate_household(
        self, num_persons: int, archetype: str, weather_year: int,
        seed: Any, equipment_spec: Any,
    ) -> tuple[Any, pd.Series | None, pd.Series | None, pd.Series | None]:
        """Generate one integer-sized household's occupancy result, DHW
        draws and cooking energy. Returns
        ``(result, dhw_liters, dhw_kwh, cooking_kwh)``.

        DHW liters generation is household-specific: occupancy's DHW model
        is not wired to service buildings, so it stays inside the
        residential path only.

        ``seed=None`` is passed to ``generate_dhw_draws`` deliberately,
        rather than this household's own ``seed``: it lets occupancy derive
        its own deterministic ``"dhw"``-kind seed
        (``occupancy.core.seed.derive_default_seed``), decorrelated from the
        household's ``elecLoad``/``Q_ig`` draws rather than replaying one
        numeric seed across two independent stochastic processes. Still
        fully reproducible -- the same building always yields the same DHW
        draws -- just independent of the household's seed.
        """
        household = HouseholdProfile(
            num_persons=num_persons, year=weather_year, seed=seed, archetype=archetype,
        )
        equipment_table = _resolve_equipment_table(household, seed, equipment_spec)
        elec_gen = ElectricityConsumptionProfile(household, equipment=equipment_table, seed=seed)
        result = elec_gen.to_result()
        draws = generate_dhw_draws(
            result.profile,
            num_persons=num_persons,
            cooking_active=result.profile.get("cooking_active"),
            seed=None,
        )
        dhw_liters = draws["dhw_liters_total"]
        # Priced per fixture rather than from the blended total: occupancy
        # resolves basin, kitchen-sink, shower and bath draws separately
        # and each is delivered at its own temperature, so one blended
        # delta-T misprices every draw but the one it matches.
        dhw_kwh = dhw_cooking.dhw_energy_kwh_by_fixture(draws)
        cooking_kwh = _generate_cooking_energy(household, elec_gen, seed)
        return result, dhw_liters, dhw_kwh, cooking_kwh

    def _apply_cooking_energy_balance(
        self, buem_inputs: dict[str, pd.Series], cooking_kwh: pd.Series,
        elec_load_was_supplied: bool,
    ) -> None:
        """Split cooking energy into the carrier that pays for it and the
        share of it that heats the room. Mutates ``buem_inputs`` in place.

        Cooking plays two separate roles that an aggregate electricity
        profile conflates:

        1. **Consumption.** A gas hob's energy is billed as gas and never
           reaches the electricity meter; an electric hob's is billed as
           electricity. occupancy models every cooking appliance
           electrically, so a gas-cooking household needs that energy
           taken back out of ``elecLoad`` before it is reported as gas,
           or the same kWh appears under both carriers.
        2. **Internal gain.** Cooking is strongly exothermic and the food
           absorbs only part of the input; the rest leaves as sensible and
           latent heat, of which the share reaching the zone is
           ``COOKING_HEAT_GAIN_FRACTION``. This is the same for both
           carriers -- a gas flame heats the kitchen exactly as a hotplate
           does. ISO 13790's ``Q_ia = Q_ig + elecLoad`` would otherwise
           credit an electric hob's *entire* input as room heat, ignoring
           the extraction hood and the energy that leaves inside the food.

        ``Q_ig`` therefore absorbs the correction in both directions: down
        by the non-recovered share for electric cooking (already counted
        in full via ``elecLoad``), up by the recovered share for gas
        cooking (not in ``elecLoad`` at all).

        A caller-supplied ``elecLoad`` is never modified -- a real measured
        series already reflects whatever that household actually did.
        """
        carrier = str(self.merged_attrs.get("cooking_carrier", "electric")).lower()
        if carrier not in ("gas", "electric", "none"):
            raise ValueError(
                f"cooking_carrier must be 'electric', 'gas' or 'none', got {carrier!r}."
            )
        gain_fraction = dhw_cooking.COOKING_HEAT_GAIN_FRACTION
        cooking_annual = float(cooking_kwh.sum())
        if carrier == "none" or cooking_annual <= 0:
            return

        if carrier == "gas" and not elec_load_was_supplied:
            buem_inputs["elecLoad"] = _scale_out_annual(
                buem_inputs["elecLoad"], cooking_annual,
            )
        if carrier == "gas":
            buem_inputs["Q_ig"] = (
                buem_inputs["Q_ig"] + cooking_kwh * gain_fraction
            ).rename(buem_inputs["Q_ig"].name)
        else:
            buem_inputs["Q_ig"] = _scale_out_annual(
                buem_inputs["Q_ig"], cooking_annual * (1.0 - gain_fraction),
            )
        logger.debug(
            "Applied cooking energy balance: carrier=%s gain_fraction=%.2f "
            "cooking=%.1f kWh", carrier, gain_fraction, cooking_annual,
        )

    def generate_electricity_profile(self):
        """Generate Q_ig/elecLoad/occ_nothome/occ_sleeping via occupancy.

        elecLoad can be overridden with a caller-supplied series
        (use_provided_elecLoad) -- Q_ig/occ_nothome/occ_sleeping still come
        from a real occupancy generation in that case, via
        occupancy.to_buem_profiles(elec_load=...), rather than skipping the
        occupancy call entirely (which would also lose
        Q_ig/occ_nothome/occ_sleeping). Household equipment can optionally
        be filtered via the "equipment" attribute (residential
        building_type only).
        """
        use_provided_elec = bool(self.merged_attrs.get("use_provided_elecLoad", False))
        provided_elec_load: pd.Series | None = None
        if use_provided_elec:
            # Captured before generation below overwrites merged_attrs["elecLoad"].
            provided_elec_load = self.merged_attrs.get("elecLoad")
            if not isinstance(provided_elec_load, pd.Series):
                raise ValueError(
                    "use_provided_elecLoad=True requires elecLoad to be a "
                    f"pandas Series; got {type(provided_elec_load).__name__}."
                )

        # Extract weather to determine year
        weather_df = self.merged_attrs.get("weather", ATTRIBUTE_SPECS["weather"].default)
        if isinstance(weather_df, pd.DataFrame) and not weather_df.empty:
            weather_year = int(weather_df.index[0].year)
        else:
            weather_year = int(ATTRIBUTE_SPECS["year"].default)

        # Get generation parameters
        building_type = self.merged_attrs.get("building_type", ATTRIBUTE_SPECS["building_type"].default)
        seed = self.merged_attrs.get("seed", ATTRIBUTE_SPECS["seed"].default)
        equipment_spec = self.merged_attrs.get("equipment", ATTRIBUTE_SPECS["equipment"].default)

        try:
            # floor_area_m2 for occupancy's area-normalized gain component
            # (occupancy_gains_handoff.md Gap 1) -- residential only stays
            # None: household archetypes deliberately carry no gain_w_per_m2
            # (occupancy's own CHANGELOG), so passing a floor area there
            # would just raise. Non-residential resolves it below.
            floor_area_m2: float | None = None

            # Second household generated only for a fractional occupancy
            # (see _generate_household); None for whole numbers and for
            # every service building.
            upper_household: tuple[Any, pd.Series | None, pd.Series | None, pd.Series | None] | None = None
            upper_weight = 0.0
            cooking_kwh: pd.Series | None = None
            dhw_kwh: pd.Series | None = None

            if building_type in RESIDENTIAL_BUILDING_TYPES:
                num_persons_mean = self._resolve_num_persons(building_type)
                # Archetype: explicit caller value wins; otherwise a first-pass
                # building_type-based default (see DEFAULT_ARCHETYPE_BY_BUILDING_TYPE's
                # docstring in cfg_attribute.py for the caveats), falling back to
                # occupancy's own "generic" for anything unmapped.
                archetype = self.merged_attrs.get("archetype") or DEFAULT_ARCHETYPE_BY_BUILDING_TYPE.get(
                    building_type, "generic"
                )
                lower_n, upper_n, upper_weight = _bracket_occupancy(num_persons_mean)
                result, dhw_liters, dhw_kwh, cooking_kwh = self._generate_household(
                    lower_n, archetype, weather_year, seed, equipment_spec,
                )
                if upper_weight > 0.0:
                    upper_household = self._generate_household(
                        upper_n, archetype, weather_year, seed, equipment_spec,
                    )
            else:
                # Non-residential: route through occupancy's ServiceBuildingProfile
                # instead of forcing every building through HouseholdProfile.
                # ServiceBuildingProfile has no per-item equipment selection
                # -- a supplied equipment selector is a no-op here, not an
                # error.
                if equipment_spec:
                    logger.warning(
                        "equipment inclusion/exclusion was supplied for "
                        "service-building building_type %r, but "
                        "occupancy.ServiceBuildingProfile has no per-item "
                        "equipment selection -- ignoring.",
                        building_type,
                    )
                capacity_raw = self.merged_attrs.get("capacity", ATTRIBUTE_SPECS["capacity"].default)
                if capacity_raw is None:
                    # Size the building's occupancy from its own floor area
                    # rather than letting occupancy apply its type's
                    # capacity_default, which describes a typical full-size
                    # building and would otherwise be applied identically to
                    # every building of the type regardless of scale. An
                    # explicit caller-supplied capacity always wins.
                    capacity_raw = derive_service_capacity(
                        building_type, self.merged_attrs.get("A_ref")
                    )
                # Explicit cast (mirrors num_persons above) -- a string capacity from
                # a JSON payload would otherwise reach ServiceBuildingProfile's
                # `self.capacity <= 0` check and raise an unrelated-looking TypeError
                # instead of a clear int-conversion error here.
                capacity = int(capacity_raw) if capacity_raw is not None else None
                try:
                    service = ServiceBuildingProfile(
                        building_type=building_type,
                        year=weather_year,
                        capacity=capacity,
                        seed=seed,
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"building_type {building_type!r} is neither a residential TABULA "
                        f"code ({sorted(RESIDENTIAL_BUILDING_TYPES)}) nor a registered "
                        "occupancy service-building type."
                    ) from exc
                result = service.to_result()
                # A_ref is in REQUIRED_FROM_CALLER, so merged_attrs always has a
                # real value here -- but it may still be the flat 100.0
                # placeholder `_convert_v3_to_v2` substitutes when a v3 client
                # omits A_ref, not the true geometry-derived floor area
                # CfgBuilding computes afterwards. All 8 service-building
                # types carry a gain_w_per_m2, so this is safe to pass
                # unconditionally for the service-building branch.
                floor_area_m2 = float(self.merged_attrs.get("A_ref", ATTRIBUTE_SPECS["A_ref"].default))
                # No DHW model for service buildings yet -- the signals
                # generate_dhw_draws() consumes are household-specific.
                dhw_liters = None

            if provided_elec_load is not None:
                # occupancy's own result.profile.index is on-the-hour
                # (00:00, 01:00, ...), while a caller-supplied series aligned
                # to buem's weather index is typically half-hour-offset
                # (00:30, 01:30, ... -- interval-midpoint timestamps). to_buem_
                # profiles()'s internal elec_load reindex is exact-match only,
                # so realign here first with the same nearest+tolerance
                # approach already used to align buem_inputs onto weather_df
                # below, rather than have a realistically-timestamped caller
                # series fail with a confusing "does not cover the index"
                # error from inside occupancy.
                provided_elec_load = _reindex_or_raise(
                    provided_elec_load, result.profile.index, "elecLoad"
                )

            buem_inputs = to_buem_profiles(
                result, floor_area_m2=floor_area_m2, elec_load=provided_elec_load
            )

            # Reproduce a fractional mean household size by blending the two
            # integer-sized households that bracket it (see
            # _bracket_occupancy). Only the gain magnitudes are blended --
            # _BLENDED_PROFILE_KEYS documents why the rest are not. A
            # caller-supplied elecLoad is a real measured series and is left
            # exactly as measured.
            if upper_household is not None:
                upper_result, upper_dhw, upper_dhw_kwh, upper_cooking = upper_household
                upper_inputs = to_buem_profiles(
                    upper_result, floor_area_m2=floor_area_m2,
                    elec_load=provided_elec_load,
                )
                for key in _BLENDED_PROFILE_KEYS:
                    if key == "elecLoad" and provided_elec_load is not None:
                        continue
                    if key in buem_inputs and key in upper_inputs:
                        buem_inputs[key] = _blend_series(
                            buem_inputs[key], upper_inputs[key], upper_weight,
                        )
                if dhw_liters is not None and upper_dhw is not None:
                    dhw_liters = _blend_series(dhw_liters, upper_dhw, upper_weight)
                if dhw_kwh is not None and upper_dhw_kwh is not None:
                    dhw_kwh = _blend_series(dhw_kwh, upper_dhw_kwh, upper_weight)
                if cooking_kwh is not None and upper_cooking is not None:
                    cooking_kwh = _blend_series(cooking_kwh, upper_cooking, upper_weight)
                logger.debug(
                    "Blended occupancy across %d/%d persons (upper weight %.2f)",
                    lower_n, upper_n, upper_weight,
                )

            # Scale one dwelling's gains up to the whole building for
            # multi-dwelling buildings (AB/MFH). TABULA models an apartment
            # block as a single thermal zone spanning every dwelling
            # (its AB/MFH archetypes carry n_Apartment counts of 15-56),
            # and buem's envelope is likewise the whole block -- so the
            # internal gains driving that envelope must cover every
            # dwelling in it, not one household.
            #
            # Scaling one generated profile is preferred over generating N
            # independent ones: it keeps occupancy's own household-size
            # calibration valid (its stochastic generators are calibrated
            # for real household sizes, not a fictitious 50-person
            # household) at a fraction of the cost. The simplification is
            # that dwellings are treated as perfectly correlated, which
            # leaves annual energy totals exact but overstates the
            # simultaneity of peak internal gains.
            #
            # occ_nothome/occ_sleeping are deliberately not scaled: they
            # are fractions of occupants, dimensionless and already
            # building-wide. A caller-supplied elecLoad is also left alone
            # -- a real measured series is whatever the caller measured.
            units = float(self.merged_attrs.get("residential_units", 1.0) or 1.0)
            if units > 1.0:
                buem_inputs["Q_ig"] = buem_inputs["Q_ig"] * units
                if provided_elec_load is None:
                    buem_inputs["elecLoad"] = buem_inputs["elecLoad"] * units
                if dhw_liters is not None:
                    dhw_liters = dhw_liters * units
                if dhw_kwh is not None:
                    dhw_kwh = dhw_kwh * units
                if cooking_kwh is not None:
                    cooking_kwh = cooking_kwh * units
                logger.info(
                    "Scaled occupancy gains by %.0f dwelling(s) for multi-dwelling "
                    "building_type=%r", units, building_type,
                )

            # Align index with weather (8760 hourly points)
            if isinstance(weather_df, pd.DataFrame) and not weather_df.empty:
                buem_inputs = {
                    key: _reindex_or_raise(series, weather_df.index, key)
                    for key, series in buem_inputs.items()
                }

            # Cooking must reach the weather index before the energy
            # balance below subtracts it from series already on that index
            # -- occupancy timestamps on the hour, weather typically on the
            # half hour, so an unaligned subtraction yields all-NaN.
            if cooking_kwh is not None and isinstance(weather_df, pd.DataFrame) and not weather_df.empty:
                cooking_kwh = _reindex_or_raise(cooking_kwh, weather_df.index, "cooking_kwh")
            if isinstance(cooking_kwh, pd.Series):
                self._apply_cooking_energy_balance(buem_inputs, cooking_kwh,
                                                   provided_elec_load is not None)

            self.merged_attrs["elecLoad"] = buem_inputs["elecLoad"]
            self.merged_attrs["Q_ig"] = buem_inputs["Q_ig"]
            self.merged_attrs["occ_nothome"] = buem_inputs["occ_nothome"]
            self.merged_attrs["occ_sleeping"] = buem_inputs["occ_sleeping"]
            self.merged_attrs["year"] = weather_year  # Force year consistency

            # DHW/cooking are both optional -- ModelBUEM treats a missing
            # dhw_liters/cooking_active as "not computed", not an error.
            if dhw_liters is not None:
                if isinstance(weather_df, pd.DataFrame) and not weather_df.empty:
                    dhw_liters = _reindex_or_raise(dhw_liters, weather_df.index, "dhw_liters")
                self.merged_attrs["dhw_liters"] = dhw_liters
            if dhw_kwh is not None:
                if isinstance(weather_df, pd.DataFrame) and not weather_df.empty:
                    dhw_kwh = _reindex_or_raise(dhw_kwh, weather_df.index, "dhw_kwh")
                self.merged_attrs["dhw_kwh"] = dhw_kwh
            if cooking_kwh is not None:
                self.merged_attrs["cooking_kwh"] = cooking_kwh
            if "cooking_active" in buem_inputs:
                self.merged_attrs["cooking_active"] = buem_inputs["cooking_active"]

        except Exception as exc:
            raise RuntimeError(f"Electricity profile generation failed: {exc}") from exc
    
    def align_timeseries(self):
        """Ensure all timeseries share weather data year/index."""
        weather_df = self.merged_attrs.get("weather")
        if not isinstance(weather_df, pd.DataFrame) or weather_df.empty:
            return
        
        weather_index = weather_df.index
        
        # Align elecLoad (already done in generate_electricity_profile, but verify)
        if (
            "elecLoad" in self.merged_attrs
            and isinstance(self.merged_attrs["elecLoad"], pd.Series)
            and not self.merged_attrs["elecLoad"].index.equals(weather_index)
        ):
            self.merged_attrs["elecLoad"] = _reindex_or_raise(
                self.merged_attrs["elecLoad"], weather_index, "elecLoad"
            )

        # Align other profiles (Q_ig, occ_nothome, etc.) if needed. dhw_liters/
        # cooking_active are optional (absent for service buildings -- see
        # generate_electricity_profile), hence included here too rather than
        # assumed always-present like the first three.
        for key in ("Q_ig", "occ_nothome", "occ_sleeping", "dhw_liters",
                    "cooking_active", "cooking_kwh"):
            if (
                key in self.merged_attrs
                and isinstance(self.merged_attrs[key], pd.Series)
                and not self.merged_attrs[key].index.equals(weather_index)
            ):
                self.merged_attrs[key] = _reindex_or_raise(self.merged_attrs[key], weather_index, key)