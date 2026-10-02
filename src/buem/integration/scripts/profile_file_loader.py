"""
File-based profile loading for optional caller-supplied timeseries:
``buem.inputs.electricity_load_profile`` and ``buem.weather.profile`` (see
``versions/v4/request_schema.json``). Files are referenced by an absolute
path expected to be reachable inside the model container -- ``BUEM_DATA_DIR``
names the shared Docker volume where a deployment mounts client-supplied
files (see ``buem.env.load_env()``). Every path, relative or absolute, is
resolved with symlinks followed and must stay inside that directory, must
be a regular file and must not exceed ``MAX_PROFILE_FILE_BYTES``; without
``BUEM_DATA_DIR`` set, file-based profiles are refused.

Both loaders raise ``ValueError`` (not ``OSError``/``FileNotFoundError``)
for read/format failures, so they surface as ordinary validation errors
through ``GeoJsonValidator``'s existing exception handling in
``_convert_components_format`` (which does not currently catch ``OSError``).
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# kWh and kW are numerically identical for hourly-resolution data (one hour
# at X kW == X kWh of energy that hour) -- buem's own elecLoad convention is
# effectively kWh-per-hour, so both pass through unscaled; only Wh needs
# converting down.
_ELEC_UNIT_TO_KWH_FACTOR = {"kWh": 1.0, "kW": 1.0, "Wh": 1e-3}

REQUIRED_WEATHER_COLUMNS = ("T", "GHI", "DHI", "DNI")

# An hourly year is well under 1 MiB in every supported format. Upper bound
# on bytes read per profile file, gzip output included.
MAX_PROFILE_FILE_BYTES = 32 * 1024 * 1024


def _resolve_profile_path(path: str, label: str) -> Path:
    """Resolve ``path`` to a regular file inside ``BUEM_DATA_DIR``.

    Symlinks are followed before the containment check, so a link whose
    target is outside the directory is rejected like any other path outside
    it. Messages name the caller's own path and never the file's content.
    """
    base = os.environ.get("BUEM_DATA_DIR")
    if not base:
        raise ValueError(
            f"{label}: file-based profiles need BUEM_DATA_DIR to be set on "
            "this deployment."
        )
    base_real = os.path.realpath(base)
    resolved = os.path.realpath(os.path.join(base_real, path))
    if os.path.commonpath([base_real, resolved]) != base_real:
        raise ValueError(f"{label}: path {path!r} is outside BUEM_DATA_DIR.")
    if not os.path.isfile(resolved):
        raise ValueError(
            f"Could not read {label} file {path!r}: not a regular file "
            "under BUEM_DATA_DIR."
        )
    if os.path.getsize(resolved) > MAX_PROFILE_FILE_BYTES:
        raise ValueError(
            f"{label}: file {path!r} exceeds "
            f"{MAX_PROFILE_FILE_BYTES // (1024 * 1024)} MiB."
        )
    return Path(resolved)


def _read_gzip_text(p: Path, label: str, path: str) -> str:
    """Decompress at most ``MAX_PROFILE_FILE_BYTES`` of text."""
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        data = fh.read(MAX_PROFILE_FILE_BYTES + 1)
    if len(data) > MAX_PROFILE_FILE_BYTES:
        raise ValueError(
            f"{label}: file {path!r} exceeds "
            f"{MAX_PROFILE_FILE_BYTES // (1024 * 1024)} MiB when decompressed."
        )
    return data


def _time_index(values: Any, label: str, path: str) -> pd.DatetimeIndex:
    """Parse timestamps without echoing the offending value."""
    try:
        return pd.to_datetime(values)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Could not parse {label} file {path!r}: a timestamp is not ISO-8601."
        ) from exc


def load_electricity_load_values(path: str, unit: str = "kWh") -> list[float]:
    """Load a flat hourly electricity-consumption array.

    Supported formats (by file extension, per
    ``buem.inputs.electricity_load_profile``'s schema description): ``.csv``
    (single column of values, no header), ``.json`` (a JSON array), ``.gz``
    (gzipped JSON array). No timestamp column -- there is no per-value time
    information in this format; the caller is responsible for building the
    actual index against the request's resolved year.

    Returns values converted to buem's own elecLoad convention (kWh, ==
    kW at hourly resolution) -- see ``_ELEC_UNIT_TO_KWH_FACTOR``.
    """
    p = _resolve_profile_path(path, "electricity_load_profile")
    try:
        if p.suffix == ".gz":
            values = json.loads(_read_gzip_text(p, "electricity_load_profile", path))
        elif p.suffix == ".json":
            values = json.loads(p.read_text(encoding="utf-8"))
        elif p.suffix == ".csv":
            values = pd.read_csv(p, header=None).iloc[:, 0].tolist()
        else:
            raise ValueError(
                f"Unsupported electricity_load_profile format {p.suffix!r} "
                f"(expected .csv, .json, or .gz): {path}"
            )
    except OSError as exc:
        raise ValueError(f"Could not read electricity_load_profile file {path!r}.") from exc
    except (json.JSONDecodeError, pd.errors.ParserError) as exc:
        raise ValueError(
            f"Could not parse electricity_load_profile file {path!r} as {p.suffix[1:]}."
        ) from exc

    if not isinstance(values, list) or not values:
        raise ValueError(
            f"electricity_load_profile at {path!r} did not contain a "
            "non-empty array of values."
        )
    if unit not in _ELEC_UNIT_TO_KWH_FACTOR:
        raise ValueError(
            f"electricity_load_profile unit must be one of "
            f"{sorted(_ELEC_UNIT_TO_KWH_FACTOR)}, got {unit!r}."
        )
    factor = _ELEC_UNIT_TO_KWH_FACTOR[unit]
    try:
        return [float(v) * factor for v in values]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"electricity_load_profile at {path!r} contains a non-numeric value."
        ) from exc


def load_weather_profile(path: str, fmt: str = "json") -> pd.DataFrame:
    """Load a caller-supplied hourly T/GHI/DHI/DNI weather timeseries.

    ``json`` (the default -- confirmed 2026-08-14 as EnerPlanET's actual
    format) is a JSON array of hourly records, each ``{"time": <ISO-8601
    timestamp>, "T": ..., "GHI": ..., "DHI": ..., "DNI": ...}``. ``csv``/
    ``parquet`` remain supported for other/internal callers: first column
    the timestamp index (any pandas-parseable format), remaining columns
    T/GHI/DHI/DNI -- buem's own weather DataFrame convention (see
    ``buem.config.weather_cache``); mirrors ``weather_cache
    ._fetch_remote()``'s own index-from-first-column convention for the
    parquet case, for consistency.
    """
    p = _resolve_profile_path(path, "weather.profile")
    try:
        if fmt == "json":
            records = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(records, list) or not records:
                raise ValueError(
                    f"weather.profile at {path!r} did not contain a "
                    "non-empty JSON array of records."
                )
            df = pd.DataFrame.from_records(records)
            if "time" not in df.columns:
                raise ValueError(
                    f"weather.profile at {path!r}: each JSON record must "
                    "have a 'time' key (ISO-8601 timestamp)."
                )
            df = df.set_index("time")
            df.index = _time_index(df.index, "weather.profile", path)
        elif fmt == "csv":
            df = pd.read_csv(p, index_col=0, parse_dates=True)
        elif fmt == "parquet":
            df = pd.read_parquet(p)
            df = df.set_index(df.columns[0])
            df.index = _time_index(df.index, "weather.profile", path)
        else:
            raise ValueError(
                f"Unsupported weather.profile format {fmt!r} "
                "(expected 'json', 'csv', or 'parquet')."
            )
    except OSError as exc:
        raise ValueError(f"Could not read weather.profile file {path!r}.") from exc
    except (json.JSONDecodeError, pd.errors.ParserError, KeyError, TypeError) as exc:
        raise ValueError(f"Could not parse weather.profile file {path!r} as {fmt}.") from exc

    missing = set(REQUIRED_WEATHER_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(
            f"weather.profile at {path!r} is missing required column(s) "
            f"{sorted(missing)} -- found {sorted(df.columns)}."
        )
    return df
