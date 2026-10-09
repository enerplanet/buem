"""Load a caller-supplied electricity load profile from a file path.

Referenced by path (buem.inputs.electricity_load_profile.path) rather than
inlined in the request JSON, per the v4 contract — an 8760-value array inline
would make every request unwieldy. The file must already be accessible inside
this container (shared Docker volume), under BUEM_ELEC_PROFILE_DIR — paths
outside that directory are rejected, since the path is caller-controlled
request input. Supported formats: CSV (single column, optional header),
JSON array, or gzipped JSON array (.gz).
"""
import csv
import gzip
import json
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "electricity_profiles")
)


def _allowed_profile_dir() -> str:
    """Realpath of the directory caller-supplied profile paths must resolve inside."""
    return os.path.realpath(os.environ.get("BUEM_ELEC_PROFILE_DIR", _DEFAULT_PROFILE_DIR))


def load_electricity_load_profile(path: str) -> list:
    """Read an electricity load profile file and return a list of floats.

    Parameters
    ----------
    path : str
        Path to the profile file, relative to or inside BUEM_ELEC_PROFILE_DIR.

    Returns
    -------
    list of float

    Raises
    ------
    ValueError
        If path resolves outside BUEM_ELEC_PROFILE_DIR, doesn't exist, has an
        unrecognised extension, or its content can't be parsed into a flat
        list of numbers. The message is intentionally generic — the caller
        controls `path`, so details (resolved path, file content, parse
        error) are logged server-side instead of returned to the client.
    """
    allowed_dir = _allowed_profile_dir()
    resolved = os.path.realpath(os.path.join(allowed_dir, path))
    if os.path.commonpath([allowed_dir, resolved]) != allowed_dir:
        logger.warning("electricity_load_profile: rejected out-of-bounds path %r -> %r", path, resolved)
        raise ValueError("electricity_load_profile: path outside allowed directory")

    if not os.path.isfile(resolved):
        logger.warning("electricity_load_profile: file not found at %r", resolved)
        raise ValueError("electricity_load_profile: file not found")

    lower = resolved.lower()
    if lower.endswith(".csv"):
        return _load_csv(resolved)
    if lower.endswith((".json.gz", ".gz")):
        return _load_json(gzip.open(resolved, "rt", encoding="utf-8"), resolved)
    if lower.endswith(".json"):
        return _load_json(open(resolved, "r", encoding="utf-8"), resolved)
    logger.warning("electricity_load_profile: unrecognised extension for %r", resolved)
    raise ValueError(
        "electricity_load_profile: unrecognised file extension — expected .csv, .json, or .json.gz"
    )


def _load_csv(path: str) -> list:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        rows = [row[0] for row in reader if row]
    if rows and not _is_number(rows[0]):
        rows = rows[1:]  # header row (e.g. "demand")
    try:
        return [float(v) for v in rows]
    except ValueError:
        logger.warning("electricity_load_profile: non-numeric value in %r", path, exc_info=True)
        raise ValueError(f"electricity_load_profile: could not parse {os.path.basename(path)} as numeric data") from None


def _load_json(fh, path: str) -> list:
    with fh:
        data = json.load(fh)
    if not isinstance(data, list):
        # ValueError on purpose: callers treat every loader failure as invalid input.
        raise ValueError("electricity_load_profile: JSON content must be a flat array of numbers")  # noqa: TRY004
    try:
        return [float(v) for v in data]
    except (TypeError, ValueError):
        logger.warning("electricity_load_profile: non-numeric value in %r", path, exc_info=True)
        raise ValueError(f"electricity_load_profile: could not parse {os.path.basename(path)} as numeric data") from None


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False
