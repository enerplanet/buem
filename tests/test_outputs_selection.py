"""``buem.outputs``: per-profile none/summary/series selection, and the
occupancy-only path taken when neither heating nor cooling is selected."""
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from buem.integration.scripts.attribute_builder import AttributeBuilder, _reindex_or_raise
from buem.integration.scripts.geojson_processor import PROFILES, GeoJsonProcessor, resolve_outputs
from buem.integration.scripts.geojson_validator import validate_geojson_request

_DUMMY = (
    Path(__file__).resolve().parent.parent
    / "src" / "buem" / "data" / "buildings" / "dummy" / "building_01_small_residential.json"
)
SUMMARY = dict.fromkeys(PROFILES, "summary")
SERIES = dict.fromkeys(PROFILES, "series")
OCCUPANCY_ONLY = {**SERIES, "heating": "none", "cooling": "none"}


def _payload() -> dict:
    """The residential dummy (8760-stamp inline weather) with gas cooking,
    so kitchen is reported."""
    payload = json.loads(_DUMMY.read_text(encoding="utf-8"))
    payload["features"][0]["properties"]["buem"]["building"]["cooking_carrier"] = "gas"
    return payload


def _validated_feature(payload: dict):
    result = validate_geojson_request(payload)
    assert result.is_valid, [str(e) for e in result.get_errors()]
    return result.validated_data["features"][0], result


def _strip_thermal_inputs(feature: dict) -> dict:
    """The validated feature without weather and envelope, selecting no
    thermal output. Bypasses the pinned contract's structural check, which
    still requires both until the pin moves to v6-draft."""
    stripped = copy.deepcopy(feature)
    attrs = stripped["properties"]["buem"]["building_attributes"]
    for key in ("weather", "use_provided_weather", "components"):
        attrs.pop(key, None)
    stripped["properties"]["buem"]["outputs"] = OCCUPANCY_ONLY
    return stripped


def test_resolve_outputs_defaults_and_precedence():
    assert resolve_outputs({}, include_timeseries=False) == SUMMARY
    assert resolve_outputs({}, include_timeseries=True) == SERIES
    selection = resolve_outputs({"outputs": {"heating": "none"}}, include_timeseries=True)
    assert selection["heating"] == "none"
    assert selection["electricity"] == "summary"
    with pytest.raises(ValueError, match="outputs.kitchen"):
        resolve_outputs({"outputs": {"kitchen": "all"}}, include_timeseries=False)


def test_none_profiles_left_out_and_arrays_only_for_series():
    times = pd.date_range("2018-01-01", periods=2, freq="h")
    profiles = dict.fromkeys(PROFILES, np.array([1.0, 1.0]))
    selection = {"heating": "series", "cooling": "none", "electricity": "summary",
                 "hot_water": "none", "kitchen": "series"}
    profile = GeoJsonProcessor(payload={})._build_thermal_load_profile(
        times, profiles, selection, 0.0, None, None, "60", "minutes",
    )
    assert set(profile["summary"]) == {"heating", "electricity", "kitchen", "peak_heating_load"}
    assert set(profile["timeseries"]) == {"unit", "kitchen_unit", "timestamps", "heating", "kitchen"}


def test_total_energy_demand_only_when_all_four_summands_selected():
    times = pd.date_range("2018-01-01", periods=2, freq="h")
    profiles = dict.fromkeys(PROFILES, np.array([1.0, 1.0]))
    proc = GeoJsonProcessor(payload={})
    everything = proc._build_thermal_load_profile(
        times, profiles, SUMMARY, 0.0, None, None, "60", "minutes", a_ref=100.0,
    )
    assert everything["summary"]["total_energy_demand"]["value"] == pytest.approx(8.0)  # kitchen excluded
    assert everything["summary"]["energy_intensity"] == {"value": pytest.approx(0.08), "unit": "kWh/m2"}
    without_electricity = proc._build_thermal_load_profile(
        times, profiles, {**SUMMARY, "electricity": "none"}, 0.0, None, None, "60", "minutes", a_ref=100.0,
    )
    assert "total_energy_demand" not in without_electricity["summary"]
    assert "energy_intensity" not in without_electricity["summary"]


def test_resolved_inputs_per_building_type(monkeypatch):
    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")
    residential, _ = _validated_feature(_payload())
    builder = AttributeBuilder(
        payload_attrs=_strip_thermal_inputs(residential)["properties"]["buem"]["building_attributes"],
    )
    builder.build(thermal=False)
    assert builder.resolved_inputs["building_type"] == "SFH"
    assert builder.resolved_inputs["country"] == "NL"
    assert builder.resolved_inputs["region_code"] is None
    assert builder.resolved_inputs["num_persons"] > 1.0
    assert builder.resolved_inputs["residential_units"] == 1.0
    assert builder.resolved_inputs["archetype"] == "family_with_children"
    assert builder.resolved_inputs["capacity"] is None

    office = json.loads((_DUMMY.parent / "building_02_medium_office.json").read_text(encoding="utf-8"))
    feature, _ = _validated_feature(office)
    builder = AttributeBuilder(
        payload_attrs=_strip_thermal_inputs(feature)["properties"]["buem"]["building_attributes"],
    )
    builder.build(thermal=False)
    assert builder.resolved_inputs["building_type"] == "office"
    assert builder.resolved_inputs["num_persons"] is None
    assert builder.resolved_inputs["archetype"] is None
    assert builder.resolved_inputs["capacity"] == 17  # 250 m2 at 15 m2 per occupant


def test_half_hour_weather_index_shifts_the_full_run_by_one_boundary_hour():
    """The documented limit of the occupancy-only identity: on an hh:30
    weather index the full run's nearest-hour alignment resolves the exact
    tie to the later hour, so the annual total moves by last hour minus
    first hour."""
    hourly = pd.Series(np.arange(24, dtype=float), index=pd.date_range("2018-01-01 00:00", periods=24, freq="h"))
    half_hour = pd.date_range("2018-01-01 00:30", periods=24, freq="h")
    aligned = _reindex_or_raise(hourly, half_hour, "elecLoad")
    assert aligned.iloc[0] == hourly.iloc[1]
    assert aligned.iloc[-1] == hourly.iloc[-1]
    assert aligned.sum() - hourly.sum() == hourly.iloc[-1] - hourly.iloc[0]


def test_occupancy_only_profiles_follow_the_request_year(monkeypatch):
    feature, _ = _validated_feature(_payload())
    attrs = _strip_thermal_inputs(feature)["properties"]["buem"]["building_attributes"]
    attrs["year"] = 2019
    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")  # a fetch would raise
    merged = AttributeBuilder(payload_attrs=attrs).build(thermal=False)
    assert merged["year"] == 2019
    assert merged["elecLoad"].index[0].year == 2019
    assert merged["weather"] is None


def test_occupancy_only_matches_full_run(monkeypatch):
    """Heating and cooling both none: no weather, no envelope, no solve, and
    electricity, hot_water and kitchen equal to a full run on an N-stamp
    weather index."""
    feature, result = _validated_feature(_payload())
    full = GeoJsonProcessor(payload={}, include_timeseries=True)._process_single_feature(
        copy.deepcopy(feature), result,
    )
    full_profile = full["properties"]["buem"]["thermal_load_profile"]
    full_ts = full_profile["timeseries"]
    assert full_profile["summary"]["energy_intensity"]["value"] == pytest.approx(
        full_profile["summary"]["total_energy_demand"]["value"] / 80.0  # the dummy's A_ref
    )

    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")  # a fetch would raise
    occ = GeoJsonProcessor(payload={})._process_single_feature(_strip_thermal_inputs(feature), result)
    occ_profile = occ["properties"]["buem"]["thermal_load_profile"]

    assert occ["properties"]["buem"]["model_metadata"]["solver_used"] == "none (occupancy only)"
    assert occ["properties"]["buem"]["model_metadata"]["resolved_inputs"] == full["properties"]["buem"]["model_metadata"]["resolved_inputs"]
    assert "energy_intensity" not in occ_profile["summary"]
    assert set(occ_profile["summary"]) == {"electricity", "hot_water", "kitchen"}
    assert set(occ_profile["timeseries"]) == {"unit", "kitchen_unit", "timestamps", "electricity", "hot_water", "kitchen"}
    assert occ_profile["timeseries"]["timestamps"] == full_ts["timestamps"]
    for name in ("electricity", "hot_water", "kitchen"):
        assert occ_profile["timeseries"][name] == full_ts[name], name
        assert occ_profile["summary"][name]["total"]["value"] > 0, name


def test_occupancy_only_request_through_process():
    payload = _payload()
    buem = payload["features"][0]["properties"]["buem"]
    buem.pop("weather")
    buem["building"].pop("envelope")
    buem["outputs"] = OCCUPANCY_ONLY
    doc = GeoJsonProcessor(payload).process()
    assert "error" not in doc["features"][0]["properties"]["buem"]
