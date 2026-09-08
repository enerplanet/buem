"""Loaders for the user-editable reference tables under
``src/buem/data/reference/``, mirroring occupancy's own
``households/data/dhw_tapping_categories.csv`` pattern (see
``scripts/extract_dhw_reference_values.py``'s docstring for the full
rationale and provenance).

Two tables live here:

``dhw_cooking_constants.csv``
    Deterministic "physical constant"-style values for DHW and cooking.
    ``buem.thermal.dhw_cooking`` derives its module-level constants from
    this loader at import time (so ``from buem.thermal.dhw_cooking import
    DHW_DELTA_T_K`` keeps working exactly as before -- this module changes
    *where the number comes from*, not the public API).

``num_persons_by_building_type.csv``
    Occupants per dwelling, by building type and optionally country and
    statistical region. Consulted by ``AttributeBuilder`` whenever a
    request supplies no explicit ``num_persons``. See
    :func:`resolve_num_persons` for the lookup order.

Editing a value in either CSV and reimporting is sufficient; no Python
code changes needed. Both fail loudly at load time on a broken hand-edit
rather than silently substituting a default.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

_PACKAGE = "buem.data"
_RESOURCE = "reference/dhw_cooking_constants.csv"
_NUM_PERSONS_RESOURCE = "reference/num_persons_by_building_type.csv"
_GLAZING_RESOURCE = "reference/glazing_reference.csv"
_SETBACK_RESOURCE = "reference/setback_profiles.csv"

# The "matches anything" token in the CSV's country/region_code columns.
ANY = "*"


@lru_cache(maxsize=1)
def load_dhw_cooking_constants() -> dict[str, float]:
    """Load ``dhw_cooking_constants.csv`` as a ``{name: value}`` dict.

    Cached (the file is read once per process) -- this is meant to back
    module-level constants resolved at import time, not re-read per call.
    Raises ``ValueError`` naming the problem if a hand-edit breaks the
    file's expected shape (a non-numeric ``value`` cell, a missing
    ``name``/``value`` column), the same "fail loudly at load time"
    convention occupancy's ``load_tapping_categories()`` already
    establishes for this kind of user-editable reference table.
    """
    target = files(_PACKAGE).joinpath(_RESOURCE)
    with target.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(
            (line for line in handle if not line.lstrip().startswith("#")),
        )
        if reader.fieldnames is None or {"name", "value"} - set(reader.fieldnames):
            raise ValueError(
                f"{_RESOURCE} is missing required columns 'name'/'value' "
                f"(found: {reader.fieldnames})"
            )
        values: dict[str, float] = {}
        for row in reader:
            name = row["name"].strip()
            try:
                values[name] = float(row["value"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{_RESOURCE}: row {name!r} has a non-numeric value "
                    f"{row['value']!r}"
                ) from exc
    if not values:
        raise ValueError(f"{_RESOURCE} contains no data rows")
    return values


@dataclass(frozen=True)
class SetbackProfile:
    """One row of ``setback_profiles.csv`` -- a heating setpoint setback
    schedule, expressed against occupant presence rather than clock time."""

    profile_name: str
    away_setback_k: float
    asleep_setback_k: float
    max_setback_k: float
    min_setpoint_c: float
    description: str
    source: str


@lru_cache(maxsize=1)
def load_setback_profiles() -> dict[str, SetbackProfile]:
    """Load ``setback_profiles.csv`` as ``{profile_name: SetbackProfile}``.

    Cached per process. Raises ``ValueError`` naming the problem on a
    broken hand-edit rather than dropping the row: a silently ignored
    setback would change heating demand with no signal.
    """
    numeric = ("away_setback_k", "asleep_setback_k", "max_setback_k", "min_setpoint_c")
    required = {"profile_name", *numeric}
    target = files(_PACKAGE).joinpath(_SETBACK_RESOURCE)
    with target.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(
            (line for line in handle if not line.lstrip().startswith("#")),
        )
        if reader.fieldnames is None or required - set(reader.fieldnames):
            raise ValueError(
                f"{_SETBACK_RESOURCE} is missing required column(s) "
                f"{sorted(required - set(reader.fieldnames or []))} "
                f"(found: {reader.fieldnames})"
            )
        table: dict[str, SetbackProfile] = {}
        for row in reader:
            name = (row["profile_name"] or "").strip()
            try:
                values = {key: float(row[key]) for key in numeric}
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{_SETBACK_RESOURCE}: row {name!r} has a non-numeric value "
                    f"in one of {list(numeric)}"
                ) from exc
            for key in ("away_setback_k", "asleep_setback_k", "max_setback_k"):
                if values[key] < 0:
                    raise ValueError(
                        f"{_SETBACK_RESOURCE}: row {name!r} has a negative "
                        f"{key} ({values[key]}). A setback lowers the setpoint; "
                        "express it as a positive number of kelvin."
                    )
            table[name] = SetbackProfile(
                profile_name=name,
                description=(row.get("description") or "").strip(),
                source=(row.get("source") or "").strip(),
                **values,
            )
    if not table:
        raise ValueError(f"{_SETBACK_RESOURCE} contains no data rows")
    return table


def resolve_setback_profile(name: object) -> SetbackProfile | None:
    """Resolve a setback profile name, or ``None`` for no setback.

    ``None`` and ``"none"`` both mean no setback, which is the default:
    see the CSV's own header for why this is off unless asked for.
    An unrecognised name raises rather than silently applying nothing --
    a typo that quietly disabled a scenario would be invisible in the
    results.
    """
    if name is None:
        return None
    text = str(name).strip()
    if not text or text.lower() == "none":
        return None
    table = load_setback_profiles()
    if text not in table:
        raise ValueError(
            f"Unknown setback_profile {text!r}. Known profiles: "
            f"{sorted(table)}. Add a row to {_SETBACK_RESOURCE}."
        )
    return table[text]


def resolve_envelope_reference(
    explicit_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    *,
    country: str = "NL",
) -> pd.DataFrame | None:
    """Locate and load the as-built envelope U-value table.

    Precedence, most specific first:

    1. ``explicit_path`` -- a table named on the command line.
    2. ``data_dir / "u_value_reference.csv"`` -- a region shipping its own
       table because its stock genuinely differs from the national one.
    3. The packaged ``<country>_envelope_reference.csv`` under
       ``data/reference/`` -- the shared default, and the file a reviewer
       is expected to read and edit.

    Returns ``None`` only when no table exists at any level for this
    country, which leaves the caller on raw TABULA U-values. Every step
    that falls through is logged with the path it tried, so a run never
    silently uses a different table than intended.
    """
    import pandas as pd  # local: keeps this module importable without pandas

    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.exists():
            raise FileNotFoundError(
                f"U-value override table not found: {path}. Remove the option "
                "to fall back to the region or packaged table."
            )
        logger.info("Envelope reference: explicit path %s", path)
        return pd.read_csv(path, comment="#")

    if data_dir is not None:
        regional = Path(data_dir) / "u_value_reference.csv"
        if regional.exists():
            logger.info("Envelope reference: region-local %s", regional)
            return pd.read_csv(regional, comment="#")

    resource = f"reference/{country.lower()}_envelope_reference.csv"
    target = files(_PACKAGE).joinpath(resource)
    if not target.is_file():
        logger.warning(
            "No envelope reference for country=%r (looked for %s, and for a "
            "u_value_reference.csv beside the building data) -- falling back "
            "to raw TABULA U-values.", country, resource,
        )
        return None
    logger.info("Envelope reference: packaged %s", resource)
    with target.open("r", encoding="utf-8") as handle:
        return pd.read_csv(handle, comment="#")


@dataclass(frozen=True)
class GlazingSpec:
    """One row of ``glazing_reference.csv`` -- a glazing product class."""

    glazing_type: str
    u_value: float
    g_value: float
    description: str
    source: str


@lru_cache(maxsize=1)
def load_glazing_table() -> dict[str, GlazingSpec]:
    """Load ``glazing_reference.csv`` as ``{glazing_type: GlazingSpec}``.

    Cached per process. Raises ``ValueError`` naming the problem if a
    hand-edit breaks the file, rather than dropping the row -- a silently
    ignored window U-value would change every simulation's heat loss with
    no signal.
    """
    required = {"glazing_type", "u_value", "g_value"}
    target = files(_PACKAGE).joinpath(_GLAZING_RESOURCE)
    with target.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(
            (line for line in handle if not line.lstrip().startswith("#")),
        )
        if reader.fieldnames is None or required - set(reader.fieldnames):
            raise ValueError(
                f"{_GLAZING_RESOURCE} is missing required column(s) "
                f"{sorted(required - set(reader.fieldnames or []))} "
                f"(found: {reader.fieldnames})"
            )
        table: dict[str, GlazingSpec] = {}
        for row in reader:
            name = (row["glazing_type"] or "").strip()
            try:
                u_value = float(row["u_value"])
                g_value = float(row["g_value"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{_GLAZING_RESOURCE}: row {name!r} has a non-numeric "
                    f"u_value/g_value ({row['u_value']!r}/{row['g_value']!r})"
                ) from exc
            if u_value <= 0:
                raise ValueError(
                    f"{_GLAZING_RESOURCE}: row {name!r} has a non-positive "
                    f"u_value {u_value}"
                )
            if not 0.0 < g_value <= 1.0:
                raise ValueError(
                    f"{_GLAZING_RESOURCE}: row {name!r} has g_value {g_value} "
                    "outside (0, 1]"
                )
            table[name] = GlazingSpec(
                glazing_type=name, u_value=u_value, g_value=g_value,
                description=(row.get("description") or "").strip(),
                source=(row.get("source") or "").strip(),
            )
    if not table:
        raise ValueError(f"{_GLAZING_RESOURCE} contains no data rows")
    return table


def resolve_glazing(value: object) -> GlazingSpec | None:
    """Resolve a glazing reference to its :class:`GlazingSpec`.

    Accepts a glazing type name from ``glazing_reference.csv``. A numeric
    value is *not* a glazing type and returns ``None``, letting the caller
    keep treating it as a bare U-value -- envelope tables predating this
    file carry numbers, and both forms stay readable.

    An unrecognised name raises rather than silently degrading: a typo in
    a hand-edited table would otherwise substitute a default U-value and
    change results with no signal.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None
    name = str(value).strip()
    if not name:
        return None
    try:
        float(name)
    except ValueError:
        pass
    else:
        return None
    table = load_glazing_table()
    if name not in table:
        raise ValueError(
            f"Unknown glazing_type {name!r}. Known types: "
            f"{sorted(table)}. Add a row to {_GLAZING_RESOURCE} or use a "
            "numeric U-value."
        )
    return table[name]


@dataclass(frozen=True)
class NumPersonsRow:
    """One row of ``num_persons_by_building_type.csv``."""

    country: str
    region_code: str
    building_type: str
    num_persons: float
    source: str


@lru_cache(maxsize=1)
def load_num_persons_table() -> tuple[NumPersonsRow, ...]:
    """Load ``num_persons_by_building_type.csv`` as a tuple of rows.

    Cached per process, like :func:`load_dhw_cooking_constants`. Raises
    ``ValueError`` naming the problem if a hand-edit breaks the file's
    expected shape, rather than silently dropping the bad row -- a
    quietly-ignored occupancy figure would change every simulation's
    output with no signal.
    """
    required = {"country", "region_code", "building_type", "num_persons"}
    target = files(_PACKAGE).joinpath(_NUM_PERSONS_RESOURCE)
    with target.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(
            (line for line in handle if not line.lstrip().startswith("#")),
        )
        if reader.fieldnames is None or required - set(reader.fieldnames):
            raise ValueError(
                f"{_NUM_PERSONS_RESOURCE} is missing required column(s) "
                f"{sorted(required - set(reader.fieldnames or []))} "
                f"(found: {reader.fieldnames})"
            )
        rows: list[NumPersonsRow] = []
        for row in reader:
            building_type = (row["building_type"] or "").strip()
            try:
                value = float(row["num_persons"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{_NUM_PERSONS_RESOURCE}: row for building_type="
                    f"{building_type!r} has a non-numeric num_persons "
                    f"{row['num_persons']!r}"
                ) from exc
            if value <= 0:
                raise ValueError(
                    f"{_NUM_PERSONS_RESOURCE}: row for building_type="
                    f"{building_type!r} has a non-positive num_persons {value}"
                )
            rows.append(NumPersonsRow(
                country=(row["country"] or ANY).strip(),
                region_code=(row["region_code"] or ANY).strip(),
                building_type=building_type,
                num_persons=value,
                source=(row.get("source") or "").strip(),
            ))
    if not rows:
        raise ValueError(f"{_NUM_PERSONS_RESOURCE} contains no data rows")
    return tuple(rows)


def resolve_num_persons(
    building_type: str | None,
    *,
    country: str | None = None,
    region_code: str | None = None,
    default: float | None = None,
) -> float | None:
    """Occupants per dwelling for one building type, most specific first.

    Lookup order, stopping at the first match:

    1. exact ``(country, region_code, building_type)``
    2. ``(country, ANY, building_type)`` -- country-wide
    3. ``(ANY, ANY, building_type)`` -- generic per-type fallback
    4. ``default``

    so a country or region only needs rows where it genuinely differs.
    Returns ``default`` (``None`` unless given) for an unknown or missing
    ``building_type``, which is the correct answer for a service building:
    its occupancy comes from ``capacity``, not from a household size.

    Parameters
    ----------
    building_type:
        buem's own class, e.g. ``"SFH"``. Matched case-sensitively, as
        the CSV stores them.
    country:
        ISO-style country code, e.g. ``"NL"``.
    region_code:
        Statistical region identifier whose meaning is the country's own
        -- a CBS ``RegioS`` municipality code such as ``"GM0200"`` for
        the Netherlands. buem does not interpret it beyond matching.
    """
    if not building_type:
        return default
    rows = load_num_persons_table()
    candidates = (
        (country, region_code),
        (country, ANY),
        (ANY, ANY),
    )
    for want_country, want_region in candidates:
        if want_country is None or want_region is None:
            continue
        for row in rows:
            if (
                row.building_type == building_type
                and row.country == want_country
                and row.region_code == want_region
            ):
                return row.num_persons
    return default


__all__ = [
    "ANY",
    "GlazingSpec",
    "NumPersonsRow",
    "SetbackProfile",
    "load_dhw_cooking_constants",
    "load_glazing_table",
    "load_num_persons_table",
    "load_setback_profiles",
    "resolve_envelope_reference",
    "resolve_glazing",
    "resolve_num_persons",
    "resolve_setback_profile",
]
