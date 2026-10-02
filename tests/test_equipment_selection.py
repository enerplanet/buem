"""
Tests for the new optional "equipment" (household inclusion/exclusion) and
the fixed "use_provided_elecLoad" (elecLoad override that still preserves
occupancy-generated Q_ig/occ_nothome/occ_sleeping) attributes -- see
CHANGELOG.md [Unreleased] and .claude/occupancy_module_activities.md.
"""
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from buem.config.cfg_attribute import HOUSEHOLD_EQUIPMENT_TYPES
from buem.integration.scripts.attribute_builder import AttributeBuilder
from buem.integration.scripts.geojson_validator import validate_geojson_request

project_root = Path(__file__).resolve().parent.parent
DUMMY_DIR = project_root / "src" / "buem" / "data" / "buildings" / "dummy"


def _load_building_attributes(fixture_name: str) -> dict:
    payload = json.loads((DUMMY_DIR / fixture_name).read_text(encoding="utf-8"))
    result = validate_geojson_request(payload)
    assert result.is_valid, [str(e) for e in result.get_errors()]
    feature = result.validated_data["features"][0]
    return feature["properties"]["buem"]["building_attributes"]


# ── full AttributeBuilder pipeline: wiring + error surfacing ─────────────


def test_equipment_wired_through_attribute_builder():
    building_attrs = _load_building_attributes("building_01_small_residential.json")
    attrs = dict(building_attrs)
    attrs["equipment"] = {"oven": True, "dish_washer": False}
    merged = AttributeBuilder(payload_attrs=attrs).build()
    assert "elecLoad" in merged
    assert not merged["elecLoad"].isna().any()


def test_equipment_unknown_id_raises_clear_error_through_builder():
    building_attrs = _load_building_attributes("building_01_small_residential.json")
    bad_attrs = dict(building_attrs)
    bad_attrs["equipment"] = {"not_a_real_appliance": True}
    with pytest.raises(RuntimeError, match="unrecognized id"):
        AttributeBuilder(payload_attrs=bad_attrs).build()


def test_equipment_selection_ignored_for_service_building(caplog):
    """occupancy.ServiceBuildingProfile has no per-item equipment selection
    yet -- a supplied equipment selector must be ignored with a warning, not
    raise, for a service building_type."""
    building_attrs = _load_building_attributes("building_02_medium_office.json")
    assert building_attrs["building_type"] == "office"
    attrs = dict(building_attrs)
    attrs["equipment"] = {"lighting": False}

    with caplog.at_level(logging.WARNING):
        merged = AttributeBuilder(payload_attrs=attrs).build()

    assert "elecLoad" in merged
    assert any(
        "no per-item equipment selection" in record.getMessage() for record in caplog.records
    )


def test_equipment_registry_matches_occupancy():
    """Drift guard: HOUSEHOLD_EQUIPMENT_TYPES is hand-copied from
    occupancy's households/data/equipment.json -- occupancy has no
    top-level export for this registry yet (see
    .claude/occupancy_module_activities.md item 1). Fails loudly the
    moment the two fall out of sync. (The pinned contract schema has no
    building.equipment property to drift-check against -- it was never
    part of EnerPlanET's real contract, so there is nothing left to
    compare it to.)"""
    from occupancy.households.electricity import default_equipment_table

    real_ids = set(default_equipment_table().keys())
    assert HOUSEHOLD_EQUIPMENT_TYPES == real_ids, (
        "buem.config.cfg_attribute.HOUSEHOLD_EQUIPMENT_TYPES has drifted "
        "from occupancy's real equipment registry -- update it to match."
    )


# ── use_provided_elecLoad (elecLoad override, occupancy patterns preserved) ──


def test_use_provided_elec_load_requires_series():
    building_attrs = _load_building_attributes("building_01_small_residential.json")
    bad_attrs = dict(building_attrs)
    bad_attrs["use_provided_elecLoad"] = True
    bad_attrs["elecLoad"] = [1.0, 2.0, 3.0]  # not a pandas Series
    with pytest.raises(ValueError, match="requires elecLoad to be a"):
        AttributeBuilder(payload_attrs=bad_attrs).build()


def test_use_provided_elec_load_preserves_occupancy_patterns():
    """Overriding elecLoad must still run a real occupancy generation for
    Q_ig/occ_nothome/occ_sleeping (via occupancy.to_buem_profiles(elec_load=
    ...)) instead of the old behavior of skipping occupancy entirely."""
    building_attrs = _load_building_attributes("building_01_small_residential.json")

    baseline = AttributeBuilder(payload_attrs=dict(building_attrs)).build()
    weather_index = baseline["weather"].index

    flat_supplied_load = pd.Series(5.0, index=weather_index, name="elecLoad")
    override_attrs = dict(building_attrs)
    override_attrs["use_provided_elecLoad"] = True
    override_attrs["elecLoad"] = flat_supplied_load

    overridden = AttributeBuilder(payload_attrs=override_attrs).build()

    # elecLoad matches the caller-supplied series...
    assert np.allclose(overridden["elecLoad"].to_numpy(), 5.0)
    # ...but Q_ig/occ_nothome/occ_sleeping are still real, occupancy-shaped
    # output (varying over the day), not flat/absent -- i.e. occupancy was
    # actually called, unlike the pre-fix total-bypass behavior.
    assert overridden["Q_ig"].nunique() > 1
    assert overridden["occ_nothome"].nunique() > 1
    assert not overridden["Q_ig"].isna().any()
