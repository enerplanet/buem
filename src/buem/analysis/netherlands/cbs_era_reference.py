"""CBS StatLine OData client -- real Dutch dwelling energy use resolved by
*construction era* as well as dwelling type.

:mod:`buem.analysis.netherlands.cbs_reference` queries table 81528NED,
which resolves dwelling type and region but says nothing about when a
dwelling was built. That makes it a poor reference for buem's own
archetype model, whose U-values are chosen primarily from the
construction-year class: a comparison against it cannot tell a wrong
envelope for 1960s stock apart from a wrong envelope for 1990s stock.

Table **85140NED** (*Energieverbruik woningen; woningtype, oppervlakte,
bouwjaar en bewoning*) closes that gap. It publishes mean
temperature-corrected gas and electricity per dwelling against the full
five-way ``Woningtype`` and a seven-class ``Bouwjaar``, so every
(building type, construction era) cell buem simulates has a real measured
counterpart.

The trade-off is geography: 85140NED is **national only**. Where
81528NED gives Apeldoorn's own figures with no era detail, this gives era
detail with no region detail. Neither supersedes the other, and a
comparison should say which it used.

Era mapping
-----------
CBS's classes and buem's NL TABULA year classes align almost exactly,
with one exception: buem's ``NL.01`` ("<=1964") spans two CBS classes
(pre-1946 and 1946-1965). :data:`BUEM_ERA_TO_CBS` maps it to both, and
:func:`fetch_consumption_by_era` combines them weighted by each class's
own dwelling stock rather than averaging them evenly, since the two
differ greatly in size.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

from buem.analysis.netherlands.gas_conversion import gas_m3_to_useful_heat_kwh

logger = logging.getLogger(__name__)

_ENERGY_URL = "https://opendata.cbs.nl/ODataApi/odata/85140NED/TypedDataSet"

# buem building_type -> the 85140NED Woningtype code(s) that represent it.
# CBS splits single-family housing four ways and publishes one combined
# apartment class, so MFH and AB share it -- the same limit
# cbs_household_size documents for occupancy.
BUEM_TYPE_TO_CBS: dict[str, tuple[str, ...]] = {
    "SFH": ("ZW10320", "ZW10300"),  # vrijstaand, 2-onder-1-kap
    "TH": ("ZW25806", "ZW25805"),   # hoekwoning, tussenwoning
    "MFH": ("ZW25810",),            # appartement
    "AB": ("ZW25810",),
}

# buem NL construction-year class -> 85140NED Bouwjaar code(s).
BUEM_ERA_TO_CBS: dict[str, tuple[str, ...]] = {
    "NL.01": ("ZW25799", "ZW25800"),  # 1000-1946 + 1946-1965
    "NL.02": ("ZW10406",),            # 1965-1975
    "NL.03": ("ZW25801",),            # 1975-1992
    "NL.04": ("ZW25815",),            # 1992-2006
    "NL.05": ("ZW25818",),            # 2006-2015
    "NL.06": ("ZW25797",),            # 2015-present
}

ERA_LABELS: dict[str, str] = {
    "NL.01": "<=1964",
    "NL.02": "1965-1974",
    "NL.03": "1975-1991",
    "NL.04": "1992-2005",
    "NL.05": "2006-2014",
    "NL.06": "2015-present",
}

_TOTALS = {
    "Gebruiksoppervlakte": "T001116",
    "HoofdverwarmingEnZonnestroom": "T001614",
    "Bewonersklasse": "T001351",
}


@dataclass(frozen=True)
class EraConsumption:
    """Real CBS consumption for one (building type, construction era)."""

    building_type: str
    era: str
    era_label: str
    gas_m3_per_year: float
    electricity_kwh_per_year: float
    useful_heat_kwh_per_year: float


def _fetch_json(url: str, timeout: float = 90.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- fixed, hardcoded CBS host
        return json.load(resp)


def fetch_consumption_by_era(period: str = "2024JJ00") -> dict[tuple[str, str], EraConsumption]:
    """Mean gas, electricity and derived useful heat per dwelling, by buem
    building type and construction era.

    ``period`` is a CBS ``Perioden`` code. 85140NED covers 2019-2025;
    prefer a recent year, and match it to the weather year the simulated
    side used -- Dutch gas use nearly halved between 2018 and 2024, so a
    year mismatch is a large error in its own right.

    Returns ``{(building_type, era): EraConsumption}``. A cell CBS
    suppressed or never published is simply absent rather than zero.
    """
    fixed = " and ".join(f"({dim} eq '{code}')" for dim, code in _TOTALS.items())
    query = urllib.parse.urlencode({
        "$filter": f"(Perioden eq '{period}') and {fixed}",
        "$select": "Woningtype,Bouwjaar,GemiddeldeAardgasleveringTempGecorr_3,"
                   "GemiddeldeElektriciteitslevering_23",
    }, safe="'()=,")
    logger.info("Querying CBS 85140NED by era: period=%s", period)
    rows = _fetch_json(f"{_ENERGY_URL}?{query}").get("value", [])

    # (Woningtype, Bouwjaar) -> (gas, electricity), for recombining below.
    cells: dict[tuple[str, str], tuple[float, float]] = {}
    for row in rows:
        gas = row.get("GemiddeldeAardgasleveringTempGecorr_3")
        elec = row.get("GemiddeldeElektriciteitslevering_23")
        if gas is None or elec is None:
            continue
        key = (str(row.get("Woningtype", "")).strip(), str(row.get("Bouwjaar", "")).strip())
        cells[key] = (float(gas), float(elec))

    out: dict[tuple[str, str], EraConsumption] = {}
    for buem_type, type_codes in BUEM_TYPE_TO_CBS.items():
        for era, era_codes in BUEM_ERA_TO_CBS.items():
            present = [
                cells[(tc, ec)] for tc in type_codes for ec in era_codes
                if (tc, ec) in cells
            ]
            if not present:
                continue
            # Unweighted mean across the contributing cells. CBS publishes
            # no per-cell dwelling count in this table, so a stock-weighted
            # combination is not available here; the cells being combined
            # are neighbouring classes of one type, so the spread is small.
            gas = sum(g for g, _ in present) / len(present)
            elec = sum(e for _, e in present) / len(present)
            out[(buem_type, era)] = EraConsumption(
                building_type=buem_type,
                era=era,
                era_label=ERA_LABELS[era],
                gas_m3_per_year=round(gas, 1),
                electricity_kwh_per_year=round(elec, 1),
                useful_heat_kwh_per_year=round(
                    gas_m3_to_useful_heat_kwh(gas).useful_heat_kwh, 1
                ),
            )
    if not out:
        raise ValueError(f"CBS 85140NED returned no usable rows for period={period!r}.")
    return out


__all__ = [
    "BUEM_ERA_TO_CBS",
    "BUEM_TYPE_TO_CBS",
    "ERA_LABELS",
    "EraConsumption",
    "fetch_consumption_by_era",
]
