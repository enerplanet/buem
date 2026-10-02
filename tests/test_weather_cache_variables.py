"""Both weather_cache fetch backends must request the same variable set.

weather >= 2.0 rejects a point query that names neither variables nor
use_case, while the pinned 1.9.3.dev19 has no such parameter, so the local
backend passes WEATHER_VARIABLES only when the installed get_point_weather
accepts it.
"""
import io

import pandas as pd
import pytest

from buem.config import weather_cache


def _frame() -> pd.DataFrame:
    idx = pd.date_range("2018-01-01", periods=3, freq="h")
    return pd.DataFrame(
        {"T": [1.0, 2.0, 3.0], "GHI": [0.0] * 3, "DHI": [0.0] * 3, "DNI": [0.0] * 3},
        index=idx,
    )


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Empty cache dir, local backend selected, no archive dir override."""
    monkeypatch.setenv("BUEM_WEATHER_DIR", str(tmp_path))
    monkeypatch.delenv("WEATHER_API_URL", raising=False)
    monkeypatch.delenv("BUEM_WEATHER_DATA_DIR", raising=False)


def test_local_backend_requests_buem_variables_when_supported(monkeypatch, isolated_cache):
    seen: dict = {}

    def fake(latitude, longitude, year, *, provider, data_dir=None, variables=None, use_case=None):
        seen.update(provider=provider, variables=variables, use_case=use_case)
        return _frame()

    monkeypatch.setattr(weather_cache, "get_point_weather", fake)
    weather_cache.get_or_fetch_weather(52.0, 5.0, 2018, "merra-2")
    assert seen["provider"] == "merra-2"
    assert seen["variables"] == weather_cache.WEATHER_VARIABLES
    assert seen["use_case"] is None


def test_local_backend_omits_variables_for_pinned_signature(monkeypatch, isolated_cache):
    """The pinned weather commit's get_point_weather has no variables
    parameter; passing one would raise TypeError."""

    def fake(latitude, longitude, year, *, provider, data_dir=None):
        return _frame()

    monkeypatch.setattr(weather_cache, "get_point_weather", fake)
    df = weather_cache.get_or_fetch_weather(52.0, 5.0, 2018, "merra-2")
    assert len(df) == 3


def test_remote_backend_requests_the_same_variables(monkeypatch, isolated_cache):
    monkeypatch.setenv("WEATHER_API_URL", "https://weather.example/api")
    seen: dict = {}

    class _Resp:
        ok = True

        def __init__(self) -> None:
            buf = io.BytesIO()
            _frame().reset_index().to_parquet(buf)
            self.content = buf.getvalue()

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(params)
        return _Resp()

    monkeypatch.setattr(weather_cache.requests, "get", fake_get)
    weather_cache.get_or_fetch_weather(52.0, 5.0, 2018, "merra-2")
    assert seen["variables"] == weather_cache.WEATHER_VARIABLES
