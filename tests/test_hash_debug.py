"""Debug: check if cfg hash is deterministic across two invocations."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from buem.config.cfg_building import CfgBuilding
from buem.integration.scripts.attribute_builder import AttributeBuilder
from buem.integration.scripts.geojson_validator import validate_geojson_request
from buem.integration.scripts.result_cache import compute_cfg_hash

BUILDING_FILE = Path(__file__).resolve().parent.parent / "src" / "buem" / "data" / "buildings" / "dummy" / "building_01_small_residential.json"


def _load_v3_attrs() -> dict:
    """Load dummy building and extract v2-style building_attributes via the validator."""
    with open(BUILDING_FILE) as f:
        payload = json.load(f)
    result = validate_geojson_request(payload)
    assert result.is_valid, f"Dummy building failed validation: {[i.message for i in result.get_errors()]}"
    feat = result.validated_data["features"][0]
    return feat["properties"]["buem"]["building_attributes"]


def test_hash_determinism():
    """cfg hash must be identical for two builds of the same building.

    The id is held fixed: it seeds the occupancy generation, so two ids
    are two buildings."""
    payload_attrs = _load_v3_attrs()

    builder1 = AttributeBuilder(payload_attrs=payload_attrs, building_id="test")
    merged1 = builder1.build()
    cfg1 = CfgBuilding(merged1).to_cfg_dict()
    h1 = compute_cfg_hash(cfg1)

    builder2 = AttributeBuilder(payload_attrs=payload_attrs, building_id="test")
    merged2 = builder2.build()
    cfg2 = CfgBuilding(merged2).to_cfg_dict()
    h2 = compute_cfg_hash(cfg2)

    # Report diffs for debugging if hashes differ
    if h1 != h2:
        for k in cfg1:
            v1, v2 = cfg1[k], cfg2[k]
            if isinstance(v1, pd.DataFrame):
                if not v1.equals(v2):
                    print(f"  DIFF: {k} (DataFrame)")
            elif isinstance(v1, pd.Series):
                if not v1.equals(v2):
                    print(f"  DIFF: {k} (Series)")
            elif isinstance(v1, np.ndarray):
                if not np.array_equal(v1, v2):
                    print(f"  DIFF: {k} (ndarray)")
            elif isinstance(v1, dict):
                if v1 != v2:
                    print(f"  DIFF: {k} (dict)")
            else:
                if v1 != v2:
                    print(f"  DIFF: {k}: {v1!r} vs {v2!r}")

    assert h1 == h2, f"Hashes differ: {h1} vs {h2}"


if __name__ == "__main__":
    test_hash_determinism()
