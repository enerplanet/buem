"""Location-keyed on-disk cache wrapping the (compulsory) weather package.

Before weather became per-location, buem loaded exactly one bundled CSV at
module-import time and cached its (expensive, ~2-3s pvlib DISC) processed
form in a single global feather file (see cfg_attribute.py). A dynamic,
per-building weather fetch needs a cache keyed by (provider, lat, lon,
year) instead of one global key, so repeated buildings at the same site
(or across a parallel batch) still avoid repeat fetch/reconstruction cost.

Two fetch backends (2026-08-04, scaffold -- see weather repo's
src/weather/api/README.md for the counterpart): local direct-archive access
(the original, default path -- calls weather.get_point_weather() against a
data_dir on this machine's filesystem) and a remote HTTP backend, for
deployments (e.g. buem's production container) that cannot reach the
archives' filesystem directly. Selected by whether WEATHER_API_URL is set;
unset, behavior is completely unchanged from before this backend existed.
"""

from __future__ import annotations

import inspect
import io
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from weather import get_point_weather  # type: ignore[import]

logger = logging.getLogger(__name__)

# What both backends ask weather for: air temperature plus the three solar
# components pvlib's plane-of-array transposition needs.
WEATHER_VARIABLES = "T,GHI,DHI,DNI"


def _cache_dir() -> Path:
    base = os.environ.get(
        "BUEM_WEATHER_DIR",
        os.path.join(os.path.dirname(__file__), "..", "data", "weather"),
    )
    d = Path(base).resolve() / "location_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(provider: str, latitude: float, longitude: float, year: int) -> Path:
    key = f"{provider}_{round(latitude, 3)}_{round(longitude, 3)}_{year}"
    return _cache_dir() / f"{key}.feather"


def _fetch_remote(latitude: float, longitude: float, year: int, provider: str) -> pd.DataFrame:
    """Fetch via the weather repo's point-query HTTP API instead of local
    filesystem access -- see weather repo's src/weather/api/README.md.

    Raises
    ------
    requests.HTTPError
        Non-2xx response (404 -- no archive for this provider/year; 401/403
        -- bad WEATHER_API_KEY; 429 -- rate limited; see the API's own
        error body for detail).
    """
    api_url = os.environ["WEATHER_API_URL"].rstrip("/")
    api_key = os.environ.get("WEATHER_API_KEY", "")
    params: dict[str, str | int | float] = {
        "provider": provider, "lat": latitude, "lon": longitude, "year": year,
        # weather >= 2.0.0's /v1/weather/point rejects a request that names
        # neither variables nor use_case.
        "variables": WEATHER_VARIABLES,
    }
    resp = requests.get(
        f"{api_url}/v1/weather/point",
        params=params,
        headers={"X-API-Key": api_key},
        timeout=30,
    )
    if not resp.ok:
        # raise_for_status() alone only gives "404 Client Error: NOT FOUND
        # for url: ..." -- the API's own error body (e.g. "No processed
        # merra-2 files matching...") is far more actionable and otherwise
        # goes unused.
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} for {resp.url}: {detail}",
            response=resp,
        )
    df = pd.read_parquet(io.BytesIO(resp.content))
    df.set_index(df.columns[0], inplace=True)
    df.index = pd.to_datetime(df.index)
    return df


def get_or_fetch_weather(
    latitude: float, longitude: float, year: int, provider: str
) -> pd.DataFrame:
    """Return a cached (or freshly fetched) weather DataFrame.

    Fetch backend is local direct-archive access by default, or the remote
    HTTP API (see _fetch_remote) when WEATHER_API_URL is set -- e.g. for a
    deployment that cannot reach the archives' filesystem directly. Either
    way, results are cached identically below.

    Raises
    ------
    FileNotFoundError
        Local backend: `weather` has no processed archive for (provider,
        year) at the requested location (set ``BUEM_WEATHER_DATA_DIR`` to
        point at one).
    requests.HTTPError
        Remote backend: see _fetch_remote.
    """
    path = _cache_path(provider, latitude, longitude, year)
    if path.exists():
        df = pd.read_feather(path)
        df.set_index(df.columns[0], inplace=True)
        df.index = pd.to_datetime(df.index)
        return df

    if os.environ.get("WEATHER_API_URL"):
        df = _fetch_remote(latitude, longitude, year, provider)
    else:
        data_dir = os.environ.get("BUEM_WEATHER_DATA_DIR")
        kwargs: dict[str, Any] = {"provider": provider, "data_dir": data_dir}
        # weather >= 2.0 rejects a call that names neither variables nor
        # use_case; the pinned 1.9.3.dev19 has no variables parameter at all.
        if "variables" in inspect.signature(get_point_weather).parameters:
            kwargs["variables"] = WEATHER_VARIABLES
        df = get_point_weather(latitude, longitude, year, **kwargs)
    try:
        df.reset_index().to_feather(path)
    except (OSError, ValueError) as exc:
        # Non-critical: caching failure should not block model execution.
        logger.warning("Could not write weather cache %s: %s", path, exc)
    return df


def distinct_locations(
    building_attrs: Iterable[dict[str, Any]],
) -> list[tuple[float, float, int, str]]:
    """Distinct (latitude, longitude, year, provider) across a batch.

    Used to pre-warm the cache for every location in a multi-building run
    before forking parallel workers (see parallelization/parallel_run.py),
    instead of assuming a single global weather dataset.
    """
    from buem.config.cfg_attribute import ATTRIBUTE_SPECS

    default_lat = ATTRIBUTE_SPECS["latitude"].default
    default_lon = ATTRIBUTE_SPECS["longitude"].default
    default_year = ATTRIBUTE_SPECS["year"].default
    default_provider = ATTRIBUTE_SPECS["weather_provider"].default

    seen: set[tuple[float, float, int, str]] = set()
    for attrs in building_attrs:
        seen.add((
            round(float(attrs.get("latitude", default_lat)), 3),
            round(float(attrs.get("longitude", default_lon)), 3),
            int(attrs.get("year", default_year)),
            str(attrs.get("weather_provider", default_provider)),
        ))
    return sorted(seen)
