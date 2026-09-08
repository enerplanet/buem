"""CBS StatLine OData client -- real Dutch household size by dwelling type.

Queried live (no bundled copy, no new HTTP-library dependency: stdlib
``urllib.request`` + ``json``), matching :mod:`buem.analysis.netherlands
.cbs_reference`'s approach to the same API.

Why three tables
----------------
No single CBS table publishes mean household size against the dwelling
types buem models, so this module combines three that together do:

``85035NED`` -- *Woningvoorraad; woningtype op 1 januari, regio*
    Dwelling stock and mean usable floor area, by dwelling type and
    region. Supplies the denominator of the occupancy ratio and the
    stock weights used to aggregate fine types into buem's own.

``86064NED`` -- *Personen; soort woonruimte, positie huishouden, regio,
31 december*
    Persons in private households, by type of living space and region.
    Its ``SoortWoonruimte`` dimension carries ``ZW10290`` and
    ``ZW10340`` -- eengezinswoning and meergezinswoning -- the same
    codes ``85035NED`` uses for its own aggregates, so persons divided
    by dwellings is an exact ratio rather than an estimate. Supplies
    the numerator.

``85140NED`` -- *Energieverbruik woningen; woningtype, oppervlakte,
bouwjaar en bewoning*
    National only, but resolves the full five-way ``Woningtype``
    (vrijstaande woning, 2-onder-1-kapwoning, hoekwoning, tussenwoning,
    appartement) that the exact ratio above cannot, and publishes mean
    electricity both per dwelling and per occupant. Their quotient is
    an occupancy proxy -- see :func:`fetch_occupancy_shape`.

The exact two-class ratio fixes the *level*; the national proxy fixes
the *shape* within each class. :func:`household_size_by_building_type`
combines them.

Limits worth knowing before using the output
--------------------------------------------
- CBS's finest geography for both regional tables is the municipality
  (``GM<4 digits>``). A locality inside a municipality -- a village such
  as Loenen within Apeldoorn -- has no CBS row of its own.
- ``MFH`` and ``AB`` cannot be separated from these tables: CBS
  publishes one ``appartement``/``meergezinswoning`` class covering
  both, so both receive the same figure. Callers holding a real
  per-dwelling usable floor area can refine that with
  :func:`occupancy_by_usable_area`, which exposes CBS's own measured
  relationship between apartment size and occupancy.
- The ratio's denominator is the whole dwelling *stock*, which includes
  unoccupied dwellings. The result is therefore persons per dwelling in
  stock, marginally below persons per *occupied* dwelling.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_STOCK_URL = "https://opendata.cbs.nl/ODataApi/odata/85035NED/TypedDataSet"
_PERSONS_URL = "https://opendata.cbs.nl/ODataApi/odata/86064NED/TypedDataSet"
_ENERGY_URL = "https://opendata.cbs.nl/ODataApi/odata/85140NED/TypedDataSet"

# Dwelling-type codes. The two aggregates are shared verbatim between
# 85035NED's Woningtype and 86064NED's SoortWoonruimte, which is what
# makes the exact persons-per-dwelling ratio possible.
EENGEZINS = "ZW10290"  # eengezinswoning -- single-family (buem SFH + TH)
MEERGEZINS = "ZW10340"  # meergezinswoning -- multi-family (buem MFH + AB)

# The five-way split, available in 85035NED (stock) and 85140NED
# (energy), mapped to the buem building_type each belongs to.
FINE_TYPE_TO_BUEM: dict[str, str] = {
    "ZW10320": "SFH",  # vrijstaande woning -- detached
    "ZW10300": "SFH",  # 2-onder-1-kapwoning -- semi-detached
    "ZW25806": "TH",  # hoekwoning -- corner terrace
    "ZW25805": "TH",  # tussenwoning -- mid terrace
    "ZW25810": "MFH",  # appartement -- see module docstring on MFH/AB
}

FINE_TYPE_LABELS: dict[str, str] = {
    "ZW10320": "vrijstaande woning",
    "ZW10300": "2-onder-1-kapwoning",
    "ZW25806": "hoekwoning",
    "ZW25805": "tussenwoning",
    "ZW25810": "appartement",
}

# "Persoon in particulier huishouden" -- excludes institutional
# households (care homes, student halls counted as institutions), whose
# occupants are not a household in the sense occupancy.HouseholdProfile
# models.
_PRIVATE_HOUSEHOLD = "1050001"

# 85035NED's "Totaal woningen" Woningkenmerk, i.e. the whole stock
# rather than the newly-built subset the same table also publishes.
_TOTAL_STOCK = "T001727"

# 85140NED "Totaal" codes for the dimensions this module does not slice
# on. Bewonersklasse must be the total too: the per-occupant figure is
# only meaningful across all occupancy classes at once.
_ENERGY_TOTALS = {
    "Gebruiksoppervlakte": "T001116",
    "Bouwjaar": "T001018",
    "HoofdverwarmingEnZonnestroom": "T001614",
    "Bewonersklasse": "T001351",
}

# 85140NED Gebruiksoppervlakte bands, with the midpoint used to place a
# caller-supplied floor area on the curve. The open-topped band is
# represented by a value near its lower edge rather than its nominal
# 10 000 m2 ceiling, which no dwelling approaches.
USABLE_AREA_BANDS: tuple[tuple[str, float, float], ...] = (
    ("A050300", 2.0, 50.0),
    ("A025408", 50.0, 75.0),
    ("A025409", 75.0, 100.0),
    ("A025410", 100.0, 150.0),
    ("A025411", 150.0, 250.0),
    ("A050301", 250.0, 400.0),
)

# National figures from the live queries in this module, retained so a
# caller without network access still gets real per-type values instead
# of buem's flat DEFAULT_NUM_PERSONS. Persons per dwelling in stock,
# private households only. Reproduce with
# household_size_by_building_type("NL01").
NATIONAL_HOUSEHOLD_SIZE_BY_BUILDING_TYPE: dict[str, float] = {
    "SFH": 2.55,
    "TH": 2.41,
    "MFH": 1.54,
    "AB": 1.54,
}


@dataclass(frozen=True)
class DwellingStock:
    """One (region, period, dwelling type) row from CBS table 85035NED."""

    type_code: str
    dwellings: float
    mean_usable_area_m2: float | None


@dataclass(frozen=True)
class HouseholdSizeResult:
    """Mean occupants per dwelling for one region, by buem building type.

    ``by_building_type`` is the figure to use as ``num_persons``.
    ``anchors`` and ``shape`` are retained so a caller can report how a
    value was arrived at rather than quoting it bare.
    """

    region_code: str
    persons_period: str
    stock_period: str
    by_building_type: dict[str, float]
    anchors: dict[str, float]
    shape: dict[str, float]
    calibration: dict[str, float]


def _fetch_json(url: str, timeout: float = 60.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- fixed, hardcoded CBS host
        return json.load(resp)


def _query(base_url: str, filter_expr: str, select: str) -> list[dict]:
    query = urllib.parse.urlencode(
        {"$filter": filter_expr, "$select": select}, safe="'()=,"
    )
    return _fetch_json(f"{base_url}?{query}").get("value", [])


def _matches(row_value: object, code: str) -> bool:
    """CBS pads dimension values to a fixed width, so a national code
    such as ``"NL01"`` arrives as ``"NL01  "``. Server-side ``eq``
    filters miss those, hence comparison after stripping."""
    return isinstance(row_value, str) and row_value.strip() == code


def fetch_dwelling_stock(region_code: str, period: str) -> dict[str, DwellingStock]:
    """Dwelling stock and mean usable floor area, by dwelling type.

    Parameters
    ----------
    region_code : str
        CBS ``RegioS`` code -- ``"NL01"`` national, or a municipality
        such as ``"GM0200"`` (Apeldoorn) / ``"GM0177"`` (Raalte).
    period : str
        CBS ``Perioden`` code, e.g. ``"2025JJ00"``. This table counts
        the stock on 1 January of the named year.

    Returns
    -------
    dict
        ``{type_code: DwellingStock}``, covering the five fine types and
        the two aggregates, for whichever CBS actually returned.
    """
    logger.info("Querying CBS 85035NED: region=%s period=%s", region_code, period)
    rows = _query(
        _STOCK_URL,
        f"(Perioden eq '{period}') and (Woningkenmerk eq '{_TOTAL_STOCK}')",
        "RegioS,Woningtype,BeginstandWoningvoorraad_1,GemiddeldeOppervlakte_2",
    )
    wanted = set(FINE_TYPE_TO_BUEM) | {EENGEZINS, MEERGEZINS}
    out: dict[str, DwellingStock] = {}
    for row in rows:
        if not _matches(row.get("RegioS"), region_code):
            continue
        code = str(row.get("Woningtype", "")).strip()
        count = row.get("BeginstandWoningvoorraad_1")
        if code not in wanted or count is None:
            continue
        area = row.get("GemiddeldeOppervlakte_2")
        out[code] = DwellingStock(code, float(count), None if area is None else float(area))
    if not out:
        raise ValueError(
            f"CBS 85035NED returned no dwelling stock for region={region_code!r} "
            f"period={period!r}."
        )
    return out


def fetch_persons_in_dwellings(region_code: str, period: str) -> dict[str, float]:
    """Persons in private households, by single-/multi-family dwelling.

    ``period`` is a CBS ``Perioden`` code, e.g. ``"2024JJ00"``. This
    table counts persons on 31 December of the named year, so pairing it
    with :func:`fetch_dwelling_stock` for 1 January of the *following*
    year compares two counts taken at the same instant.

    Returns ``{EENGEZINS: persons, MEERGEZINS: persons}``.
    """
    logger.info("Querying CBS 86064NED: region=%s period=%s", region_code, period)
    rows = _query(
        _PERSONS_URL,
        f"(Perioden eq '{period}') and "
        f"(PositieInHetHuishouden eq '{_PRIVATE_HOUSEHOLD}')",
        "RegioS,SoortWoonruimte,Personen_1",
    )
    out: dict[str, float] = {}
    for row in rows:
        if not _matches(row.get("RegioS"), region_code):
            continue
        code = str(row.get("SoortWoonruimte", "")).strip()
        persons = row.get("Personen_1")
        if code in (EENGEZINS, MEERGEZINS) and persons is not None:
            out[code] = float(persons)
    if not out:
        raise ValueError(
            f"CBS 86064NED returned no person counts for region={region_code!r} "
            f"period={period!r}."
        )
    return out


def fetch_occupancy_shape(period: str) -> dict[str, float]:
    """National occupancy proxy per fine dwelling type, from 85140NED.

    CBS publishes, for each dwelling type, both mean electricity per
    dwelling and mean electricity per occupant. Their quotient carries
    no assumption about how much electricity a person uses -- the same
    consumption appears in both -- leaving a dimensionless estimate of
    occupants per dwelling.

    The estimate is biased low in level, because CBS's per-occupant
    figure averages each dwelling's own consumption-per-occupant rather
    than dividing two population means; by Jensen's inequality that
    exceeds the ratio of means whenever consumption grows sub-linearly
    with occupancy, as it does. The bias is a roughly common factor
    across types, so the *relative* pattern survives it. Callers should
    therefore treat this as a shape to be calibrated against an exact
    anchor, which is what :func:`household_size_by_building_type` does,
    rather than as an occupancy figure in its own right.

    Returns ``{fine type_code: implied occupants per dwelling}``.
    """
    logger.info("Querying CBS 85140NED: period=%s", period)
    totals = " and ".join(f"({dim} eq '{code}')" for dim, code in _ENERGY_TOTALS.items())
    rows = _query(
        _ENERGY_URL,
        f"(Perioden eq '{period}') and {totals}",
        "Woningtype,GemiddeldeElektriciteitslevering_23,"
        "GemElektriciteitsleveringPerBewoner_33",
    )
    out: dict[str, float] = {}
    for row in rows:
        code = str(row.get("Woningtype", "")).strip()
        per_dwelling = row.get("GemiddeldeElektriciteitslevering_23")
        per_occupant = row.get("GemElektriciteitsleveringPerBewoner_33")
        if code in FINE_TYPE_TO_BUEM and per_dwelling and per_occupant:
            out[code] = float(per_dwelling) / float(per_occupant)
    if not out:
        raise ValueError(f"CBS 85140NED returned no energy rows for period={period!r}.")
    return out


def occupancy_by_usable_area(period: str, type_code: str = "ZW25810") -> list[tuple[float, float, float]]:
    """The occupancy proxy resolved by usable floor area, for one type.

    Exposes CBS's own measured relationship between dwelling size and
    occupancy, which is the only evidence in these tables capable of
    separating a large-unit multi-family house from a small-unit
    apartment block -- the ``MFH``/``AB`` distinction CBS itself does
    not publish. Subject to the same level bias as
    :func:`fetch_occupancy_shape`, so use it for relative placement.

    Returns ``[(band_lower_m2, band_upper_m2, implied occupants), ...]``
    ordered by size, for whichever bands CBS populated.
    """
    fixed = " and ".join(
        f"({dim} eq '{code}')"
        for dim, code in _ENERGY_TOTALS.items()
        if dim != "Gebruiksoppervlakte"
    )
    rows = _query(
        _ENERGY_URL,
        f"(Perioden eq '{period}') and (Woningtype eq '{type_code}') and {fixed}",
        "Gebruiksoppervlakte,GemiddeldeElektriciteitslevering_23,"
        "GemElektriciteitsleveringPerBewoner_33",
    )
    by_band = {str(r.get("Gebruiksoppervlakte", "")).strip(): r for r in rows}
    out: list[tuple[float, float, float]] = []
    for code, lower, upper in USABLE_AREA_BANDS:
        row = by_band.get(code)
        if row is None:
            continue
        per_dwelling = row.get("GemiddeldeElektriciteitslevering_23")
        per_occupant = row.get("GemElektriciteitsleveringPerBewoner_33")
        if per_dwelling and per_occupant:
            out.append((lower, upper, float(per_dwelling) / float(per_occupant)))
    return out


def household_size_by_building_type(
    region_code: str,
    *,
    persons_period: str = "2024JJ00",
    stock_period: str = "2025JJ00",
    shape_period: str = "2024JJ00",
) -> HouseholdSizeResult:
    """Mean occupants per dwelling for one region, by buem building type.

    Combines an exact regional ratio with a national shape:

    1. Persons in private households divided by dwellings in stock gives
       an exact mean occupancy for eengezinswoningen and for
       meergezinswoningen in this region -- two numbers, both real, but
       one class short of what buem needs.
    2. :func:`fetch_occupancy_shape` gives a national proxy for all five
       fine types, which distinguishes detached from terraced but is
       biased low in level.
    3. Stock-weighting the proxy across the four single-family types
       yields the class mean the proxy implies. The factor that maps
       that onto the exact anchor from step 1 rescales every fine type,
       removing the proxy's level bias while keeping its between-type
       shape and adapting it to this region.
    4. ``SFH`` and ``TH`` are then stock-weighted means of their fine
       types. ``MFH`` and ``AB`` both take the meergezins anchor
       unchanged, CBS publishing no split between them.

    The default periods pair persons on 31 December 2024 with stock on
    1 January 2025 -- the same instant.
    """
    stock = fetch_dwelling_stock(region_code, stock_period)
    persons = fetch_persons_in_dwellings(region_code, persons_period)
    shape = fetch_occupancy_shape(shape_period)

    anchors: dict[str, float] = {}
    for code in (EENGEZINS, MEERGEZINS):
        if code in persons and code in stock and stock[code].dwellings > 0:
            anchors[code] = persons[code] / stock[code].dwellings
    missing = {EENGEZINS, MEERGEZINS} - set(anchors)
    if missing:
        raise ValueError(
            f"Cannot anchor household size for region={region_code!r}: CBS is "
            f"missing persons or stock for {sorted(missing)}."
        )

    single_family = [c for c, t in FINE_TYPE_TO_BUEM.items() if t in ("SFH", "TH")]
    weighted = sum(
        stock[c].dwellings * shape[c] for c in single_family if c in stock and c in shape
    )
    weight = sum(stock[c].dwellings for c in single_family if c in stock and c in shape)
    if weight <= 0:
        raise ValueError(
            f"Cannot calibrate household size for region={region_code!r}: no "
            f"single-family stock matched CBS's fine dwelling types."
        )
    factor = anchors[EENGEZINS] / (weighted / weight)

    by_type: dict[str, float] = {}
    for buem_type in ("SFH", "TH"):
        codes = [
            c for c, t in FINE_TYPE_TO_BUEM.items()
            if t == buem_type and c in stock and c in shape
        ]
        total = sum(stock[c].dwellings for c in codes)
        if total <= 0:
            continue
        by_type[buem_type] = round(
            sum(stock[c].dwellings * shape[c] * factor for c in codes) / total, 3
        )
    by_type["MFH"] = round(anchors[MEERGEZINS], 3)
    by_type["AB"] = by_type["MFH"]

    logger.info(
        "CBS household size for %s: %s (eengezins anchor %.3f, calibration %.4f)",
        region_code,
        ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
        anchors[EENGEZINS],
        factor,
    )
    return HouseholdSizeResult(
        region_code=region_code,
        persons_period=persons_period,
        stock_period=stock_period,
        by_building_type=by_type,
        anchors={"eengezins": round(anchors[EENGEZINS], 4), "meergezins": round(anchors[MEERGEZINS], 4)},
        shape={FINE_TYPE_LABELS[c]: round(v, 4) for c, v in sorted(shape.items())},
        calibration={"single_family_factor": round(factor, 4)},
    )


__all__ = [
    "EENGEZINS",
    "FINE_TYPE_LABELS",
    "FINE_TYPE_TO_BUEM",
    "MEERGEZINS",
    "NATIONAL_HOUSEHOLD_SIZE_BY_BUILDING_TYPE",
    "USABLE_AREA_BANDS",
    "DwellingStock",
    "HouseholdSizeResult",
    "fetch_dwelling_stock",
    "fetch_occupancy_shape",
    "fetch_persons_in_dwellings",
    "household_size_by_building_type",
    "occupancy_by_usable_area",
]
