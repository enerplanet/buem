"""
BAG use-function (``gebruiksdoel``) reader -- the signal that separates a
real non-residential building from a dwelling.

Why this exists
---------------
``rivm_energy_labels``' ``aant_verblijfsobj`` counts *verblijfsobjecten*
(BAG addressable units) registered under a Pand, but carries no
indication of what those units are **for**. A village shop, office or
school has exactly the same "one registered unit" signature as a
single-family house, so a classifier working from unit counts alone
routes it into the residential TABULA path and simulates it as a
dwelling.

BAG's own ``gebruiksdoel`` attribute is the authoritative answer: it is
the legally registered use function of each verblijfsobject
(``woonfunctie``, ``winkelfunctie``, ``kantoorfunctie``, ...). It lives
on the verblijfsobject record rather than the Pand, which is why it is
absent from both the 3D BAG Pand attributes ``cityjson_extractor``
reads and the RIVM energy-labels export.

Data source and shape
---------------------
The extract is produced by ``scripts/fetch_bag_use_functions.py`` from
the national BAG WFS service published by PDOK (the Dutch public
geo-data platform), and cached as a per-region CSV alongside the
region's other reference tables. It is a machine-local artifact of a
real fetch rather than a shipped dataset -- the same treatment
``weather``'s processed archives and the RIVM GeoPackage already get.

Expected columns::

    bag_pand_id     TEXT  -- full "NL.IMBAG.Pand.<digits>" form, matching
                             cityjson_extractor's own column
    gebruiksdoel    TEXT  -- comma-joined use functions of one
                             verblijfsobject, e.g. "woonfunctie" or
                             "gezondheidszorgfunctie,winkelfunctie"
    oppervlakte     REAL  -- the unit's registered floor area [m2]
    status          TEXT  -- BAG lifecycle status

One row per verblijfsobject, so a Pand with several units has several
rows; :func:`summarize_use_by_pand` collapses that to one record per
building.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

RESIDENTIAL_FUNCTION = "woonfunctie"
"""The one ``gebruiksdoel`` value that denotes a dwelling."""

IN_USE_STATUS_PREFIX = "Verblijfsobject in gebruik"
"""Only units in active use describe a building's real function. BAG also
carries units that are merely *formed* (``Verblijfsobject gevormd``,
registered but not yet occupied) or under renovation; counting those
would attribute a use to a building that does not yet have one."""


@dataclass(frozen=True)
class PandUseSummary:
    """One building's registered use, collapsed from its unit rows."""

    n_residential_units: int
    """Units carrying ``woonfunctie`` -- the dwelling count."""

    n_non_residential_units: int
    """Units carrying at least one function other than ``woonfunctie``."""

    function_counts: dict[str, int]
    """Unit count per non-residential function, e.g.
    ``{"winkelfunctie": 2, "kantoorfunctie": 1}``."""

    non_residential_area_m2: float
    """Summed registered floor area of the non-residential units."""

    @property
    def is_purely_non_residential(self) -> bool:
        """True when the building has a registered use and none of it is
        residential.

        A building with *both* is genuinely mixed-use (the classic Dutch
        shop-with-a-flat-above) and is deliberately not claimed here:
        buem models one use per building, so a mixed building stays on
        the residential path rather than losing its dwellings.
        """
        return self.n_non_residential_units > 0 and self.n_residential_units == 0

    @property
    def dominant_function(self) -> str | None:
        """The most-registered non-residential function, or ``None``.

        Ties break alphabetically so the result is stable across runs
        rather than dependent on dict insertion order.
        """
        if not self.function_counts:
            return None
        return min(self.function_counts, key=lambda fn: (-self.function_counts[fn], fn))


def load_use_functions(csv_path: str | Path) -> pd.DataFrame:
    """Read a region's cached BAG use-function extract.

    Parameters
    ----------
    csv_path : str or Path
        Path to the extract written by
        ``scripts/fetch_bag_use_functions.py``.

    Returns
    -------
    pd.DataFrame
        The extract's rows, filtered to units in active use (see
        :data:`IN_USE_STATUS_PREFIX`).

    Raises
    ------
    FileNotFoundError
        If ``csv_path`` does not exist.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"BAG use-function extract not found: {csv_path}. "
            "Generate it with scripts/fetch_bag_use_functions.py."
        )
    df = pd.read_csv(csv_path, dtype={"bag_pand_id": str, "gebruiksdoel": str, "status": str})
    in_use = df[df["status"].fillna("").str.startswith(IN_USE_STATUS_PREFIX)]
    logger.info(
        "load_use_functions: %d verblijfsobjecten read, %d in active use",
        len(df), len(in_use),
    )
    return in_use.reset_index(drop=True)


def summarize_use_by_pand(use_df: pd.DataFrame) -> dict[str, PandUseSummary]:
    """Collapse per-verblijfsobject rows into one summary per Pand.

    A unit's ``gebruiksdoel`` may list several functions at once (BAG
    allows a single unit to be registered for more than one purpose);
    each is counted separately, but the unit contributes at most one to
    ``n_residential_units``/``n_non_residential_units``.
    """
    summaries: dict[str, PandUseSummary] = {}
    for pand_id, group in use_df.groupby("bag_pand_id"):
        n_residential = 0
        n_non_residential = 0
        functions: Counter[str] = Counter()
        non_residential_area = 0.0
        for _, row in group.iterrows():
            raw = row.get("gebruiksdoel")
            if not isinstance(raw, str) or not raw:
                continue
            unit_functions = [fn.strip() for fn in raw.split(",") if fn.strip()]
            other = [fn for fn in unit_functions if fn != RESIDENTIAL_FUNCTION]
            if RESIDENTIAL_FUNCTION in unit_functions:
                n_residential += 1
            if other:
                n_non_residential += 1
                functions.update(other)
                area = row.get("oppervlakte")
                if area is not None and not pd.isna(area):
                    non_residential_area += float(area)
        summaries[str(pand_id)] = PandUseSummary(
            n_residential_units=n_residential,
            n_non_residential_units=n_non_residential,
            function_counts=dict(functions),
            non_residential_area_m2=non_residential_area,
        )

    n_pure = sum(1 for s in summaries.values() if s.is_purely_non_residential)
    logger.info(
        "summarize_use_by_pand: %d buildings with a registered use, %d purely non-residential",
        len(summaries), n_pure,
    )
    return summaries


__all__ = [
    "IN_USE_STATUS_PREFIX",
    "RESIDENTIAL_FUNCTION",
    "PandUseSummary",
    "load_use_functions",
    "summarize_use_by_pand",
]
