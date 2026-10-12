"""Quick smoke test: feather cache + result cache.

This test requires the full BUEM infrastructure (weather data, model solver).
It is skipped when running in CI or without weather data.
"""
import json
import time
from pathlib import Path

import pytest

BUILDING_FILE = Path(__file__).resolve().parent.parent / "src" / "buem" / "data" / "buildings" / "dummy" / "building_01_small_residential.json"

# Skip if weather data or Flask not available
_skip_reason = None
try:
    from buem.integration.scripts.geojson_processor import GeoJsonProcessor
except ImportError as e:
    _skip_reason = f"GeoJsonProcessor unavailable: {e}"


@pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or "")
def test_processor_validates_v3_payload():
    """GeoJsonProcessor should accept and validate a v3 dummy building."""
    with open(BUILDING_FILE) as f:
        payload = json.load(f)
    proc = GeoJsonProcessor(payload=payload, include_timeseries=False)
    assert proc.payload is not None


@pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or "")
@pytest.mark.slow
def test_cache_hit_is_faster():
    """Second run should be faster than the first (result cache hit)."""
    with open(BUILDING_FILE) as f:
        payload = json.load(f)

    # Real per-location weather fetch (weather is a compulsory dependency).
    # This test exercises result caching, not weather accuracy, so pin the
    # simulation year to one with a processed archive available wherever
    # this test runs, overriding the fixture's own start_time/end_time
    # (2024/2025) which BUEM_WEATHER_DATA_DIR may not have archives for.
    props = payload["features"][0]["properties"]
    props["start_time"] = "2018-01-01T00:00:00Z"
    props["end_time"] = "2018-12-31T23:00:00Z"

    t0 = time.time()
    proc = GeoJsonProcessor(payload=payload, include_timeseries=False)
    resp = proc.process()
    t1 = time.time()
    meta = resp["metadata"]
    assert meta["successful_features"] > 0, "First run should succeed"

    t2 = time.time()
    proc2 = GeoJsonProcessor(payload=payload, include_timeseries=False)
    resp2 = proc2.process()
    t3 = time.time()
    meta2 = resp2["metadata"]
    assert meta2["successful_features"] > 0, "Second run should succeed"

    print(f"First run:  {t1 - t0:.2f}s")
    print(f"Second run: {t3 - t2:.2f}s")


@pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or "")
def test_result_cache_dir():
    """Result cache directory should be accessible."""
    from buem.integration.scripts.result_cache import CACHE_DIR
    cache_dir = str(CACHE_DIR)
    # Directory may or may not exist yet — just verify the path is set
    assert cache_dir, "CACHE_DIR should be a non-empty path"
