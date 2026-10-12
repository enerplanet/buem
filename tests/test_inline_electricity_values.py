"""Inline electricity_load_profile values: conversion, rejection rules and
the exactly-one-of values/path rule, shared with the file form."""
import math

import pytest

from buem.integration.scripts.geojson_validator import GeoJsonValidator
from buem.integration.scripts.profile_file_loader import electricity_values_to_kwh


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
