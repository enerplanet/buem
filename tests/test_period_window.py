"""An occupancy-only request returns the period properties.start_time to
properties.end_time, start inclusive and end exclusive."""
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from buem.integration.scripts.geojson_processor import PROFILES, GeoJsonProcessor
from buem.integration.scripts.geojson_validator import validate_geojson_request

_DUMMY = (
    Path(__file__).resolve().parent.parent
    / "src" / "buem" / "data" / "buildings" / "dummy" / "building_01_small_residential.json"
)
OCCUPANCY_ONLY = {**dict.fromkeys(PROFILES, "series"), "heating": "none", "cooling": "none"}


def _occupancy_only_feature(start_time: str, end_time: str):
    payload = json.loads(_DUMMY.read_text(encoding="utf-8"))
    props = payload["features"][0]["properties"]
    props["start_time"], props["end_time"] = start_time, end_time
    result = validate_geojson_request(payload)
    assert result.is_valid, [str(e) for e in result.get_errors()]
    feature = copy.deepcopy(result.validated_data["features"][0])
    attrs = feature["properties"]["buem"]["building_attributes"]
    for key in ("weather", "use_provided_weather", "components"):
        attrs.pop(key, None)
    feature["properties"]["buem"]["outputs"] = OCCUPANCY_ONLY
    return feature, result


def test_week_returns_168_hours(monkeypatch):
    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")
    feature, result = _occupancy_only_feature("2018-01-01T00:00:00Z", "2018-01-08T00:00:00Z")
    out = GeoJsonProcessor(payload={})._process_single_feature(feature, result)
    profile = out["properties"]["buem"]["thermal_load_profile"]
    stamps = profile["timeseries"]["timestamps"]
    assert len(stamps) == 168
    assert stamps[0] == "2018-01-01T00:00:00+00:00"
    assert stamps[-1] == "2018-01-07T23:00:00+00:00"
    assert profile["start_time"] == stamps[0] and profile["end_time"] == stamps[-1]
    electricity = profile["timeseries"]["electricity"]
    assert len(electricity) == 168
    assert profile["summary"]["electricity"]["total"]["value"] == pytest.approx(sum(electricity))


def test_period_outside_the_simulated_year_is_rejected(monkeypatch):
    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")
    feature, result = _occupancy_only_feature("2018-12-01T00:00:00Z", "2019-02-01T00:00:00Z")
    with pytest.raises(ValueError, match="outside the simulated hours"):
        GeoJsonProcessor(payload={})._process_single_feature(feature, result)


def test_window_reads_zoneless_stamps_as_utc_and_keeps_short_series():
    index = pd.date_range("2018-01-01", periods=48, freq="h", tz="UTC")
    profiles = {"electricity": np.arange(48.0), "heating": []}
    times, kept = GeoJsonProcessor._window(index, profiles, "2018-01-02T00:00:00", None)
    assert len(times) == 24 and times[0] == pd.Timestamp("2018-01-02", tz="UTC")
    assert kept["electricity"].tolist() == list(range(24, 48))
    assert kept["heating"] == []
