"""
Process GeoJSON payloads: extract attributes, run thermal model, return results.
"""
import gzip
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from buem.config.cfg_building import CfgBuilding
from buem.integration.scripts.attribute_builder import AttributeBuilder
from buem.integration.scripts.geojson_validator import create_validation_report, validate_geojson_request
from buem.integration.scripts.result_cache import compute_cfg_hash, get_cached_result, store_result
from buem.main import run_model
from buem.thermal import dhw_cooking

logger = logging.getLogger(__name__)

PROFILES = ("heating", "cooling", "electricity", "hot_water", "kitchen")
OUTPUT_LEVELS = ("none", "summary", "series")


def resolve_outputs(buem: dict[str, Any], include_timeseries: bool) -> dict[str, str]:
    """Output level per profile from the request's ``buem.outputs``.

    ``outputs`` wins when present, with ``summary`` for any profile it
    leaves out. Without it, ``include_timeseries`` selects ``series`` for
    every profile and ``summary`` otherwise.
    """
    outputs = buem.get("outputs")
    if not isinstance(outputs, dict):
        return dict.fromkeys(PROFILES, "series" if include_timeseries else "summary")
    selection = {}
    for name in PROFILES:
        level = outputs.get(name, "summary")
        if level not in OUTPUT_LEVELS:
            raise ValueError(f"outputs.{name} must be one of {OUTPUT_LEVELS}, got {level!r}")
        selection[name] = level
    return selection


class GeoJsonProcessor:
    """
    Process GeoJSON FeatureCollection with building energy model specifications.

    Workflow:
    1. Extract building attributes from GeoJSON feature
    2. Merge with database/defaults via AttributeBuilder
    3. Run thermal model (heating/cooling loads)
    4. Compute summary statistics
    5. Save timeseries .gz file (optional)
    6. Return results in GeoJSON format

    Parameters
    ----------
    payload : Dict[str, Any]
        GeoJSON FeatureCollection or single Feature.
    include_timeseries : bool, optional
        Include the hourly arrays inline in each feature's
        thermal_load_profile.timeseries (default: False). A feature's own
        ``buem.outputs`` takes precedence, see :func:`resolve_outputs`.
    save_timeseries_file : bool, optional
        Write the hourly arrays to a gzip JSON file under result_save_dir
        and return its download path in thermal_load_profile.timeseries_file
        (default: False). Independent of include_timeseries.
    db_fetcher : Callable, optional
        Function(building_id) -> Dict of additional attributes.
    result_save_dir : str or Path, optional
        Directory for saving .gz files (default: env BUEM_RESULTS_DIR).
    """

    def __init__(
        self,
        payload: dict[str, Any],
        include_timeseries: bool = False,
        save_timeseries_file: bool = False,
        db_fetcher: Callable[[str], dict[str, Any]] | None = None,
        result_save_dir: str | None = None,
    ):
        self.payload = payload
        self.include_timeseries = include_timeseries
        self.save_timeseries_file = save_timeseries_file
        self.db_fetcher = db_fetcher

        # Result save directory
        if result_save_dir:
            self.result_save_dir = Path(result_save_dir)
        else:
            import os
            default_dir = Path(__file__).resolve().parents[1] / "results"
            self.result_save_dir = Path(os.environ.get("BUEM_RESULTS_DIR", str(default_dir)))

    def process(self) -> dict[str, Any]:
        """
        Process all features and return GeoJSON FeatureCollection with results.

        Returns
        -------
        Dict[str, Any]
            GeoJSON FeatureCollection with thermal_load_profile added to each feature.

        Raises
        ------
        ValueError
            If payload validation fails with critical errors.
        """
        start_time = time.time()

        # Step 1: Validate payload structure and format
        validation_result = validate_geojson_request(self.payload)

        if not validation_result.is_valid:
            errors = validation_result.get_errors()
            error_msgs = [issue.message for issue in errors]
            validation_report = create_validation_report(validation_result)
            logger.error(f"Payload validation failed:\n{validation_report}")
            raise ValueError(f"Invalid GeoJSON payload: {'; '.join(error_msgs[:3])}")

        # Log validation warnings if any
        warnings = validation_result.get_warnings()
        if warnings:
            warning_msgs = [issue.message for issue in warnings]
            logger.warning(f"Validation warnings: {'; '.join(warning_msgs)}")

        # Use validated data (with any format conversions applied)
        validated_payload = validation_result.validated_data or self.payload

        # Extract features from validated payload
        if validated_payload.get("type") == "Feature":
            features = [validated_payload]
        elif validated_payload.get("type") == "FeatureCollection":
            features = validated_payload.get("features", [])
        else:
            raise ValueError("Validated payload has unexpected structure")

        # Process each feature
        out_features = []
        processing_errors = []

        for i, feat in enumerate(features):
            try:
                processed = self._process_single_feature(feat, validation_result)
                out_features.append(processed)
            except Exception as exc:
                error_msg = f"Feature {feat.get('id', f'index_{i}')} failed: {exc}"
                logger.exception(error_msg)
                processing_errors.append(error_msg)

                # Include error in feature response
                feat.setdefault("properties", {}).setdefault("buem", {})
                # Same reason as the success path's pop() below: a raise
                # partway through _process_single_feature can leave a
                # non-JSON-serializable DataFrame under building_attributes
                # (mutated in place before the exception), which would
                # otherwise crash jsonify() here and bury this error
                # response behind an unrelated TypeError.
                feat["properties"]["buem"].pop("building_attributes", None)
                feat["properties"]["buem"]["error"] = {
                    "type": "processing_error",
                    "message": str(exc),
                    "feature_id": feat.get('id'),
                    "timestamp": datetime.now(UTC).isoformat()
                }
                out_features.append(feat)

        # Build response with metadata
        response = {
            "type": "FeatureCollection",
            "features": out_features,
            "processed_at": datetime.now(UTC).isoformat(),
            "processing_elapsed_s": round(time.time() - start_time, 3),
            "metadata": {
                "total_features": len(features),
                "successful_features": len(features) - len(processing_errors),
                "failed_features": len(processing_errors),
                "validation_warnings": len(warnings)
            }
        }

        # Include validation issues in response if any
        if warnings or processing_errors:
            response["validation_report"] = {
                "warnings": [{"path": w.path, "message": w.message} for w in warnings],
                "processing_errors": processing_errors
            }

        return response

    def _process_single_feature(self, feature: dict[str, Any], validation_result) -> dict[str, Any]:
        """
        Process single GeoJSON feature: build attributes, run model, add results.

        Parameters
        ----------
        feature : Dict[str, Any]
            GeoJSON Feature with properties.buem.building_attributes.
        validation_result : ValidationResult
            Validation result from input validation.

        Returns
        -------
        Dict[str, Any]
            Feature with added thermal_load_profile in properties.buem.
        """
        props = feature.setdefault("properties", {})
        buem = props.setdefault("buem", {})
        building_id = feature.get("id")
        payload_attrs = buem.get("building_attributes", {})
        selection = resolve_outputs(buem, self.include_timeseries)
        thermal = selection["heating"] != "none" or selection["cooling"] != "none"

        # Default the weather-fetch year to the request's own simulation
        # period (already required on every request) rather than always
        # silently using ATTRIBUTE_SPECS' generic default year, unless the
        # caller explicitly supplied "year" in building_attributes.
        if "year" not in payload_attrs and props.get("start_time"):
            payload_attrs = dict(payload_attrs)
            payload_attrs["year"] = pd.Timestamp(props["start_time"]).year

        logger.info(f"Processing feature {building_id}")

        builder = AttributeBuilder(
            payload_attrs=payload_attrs,
            building_id=building_id,
            db_fetcher=self.db_fetcher,
        )
        merged_attrs = builder.build(thermal=thermal)

        start = time.time()
        if thermal:
            use_milp = bool(buem.get("use_milp", False))
            times, profiles = self._run_thermal(merged_attrs, use_milp, building_id)
            solver_used = "MILP" if use_milp else "LP (CLARABEL)"
        else:
            times, profiles = self._occupancy_only(merged_attrs)
            solver_used = "none (occupancy only)"
        elapsed = time.time() - start

        profile = self._build_thermal_load_profile(
            times, profiles, selection, elapsed,
            props.get("start_time"), props.get("end_time"),
            props.get("resolution", "60"), props.get("resolution_unit", "minutes"),
            a_ref=float(merged_attrs["A_ref"]),
        )

        # Model metadata -- a sibling of thermal_load_profile directly
        # under buem (per response_schema.json's `buem` $def and the
        # gateway's ResponseBlock struct), not nested inside profile.
        buem["model_metadata"] = {
            "model_version": "BUEM-v3.0",
            "solver_used": solver_used,
            "processing_time": {"value": round(elapsed, 3), "unit": "s"},
            "weather_year": int(merged_attrs["year"]),
            "resolved_inputs": builder.resolved_inputs,
            "validation_warnings": [w.message for w in validation_result.get_warnings()]
        }

        # The file is written only on request; the inline arrays below do
        # not depend on it.
        if self.save_timeseries_file and len(times):
            try:
                fname = self._save_timeseries(times, profiles, selection)
                profile["timeseries_file"] = f"/api/files/{fname}"
            except Exception:
                logger.exception(f"Timeseries save failed for {building_id}")

        # Attach results
        buem["thermal_load_profile"] = profile

        # building_attributes was the request's own input (validated,
        # already consumed by AttributeBuilder above) -- it isn't part of
        # the documented response shape (model_metadata + thermal_load_profile,
        # per response_schema.json's buem $def), and buem.weather.profile /
        # inline buem.weather both leave a raw, non-JSON-serializable
        # DataFrame under it that would otherwise crash jsonify() here.
        buem.pop("building_attributes", None)

        logger.info(f"Successfully processed feature {building_id} in {elapsed:.2f}s")

        return feature

    def _run_thermal(
        self, merged_attrs: dict[str, Any], use_milp: bool, building_id: Any,
    ) -> tuple[Any, dict[str, Any]]:
        """Solve the 5R1C model, or take the cached result for an identical
        cfg, and collect every reported profile."""
        cfg = CfgBuilding(merged_attrs).to_cfg_dict()
        cache_key = compute_cfg_hash(cfg)
        res = get_cached_result(cache_key)
        if res is not None:
            logger.info(f"Cache hit for feature {building_id} (key={cache_key[:12]}…)")
        else:
            res = run_model(cfg, plot=False, use_milp=use_milp)
            store_result(cache_key, res)

        elec = cfg.get("elecLoad")
        profiles = {
            "heating": res.get("heating", []),
            "cooling": res.get("cooling", []),
            "electricity": elec.values if isinstance(elec, pd.Series) else (elec or []),
            # Absent from res (like electricity) when the request carried no
            # dhw_liters/cooking_active occupancy signal -- a service
            # building, or residential input missing that detail.
            "hot_water": res.get("dhw", []),
            "kitchen": res.get("cooking_gas", []),
        }
        return res.get("times", []), profiles

    @staticmethod
    def _occupancy_only(merged_attrs: dict[str, Any]) -> tuple[pd.DatetimeIndex, dict[str, Any]]:
        """Profiles for a request that selects neither heating nor cooling:
        occupancy's electricity plus the hot-water and kitchen series a
        full run reports, without a solve. A full run places occupancy's
        hours on the weather index, which the contract delivers in UTC;
        without weather the same convention applies."""
        elec = merged_attrs["elecLoad"]
        times = elec.index if elec.index.tz is not None else elec.index.tz_localize("UTC")
        dhw = dhw_cooking.reported_dhw_kwh(merged_attrs)
        cooking = dhw_cooking.reported_cooking_gas_kwh(merged_attrs)
        profiles = {
            "heating": [],
            "cooling": [],
            "electricity": elec.values,
            "hot_water": dhw.values if dhw is not None else [],
            "kitchen": cooking.values if cooking is not None else [],
        }
        return times, profiles

    def _validate_array(self, data, array_name: str) -> np.ndarray:
        """
        Validate and sanitize numerical arrays for thermal loads.

        Parameters
        ----------
        data : Any
            Input data to be converted to array.
        array_name : str
            Name of the array for logging.

        Returns
        -------
        np.ndarray
            Validated and sanitized array.
        """
        try:
            arr = np.asarray(data, dtype=float)

            # Sanitize NaN/inf
            arr = np.nan_to_num(arr, nan=0.0, posinf=1e9, neginf=-1e9)

            # Check for remaining NaN
            nan_count = np.isnan(arr).sum()
            if nan_count > 0:
                logger.warning(f"Array {array_name}: {nan_count}/{arr.size} NaN values replaced with 0")
                arr = np.nan_to_num(arr, nan=0.0)

            return arr

        except (TypeError, ValueError) as e:
            logger.error(f"Failed to validate array {array_name}: {e}")
            return np.array([], dtype=float)

    def _build_thermal_load_profile(
        self, times, profiles: dict[str, Any], selection: dict[str, str], elapsed,
        start_time, end_time, resolution, resolution_unit, a_ref: float | None = None,
    ) -> dict[str, Any]:
        """
        Build the thermal load profile matching the response schema, with
        only the profiles ``selection`` asks for.

        Parameters
        ----------
        times : pd.DatetimeIndex or list
            Timestamps for the simulation.
        profiles : dict
            One array per name in PROFILES, in kW. heating, cooling,
            electricity and hot_water are thermal/electric energy folded
            into total_energy_demand. kitchen is kW_gas, a different fuel
            channel (see dhw_cooking.cooking_gas_energy_kwh), reported
            separately and excluded from total_energy_demand rather than
            summed with electricity/thermal kWh as if interchangeable.
        selection : dict
            Output level per profile name, see :func:`resolve_outputs`.
        elapsed : float
            Processing time in seconds.
        start_time, end_time : str
            Time range strings.
        resolution, resolution_unit : str
            Time resolution specification.
        a_ref : float, optional
            Reference floor area in m2; with total_energy_demand it
            yields summary.energy_intensity.

        Returns
        -------
        Dict[str, Any]
            Thermal load profile matching response schema.
        """
        # Handle time arrays
        has_times = False
        if isinstance(times, pd.DatetimeIndex) and not times.empty:
            has_times = True
            start_iso = times[0].isoformat()
            end_iso = times[-1].isoformat()
        elif times is not None and len(times) > 0:
            has_times = True
            if hasattr(times[0], 'isoformat'):
                start_iso = times[0].isoformat()
                end_iso = times[-1].isoformat()
            else:
                start_iso = str(times[0])
                end_iso = str(times[-1])
        else:
            start_iso = start_time or "2018-01-01T00:00:00Z"
            end_iso = end_time or "2018-12-31T23:00:00Z"

        # Calculate summary statistics -- {value, unit} measurement objects
        # matching response_schema.json's `energy_summary` $def (and the
        # gateway's LoadStats/Quantity structs).
        def safe_stats(arr, power_unit="kW", energy_unit="kWh"):
            """Calculate safe {value, unit} statistics for array."""
            if len(arr) == 0:
                zero_power = {"value": 0.0, "unit": power_unit}
                return {
                    "total": {"value": 0.0, "unit": energy_unit},
                    "max": dict(zero_power),
                    "min": dict(zero_power),
                    "mean": dict(zero_power),
                    "median": dict(zero_power),
                    "std": dict(zero_power),
                }

            return {
                "total": {"value": float(np.sum(arr)), "unit": energy_unit},
                "max": {"value": float(np.max(arr)), "unit": power_unit},
                "min": {"value": float(np.min(arr)), "unit": power_unit},
                "mean": {"value": float(np.mean(arr)), "unit": power_unit},
                "median": {"value": float(np.median(arr)), "unit": power_unit},
                "std": {"value": float(np.std(arr)), "unit": power_unit}
            }

        arrays = {name: self._validate_array(profiles.get(name, []), name) for name in PROFILES}
        # A gas channel, not thermal/electric kWh -- kept out of safe_stats'
        # default units so it is never mistaken for one in the response.
        units = {"kitchen": ("kW_gas", "kWh_gas")}

        # Field names hot_water/kitchen match buem-gateway's v6-draft schema
        # and enerplanet's own DB columns (hot_water_kwh_a/kitchen_kwh_a),
        # not buem's internal dhw/cooking_gas attribute names.
        summary: dict[str, Any] = {}
        for name in PROFILES:
            if selection[name] == "none":
                continue
            arr = np.abs(arrays[name]) if name == "cooling" else arrays[name]  # cooling reported positive
            summary[name] = safe_stats(arr, *units.get(name, ("kW", "kWh")))

        if "heating" in summary:
            summary["peak_heating_load"] = {"value": summary["heating"]["max"]["value"], "unit": "kW"}
        if "cooling" in summary:
            summary["peak_cooling_load"] = {"value": summary["cooling"]["max"]["value"], "unit": "kW"}
        # Only when every summand is selected, so the field always means
        # the same quantity. kitchen is excluded: gas is a different fuel
        # channel from electricity/thermal kWh, not another thermal load,
        # so summing it here would mix units as if they were
        # interchangeable.
        summands = ("heating", "cooling", "electricity", "hot_water")
        if all(name in summary for name in summands):
            total_energy = sum(summary[name]["total"]["value"] for name in summands)
            summary["total_energy_demand"] = {"value": total_energy, "unit": "kWh"}
            if a_ref:
                summary["energy_intensity"] = {"value": total_energy / a_ref, "unit": "kWh/m2"}

        profile = {
            "start_time": start_iso,
            "end_time": end_iso,
            "resolution": resolution,
            "resolution_unit": resolution_unit,
            "summary": summary,
        }

        series = [name for name in PROFILES if selection[name] == "series"]
        if series and has_times:
            timeseries: dict[str, Any] = {"unit": "kW"}  # applies to every series except kitchen
            if "kitchen" in series:
                timeseries["kitchen_unit"] = "kW_gas"
            timeseries["timestamps"] = (
                [t.isoformat() for t in times] if isinstance(times, pd.DatetimeIndex) else [str(t) for t in times]
            )
            for name in series:
                timeseries[name] = arrays[name].tolist()
            profile["timeseries"] = timeseries

        return profile

    def _save_timeseries(self, times, profiles: dict[str, Any], selection: dict[str, str]) -> str:
        """
        Save the selected profiles' timeseries as gzip-compressed JSON.

        Returns
        -------
        str
            Filename (e.g., 'buem_ts_abc123.json.gz').
        """
        self.result_save_dir.mkdir(parents=True, exist_ok=True)
        fname = f"buem_ts_{uuid.uuid4().hex}.json.gz"
        full_path = self.result_save_dir / fname

        file_keys = {"heating": "heat", "cooling": "cool"}
        payload: dict[str, Any] = {"index": [t.isoformat() for t in times]}
        for name in PROFILES:
            if selection[name] != "none":
                payload[file_keys.get(name, name)] = [float(x) for x in np.asarray(profiles[name], dtype=float)]

        with gzip.open(full_path, "wt", encoding="utf-8") as gz:
            json.dump(payload, gz, indent=None)

        logger.info(f"Saved timeseries: {full_path}")
        return fname
