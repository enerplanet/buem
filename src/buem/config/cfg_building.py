from __future__ import annotations

import copy
import json
import logging
from typing import Any

import numpy as np
import pandas as pd

from buem.buildings.mapping.live_synthesis import (
    normalize_opening_azimuths,
    synthesize_missing_openings,
)
from buem.config.attribute_types import AttributeCategory, AttributeSpec, AttrType
from buem.config.cfg_attribute import ATTRIBUTE_SPECS
from buem.config.cfg_attribute import cfg as DEFAULT_CFG
from buem.config.setback import apply_setback

logger = logging.getLogger(__name__)


def _series_to_list(s: pd.Series | None) -> list | None:
    """Convert pandas Series to list or return None."""
    if s is None:
        return None
    return list(s.values)


def _df_to_serializable(df: pd.DataFrame | None) -> dict[str, Any] | None:
    """Convert DataFrame to serializable dict with ISO timestamps and column lists."""
    if df is None:
        return None
    return {
        "index": [ts.isoformat() for ts in list(df.index)],
        **{col: list(df[col].values) for col in df.columns},
    }

def _get_spec_keys_by_category(category: AttributeCategory) -> dict[str, AttributeSpec]:
    """Return subset of ATTRIBUTE_SPECS for a category."""
    return {k: v for k, v in ATTRIBUTE_SPECS.items() if v.category == category}


class WeatherConfig:
    """
    Holder for weather timeseries.

    Accepts:
      - None -> uses DEFAULT_CFG['weather']
      - pandas.DataFrame -> used directly (copy made)
      - dict -> expected keys like "T","GHI","DNI","DHI" as lists and optional "index" (ISO strings)

    Attributes:
      df: pandas.DataFrame or None
    """

    def __init__(self, value: Any | None):
        if isinstance(value, pd.DataFrame):
            self.df = value.copy()
            return

        if isinstance(value, dict):
            # build from dict
            cols = {}
            index = None
            if "index" in value:
                index = pd.to_datetime(value["index"])
            for c in ("T", "GHI", "DNI", "DHI"):
                if c in value:
                    cols[c] = np.asarray(value[c], dtype=float)
            if cols:
                if index is None:
                    # try to reuse default index length if available
                    default_weather = DEFAULT_CFG.get("weather")
                    if isinstance(default_weather, pd.DataFrame) and len(default_weather) == len(next(iter(cols.values()))):
                        index = default_weather.index
                    else:
                        index = pd.date_range("2025-01-01", periods=len(next(iter(cols.values()))), freq="h")
                self.df = pd.DataFrame(cols, index=index)
                return

        # fallback to default weather DataFrame (deep copy)
        default_weather = DEFAULT_CFG.get("weather")
        self.df = copy.deepcopy(default_weather) if default_weather is not None else None

    @property
    def index(self) -> pd.DatetimeIndex | None:
        """Return datetime index or None."""
        return None if self.df is None else self.df.index

    @property
    def n_hours(self) -> int:
        """Number of rows in weather timeseries (0 if none)."""
        return 0 if self.df is None else len(self.df)

    def to_serializable(self) -> dict[str, Any] | None:
        """Return a JSON-serializable representation of the weather timeseries."""
        return _df_to_serializable(self.df)


class BooleanConfig:
    """
    Container for boolean flags. Uses ATTRIBUTE_SPECS to discover boolean keys.

    Initializes from provided dict and uses defaults from DEFAULT_CFG for missing boolean flags.
    New boolean keys supplied by the caller are preserved.
    """

    def __init__(self, value: dict[str, Any] | None):
        self._data: dict[str, bool] = {}
        bool_specs = _get_spec_keys_by_category(AttributeCategory.BOOLEAN)
        # start from DEFAULT_CFG booleans
        for k, spec in bool_specs.items():
            self._data[k] = bool(spec.default)
        # override/extend with provided values
        if isinstance(value, dict):
            for k, v in value.items():
                if k in bool_specs:
                    self._data[k] = bool(v)

    def as_dict(self) -> dict[str, bool]:
        """Return a plain dict of booleans."""
        return dict(self._data)

    def update(self, d: dict[str, Any]):
        """Shallow update boolean values from a dict."""
        for k, v in d.items():
            if k in self._data:
                self._data[k] = bool(v)


class FixedConfig:
    """
    Container for fixed/numeric parameters. Uses ATTRIBUTE_SPECS to determine types and defaults.

    Initializes from provided dict and fills missing values from DEFAULT_CFG.
    If DEFAULT_CFG contains pandas.Series for a key and the caller provides a list of
    matching length to the weather index, the list is converted to pandas.Series.
    """

    def __init__(self, value: dict[str, Any] | None, weather_index: pd.DatetimeIndex | None):
        # copy defaults except weather
        self._data: dict[str, Any] = {}
        fixed_specs = _get_spec_keys_by_category(AttributeCategory.FIXED)

        # Initialize defaults
        for k, spec in fixed_specs.items():
            self._data[k] = copy.deepcopy(spec.default)

        # apply provided values
        if isinstance(value, dict):
            for k, v in value.items():
                if k not in fixed_specs:
                    # allow dynamic addition of new attributes (store as-is)
                    self._data[k] = copy.deepcopy(v)
                    continue
                spec = fixed_specs[k]

                # convert lists to series if spec says SERIES and index available
                if spec.type == AttrType.SERIES and isinstance(v, (list, tuple, np.ndarray)) and weather_index is not None:
                    arr = np.asarray(v, dtype=float)
                    if len(arr) == len(weather_index):
                        self._data[k] = pd.Series(arr, index=weather_index)
                        continue
                if isinstance(v, pd.Series) and weather_index is not None:
                    self._data[k] = v.reindex(weather_index)
                    continue
                self._data[k] = copy.deepcopy(v)

    def as_dict(self) -> dict[str, Any]:
        """Return fixed parameters preserving pandas objects."""
        return dict(self._data)

    def to_serializable(self) -> dict[str, Any]:
        """Return JSON-serializable representation: Series->lists, numpy scalars->py scalars."""
        out: dict[str, Any] = {}
        for k, v in self._data.items():
            if isinstance(v, pd.Series):
                out[k] = _series_to_list(v)
            elif isinstance(v, pd.DataFrame):
                out[k] = _df_to_serializable(v)
            elif isinstance(v, (np.integer, np.floating)):
                out[k] = v.item()
            else:
                try:
                    json.dumps(v)
                    out[k] = v
                except TypeError:
                    out[k] = str(v)
        return out

    def update(self, d: dict[str, Any], weather_index: pd.DatetimeIndex | None):
        """Update fixed params with same conversion rules as initializer."""
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            if k in self._data:
                spec = ATTRIBUTE_SPECS.get(k)
                if spec and spec.type == AttrType.SERIES and isinstance(v, (list, tuple, np.ndarray)) and weather_index is not None:
                    arr = np.asarray(v, dtype=float)
                    if len(arr) == len(weather_index):
                        self._data[k] = pd.Series(arr, index=weather_index)
                        continue
                if isinstance(v, pd.Series) and weather_index is not None:
                    self._data[k] = v.reindex(weather_index)
                    continue
            # dynamic add or overwrite
            self._data[k] = copy.deepcopy(v)


class CfgBuilding:
    """
    Main configuration API.

    Constructor requires a JSON string describing the building configuration. The JSON
    may provide:
      - nested sections: {"weather": {...}, "booleans": {...}, "fixed": {...}}

    Behavior:
      - Initialization requires a JSON string
      - Missing attributes are filled from ATTRIBUTE_SPECS defaults (taken from cfg_attribute).
      - New attributes present in input are preserved and included in outputs.
    """

    def __init__(self, json_input):
        """
        Initialize CfgBuilding from either:
          - a Python dict (preferred when caller already has pandas objects), or
          - a non-empty JSON string (backwards compatible).
        """
        # Accept dict directly (preserves pandas objects) or a JSON string
        if isinstance(json_input, dict):
            parsed = json_input
        elif isinstance(json_input, str):
            if not json_input.strip():
                raise ValueError("CfgBuilding requires a non-empty JSON string on initialization.")
            parsed = json.loads(json_input)
        else:
            raise TypeError("CfgBuilding requires a dict or non-empty JSON string on initialization.")

        # ensure all attributes are present (fill missing from specs)
        parsed_filled = self._ensure_and_normalize_input(parsed)
        # keep normalized input available for building the internal cfg (includes 'components')
        self._parsed_filled = parsed_filled

        # Extract categories
        weather_input = parsed_filled.get("weather")
        booleans_input = {k: parsed_filled[k] for k in parsed_filled if k in _get_spec_keys_by_category(AttributeCategory.BOOLEAN)}
        fixed_input = {k: parsed_filled[k] for k in parsed_filled if k in _get_spec_keys_by_category(AttributeCategory.FIXED)}

        # build components (weather first to obtain index)
        self.weather = WeatherConfig(weather_input)
        self.booleans = BooleanConfig(booleans_input)
        self.fixed = FixedConfig(fixed_input, weather_index=self.weather.index)

        # internal merged cfg (kept up-to-date by _build_internal_cfg)
        self._build_internal_cfg()

    def _ensure_and_normalize_input(self, data: Any) -> dict[str, Any]:
        """
        Ensure all attributes (from ATTRIBUTE_SPECS) exist in the input dict. Missing attributes
        are injected from defaults. For pandas objects in defaults, convert them to serializable
        forms (lists or nested dicts) so JSON input/POST payloads remain plain JSON.
        """
        if not isinstance(data, dict):
            data = {}
        out = dict(data)  # shallow copy

        # fill missing from specs
        for name, spec in ATTRIBUTE_SPECS.items():
            if name in out:
                continue
            # supply default in JSON-friendly form
            default = spec.default
            if spec.type == AttrType.DATAFRAME and isinstance(default, pd.DataFrame):
                # convert to serializable dict of lists
                out[name] = _df_to_serializable(default)
            elif spec.type == AttrType.SERIES and isinstance(default, pd.Series):
                out[name] = _series_to_list(default)
            else:
                # primitive or list/dict assumed JSON-serializable
                out[name] = copy.deepcopy(default)
        return out

    def _build_internal_cfg(self):
        """Merge weather, boolean, fixed and other attributes into a single internal dict."""
        cfg: dict[str, Any] = {}
        # weather as DataFrame
        cfg["weather"] = self.weather.df
        # booleans and fixed attributes
        cfg.update(self.booleans.as_dict())
        cfg.update(self.fixed.as_dict())

        # include other attributes from ATTRIBUTE_SPECS that are not in WEATHER/BOOLEAN/FIXED
        # (e.g., 'components' and other "OTHER" category attributes)
        for name, spec in ATTRIBUTE_SPECS.items():
            if spec.category not in (AttributeCategory.WEATHER, AttributeCategory.BOOLEAN, AttributeCategory.FIXED):
                # prefer value from the originally-parsed input (self._parsed_filled),
                # which already contains defaults if the caller omitted them.
                try:
                    cfg[name] = copy.deepcopy(self._parsed_filled.get(name))
                except (TypeError, copy.Error, AttributeError):
                    # fallback: use the spec default if the parsed value isn't deepcopy-able
                    cfg[name] = copy.deepcopy(spec.default)

        # include any dynamically added items present in fixed._data (already added via update),
        # leave _cfg on the instance
        self._cfg = cfg

    def to_cfg_dict(self) -> dict[str, Any]:
        """
        Return configuration dict using the canonical structured representation.

        Returns a deep copy of the internal cfg where:
         - 'weather' is a pandas.DataFrame
         - 'components' is the structured tree (dict)
         - Windows/Doors/Ventilation are internally synthesized from Walls
           geometry when the caller didn't supply them (see
           buem.buildings.mapping.live_synthesis -- this is the single
           choke point both the live API path (AttributeBuilder ->
           CfgBuilding) and the config-only/demo path (CfgBuilding called
           directly) converge on, so both benefit uniformly)
         - if 'A_ref' is not present but components are present, A_ref is derived
        """
        self._build_internal_cfg()
        cfg = copy.deepcopy(self._cfg)

        # Ensure components is present and compute A_ref if missing
        comps = cfg.get("components")
        if not isinstance(comps, dict) or not comps:
            # structured components are required for the cleaned codebase
            # return cfg as-is; downstream code (validate_cfg) will enforce presence
            return cfg

        # LOD2 -> LOD3: synthesize any missing Windows/Doors/Ventilation from
        # Walls geometry + a resolved TABULA archetype (or documented safe
        # defaults) before anything downstream sees an incomplete envelope.
        # No-op when the caller already supplied all three explicitly.
        comps = synthesize_missing_openings(
            comps,
            building_type=cfg.get("building_type"),
            construction_period=cfg.get("construction_period"),
            country=cfg.get("country"),
            bldg_tabula_id=cfg.get("bldg_tabula_id"),
            window_to_wall_ratio=cfg.get("window_to_wall_ratio"),
        )
        # A window/door cannot face a different direction than the wall (or
        # roof, for a skylight) it's embedded in -- force azimuth/tilt to
        # match whenever a `surface` (parent_id) reference resolves, whether
        # the opening was just synthesized above (already consistent, a
        # no-op) or explicitly supplied by the caller (corrected here, not
        # rejected). See live_synthesis.normalize_opening_azimuths.
        comps = normalize_opening_azimuths(comps)
        cfg["components"] = comps

        # ISO 13790 section 13 intermittency: turn the scalar heating
        # setpoint into an hourly schedule when a setback profile is named.
        # No-op otherwise, which is the default -- see buem.config.setback
        # for why this is opt-in. Applied here for the same reason opening
        # synthesis is: this is the one point both the live API path and the
        # config-only path converge on.
        cfg = apply_setback(cfg)

        # compute aggregated A_ref if absent
        if "A_ref" not in cfg:
            total_area = 0.0
            for comp_data in comps.values():
                if isinstance(comp_data, dict):
                    for e in comp_data.get("elements", []):
                        try:
                            total_area += float(e.get("area", 0.0))
                        except (TypeError, ValueError, AttributeError) as exc:
                            logger.debug("Skipping element with non-numeric area %r: %s", e, exc)
            if total_area > 0:
                cfg["A_ref"] = total_area

        return cfg

    def to_json(self) -> str:
        """
        Return a JSON string suitable for API responses.
        Contains all attributes (including dynamically added ones).

        Series/DataFrame are converted to lists/serializable dicts.
        """
        self._build_internal_cfg()
        out: dict[str, Any] = {}
        out["weather"] = _df_to_serializable(self._cfg.get("weather"))
        for k, v in self._cfg.items():
            if k == "weather":
                continue
            if isinstance(v, pd.Series):
                out[k] = _series_to_list(v)
            elif isinstance(v, pd.DataFrame):
                out[k] = _df_to_serializable(v)
            elif isinstance(v, (np.integer, np.floating)):
                out[k] = v.item()
            else:
                try:
                    json.dumps(v)
                    out[k] = v
                except TypeError:
                    out[k] = str(v)
        return json.dumps(out, indent=2)

    def update_from_dict(self, d: dict[str, Any]):
        """
        Update configuration from a dict. Supports nested {'weather':..., 'booleans':..., 'fixed':...}
        or flat mapping. Missing attributes are left unchanged. Defaults remain available for
        attributes not supplied.
        """
        if not isinstance(d, dict):
            return
        if "weather" in d:
            self.weather = WeatherConfig(d.get("weather"))
        # booleans
        bool_updates = {k: v for k, v in d.items() if k in _get_spec_keys_by_category(AttributeCategory.BOOLEAN)}
        if bool_updates:
            self.booleans.update({k: v for k, v in d.items() if k in _get_spec_keys_by_category(AttributeCategory.BOOLEAN)})
        # fixed
        fixed_updates = {k: v for k, v in d.items() if k in _get_spec_keys_by_category(AttributeCategory.FIXED)}
        if fixed_updates:
            self.fixed.update(fixed_updates, weather_index=self.weather.index)
        self._build_internal_cfg()

    @classmethod
    def from_json_file(cls, path: str) -> "CfgBuilding":
        """Construct CfgBuilding from a JSON file path."""
        with open(path, "r", encoding="utf-8") as fh:
            s = fh.read()
        return cls(s)
