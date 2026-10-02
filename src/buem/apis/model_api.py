import json
import logging
import time
import traceback

import requests
from flask import Blueprint, current_app, jsonify, request

from buem.config.cfg_building import CfgBuilding
from buem.config.validator import validate_cfg

# new import: integration processor
from buem.integration.scripts.geojson_processor import GeoJsonProcessor
from buem.main import run_model

bp = Blueprint("model_api", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)

def _to_serializable_timeseries(times_index, arr):
    return {
        "index": [ts.isoformat() for ts in list(times_index)],
        "values": [float(x) for x in list(arr)],
    }

@bp.route("/run", methods=["POST"])
def run_building_model():
    start = time.time()
    try:
        payload = request.get_json(force=True)
        include_ts = (request.args.get("include_timeseries", None) or
        (str(payload.get("include_timeseries")).lower() if payload and 
         "include_timeseries" in payload else "false")
        )
        include_ts = str(include_ts).lower() == "true"

        cfgb = CfgBuilding(json.dumps(payload))
        cfg = cfgb.to_cfg_dict()

        # run centralized validator (returns list of issues)
        issues = validate_cfg(cfg)
        if issues:
            return jsonify({"status": "error", "error": "validation_failed", "issues": issues}), 400

        # call centralized runner (allow caller to request MILP)
        use_milp = bool(payload.get("use_milp", False))
        res = run_model(cfg, plot=False, use_milp=use_milp)
        times = res["times"]
        heating = res["heating"]
        cooling = res["cooling"]

        if include_ts:
            result = {
                "heating": _to_serializable_timeseries(times, heating),
                "cooling": _to_serializable_timeseries(times, cooling),
                "meta": {"n_points": len(times), "elapsed_s": round(res.get("elapsed_s", time.time()-start), 3)},
            }
        else:
            hvals = [float(x) for x in list(heating)]
            cvals = [float(x) for x in list(cooling)]
            result = {
                "heating": {
                    "start_time": times[0].isoformat(),
                    "end_time": times[-1].isoformat(),
                    "n_points": len(times),
                    "heating_total_kWh": float(sum(hvals)) if hvals else 0.0,
                    "heating_peak_kW": float(max(hvals)) if hvals else 0.0,
                },
                "cooling": {
                    "start_time": times[0].isoformat(),
                    "end_time": times[-1].isoformat(),
                    "n_points": len(times),
                    "cooling_total_kWh": float(sum(abs(x) for x in cvals)) if cvals else 0.0,
                    "cooling_peak_kW": float(max(abs(x) for x in cvals)) if cvals else 0.0,
                },
                "meta": {"n_points": len(times), "elapsed_s": round(res.get("elapsed_s", time.time()-start), 3)},
            }

        forward_url = payload.get("forward_url")
        if forward_url:
            try:
                r = requests.post(forward_url, json=result, timeout=30)
                result["forward"] = {"status_code": r.status_code, "response_text": r.text}
            except Exception as ex:
                current_app.logger.exception("Forwarding failed")
                result["forward"] = {"error": str(ex)}

        current_app.logger.info("Model run completed, points=%d elapsed=%.3fs", len(times), result["meta"]["elapsed_s"])
        return jsonify({"status": "ok", "result": result}), 200

    except ValueError as ve:
        # validation or other expected errors -> return 400
        current_app.logger.warning("Validation error: %s", str(ve))
        return jsonify({"status": "error", "error": "validation_failed", "message": str(ve)}), 400

    except Exception as exc:
        current_app.logger.exception("API run failed")
        return jsonify({"status": "error", "error": str(exc), "trace": traceback.format_exc()}), 500

# add a unified processing route
@bp.route("/process", methods=["GET", "POST"])
def process_payload():
    """
    Single entry point that accepts either:
      - GeoJSON Feature or FeatureCollection containing properties.buem
      - A plain JSON config (the same payload accepted by /api/run)

    Behavior:
      - GeoJSON -> GeoJsonProcessor processes each feature and returns FeatureCollection.
      - Plain cfg JSON -> runs model once and returns heating/cooling summary (same shape as /run).
    Query param or payload flag:
      - include_timeseries=true to include full arrays in GeoJSON output (be careful with payload size).
      - save_timeseries_file=true to also write the arrays to a gzip JSON file under
        BUEM_RESULTS_DIR and return its /api/files path in thermal_load_profile.timeseries_file.
    """
    start = time.time()
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    # detect geojson-like
    is_geo = isinstance(payload, dict) and (
        payload.get("type") in ("Feature", "FeatureCollection")
        or "features" in payload
        or "buem" in payload
    )

    if is_geo:
        try:
            include_ts = bool(request.args.get("include_timeseries", "false").lower() == "true") or bool(payload.get("include_timeseries", False))
            save_file = bool(request.args.get("save_timeseries_file", "false").lower() == "true") or bool(payload.get("save_timeseries_file", False))
            processor = GeoJsonProcessor(payload, include_timeseries=include_ts, save_timeseries_file=save_file)
            out_doc = processor.process()
            current_app.logger.info("Processed geojson payload features=%d elapsed=%.3fs", len(out_doc.get("features", [])), time.time()-start)
            return jsonify(out_doc), 200
        except ValueError as ve:
            current_app.logger.warning("GeoJSON processing error: %s", str(ve))
            return jsonify({"status": "error", "error": "geojson_processing_failed", "message": str(ve)}), 400
        except Exception as exc:
            current_app.logger.exception("GeoJSON processing failed")
            return jsonify({"status": "error", "error": str(exc), "trace": traceback.format_exc()}), 500

    # fallback: treat as single config -> reuse /run behavior
    try:
        include_ts = bool(request.args.get("include_timeseries", "false").lower() == "true") or bool(payload.get("include_timeseries", False))
        cfgb = CfgBuilding(json.dumps(payload))
        cfg = cfgb.to_cfg_dict()

        issues = validate_cfg(cfg)
        if issues:
            return jsonify({"status": "error", "error": "validation_failed", "issues": issues}), 400

        use_milp = bool(payload.get("use_milp", False))
        res = run_model(cfg, plot=False, use_milp=use_milp)
        times = res["times"]
        heating = res["heating"]
        cooling = res["cooling"]

        if include_ts:
            result = {
                "heating": {
                    "index": [ts.isoformat() for ts in list(times)],
                    "values": [float(x) for x in list(heating)],
                },
                "cooling": {
                    "index": [ts.isoformat() for ts in list(times)],
                    "values": [float(x) for x in list(cooling)],
                },
                "meta": {"n_points": len(times), "elapsed_s": round(res.get("elapsed_s", time.time()-start), 3)},
            }
        else:
            hvals = [float(x) for x in list(heating)]
            cvals = [float(x) for x in list(cooling)]
            result = {
                "heating": {
                    "start_time": times[0].isoformat(),
                    "end_time": times[-1].isoformat(),
                    "n_points": len(times),
                    "heating_total_kWh": float(sum(hvals)) if hvals else 0.0,
                    "heating_peak_kW": float(max(hvals)) if hvals else 0.0,
                },
                "cooling": {
                    "start_time": times[0].isoformat(),
                    "end_time": times[-1].isoformat(),
                    "n_points": len(times),
                    "cooling_total_kWh": float(sum(abs(x) for x in cvals)) if cvals else 0.0,
                    "cooling_peak_kW": float(max(abs(x) for x in cvals)) if cvals else 0.0,
                },
                "meta": {"n_points": len(times), "elapsed_s": round(res.get("elapsed_s", time.time()-start), 3)},
            }

        current_app.logger.info("Processed cfg payload elapsed=%.3fs", time.time()-start)
        return jsonify({"status": "ok", "result": result}), 200

    except ValueError as ve:
        current_app.logger.warning("Validation error: %s", str(ve))
        return jsonify({"status": "error", "error": "validation_failed", "message": str(ve)}), 400
    except Exception as exc:
        current_app.logger.exception("Processing failed")
        return jsonify({"status": "error", "error": str(exc), "trace": traceback.format_exc()}), 500