"""
Tests for geojson_validator.py's request -> internal-format conversion,
and the domain checks that survive validation against the pinned
contract schema (json_schema/request_schema.json): the solver.compute_cooling
guard (a contract-defined field this model doesn't implement yet) and
file-based buem.inputs.electricity_load_profile / buem.weather.profile
loading.

building_type and weather.year/provider are NOT enum/range-checked here
any more -- the pinned contract leaves building_type free text and treats
weather.year/provider as informational metadata, so buem no longer
duplicates constraints the contract doesn't have. See
json_schema/README.md.
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from buem.config.cfg_building import CfgBuilding
from buem.integration.scripts.attribute_builder import AttributeBuilder
from buem.integration.scripts.geojson_validator import validate_geojson_request
from buem.thermal.model_buem import ModelBUEM

project_root = Path(__file__).resolve().parent.parent
DUMMY_DIR = project_root / "src" / "buem" / "data" / "buildings" / "dummy"


def _load_payload(fixture_name: str = "building_01_small_residential.json") -> dict:
    """Load a dummy fixture -- already schema-valid, including a full-year
    inline weather block."""
    return json.loads((DUMMY_DIR / fixture_name).read_text(encoding="utf-8"))


def _building_attrs(payload: dict) -> dict:
    result = validate_geojson_request(payload)
    assert result.is_valid, [str(e) for e in result.get_errors()]
    return result.validated_data["features"][0]["properties"]["buem"]["building_attributes"]


# ── building_type: free text, not enum-checked ───────────────────────────


def test_valid_residential_building_type_passes():
    payload = _load_payload()
    assert payload["features"][0]["properties"]["buem"]["building"]["building_type"] == "SFH"
    attrs = _building_attrs(payload)
    assert attrs["building_type"] == "SFH"


def test_valid_service_building_type_passes():
    payload = _load_payload("building_02_medium_office.json")
    assert payload["features"][0]["properties"]["buem"]["building"]["building_type"] == "office"
    attrs = _building_attrs(payload)
    assert attrs["building_type"] == "office"


def test_unrecognised_building_type_passes_validation_fails_later():
    """The pinned contract leaves building_type free text -- an
    unrecognised value passes request validation (nothing here to reject
    it) and only fails once AttributeBuilder tries to resolve an
    occupancy profile from it."""
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["building"]["building_type"] = "not_a_real_type"
    attrs = _building_attrs(payload)
    assert attrs["building_type"] == "not_a_real_type"

    # generate_electricity_profile() wraps the underlying ValueError in a
    # RuntimeError (see attribute_builder.py) -- the message still names
    # the offending value.
    with pytest.raises(RuntimeError, match="not_a_real_type"):
        AttributeBuilder(payload_attrs=attrs).build()


def test_missing_building_type_still_passes():
    """building_type stays optional -- absence is not an error;
    AttributeBuilder's own default applies."""
    payload = _load_payload()
    del payload["features"][0]["properties"]["buem"]["building"]["building_type"]
    attrs = _building_attrs(payload)
    assert "building_type" not in attrs


# ── buem.weather: index/variables required, provider/year forwarded as metadata ──


def test_weather_provider_and_year_forwarded_as_metadata():
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["weather"]["provider"] = "cosmo-rea6"
    payload["features"][0]["properties"]["buem"]["weather"]["year"] = 2018
    attrs = _building_attrs(payload)
    assert attrs["weather_provider"] == "cosmo-rea6"
    assert attrs["year"] == 2018
    # metadata alongside the real thing -- the inline timeseries is what's used
    assert attrs["use_provided_weather"] is True
    assert isinstance(attrs["weather"], pd.DataFrame)


def test_weather_without_provider_or_year_forwards_nothing():
    payload = _load_payload()
    attrs = _building_attrs(payload)
    assert "weather_provider" not in attrs
    assert "year" not in attrs
    assert attrs["use_provided_weather"] is True


def test_weather_missing_is_rejected():
    """weather.index/variables are required by the pinned contract on
    every request -- omitting weather entirely fails validation, not a
    later self-fetch."""
    payload = _load_payload()
    del payload["features"][0]["properties"]["buem"]["weather"]
    result = validate_geojson_request(payload)
    assert not result.is_valid
    assert any("weather" in str(e.message) for e in result.get_errors())


# ── component-level U/b_transmission promotion ───────────────────────────
#
# model_buem.py's conductance calc reads b_transmission from the component
# only once U is component-level (its per-element branch is unreachable
# once a component U exists) -- so uniform per-element b_transmission must
# be promoted the same way U is, or it is silently dropped.


def test_uniform_b_transmission_promoted_alongside_uniform_u():
    """building_01's Floor has one element (trivially uniform U and
    b_transmission=0.5, TABULA's documented ground-contact-floor default)
    -- both must end up component-level."""
    payload = _load_payload()
    attrs = _building_attrs(payload)
    floor = attrs["components"]["Floor"]
    assert floor["U"] == 1.7
    assert floor["b_transmission"] == 0.5
    assert "b_transmission" not in floor["elements"][0]


def test_nonuniform_b_transmission_left_per_element():
    """Walls share one U (promoted) but get a non-uniform b_transmission --
    it must NOT be promoted (that would silently discard the per-element
    difference), so it stays on each element."""
    payload = _load_payload()
    elements = payload["features"][0]["properties"]["buem"]["building"]["envelope"]["elements"]
    walls = [e for e in elements if e["type"] == "wall"]
    walls[0]["b_transmission"] = {"value": 0.5, "unit": "-"}
    attrs = _building_attrs(payload)
    wall_comp = attrs["components"]["Walls"]
    assert "b_transmission" not in wall_comp
    b_values = {e.get("b_transmission", 1.0) for e in wall_comp["elements"]}
    assert b_values == {0.5, 1.0}


# ── cooking_carrier/include_dhw forwarding ───────────────────────────────
#
# AttributeBuilder defaults cooking_carrier to "electric" (model_buem.py
# only reports gas cooking energy when it's "gas") -- without this, no
# request could ever reach the "gas" branch.


def test_cooking_carrier_and_include_dhw_forwarded():
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["building"]["cooking_carrier"] = "gas"
    payload["features"][0]["properties"]["buem"]["building"]["include_dhw"] = False
    attrs = _building_attrs(payload)
    assert attrs["cooking_carrier"] == "gas"
    assert attrs["include_dhw"] is False


def test_cooking_carrier_omitted_leaves_default_to_attribute_builder():
    payload = _load_payload()
    attrs = _building_attrs(payload)
    assert "cooking_carrier" not in attrs
    assert "include_dhw" not in attrs


# ── region_code/setback_profile forwarding ───────────────────────────────
#
# Both are real ATTRIBUTE_SPECS attributes (num_persons CSV lookup and
# ISO 13790 s13 setback respectively) but were missing from the
# building.* forwarding allowlist -- the same gap cooking_carrier had.


def test_region_code_and_setback_profile_forwarded():
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["building"]["region_code"] = "GM0200"
    payload["features"][0]["properties"]["buem"]["building"]["setback_profile"] = "night_only"
    attrs = _building_attrs(payload)
    assert attrs["region_code"] == "GM0200"
    assert attrs["setback_profile"] == "night_only"


def test_region_code_and_setback_profile_omitted_leave_default():
    payload = _load_payload()
    attrs = _building_attrs(payload)
    assert "region_code" not in attrs
    assert "setback_profile" not in attrs


# ── building.equipment forwarding ────────────────────────────────────────


def test_equipment_forwarded():
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["building"]["equipment"] = {
        "oven": True, "dish_washer": False,
    }
    attrs = _building_attrs(payload)
    assert attrs["equipment"] == {"oven": True, "dish_washer": False}


# ── solver.compute_cooling: contract-defined, not implemented here ──────


def test_compute_cooling_true_rejected():
    """The pinned contract defines real conditional-cooling semantics for
    this flag; ModelBUEM doesn't implement them (main.py::run_model
    accepts the argument but nothing reads cfg["compute_cooling"], and
    geojson_processor.py never passes it through -- heating and cooling
    are always computed and returned regardless). Rejecting a true value
    keeps that gap loud instead of silently returning a response that
    doesn't match what was requested."""
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["solver"] = {"compute_cooling": True}
    result = validate_geojson_request(payload)
    assert not result.is_valid
    assert any("compute_cooling" in str(e.message) for e in result.get_errors())


def test_compute_cooling_false_is_not_rejected():
    """Explicit false matches today's always-on behavior -- nothing to reject."""
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["solver"] = {"compute_cooling": False}
    attrs = _building_attrs(payload)
    assert attrs["building_type"] == "SFH"  # got through conversion fine


# ── file-based buem.inputs.electricity_load_profile ──────────────────────


def test_electricity_load_profile_json_file_loaded(tmp_path):
    values = [float(i % 5) for i in range(8760)]
    profile_path = tmp_path / "elec.json"
    profile_path.write_text(json.dumps(values), encoding="utf-8")

    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["inputs"] = {
        "electricity_load_profile": {"path": str(profile_path), "unit": "kWh"}
    }
    attrs = _building_attrs(payload)
    assert attrs["use_provided_elecLoad"] is True
    assert isinstance(attrs["elecLoad"], pd.Series)
    assert len(attrs["elecLoad"]) == 8760
    assert attrs["elecLoad"].iloc[3] == 3.0


def test_electricity_load_profile_wh_unit_converted(tmp_path):
    profile_path = tmp_path / "elec.json"
    profile_path.write_text(json.dumps([1000.0] * 8760), encoding="utf-8")

    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["inputs"] = {
        "electricity_load_profile": {"path": str(profile_path), "unit": "Wh"}
    }
    attrs = _building_attrs(payload)
    assert attrs["elecLoad"].iloc[0] == pytest.approx(1.0)  # 1000 Wh -> 1 kWh


def test_electricity_load_profile_missing_file_reported_as_error(tmp_path):
    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["inputs"] = {
        "electricity_load_profile": {"path": str(tmp_path / "does_not_exist.json")}
    }
    result = validate_geojson_request(payload)
    assert not result.is_valid
    assert any("Could not read" in str(e.message) for e in result.get_errors())


# ── file-based buem.weather.profile (buem-side extension, alongside the
#    contract-required inline index/variables -- profile takes over when present) ──


def test_weather_profile_csv_file_loaded(tmp_path):
    idx = pd.date_range("2018-01-01", periods=24, freq="h")
    df = pd.DataFrame({"T": 5.0, "GHI": 100.0, "DHI": 50.0, "DNI": 200.0}, index=idx)
    profile_path = tmp_path / "weather.csv"
    df.to_csv(profile_path)

    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["weather"]["profile"] = {
        "path": str(profile_path), "format": "csv",
    }
    attrs = _building_attrs(payload)
    assert attrs["use_provided_weather"] is True
    assert isinstance(attrs["weather"], pd.DataFrame)
    assert list(attrs["weather"]["T"]) == [5.0] * 24
    assert {"T", "GHI", "DHI", "DNI"} <= set(attrs["weather"].columns)


def test_weather_profile_missing_column_reported_as_error(tmp_path):
    idx = pd.date_range("2018-01-01", periods=24, freq="h")
    df = pd.DataFrame({"T": 5.0, "GHI": 100.0}, index=idx)  # missing DHI/DNI
    profile_path = tmp_path / "weather_bad.csv"
    df.to_csv(profile_path)

    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["weather"]["profile"] = {
        "path": str(profile_path), "format": "csv",
    }
    result = validate_geojson_request(payload)
    assert not result.is_valid
    assert any("missing required column" in str(e.message) for e in result.get_errors())


def test_weather_profile_json_file_loaded_default_format(tmp_path):
    """json is the default format (EnerPlanET's actual format,
    confirmed 2026-08-14) -- omitting `format` entirely must still work."""
    records = [
        {"time": f"2018-01-01T{h:02d}:00:00", "T": 5.0, "GHI": 100.0, "DHI": 50.0, "DNI": 200.0}
        for h in range(24)
    ]
    profile_path = tmp_path / "weather.json"
    profile_path.write_text(json.dumps(records), encoding="utf-8")

    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["weather"]["profile"] = {
        "path": str(profile_path)  # format omitted -> json default
    }
    attrs = _building_attrs(payload)
    assert attrs["use_provided_weather"] is True
    assert isinstance(attrs["weather"], pd.DataFrame)
    assert len(attrs["weather"]) == 24
    assert list(attrs["weather"]["GHI"]) == [100.0] * 24


def test_weather_profile_json_missing_time_key_reported_as_error(tmp_path):
    records = [{"T": 5.0, "GHI": 100.0, "DHI": 50.0, "DNI": 200.0}]  # no "time"
    profile_path = tmp_path / "weather_bad.json"
    profile_path.write_text(json.dumps(records), encoding="utf-8")

    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["weather"]["profile"] = {
        "path": str(profile_path), "format": "json",
    }
    result = validate_geojson_request(payload)
    assert not result.is_valid
    assert any("'time' key" in str(e.message) for e in result.get_errors())


# ── full end-to-end: real request -> AttributeBuilder -> ModelBUEM ───────


def test_electricity_load_profile_end_to_end(tmp_path):
    """A real request with a file-based electricity_load_profile must run
    all the way through the model, not just survive conversion --
    exercises the index-alignment path between the file's raw (unindexed)
    array and buem's actual (half-hour-offset) weather index."""
    values = [2.5] * 8760
    profile_path = tmp_path / "elec.json"
    profile_path.write_text(json.dumps(values), encoding="utf-8")

    payload = _load_payload()
    payload["features"][0]["properties"]["buem"]["inputs"] = {
        "electricity_load_profile": {"path": str(profile_path), "unit": "kWh"}
    }
    building_attrs = _building_attrs(payload)

    merged = AttributeBuilder(payload_attrs=building_attrs).build()
    assert merged["elecLoad"].nunique() == 1  # flat 2.5 supplied profile, uniformly applied
    assert merged["Q_ig"].nunique() > 1  # occupancy still ran for real

    cfg = CfgBuilding(merged).to_cfg_dict()
    model = ModelBUEM(cfg)
    model.sim_model(use_milp=False)
    assert model.heating_load is not None
    assert len(model.heating_load) == len(cfg["weather"])
