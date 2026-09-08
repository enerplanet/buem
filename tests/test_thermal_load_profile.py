"""GeoJsonProcessor._build_thermal_load_profile's hot_water/kitchen fields.

No weather/occupancy pipeline needed -- GeoJsonProcessor.__init__ only
stores its arguments, so these exercise the profile-shaping method
directly with synthetic arrays.
"""
import numpy as np
import pandas as pd
import pytest

from buem.integration.scripts.geojson_processor import GeoJsonProcessor


def _processor(include_timeseries: bool = False) -> GeoJsonProcessor:
    return GeoJsonProcessor(payload={}, include_timeseries=include_timeseries)


def test_hot_water_folded_in_kitchen_excluded_from_total():
    """hot_water is a fuel-agnostic heat-demand figure like heating/cooling
    -- included in total_energy_demand. kitchen is literal gas energy
    (dhw_cooking.cooking_gas_energy_kwh) -- excluded, and unit-marked so it
    is never mistaken for electricity/thermal kWh."""
    proc = _processor()
    times = pd.date_range("2018-01-01", periods=3, freq="h")
    heating = np.array([1.0, 2.0, 3.0])
    cooling = np.array([0.0, 0.0, 0.0])
    electricity = np.array([0.5, 0.5, 0.5])
    hot_water = np.array([0.2, 0.2, 0.2])
    kitchen = np.array([0.1, 0.1, 0.1])

    profile = proc._build_thermal_load_profile(
        times, heating, cooling, electricity, hot_water, kitchen, 0.01,
        None, None, "60", "minutes",
    )
    summary = profile["summary"]
    assert summary["hot_water"]["total"]["value"] == pytest.approx(0.6)
    assert summary["kitchen"]["total"]["value"] == pytest.approx(0.3)
    assert summary["kitchen"]["total"]["unit"] == "kWh_gas"
    assert summary["kitchen"]["max"]["unit"] == "kW_gas"

    expected_total = heating.sum() + np.abs(cooling).sum() + electricity.sum() + hot_water.sum()
    assert summary["total_energy_demand"]["value"] == pytest.approx(expected_total)


def test_timeseries_includes_hot_water_and_kitchen_with_gas_unit_marker():
    proc = _processor(include_timeseries=True)
    times = pd.date_range("2018-01-01", periods=2, freq="h")
    zeros = np.zeros(2)
    profile = proc._build_thermal_load_profile(
        times, zeros, zeros, zeros, np.array([1.0, 2.0]), np.array([3.0, 4.0]),
        0.01, None, None, "60", "minutes",
    )
    ts = profile["timeseries"]
    assert ts["hot_water"] == [1.0, 2.0]
    assert ts["kitchen"] == [3.0, 4.0]
    assert ts["kitchen_unit"] == "kW_gas"


def test_missing_hot_water_and_kitchen_report_as_zero():
    """Service buildings (no dhw_liters/cooking_active) get empty arrays --
    reported as zero, not omitted, matching heating/cooling/electricity's
    own safe_stats(empty) convention."""
    proc = _processor()
    times = pd.date_range("2018-01-01", periods=2, freq="h")
    zeros = np.zeros(2)
    empty = np.array([])
    profile = proc._build_thermal_load_profile(
        times, zeros, zeros, zeros, empty, empty, 0.01, None, None, "60", "minutes",
    )
    summary = profile["summary"]
    assert summary["hot_water"]["total"]["value"] == 0.0
    assert summary["kitchen"]["total"] == {"value": 0.0, "unit": "kWh_gas"}
