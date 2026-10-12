"""Multi-dwelling diversification and building_id-derived seeds reach the
attribute builder through occupancy.building_demand()."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from buem.integration.scripts.attribute_builder import AttributeBuilder
from buem.integration.scripts.geojson_validator import validate_geojson_request

DUMMY_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "buem" / "data" / "buildings" / "dummy"
)


def _residential_attrs() -> dict:
    payload = json.loads(
        (DUMMY_DIR / "building_01_small_residential.json").read_text(encoding="utf-8")
    )
    result = validate_geojson_request(payload)
    assert result.is_valid, [str(e) for e in result.get_errors()]
    return result.validated_data["features"][0]["properties"]["buem"]["building_attributes"]


def _elec(attrs: dict, building_id: str | None = None) -> pd.Series:
    merged = AttributeBuilder(payload_attrs=dict(attrs), building_id=building_id).build()
    return merged["elecLoad"]


def test_building_id_seeds_the_profile():
    attrs = _residential_attrs()
    assert _elec(attrs, "A").equals(_elec(attrs, "A"))
    assert not _elec(attrs, "A").equals(_elec(attrs, "B"))
    assert not _elec(attrs, "A").equals(_elec(attrs))


def test_explicit_seed_overrides_building_id():
    attrs = {**_residential_attrs(), "seed": 777}
    assert _elec(attrs, "A").equals(_elec(attrs, "B"))


def test_multi_dwelling_diversifies_peak():
    attrs = {**_residential_attrs(), "seed": 123}
    copied = _elec({**attrs, "residential_units": 1.0}, "block") * 20.0
    block = _elec({**attrs, "residential_units": 20.0}, "block")
    assert block.max() / block.mean() < copied.max() / copied.mean()


def test_provided_elec_load_is_used_unscaled():
    attrs = {**_residential_attrs(), "residential_units": 5.0}
    index = _elec(attrs).index
    attrs["elecLoad"] = pd.Series(1.0, index=index, name="elecLoad")
    attrs["use_provided_elecLoad"] = True
    assert np.allclose(_elec(attrs).to_numpy(), 1.0)
