"""Caller-supplied profile paths resolve inside BUEM_DATA_DIR only.

Covers both loaders: a relative and an absolute path inside the directory
load; `..`, an absolute path outside and a symlink that leaves the
directory are rejected; the loaders refuse to run without BUEM_DATA_DIR;
oversized and non-regular files are rejected; error messages never carry
file content.
"""
import json
import os

import pandas as pd
import pytest

from buem.integration.scripts import profile_file_loader as pfl


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("BUEM_DATA_DIR", str(d))
    return d


def _elec_file(directory, name="elec.json", values=None):
    p = directory / name
    p.write_text(json.dumps([1.0, 2.0, 3.0] if values is None else values), encoding="utf-8")
    return p


def _weather_csv(path):
    idx = pd.date_range("2018-01-01", periods=3, freq="h")
    pd.DataFrame({"T": 1.0, "GHI": 0.0, "DHI": 0.0, "DNI": 0.0}, index=idx).to_csv(path)
    return path


def test_relative_path_inside_data_dir_loads(data_dir):
    _elec_file(data_dir)
    assert pfl.load_electricity_load_values("elec.json") == [1.0, 2.0, 3.0]


def test_absolute_path_inside_data_dir_loads(data_dir):
    p = _elec_file(data_dir)
    assert pfl.load_electricity_load_values(str(p)) == [1.0, 2.0, 3.0]


def test_parent_traversal_is_rejected(data_dir, tmp_path):
    _elec_file(tmp_path, "outside.json")
    with pytest.raises(ValueError, match="outside BUEM_DATA_DIR"):
        pfl.load_electricity_load_values("../outside.json")


def test_absolute_path_outside_is_rejected(data_dir, tmp_path):
    p = _elec_file(tmp_path, "outside.json")
    with pytest.raises(ValueError, match="outside BUEM_DATA_DIR"):
        pfl.load_electricity_load_values(str(p))


def test_symlink_leaving_data_dir_is_rejected(data_dir, tmp_path):
    target = _elec_file(tmp_path, "outside.json")
    (data_dir / "link.json").symlink_to(target)
    with pytest.raises(ValueError, match="outside BUEM_DATA_DIR"):
        pfl.load_electricity_load_values("link.json")


def test_unset_data_dir_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("BUEM_DATA_DIR", raising=False)
    p = _elec_file(tmp_path)
    with pytest.raises(ValueError, match="BUEM_DATA_DIR"):
        pfl.load_electricity_load_values(str(p))


def test_oversized_file_is_rejected(data_dir, monkeypatch):
    monkeypatch.setattr(pfl, "MAX_PROFILE_FILE_BYTES", 16)
    _elec_file(data_dir, values=[1.0] * 100)
    with pytest.raises(ValueError, match="exceeds"):
        pfl.load_electricity_load_values("elec.json")


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs a character device")
def test_non_regular_file_is_rejected(monkeypatch):
    monkeypatch.setenv("BUEM_DATA_DIR", "/dev")
    with pytest.raises(ValueError, match="regular file"):
        pfl.load_electricity_load_values("/dev/zero")


def test_parse_error_does_not_echo_file_content(data_dir):
    (data_dir / "elec.json").write_text("NOT-A-NUMBER-MARKER", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        pfl.load_electricity_load_values("elec.json")
    assert "NOT-A-NUMBER" not in str(excinfo.value)


def test_non_numeric_value_does_not_echo_file_content(data_dir):
    _elec_file(data_dir, values=[1.0, "NOT-A-NUMBER-MARKER"])
    with pytest.raises(ValueError) as excinfo:
        pfl.load_electricity_load_values("elec.json")
    assert "NOT-A-NUMBER" not in str(excinfo.value)


def test_weather_profile_inside_loads(data_dir):
    _weather_csv(data_dir / "weather.csv")
    assert len(pfl.load_weather_profile("weather.csv", "csv")) == 3


def test_weather_profile_outside_is_rejected(data_dir, tmp_path):
    p = _weather_csv(tmp_path / "weather.csv")
    with pytest.raises(ValueError, match="outside BUEM_DATA_DIR"):
        pfl.load_weather_profile(str(p), "csv")


def test_weather_profile_bad_timestamp_does_not_echo_content(data_dir):
    records = [{"time": "NOT-A-NUMBER-MARKER", "T": 1.0, "GHI": 0.0, "DHI": 0.0, "DNI": 0.0}]
    (data_dir / "w.json").write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        pfl.load_weather_profile("w.json", "json")
    assert "NOT-A-NUMBER" not in str(excinfo.value)
