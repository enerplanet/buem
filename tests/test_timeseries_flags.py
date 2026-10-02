"""`include_timeseries` returns inline arrays; `save_timeseries_file` writes
the gzip JSON file. The two are independent on /api/process."""
import copy
import json
from pathlib import Path

import pytest

from buem.apis.api_server import create_app
from buem.integration.scripts.geojson_processor import GeoJsonProcessor

_EXAMPLE = Path(__file__).resolve().parent.parent / "src" / "buem" / "integration" / "json_schema" / "example_request.json"


@pytest.fixture
def payload() -> dict:
    """The pinned contract example, minus the two parts buem rejects
    (solver.compute_cooling and the absent electricity profile file)."""
    p = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    buem = p["features"][0]["properties"]["buem"]
    buem["solver"].pop("compute_cooling", None)
    buem.pop("inputs", None)
    return p


def _profile(doc: dict) -> dict:
    return doc["features"][0]["properties"]["buem"]["thermal_load_profile"]


def test_inline_arrays_without_writing_a_file(payload, tmp_path):
    doc = GeoJsonProcessor(copy.deepcopy(payload), include_timeseries=True, result_save_dir=str(tmp_path)).process()
    profile = _profile(doc)
    assert "timeseries" in profile
    assert "timeseries_file" not in profile
    assert list(tmp_path.iterdir()) == []


def test_file_without_inline_arrays(payload, tmp_path):
    doc = GeoJsonProcessor(
        copy.deepcopy(payload), include_timeseries=False, save_timeseries_file=True, result_save_dir=str(tmp_path),
    ).process()
    profile = _profile(doc)
    assert "timeseries" not in profile
    assert profile["timeseries_file"].startswith("/api/files/buem_ts_")
    written = list(tmp_path.glob("buem_ts_*.json.gz"))
    assert len(written) == 1


def test_neither_flag_writes_nothing(payload, tmp_path):
    doc = GeoJsonProcessor(copy.deepcopy(payload), result_save_dir=str(tmp_path)).process()
    profile = _profile(doc)
    assert "timeseries" not in profile and "timeseries_file" not in profile
    assert "total_energy_demand" in profile["summary"]
    assert list(tmp_path.iterdir()) == []


def test_process_route_reads_both_query_flags(payload, tmp_path, monkeypatch):
    monkeypatch.setenv("BUEM_RESULTS_DIR", str(tmp_path))
    client = create_app().test_client()

    inline = client.post("/api/process?include_timeseries=true", json=copy.deepcopy(payload))
    assert inline.status_code == 200, inline.get_json()
    profile = _profile(inline.get_json())
    assert "timeseries" in profile and "timeseries_file" not in profile
    assert list(tmp_path.glob("buem_ts_*.json.gz")) == []

    saved = client.post("/api/process?save_timeseries_file=true", json=copy.deepcopy(payload))
    assert saved.status_code == 200, saved.get_json()
    profile = _profile(saved.get_json())
    assert "timeseries" not in profile and "timeseries_file" in profile
    assert len(list(tmp_path.glob("buem_ts_*.json.gz"))) == 1
