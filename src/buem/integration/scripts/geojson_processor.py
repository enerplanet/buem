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

logger = logging.getLogger(__name__)


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
        Save hourly timeseries to .gz file (default: False).
    db_fetcher : Callable, optional
        Function(building_id) -> Dict of additional attributes.
    result_save_dir : str or Path, optional
        Directory for saving .gz files (default: env BUEM_RESULTS_DIR).
    """

    def __init__(
        self,
        payload: dict[str, Any],
        include_timeseries: bool = False,
        db_fetcher: Callable[[str], dict[str, Any]] | None = None,
        result_save_dir: str | None = None,
    ):
        self.payload = payload
        self.include_timeseries = include_timeseries
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

        # Default the weather-fetch year to the request's own simulation
        # period (already required on every request) rather than always
        # silently using ATTRIBUTE_SPECS' generic default year, unless the
        # caller explicitly supplied "year" in building_attributes.
        if "year" not in payload_attrs and props.get("start_time"):
            payload_attrs = dict(payload_attrs)
            payload_attrs["year"] = pd.Timestamp(props["start_time"]).year

        # Log feature processing start
        logger.info(f"Processing feature {building_id}")

        # Build complete attributes
        builder = AttributeBuilder(
            payload_attrs=payload_attrs,
            building_id=building_id,
            db_fetcher=self.db_fetcher,
        )
        merged_attrs = builder.build()

        # Convert to model config
        cfg = CfgBuilding(merged_attrs).to_cfg_dict()

        # Run thermal model (single-pass LP solver, CLARABEL)
        # Check result cache first — identical configs produce identical outputs.
        use_milp = bool(buem.get("use_milp", False))
        cache_key = compute_cfg_hash(cfg)
        cached = get_cached_result(cache_key)

        start = time.time()
        if cached is not None:
            res = cached
            elapsed = time.time() - start
            logger.info(f"Cache hit for feature {building_id} (key={cache_key[:12]}…)")
        else:
            res = run_model(cfg, plot=False, use_milp=use_milp)
            elapsed = time.time() - start
            store_result(cache_key, res)

        # Extract results with validation
        times = res.get("times", [])
        heating = self._validate_array(res.get("heating", []), "heating")
        cooling = self._validate_array(res.get("cooling", []), "cooling")

        # Electricity: prefer model output, else use cfg elecLoad
        if "electricity" in res:
            electricity = self._validate_array(res["electricity"], "electricity")
        else:
            elec_cfg = cfg.get("elecLoad")
            if isinstance(elec_cfg, pd.Series):
                electricity = self._validate_array(elec_cfg.values, "electricity")
            else:
                electricity = self._validate_array(elec_cfg or [], "electricity")

        # DHW/cooking_gas: absent from res (like electricity above) when the
        # request carried no dhw_liters/cooking_active occupancy signal --
        # a service building, or residential input missing that detail.
        dhw = self._validate_array(res.get("dhw", []), "dhw")
        cooking_gas = self._validate_array(res.get("cooking_gas", []), "cooking_gas")

        # Build comprehensive thermal load profile
        profile = self._build_thermal_load_profile(
            times, heating, cooling, electricity, dhw, cooking_gas, elapsed,
            props.get("start_time"), props.get("end_time"),
            props.get("resolution", "60"), props.get("resolution_unit", "minutes")
        )

        # Add model metadata -- a sibling of thermal_load_profile directly
        # under buem (per response_schema.json's `buem` $def and the
        # gateway's ResponseBlock struct), not nested inside profile.
        buem["model_metadata"] = {
            "model_version": "BUEM-v3.0",
            "solver_used": "MILP" if use_milp else "LP (CLARABEL)",
            "processing_time": {"value": round(elapsed, 3), "unit": "s"},
            "weather_year": int(getattr(cfg.get("weather", pd.DataFrame()).index, "year", [2018])[0]) if hasattr(cfg.get("weather", pd.DataFrame()).index, "year") else 2018,
            "validation_warnings": [w.message for w in validation_result.get_warnings()]
        }

        # Save timeseries if requested
        if self.include_timeseries and len(times):
            try:
                fname = self._save_timeseries(times, heating, cooling, electricity, dhw, cooking_gas)
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
        self, times, heating, cooling, electricity, dhw, cooking_gas, elapsed,
        start_time, end_time, resolution, resolution_unit
    ) -> dict[str, Any]:
        """
        Build comprehensive thermal load profile matching response schema.

        Parameters
        ----------
        times : pd.DatetimeIndex or list
            Timestamps for the simulation.
        heating, cooling, electricity, dhw : np.ndarray
            Load arrays in kW -- thermal/electric energy, folded into
            total_energy_demand.
        cooking_gas : np.ndarray
            Load array in kW_gas -- a different fuel channel (see
            dhw_cooking.cooking_gas_energy_kwh), reported separately and
            excluded from total_energy_demand rather than summed with
            electricity/thermal kWh as if interchangeable.
        elapsed : float
            Processing time in seconds.
        start_time, end_time : str
            Time range strings.
        resolution, resolution_unit : str
            Time resolution specification.

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

        heating_stats = safe_stats(heating)
        cooling_stats = safe_stats(np.abs(cooling))  # Ensure positive for cooling
        electricity_stats = safe_stats(electricity)
        dhw_stats = safe_stats(dhw)
        # A gas channel, not thermal/electric kWh -- kept out of safe_stats'
        # default units so it is never mistaken for one in the response.
        cooking_gas_stats = safe_stats(cooking_gas, power_unit="kW_gas", energy_unit="kWh_gas")

        # Calculate overall metrics. cooking_gas is excluded: gas is a
        # different fuel channel from electricity/thermal kWh, not another
        # thermal load, so summing it here would make total_energy_demand
        # mix units as if they were interchangeable.
        total_energy = (
            heating_stats["total"]["value"] + cooling_stats["total"]["value"]
            + electricity_stats["total"]["value"] + dhw_stats["total"]["value"]
        )
        peak_heating = heating_stats["max"]["value"]
        peak_cooling = cooling_stats["max"]["value"]

        # Estimate floor area for energy intensity (if available)
        energy_intensity = None
        # This would need to be calculated from building attributes if available

        profile = {
            "start_time": start_iso,
            "end_time": end_iso,
            "resolution": resolution,
            "resolution_unit": resolution_unit,
            "summary": {
                "heating": heating_stats,
                "cooling": cooling_stats,
                "electricity": electricity_stats,
                # Field names hot_water/kitchen match buem-gateway's
                # v6-draft schema and enerplanet's own DB columns
                # (hot_water_kwh_a/kitchen_kwh_a), not buem's internal
                # dhw/cooking_gas attribute names.
                "hot_water": dhw_stats,
                "kitchen": cooking_gas_stats,
                "total_energy_demand": {"value": total_energy, "unit": "kWh"},
                "peak_heating_load": {"value": peak_heating, "unit": "kW"},
                "peak_cooling_load": {"value": peak_cooling, "unit": "kW"}
            }
        }

        # Add energy intensity if floor area is available
        if energy_intensity is not None:
            profile["summary"]["energy_intensity"] = {"value": energy_intensity, "unit": "kWh/m2"}

        # Include timeseries data if specifically requested in response (not just for saving)
        if self.include_timeseries and has_times:
            profile["timeseries"] = {
                "unit": "kW",  # applies to every series below except kitchen
                "kitchen_unit": "kW_gas",
                "timestamps": [t.isoformat() for t in times] if isinstance(times, pd.DatetimeIndex) else [str(t) for t in times],
                "heating": heating.tolist(),
                "cooling": cooling.tolist(),
                "electricity": electricity.tolist(),
                "hot_water": dhw.tolist(),
                "kitchen": cooking_gas.tolist(),
            }

        return profile

    def _save_timeseries(self, times, heating, cooling, electricity, dhw, cooking_gas) -> str:
        """
        Save timeseries as gzip-compressed JSON.

        Returns
        -------
        str
            Filename (e.g., 'buem_ts_abc123.json.gz').
        """
        self.result_save_dir.mkdir(parents=True, exist_ok=True)
        fname = f"buem_ts_{uuid.uuid4().hex}.json.gz"
        full_path = self.result_save_dir / fname

        # Convert times to list of ISO strings (handles DatetimeIndex or list)
        if isinstance(times, pd.DatetimeIndex):
            time_list = [t.isoformat() for t in times]
        else:
            time_list = [t.isoformat() for t in times]

        payload = {
            "index": time_list,
            "heat": [float(x) for x in heating.tolist()],
            "cool": [float(x) for x in cooling.tolist()],
            "electricity": [float(x) for x in electricity.tolist()] if len(electricity) else [],
            "hot_water": [float(x) for x in dhw.tolist()] if len(dhw) else [],
            "kitchen": [float(x) for x in cooking_gas.tolist()] if len(cooking_gas) else [],
        }

        with gzip.open(full_path, "wt", encoding="utf-8") as gz:
            json.dump(payload, gz, indent=None)

        logger.info(f"Saved timeseries: {full_path}")
        return fname
