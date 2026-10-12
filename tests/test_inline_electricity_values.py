"""Inline electricity_load_profile values: conversion, rejection rules, the
exactly-one-of values/path rule shared with the file form, and the request
path through the pinned v6-draft contract."""
import json
import math
from pathlib import Path

import pytest

from buem.integration.scripts.geojson_validator import GeoJsonValidator, validate_geojson_request
from buem.integration.scripts.profile_file_loader import electricity_values_to_kwh

_DUMMY = Path(__file__).resolve().parent.parent / "src" / "buem" / "data" / "buildings" / "dummy"


def test_values_convert_to_kwh_per_hour():
    assert electricity_values_to_kwh([1, 2.5], "kWh", "values") == [1.0, 2.5]
    assert electricity_values_to_kwh([1, 2.5], "kW", "values") == [1.0, 2.5]
    assert electricity_values_to_kwh([1000], "Wh", "values") == [1.0]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "non-empty"),
        ("1,2,3", "non-empty"),
        ([1, "x"], "non-numeric"),
        ([1, None], "non-numeric"),
        ([1, math.nan], "non-finite"),
        ([1, math.inf], "non-finite"),
        ([1, -0.5], "negative"),
    ],
)
def test_bad_values_are_rejected(values, message):
    with pytest.raises(ValueError, match=f"electricity_load_profile values .*{message}"):
        electricity_values_to_kwh(values, "kWh", "values")


def test_exactly_one_of_values_or_path():
    with pytest.raises(ValueError, match="exactly one of values or path"):
        GeoJsonValidator._electricity_values({"unit": "kWh"})
    with pytest.raises(ValueError, match="exactly one of values or path"):
        GeoJsonValidator._electricity_values({"values": [1.0], "path": "p.csv"})
    assert GeoJsonValidator._electricity_values({"values": [2, 3], "unit": "kW"}) == [2.0, 3.0]


def _request(profile: dict) -> dict:
    payload = json.loads((_DUMMY / "building_01_small_residential.json").read_text(encoding="utf-8"))
    payload["features"][0]["properties"]["buem"].setdefault("inputs", {})["electricity_load_profile"] = profile
    return payload


def test_inline_values_become_the_provided_elec_load():
    result = validate_geojson_request(_request({"values": [0.5] * 8760, "unit": "kW"}))
    assert result.is_valid, [str(e) for e in result.get_errors()]
    attrs = result.validated_data["features"][0]["properties"]["buem"]["building_attributes"]
    assert attrs["use_provided_elecLoad"] is True
    assert len(attrs["elecLoad"]) == 8760
    assert float(attrs["elecLoad"].iloc[0]) == 0.5
    assert attrs["elecLoad"].index[0].month == 1 and attrs["elecLoad"].index[0].day == 1


@pytest.mark.parametrize(
    "profile",
    [
        {"values": [1.0] * 8760, "path": "p.csv"},
        {"unit": "kWh"},
        {"values": []},
        {"values": [1.0, -1.0]},
        {"values": [1.0] * 8760, "unit": "Wh"},
    ],
)
def test_contract_rejects_bad_inline_profiles(profile):
    result = validate_geojson_request(_request(profile))
    assert not result.is_valid
    assert any("electricity_load_profile" in f"{e}" for e in result.get_errors())
