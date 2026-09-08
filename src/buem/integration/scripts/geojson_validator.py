"""Validation and conversion for incoming BUEM GeoJSON requests.

Structural validation is delegated to the pinned contract schema
(json_schema/request_schema.json, see json_schema/README.md) via
`jsonschema` -- this module does not redefine the contract's shape. What
remains here:

- domain rules the generic schema can't express (a solver feature the
  pinned contract allows but this model doesn't implement yet; start_time
  before end_time),
- converting a validated request into the model's internal
  `building_attributes.components` shape (`_convert_to_internal_format`
  and its helpers) -- unchanged by the contract migration, since these
  transform already-valid input rather than validate it,
- detailed, per-field error reporting (`ValidationIssue`/`ValidationResult`,
  `create_validation_report`).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd
from jsonschema import Draft202012Validator

from buem.config.building_registry import DEFAULT_YEAR
from buem.integration.scripts.profile_file_loader import (
    load_electricity_load_values,
    load_weather_profile,
)
from buem.integration.scripts.schema_manager import schema_manager

# Plausibility bounds for a caller-supplied weather profile, checked at
# the request boundary so a client gets actionable feedback rather than
# an implausible result. Deliberately wide: these flag values that cannot
# be physically correct (or indicate a unit mix-up, e.g. irradiance in
# the wrong unit or temperature in Fahrenheit), not values that are
# merely unusual for a given location. Reported as warnings, never
# errors -- a genuinely extreme but real climate must still be
# simulable. Provider-fetched weather is not checked here; that is the
# `weather` package's own responsibility.
WEATHER_PROFILE_PLAUSIBLE_RANGES = {
    "T": (-60.0, 60.0),      # degC
    "GHI": (0.0, 1500.0),    # W/m2 -- above the solar constant at surface
    "DNI": (0.0, 1500.0),    # W/m2
    "DHI": (0.0, 1500.0),    # W/m2
}

logger = logging.getLogger(__name__)


class ValidationLevel(str, Enum):
    """Validation severity levels."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """Single validation issue with context."""
    level: ValidationLevel
    message: str
    path: str
    value: Any = None
    suggestion: str | None = None


@dataclass
class ValidationResult:
    """Complete validation result with detailed reporting."""
    is_valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    validated_data: dict[str, Any] | None = None

    def add_issue(self, level: ValidationLevel, message: str, path: str,
                  value: Any = None, suggestion: str | None = None):
        """Add a validation issue."""
        self.issues.append(ValidationIssue(level, message, path, value, suggestion))
        if level == ValidationLevel.ERROR:
            self.is_valid = False

    def get_errors(self) -> list[ValidationIssue]:
        """Get only error-level issues."""
        return [issue for issue in self.issues if issue.level == ValidationLevel.ERROR]

    def get_warnings(self) -> list[ValidationIssue]:
        """Get warning-level issues."""
        return [issue for issue in self.issues if issue.level == ValidationLevel.WARNING]

    def summary(self) -> str:
        """Get a summary of validation results."""
        errors = len(self.get_errors())
        warnings = len(self.get_warnings())
        if errors > 0:
            return f"Validation failed: {errors} errors, {warnings} warnings"
        elif warnings > 0:
            return f"Validation passed with {warnings} warnings"
        else:
            return "Validation passed successfully"


class GeoJsonValidator:
    """Validates a BUEM GeoJSON request against the pinned contract schema,
    then converts it into the model's internal attribute shape.
    """

    def __init__(self, strict_mode: bool = False):
        """
        Parameters
        ----------
        strict_mode : bool
            If True, warnings are treated as errors.
        """
        self.strict_mode = strict_mode
        self._schema_validator = Draft202012Validator(schema_manager.load_schema("request"))

    def validate(self, payload: dict[str, Any]) -> ValidationResult:
        """Validate a GeoJSON payload against the pinned contract schema and
        convert it into the model's internal representation.

        Parameters
        ----------
        payload : Dict[str, Any]
            Raw GeoJSON payload to validate.

        Returns
        -------
        ValidationResult
        """
        result = ValidationResult(is_valid=True)

        schema_errors = list(self._schema_validator.iter_errors(payload))
        if schema_errors:
            for error in schema_errors:
                path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else "root"
                result.add_issue(ValidationLevel.ERROR, error.message, path)
            return result

        result.validated_data = payload
        features = payload.get("features", [])
        self._validate_features(features, result)
        self._validate_time_consistency(features, result)
        for i, feature in enumerate(features):
            try:
                self._convert_to_internal_format(feature, result, i)
            except (TypeError, ValueError, KeyError, AttributeError, IndexError) as e:
                result.add_issue(
                    ValidationLevel.ERROR,
                    f"Failed to convert request to internal format: {e!s}",
                    f"features[{i}].properties.buem.building",
                )

        if self.strict_mode and result.get_warnings():
            result.is_valid = False

        return result

    def _validate_features(self, features: list[dict], result: ValidationResult):
        """Domain rules the pinned schema can't express structurally."""
        for i, feature in enumerate(features):
            self._validate_single_feature(feature, f"features[{i}]", result)

    def _validate_single_feature(self, feature: dict, path: str, result: ValidationResult):
        buem_data = feature.get("properties", {}).get("buem", {})

        # solver.compute_cooling: the pinned contract (schemas/v5) defines
        # real conditional-cooling semantics for this flag (comfortT_ub
        # enforced + cooling returned only when true). ModelBUEM does not
        # implement it -- main.py::run_model accepts a compute_cooling
        # argument but nothing in the solver reads cfg["compute_cooling"],
        # and geojson_processor.py never passes it through; heating and
        # cooling are always computed and returned regardless. Rejecting
        # here keeps that gap loud instead of silently returning a
        # response that doesn't match what was requested. Discovered
        # 2026-09-04 during the contract migration -- see CHANGELOG.
        solver_block = buem_data.get("solver")
        if isinstance(solver_block, dict) and solver_block.get("compute_cooling"):
            result.add_issue(
                ValidationLevel.ERROR,
                "solver.compute_cooling is defined by the contract but not yet "
                "implemented by this BUEM deployment",
                f"{path}.properties.buem.solver.compute_cooling",
                suggestion=(
                    "Omit compute_cooling -- both heating and cooling loads are "
                    "always computed and returned unconditionally today"
                ),
            )

    def _validate_time_consistency(self, features: list[dict], result: ValidationResult):
        """Validate time range consistency."""
        for i, feature in enumerate(features):
            props = feature.get("properties", {})
            start_time = props.get("start_time")
            end_time = props.get("end_time")

            if start_time and end_time:
                if isinstance(start_time, str):
                    start_time = datetime.fromisoformat(start_time)
                if isinstance(end_time, str):
                    end_time = datetime.fromisoformat(end_time)

                if start_time >= end_time:
                    result.add_issue(
                        ValidationLevel.ERROR,
                        "end_time must be after start_time",
                        f"features[{i}].properties",
                        suggestion="Check time range validity",
                    )

    def _convert_to_internal_format(self, feature: dict, result: ValidationResult, feature_idx: int):
        """
        Convert a validated request (building.envelope.elements with
        {value,unit} objects) into the model's internal representation
        (building_attributes.components.{Walls,Roof,...}.elements[] with
        plain numeric values).
        """
        buem_data = feature["properties"]["buem"]
        building = buem_data["building"]
        envelope = building.get("envelope", {})
        elements = envelope.get("elements", [])
        thermal = building.get("thermal", {})

        # Extract scalar values from {value, unit} objects
        def extract_value(obj):
            """Extract numeric value from either {value, unit} dict or plain value."""
            if isinstance(obj, dict) and "value" in obj:
                return obj["value"]
            return obj

        # Extract building-level attributes
        latitude = feature.get("geometry", {}).get("coordinates", [0, 0])[1]
        longitude = feature.get("geometry", {}).get("coordinates", [0, 0])[0]
        A_ref = extract_value(building.get("A_ref", 100.0))
        h_room = extract_value(building.get("h_room", 2.5))

        # Group elements by type -> component categories
        type_map = {
            "wall": "Walls",
            "roof": "Roof",
            "floor": "Floor",
            "window": "Windows",
            "door": "Doors",
            "ventilation": "Ventilation",
        }

        components: dict[str, dict[str, Any]] = {}
        for elem in elements:
            elem_type = elem.get("type", "").lower()
            comp_key = type_map.get(elem_type)
            if not comp_key:
                result.add_issue(
                    ValidationLevel.WARNING,
                    f"Unknown element type '{elem_type}' in envelope",
                    f"features[{feature_idx}].properties.buem.building.envelope",
                    suggestion=f"Valid types: {', '.join(type_map.keys())}",
                )
                continue

            if comp_key not in components:
                components[comp_key] = {"elements": []}

            if elem_type == "ventilation":
                converted_elem = {
                    "id": elem.get("id", f'Vent_{len(components[comp_key]["elements"]) + 1}'),
                    "air_changes": extract_value(elem.get("air_changes", 0.5)),
                }
            else:
                converted_elem = {
                    "id": elem.get("id", f'{comp_key}_{len(components[comp_key]["elements"]) + 1}'),
                    "area": extract_value(elem.get("area", 0)),
                    "azimuth": extract_value(elem.get("azimuth", 0)),
                    "tilt": extract_value(elem.get("tilt", 0)),
                }

                # U-value (per-element)
                if "U" in elem:
                    converted_elem["U"] = extract_value(elem["U"])

                # b_transmission
                if "b_transmission" in elem:
                    converted_elem["b_transmission"] = extract_value(elem["b_transmission"])

                # Window-specific: g_gl, parent_id->surface
                if elem_type == "window":
                    if "g_gl" in elem:
                        components[comp_key].setdefault("g_gl", extract_value(elem["g_gl"]))
                    if "parent_id" in elem:
                        converted_elem["surface"] = elem["parent_id"]

                # Door-specific: parent_id->surface
                if elem_type == "door" and "parent_id" in elem:
                    converted_elem["surface"] = elem["parent_id"]

            components[comp_key]["elements"].append(converted_elem)

        # Set component-level U-values from first element if all share the same value
        for comp_key, comp_data in components.items():
            if comp_key == "Ventilation":
                continue
            elems = comp_data["elements"]
            u_values = [e.get("U") for e in elems if "U" in e]
            if u_values and len(u_values) == len(elems) and len(set(u_values)) == 1:
                comp_data["U"] = u_values[0]
                for e in elems:
                    e.pop("U", None)

                # Once U is component-level, model_buem.py's conductance calc
                # reads b_transmission from the component only -- its
                # per-element b_transmission is reachable solely through the
                # no-component-U branch. Promote it the same way, or a
                # non-default per-element value (e.g. TABULA's 0.5 for
                # ground-contact floors) is silently dropped.
                b_values = [e.get("b_transmission", 1.0) for e in elems]
                if len(set(b_values)) == 1:
                    comp_data["b_transmission"] = b_values[0]
                    for e in elems:
                        e.pop("b_transmission", None)

        # Ensure all required component types exist (validator expects all five)
        _default_U = {"Walls": 1.0, "Windows": 2.8, "Roof": 1.0, "Floor": 1.0, "Doors": 3.0}
        for required in ("Walls", "Windows", "Roof", "Floor", "Doors"):
            if required not in components:
                components[required] = {"U": _default_U[required], "elements": []}

        # Extract thermal parameters
        n_air_infiltration = extract_value(thermal.get("n_air_infiltration", 0.5))
        n_air_use = extract_value(thermal.get("n_air_use", 0.5))
        comfortT_lb = extract_value(thermal.get("comfortT_lb", 21))
        comfortT_ub = extract_value(thermal.get("comfortT_ub", 24))
        c_m = extract_value(thermal.get("c_m", 165.0))
        design_T_min = extract_value(thermal.get("design_T_min", -12.0))
        F_sh_hor = extract_value(thermal.get("F_sh_hor", 0.8))
        F_sh_vert = extract_value(thermal.get("F_sh_vert", 0.75))
        F_f = extract_value(thermal.get("F_f", 0.2))
        F_w = extract_value(thermal.get("F_w", 1.0))

        # Build internal building_attributes
        building_attributes = {
            "latitude": latitude,
            "longitude": longitude,
            "A_ref": A_ref,
            "h_room": h_room,
            "components": components,
            # Thermal parameters
            "n_air_infiltration": n_air_infiltration,
            "n_air_use": n_air_use,
            "comfortT_lb": comfortT_lb,
            "comfortT_ub": comfortT_ub,
            "c_m": c_m,
            "design_T_min": design_T_min,
            "F_sh_hor": F_sh_hor,
            "F_sh_vert": F_sh_vert,
            "F_f": F_f,
            "F_w": F_w,
        }

        # Optional thermal parameters (pass through when present)
        for thermal_key in ("phi_int", "q_w_nd", "F_red_htr"):
            if thermal_key in thermal:
                building_attributes[thermal_key] = extract_value(thermal[thermal_key])

        # thermalClass
        tc = thermal.get("thermal_class")
        if tc:
            building_attributes["thermalClass"] = tc

        # g_gl_n_Window: extract from Windows component g_gl if set
        win_comp = components.get("Windows", {})
        if "g_gl" in win_comp:
            building_attributes["g_gl_n_Window"] = win_comp["g_gl"]

        # Optional building metadata. capacity/num_persons/archetype/
        # residential_units forward occupancy generation inputs --
        # AttributeBuilder.generate_electricity_profile() reads them from
        # merged_attrs and does its own numeric casts, so a plain
        # passthrough is sufficient here. seed is deliberately NOT
        # forwarded: it is an internal reproducibility knob, not part of
        # the EnerPlanET request contract. building_type is intentionally
        # not enum-checked here -- the pinned contract leaves it free
        # text; an unrecognised value surfaces as a clear ValueError from
        # AttributeBuilder.generate_electricity_profile() when the model
        # actually needs to resolve an occupancy profile from it.
        for key in ("building_type", "construction_period", "country", "n_storeys",
                    "neighbour_status", "attic_condition", "cellar_condition",
                    "capacity", "num_persons", "residential_units", "archetype",
                    "equipment", "window_to_wall_ratio", "cooking_carrier",
                    "include_dhw", "region_code", "setback_profile"):
            if key in building:
                building_attributes[key] = building[key]

        # Weather source metadata (buem.weather.provider/year) -- passed
        # through for record-keeping; the pinned contract requires
        # buem.weather.index/variables on every request (weather is
        # resolved by the caller, never by buem on this path -- see
        # enerplanet/buem#10), so these are informational only and are
        # not used to select an archive here.
        weather_block = buem_data.get("weather") or {}
        resolved_year = None
        if isinstance(weather_block, dict) and weather_block.get("year") is not None:
            resolved_year = int(weather_block["year"])
            building_attributes["year"] = resolved_year
        if isinstance(weather_block, dict) and weather_block.get("provider"):
            building_attributes["weather_provider"] = weather_block["provider"]

        # Caller-supplied inline weather timeseries (buem.weather.index +
        # .variables) -- the shape weather serve's GET .../point?format=json
        # returns, and required by the pinned contract on every request.
        inline_weather = self._weather_from_payload(weather_block if isinstance(weather_block, dict) else None)
        if inline_weather is not None:
            building_attributes["weather"] = inline_weather
            building_attributes["use_provided_weather"] = True

        # Caller-supplied weather profile file (buem.weather.profile) --
        # overrides the inline index/variables when present. Not part of
        # the pinned contract; a buem-side extension for local/offline use.
        weather_profile = weather_block.get("profile") if isinstance(weather_block, dict) else None
        if isinstance(weather_profile, dict) and weather_profile.get("path"):
            weather_df = load_weather_profile(
                weather_profile["path"], weather_profile.get("format", "json"),
            )
            self._check_weather_profile_ranges(weather_df, result, feature_idx)
            building_attributes["weather"] = weather_df
            building_attributes["use_provided_weather"] = True

        # Caller-supplied electricity load profile file
        # (buem.inputs.electricity_load_profile) -- a flat hourly array
        # with no timestamp column, indexed here against the resolved
        # year above (or AttributeBuilder's own DEFAULT_YEAR if none was
        # resolved). A length that doesn't match the real weather index is
        # not checked here -- AttributeBuilder._reindex_or_raise() catches
        # a genuine mismatch downstream with a clear error, once the real
        # weather index is known.
        inputs_block = buem_data.get("inputs")
        elec_profile = inputs_block.get("electricity_load_profile") if isinstance(inputs_block, dict) else None
        if isinstance(elec_profile, dict) and elec_profile.get("path"):
            values = load_electricity_load_values(
                elec_profile["path"], elec_profile.get("unit", "kWh"),
            )
            index = pd.date_range(
                f"{resolved_year or DEFAULT_YEAR}-01-01", periods=len(values), freq="h",
            )
            building_attributes["elecLoad"] = pd.Series(values, index=index, name="elecLoad")
            building_attributes["use_provided_elecLoad"] = True

        # Extract solver settings
        solver = buem_data.get("solver", {})
        use_milp = solver.get("use_milp", False)

        # Replace buem section with the internal format
        buem_data["building_attributes"] = building_attributes
        buem_data["use_milp"] = use_milp

        result.add_issue(
            ValidationLevel.INFO,
            "Converted request (building.envelope) to internal format (building_attributes.components)",
            f"features[{feature_idx}].properties.buem",
        )

    @staticmethod
    def _check_weather_profile_ranges(
        weather_df: pd.DataFrame, result: ValidationResult, feature_idx: int,
    ):
        """Range-check a caller-supplied weather profile.

        Runs at the request boundary, where a client can still correct
        the input, rather than inside the thermal model -- which consumes
        weather exactly as given and never masks or adjusts it. Issues
        are warnings, not errors: a real but extreme climate must remain
        simulable, so this surfaces likely unit mix-ups and corrupt data
        without blocking the request. See
        ``WEATHER_PROFILE_PLAUSIBLE_RANGES``.
        """
        path = f"features[{feature_idx}].properties.buem.weather.profile"
        for column, (low, high) in WEATHER_PROFILE_PLAUSIBLE_RANGES.items():
            if column not in weather_df.columns:
                continue
            series = weather_df[column]
            observed_min, observed_max = float(series.min()), float(series.max())
            if observed_min < low or observed_max > high:
                result.add_issue(
                    ValidationLevel.WARNING,
                    f"weather.profile column {column!r} spans "
                    f"{observed_min:.1f} to {observed_max:.1f}, outside the "
                    f"plausible range {low} to {high}",
                    path,
                    suggestion=(
                        f"Check {column!r}'s units and source data. "
                        "The profile is used exactly as supplied -- buem does "
                        "not clip or adjust weather values."
                    ),
                )

    @staticmethod
    def _weather_from_payload(weather_block: dict[str, Any] | None) -> pd.DataFrame | None:
        """Convert a caller-supplied buem.weather block's inline index/variables
        into a DataFrame (DatetimeIndex, columns among T/GHI/DNI/DHI).

        Shape matches weather serve's GET .../point?format=json response:
        {"index": [ISO 8601 strings], "variables": {"T": [...], "GHI": [...],
        "DNI": [...], "DHI": [...]}}. Returns None if weather_block is falsy,
        has no "index", or has no recognized column under "variables".
        """
        if not weather_block or "index" not in weather_block:
            return None
        variables = weather_block.get("variables", {})
        cols = {c: variables[c] for c in ("T", "GHI", "DNI", "DHI") if c in variables}
        if not cols:
            return None
        return pd.DataFrame(cols, index=pd.to_datetime(weather_block["index"]))


def validate_geojson_request(payload: dict[str, Any], strict_mode: bool = False) -> ValidationResult:
    """
    Convenience function to validate a GeoJSON request against the pinned
    contract schema and convert it into the model's internal
    representation.

    Parameters
    ----------
    payload : Dict[str, Any]
        GeoJSON payload to validate.
    strict_mode : bool
        Treat warnings as errors.

    Returns
    -------
    ValidationResult
        Validation results with detailed error reporting.
    """
    validator = GeoJsonValidator(strict_mode=strict_mode)
    return validator.validate(payload)


def create_validation_report(result: ValidationResult) -> str:
    """
    Create a detailed validation report.

    Parameters
    ----------
    result : ValidationResult
        Validation result to report.

    Returns
    -------
    str
        Formatted validation report.
    """
    report = ["=== VALIDATION REPORT ==="]
    report.append(f"Status: {result.summary()}")
    report.append("")

    if result.get_errors():
        report.append("ERRORS:")
        for issue in result.get_errors():
            report.append(f"  ❌ {issue.path}: {issue.message}")
            if issue.suggestion:
                report.append(f"     \U0001f4a1 Suggestion: {issue.suggestion}")
        report.append("")

    if result.get_warnings():
        report.append("WARNINGS:")
        for issue in result.get_warnings():
            report.append(f"  ⚠️  {issue.path}: {issue.message}")
            if issue.suggestion:
                report.append(f"     \U0001f4a1 Suggestion: {issue.suggestion}")
        report.append("")

    info_issues = [i for i in result.issues if i.level == ValidationLevel.INFO]
    if info_issues:
        report.append("INFO:")
        for issue in info_issues:
            report.append(f"  ℹ️  {issue.path}: {issue.message}")
        report.append("")

    return "\n".join(report)
