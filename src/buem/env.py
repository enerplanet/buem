"""Centralised environment bootstrap for BUEM.

Loads .env from the project root (searched upward from this file, or from
``BUEM_HOME`` if set) and applies sensible path defaults relative to the
installed package location.  Safe to call multiple times.
"""
from __future__ import annotations

import os
from pathlib import Path

_loaded = False


def _find_dotenv() -> Path | None:
    """Walk upward from this file to find the nearest .env."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_env() -> None:
    """Load .env and set default env vars for all BUEM paths.

    Called automatically on first import of ``buem`` and explicitly by the
    CLI entry-point.  Subsequent calls are no-ops.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    # 1. Load .env (does not override variables already set in the environment)
    try:
        from dotenv import load_dotenv

        dotenv_home = os.environ.get("BUEM_HOME")
        found: Path | None
        if dotenv_home:
            found = Path(dotenv_home) / ".env"
            load_dotenv(found, override=False)
        else:
            found = _find_dotenv()
            if found:
                load_dotenv(found, override=False)

        # Resolve any relative paths in BUEM_ env vars to absolute, using the
        # .env file location (project root) as the base.  Without this, a value
        # like "./src/buem/data/weather" is ambiguous — it depends on whatever
        # the cwd happens to be at import time.
        if found and found.is_file():
            _root = found.parent
            for _var in (
                "BUEM_WEATHER_DIR", "BUEM_WEATHER_DATA_DIR",
                "BUEM_RESULTS_DIR", "BUEM_LOG_DIR", "BUEM_DATA_DIR",
                "BUEM_CBC_EXE", "BUEM_LOG_FILE", "BUEM_ELEC_PROFILE_DIR",
            ):
                _val = os.environ.get(_var)
                if _val and not Path(_val).is_absolute():
                    os.environ[_var] = str((_root / _val).resolve())
    except ImportError:
        pass  # python-dotenv is optional; OS env vars still work

    # 2. Apply defaults relative to the package directory so that an
    #    installed package (or editable install) works out of the box.
    #    BUEM_WEATHER_DIR holds the bundled offline-fallback CSV and also
    #    roots the dynamic per-location weather cache (see
    #    buem.config.weather_cache). BUEM_WEATHER_DATA_DIR (no default
    #    here) points at the `weather` package's own pre-processed
    #    provider archives; when unset, weather.get_point_weather() falls
    #    back to its own data_root() convention. BUEM_DATA_DIR (no default
    #    either) is the shared volume a deployment mounts client-supplied
    #    files into, referenced by buem.inputs.electricity_load_profile.path
    #    / buem.weather.profile.path (see
    #    buem.integration.scripts.profile_file_loader). Every such path is
    #    resolved and must stay inside it; unset, file-based profiles are
    #    refused.
    _pkg = Path(__file__).parent
    os.environ.setdefault("BUEM_WEATHER_DIR", str(_pkg / "data" / "weather"))
    os.environ.setdefault("BUEM_RESULTS_DIR", str(_pkg / "results"))
    os.environ.setdefault("BUEM_LOG_DIR",     str(_pkg / "logs"))
    os.environ.setdefault("BUEM_ELEC_PROFILE_DIR", str(_pkg / "data" / "electricity_profiles"))

    # BUEM_RESULTS_DIR/BUEM_LOG_DIR are output directories the app writes
    # to at runtime (result cache, log files) -- ensure they exist rather
    # than waiting for whichever code path happens to write there first.
    # BUEM_WEATHER_DIR is deliberately NOT created here: it must already
    # contain the bundled weather CSV, and cfg_attribute.py raises
    # FileNotFoundError with a clear message if it doesn't -- silently
    # creating an empty directory would turn that into a more confusing
    # failure later.
    Path(os.environ["BUEM_RESULTS_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["BUEM_LOG_DIR"]).mkdir(parents=True, exist_ok=True)
