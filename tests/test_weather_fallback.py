"""
Tests for buem.weather's inline-payload parsing (GeoJsonValidator.
_weather_from_payload) and the BUEM_WEATHER_FALLBACK gate on
AttributeBuilder.generate_weather_profile()'s own per-location fetch --
see enerplanet/buem#10.
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from buem.integration.scripts.attribute_builder import AttributeBuilder
from buem.integration.scripts.geojson_processor import GeoJsonProcessor
from buem.integration.scripts.geojson_validator import GeoJsonValidator, validate_geojson_request

project_root = Path(__file__).resolve().parent.parent
DUMMY_DIR = project_root / "src" / "buem" / "data" / "buildings" / "dummy"

_WEATHER_JSON = {
    "index": ["2018-01-01T00:00:00", "2018-01-01T01:00:00"],
    "variables": {
        "T": [1.0, 2.0],
        "GHI": [0.0, 10.0],
        "DNI": [0.0, 5.0],
        "DHI": [0.0, 5.0],
    },
}


def _payload_with_weather(weather_block: dict | None) -> dict:
    payload = json.loads((DUMMY_DIR / "building_01_small_residential.json").read_text(encoding="utf-8"))
    if weather_block is not None:
        payload["features"][0]["properties"]["buem"]["weather"] = weather_block
    return payload


def _building_attributes(payload: dict) -> dict:
    result = validate_geojson_request(payload)
    assert result.is_valid, [str(e) for e in result.get_errors()]
    return result.validated_data["features"][0]["properties"]["buem"]["building_attributes"]


def test_weather_from_payload_none_returns_none():
    assert GeoJsonValidator._weather_from_payload(None) is None


def test_weather_from_payload_missing_index_returns_none():
    assert GeoJsonValidator._weather_from_payload({"T": [1.0, 2.0]}) is None


def test_weather_from_payload_no_known_columns_returns_none():
    assert GeoJsonValidator._weather_from_payload({"index": ["2018-01-01T00:00:00"]}) is None


def test_weather_from_payload_builds_dataframe_matching_weather_serve_shape():
    """Matches weather serve's actual GET .../point?format=json shape --
    variables nested under "variables", not top-level keys alongside
    "index" (weather enerplanet/weather#13)."""
    df = GeoJsonValidator._weather_from_payload(_WEATHER_JSON)
    assert isinstance(df, pd.DataFrame)
    assert isinstance(df.index, pd.DatetimeIndex)
    assert list(df.columns) == ["T", "GHI", "DNI", "DHI"]
    assert list(df["T"]) == [1.0, 2.0]


def test_weather_from_payload_handles_partial_columns():
    df = GeoJsonValidator._weather_from_payload({"index": ["2018-01-01T00:00:00"], "variables": {"T": [1.0]}})
    assert list(df.columns) == ["T"]


def test_weather_from_payload_rejects_stale_flat_shape():
    """The old flat shape (variables as top-level keys, not nested under
    "variables") must not be silently accepted -- returns None, same as
    "no columns given"."""
    assert GeoJsonValidator._weather_from_payload({"index": ["2018-01-01T00:00:00"], "T": [1.0]}) is None


def test_inline_weather_reaches_building_attributes():
    """A request with an inline buem.weather block gets it parsed into
    building_attributes["weather"], with use_provided_weather set so
    AttributeBuilder doesn't also try its own fetch."""
    attrs = _building_attributes(_payload_with_weather(_WEATHER_JSON))
    assert isinstance(attrs["weather"], pd.DataFrame)
    assert attrs["use_provided_weather"] is True


def test_missing_weather_raises_when_fallback_disabled(monkeypatch):
    """BUEM_WEATHER_FALLBACK=false: AttributeBuilder itself must fail
    loudly when given no weather, not silently trigger its own fetch.

    A request missing buem.weather never reaches AttributeBuilder at all
    any more -- the pinned contract schema requires it, so
    validate_geojson_request() rejects it first. This exercises
    AttributeBuilder's own gate directly (payload_attrs built by hand,
    bypassing the validator), the way a caller that constructs cfg
    without going through the GeoJSON layer would."""
    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")
    attrs = _building_attributes(_payload_with_weather(_WEATHER_JSON))
    attrs = {k: v for k, v in attrs.items() if k not in ("weather", "use_provided_weather")}
    with pytest.raises(RuntimeError, match="BUEM_WEATHER_FALLBACK"):
        AttributeBuilder(payload_attrs=attrs).build()


def test_inline_weather_bypasses_fallback_gate_even_when_disabled(monkeypatch):
    """Caller-supplied weather is used regardless of BUEM_WEATHER_FALLBACK --
    the gate only blocks buem's own fetch, never a real caller-supplied value."""
    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")
    attrs = _building_attributes(_payload_with_weather(_WEATHER_JSON))
    merged = AttributeBuilder(payload_attrs=attrs).build()
    assert "elecLoad" in merged


def test_geojson_processor_strips_building_attributes_from_response(monkeypatch):
    """Regression test: the response used to embed the raw, pre-
    AttributeBuilder building_attributes dict unchanged in the output
    feature -- including the weather DataFrame this module's parsing now
    sets, which crashes Flask's jsonify() (found via a real end-to-end
    request through buem-gateway, not by code reading). building_attributes
    was never part of the documented response shape (model_metadata +
    thermal_load_profile) to begin with. See
    _process_single_feature's building_attributes.pop() and
    enerplanet/buem#10."""
    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")
    payload = _payload_with_weather(_WEATHER_JSON)
    payload["features"][0]["properties"]["end_time"] = "2018-01-01T01:00:00Z"  # the two weather hours
    result = GeoJsonProcessor(payload=payload, include_timeseries=False).process()

    buem_out = result["features"][0]["properties"]["buem"]
    assert buem_out["thermal_load_profile"]["summary"]["heating"]["total"]["value"] > 0
    assert "building_attributes" not in buem_out
