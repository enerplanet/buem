"""
Build complete building attributes by merging payload, database, and defaults.
Generate weather and electricity profiles, and align timeseries indices.
"""
import logging
import os
from collections.abc import Callable
from typing import Any

import pandas as pd

# occupancy (https://github.com/UU-BUEM/occupancy) is a compulsory
# dependency, same treatment as weather -- imported unconditionally like
# pandas/pvlib.
from occupancy import building_demand  # type: ignore[import]

from buem.config.cfg_attribute import ATTRIBUTE_SPECS, RESIDENTIAL_BUILDING_TYPES
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
        # The occupancy inputs actually used after defaults, for
        # model_metadata.resolved_inputs; None where a field does not
        # apply to the building type.
        self.resolved_inputs: dict[str, Any] = {}

    def build(self, thermal: bool = True) -> dict[str, Any]:
        """
        Build complete attribute dictionary.

        Parameters
        ----------
        thermal : bool, optional
            False for a request that selects neither heating nor cooling:
            no weather is fetched, the envelope is not required and the
            cfg is not validated for a solve. The occupancy-derived
            profiles then follow the request's own year.

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
        required = REQUIRED_FROM_CALLER if thermal else tuple(k for k in REQUIRED_FROM_CALLER if k != "components")
        missing_required = [k for k in required if k not in self._provided_keys]
        if missing_required:
            raise ValueError(
                f"Missing required building attributes (not supplied via payload "
                f"or database): {missing_required}. These identify the specific "
                "building being modeled and are not safe to default silently."
            )

        # Step 3: Fetch a location-specific weather DataFrame (unless opted out)
        if thermal:
            self.generate_weather_profile()
        elif not self.merged_attrs.get("use_provided_weather", False):
            # Drop the module-default frame so the profiles follow the
            # request's year instead of the default frame's.
            self.merged_attrs["weather"] = None

        # Step 4: Generate electricity profile (unless opted out)
        self.generate_electricity_profile()

        # Step 5: Align timeseries indices to weather year
        self.align_timeseries()

        # Step 6: Validate complete config
        if thermal:
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

    def generate_electricity_profile(self):
        """Generate Q_ig/elecLoad/occ_nothome/occ_sleeping, DHW and cooking
        through occupancy.building_demand(), then align them to the weather
        index.

        elecLoad can be overridden with a caller-supplied series
        (use_provided_elecLoad); Q_ig/occ_nothome/occ_sleeping still come
        from a real occupancy generation in that case. The household-size,
        archetype, capacity, blending, dwelling-scaling and cooking-carrier
        rules live in occupancy since 6.1.0+enerplanet.1; buem prices the
        DHW draws (thermal.dhw_cooking) and aligns everything to weather.
        """
        use_provided_elec = bool(self.merged_attrs.get("use_provided_elecLoad", False))
        provided_elec_load: pd.Series | None = None
        if use_provided_elec:
            provided_elec_load = self.merged_attrs.get("elecLoad")
            if not isinstance(provided_elec_load, pd.Series):
                raise ValueError(
                    "use_provided_elecLoad=True requires elecLoad to be a "
                    f"pandas Series; got {type(provided_elec_load).__name__}."
                )

        weather_df = self.merged_attrs.get("weather", ATTRIBUTE_SPECS["weather"].default)
        has_weather = isinstance(weather_df, pd.DataFrame) and not weather_df.empty
        weather_year = int(weather_df.index[0].year) if has_weather else int(self.merged_attrs["year"])
        building_type = self.merged_attrs.get("building_type", ATTRIBUTE_SPECS["building_type"].default)
        residential = building_type in RESIDENTIAL_BUILDING_TYPES

        def align(series: pd.Series, name: str) -> pd.Series:
            return _reindex_or_raise(series, weather_df.index, name) if has_weather else series

        try:
            demand = building_demand(
                building_type,
                country=self.merged_attrs.get("country"),
                region_code=self.merged_attrs.get("region_code"),
                year=weather_year,
                residential_units=float(self.merged_attrs.get("residential_units", 1.0) or 1.0),
                # A_ref is in REQUIRED_FROM_CALLER, so it is always set here.
                # Only service buildings use it: for occupancy's per-type
                # gain density and the capacity derived from floor area.
                floor_area_m2=None if residential else float(
                    self.merged_attrs.get("A_ref", ATTRIBUTE_SPECS["A_ref"].default)
                ),
                cooking_carrier=str(self.merged_attrs.get("cooking_carrier", "electric")),
                seed=self.merged_attrs.get("seed", ATTRIBUTE_SPECS["seed"].default),
                num_persons=self.merged_attrs.get("num_persons"),
                archetype=self.merged_attrs.get("archetype"),
                capacity=self.merged_attrs.get("capacity"),
                equipment=self.merged_attrs.get("equipment", ATTRIBUTE_SPECS["equipment"].default),
                elec_load=provided_elec_load,
                cooking_heat_gain_fraction=dhw_cooking.COOKING_HEAT_GAIN_FRACTION,
            )

            self.resolved_inputs = {
                "building_type": demand.building_type,
                "country": self.merged_attrs.get("country"),
                "region_code": self.merged_attrs.get("region_code"),
                "num_persons": demand.num_persons,
                "residential_units": demand.residential_units,
                "archetype": demand.archetype,
                "capacity": demand.capacity,
            }

            if not demand.elec_load_as_gain:
                # occupancy folded the service type's equipment and lighting
                # gain density into Q_ig, so elecLoad must not be added to the
                # internal gains a second time (enerplanet/buem#16).
                self.merged_attrs["elec_load_as_gain"] = False

            for key, series in demand.buem_profiles().items():
                self.merged_attrs[key] = align(series, key)
            self.merged_attrs["year"] = weather_year  # Force year consistency

            # DHW and cooking are optional: ModelBUEM treats a missing
            # dhw_liters/cooking_active as "not computed", not an error.
            if demand.dhw_draws is not None:
                # Priced per fixture: occupancy delivers each draw at its own
                # temperature, so one blended delta-T would misprice every
                # draw but the one it matches.
                self.merged_attrs["dhw_liters"] = align(demand.dhw_draws["dhw_liters_total"], "dhw_liters")
                self.merged_attrs["dhw_kwh"] = align(
                    dhw_cooking.dhw_energy_kwh_by_fixture(demand.dhw_draws), "dhw_kwh"
                )
            if demand.cooking_kwh is not None:
                self.merged_attrs["cooking_kwh"] = align(demand.cooking_kwh, "cooking_kwh")

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