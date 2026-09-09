"""
Integration smoke-test: run a residential and a service-building dummy
fixture end-to-end (GeoJSON -> AttributeBuilder -> ModelBUEM).

Deliberately calls ModelBUEM.sim_model() directly instead of
buem.main.run_model() -- this test only needs the solve itself, not
run_model()'s plotting/reporting side effects.
"""
import copy
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

project_root = Path(__file__).resolve().parent.parent
os.environ.setdefault("BUEM_WEATHER_DIR", str(project_root / "src" / "buem" / "data" / "weather"))

from buem.config.cfg_attribute import cfg as DEFAULT_CFG
from buem.config.cfg_building import CfgBuilding
from buem.integration.scripts.attribute_builder import AttributeBuilder
from buem.integration.scripts.geojson_validator import validate_geojson_request
from buem.thermal.model_buem import ModelBUEM

DUMMY_DIR = project_root / "src" / "buem" / "data" / "buildings" / "dummy"


def _load_building_attributes(fixture_name: str) -> dict:
    """Validate+convert a dummy v3 GeoJSON fixture to a flat v2 building_attributes dict."""
    payload = json.loads((DUMMY_DIR / fixture_name).read_text(encoding="utf-8"))
    result = validate_geojson_request(payload)
    assert result.is_valid, [str(e) for e in result.get_errors()]
    feature = result.validated_data["features"][0]
    return feature["properties"]["buem"]["building_attributes"]


@pytest.mark.parametrize(
    "fixture_name,expected_building_type",
    [
        ("building_01_small_residential.json", "SFH"),
        ("building_02_medium_office.json", "office"),
    ],
)
def test_dummy_fixture_runs_end_to_end(fixture_name, expected_building_type):
    """A residential (household) and a services (ServiceBuildingProfile) fixture
    both run through the full AttributeBuilder -> ModelBUEM pipeline without error."""
    building_attrs = _load_building_attributes(fixture_name)
    assert building_attrs["building_type"] == expected_building_type

    # Real per-location weather fetch (weather is a compulsory dependency;
    # requires BUEM_WEATHER_DATA_DIR / WEATHER_DATA_DIR to point at
    # processed provider archives -- see .env.example).
    merged = AttributeBuilder(payload_attrs=building_attrs).build()
    cfg = CfgBuilding(merged).to_cfg_dict()

    model = ModelBUEM(cfg)
    model.sim_model(use_milp=False)

    assert model.heating_load is not None
    assert model.cooling_load is not None
    assert len(model.heating_load) == len(cfg["weather"])
    # Sanity: no runaway negative "heating" or positive "cooling" (sign convention).
    assert (model.heating_load >= 0).all()
    assert (model.cooling_load <= 0).all()


def test_dhw_cooking_wired_for_residential_not_service():
    """occupancy v4.0.0's generate_dhw_draws()/cooking_active should flow
    all the way through AttributeBuilder -> CfgBuilding -> ModelBUEM's
    post-processed dhw_kWh/cooking_gas_kWh, for the residential fixture
    only -- occupancy's DHW model isn't wired to service buildings yet
    (see .claude/dhw_cooking_heat_handoff.md)."""
    residential_attrs = _load_building_attributes("building_01_small_residential.json")
    merged = AttributeBuilder(payload_attrs=residential_attrs).build()
    assert isinstance(merged.get("dhw_liters"), pd.Series)
    assert (merged["dhw_liters"] >= 0).all()

    cfg = CfgBuilding(merged).to_cfg_dict()
    assert isinstance(cfg.get("dhw_liters"), pd.Series)  # survives CfgBuilding's ATTRIBUTE_SPECS pass-through

    model = ModelBUEM(cfg)
    model.sim_model(use_milp=False)

    assert model.dhw_kWh is not None
    assert (model.dhw_kWh >= 0).all()
    assert model.dhw_kWh.sum() > 0  # a real household draws *some* hot water over a year
    assert "DHW Load" in model.detailedResults.columns

    if model.cooking_gas_kWh is not None:
        # cooking_active is present only when the household's equipment
        # table includes a "kitchen"-category item -- real but not
        # guaranteed for every archetype/seed, so this branch is
        # conditional rather than a hard assertion.
        assert (model.cooking_gas_kWh >= 0).all()
        assert "Cooking Gas Load" in model.detailedResults.columns

    # Service buildings: no DHW model on occupancy's side yet -- confirm
    # this stays a clean "not computed", not a silent wrong value or a crash.
    service_attrs = _load_building_attributes("building_02_medium_office.json")
    merged_service = AttributeBuilder(payload_attrs=service_attrs).build()
    assert merged_service.get("dhw_liters") is None

    cfg_service = CfgBuilding(merged_service).to_cfg_dict()
    model_service = ModelBUEM(cfg_service)
    model_service.sim_model(use_milp=False)
    assert model_service.dhw_kWh is None


def _air_gain_kw(model: ModelBUEM) -> np.ndarray:
    """Per-timestep air-node gain the solver assembled: 0.5 * Q_ia."""
    _, _, milp_meta = model._addConstraints()
    return np.asarray(milp_meta["Q_air"], dtype=float)


def test_service_internal_gains_exclude_elec_load():
    """Households: Q_ia = Q_ig + elecLoad. Service types: Q_ia = Q_ig, because
    occupancy already folds its per-type equipment and lighting gain density
    into Q_ig for them (enerplanet/buem#16)."""
    residential_attrs = _load_building_attributes("building_01_small_residential.json")
    cfg = CfgBuilding(AttributeBuilder(payload_attrs=residential_attrs).build()).to_cfg_dict()
    assert cfg["elec_load_as_gain"] is True
    model = ModelBUEM(cfg)
    model.sim_model(use_milp=False)
    expected = 0.5 * (cfg["Q_ig"].to_numpy(dtype=float) + cfg["elecLoad"].to_numpy(dtype=float))
    np.testing.assert_allclose(_air_gain_kw(model), expected, rtol=0, atol=1e-12)

    # Same envelope, service occupancy: bakery, capacity 4, A_ref 80 m2.
    service_attrs = dict(residential_attrs, building_type="bakery", capacity=4, A_ref=80.0)
    cfg_service = CfgBuilding(AttributeBuilder(payload_attrs=service_attrs).build()).to_cfg_dict()
    assert cfg_service["elec_load_as_gain"] is False
    assert cfg_service["elecLoad"].sum() > cfg_service["Q_ig"].sum()  # the term being excluded is large
    model_service = ModelBUEM(cfg_service)
    model_service.sim_model(use_milp=False)
    np.testing.assert_allclose(
        _air_gain_kw(model_service), 0.5 * cfg_service["Q_ig"].to_numpy(dtype=float), rtol=0, atol=1e-12
    )

    # Cooling with elecLoad counted twice is at least an order of magnitude higher.
    cfg_double = dict(cfg_service, elec_load_as_gain=True)
    model_double = ModelBUEM(cfg_double)
    model_double.sim_model(use_milp=False)
    cooling = float(-model_service.cooling_load.sum())
    cooling_double = float(-model_double.cooling_load.sum())
    assert cooling_double > 10 * cooling, (cooling, cooling_double)


def test_time_varying_comfort_bounds_change_heating_shape():
    """A comfortT_lb schedule with a deep night setback should shift heating
    demand away from those hours, unlike a flat scalar bound -- exercising the
    Tier-3 scalar-or-pd.Series support in ModelBUEM._init5R1C."""
    weather_index = DEFAULT_CFG["weather"].index
    hours = weather_index.hour.to_numpy()
    night_mask = (hours >= 0) & (hours < 6)

    scalar_cfg = copy.deepcopy(DEFAULT_CFG)
    model_scalar = ModelBUEM(scalar_cfg)
    model_scalar.sim_model(use_milp=False)

    # Night setback (0-6h) only: the daytime value tracks the scalar
    # baseline so the two runs differ in the setback alone, not in their
    # daytime setpoint.
    daytime_lb = float(scalar_cfg["comfortT_lb"])
    lb_series = pd.Series(np.where(night_mask, 15.0, daytime_lb), index=weather_index)
    series_cfg = copy.deepcopy(DEFAULT_CFG)
    series_cfg["comfortT_lb"] = lb_series
    model_series = ModelBUEM(series_cfg)
    model_series.sim_model(use_milp=False)

    assert not np.allclose(model_series.heating_load, model_scalar.heating_load)
    assert model_series.heating_load[night_mask].sum() <= model_scalar.heating_load[night_mask].sum()


