"""
Domestic hot water (DHW) and gas-cooking heat-demand energy conversion.

``occupancy.generate_dhw_draws()`` (``households/dhw.py``) generates
stochastic hourly DHW draw *volumes* in liters, and an optional
``cooking_active`` boolean signal identifying which hours a household's
kitchen equipment was in use. Converting volume/activity into energy is
outside occupancy's scope by design (occupancy owns occupant/equipment
behavior; buem owns temperature/delta-T/energy math). This module
performs that conversion.

Deliberately not part of ``ModelBUEM``'s 5R1C solve: DHW leaves the
building via the drain rather than acting as an internal heat gain, and
none of ISO 13790 / EN ISO 52016-1 / EN 15316-3 / NTA 8800 model it
through a building's RC network. Each standard instead computes DHW as a
separate net energy-need term added to the space-heating need afterward
-- the pattern this module's functions follow: they take
``ModelBUEM.sim_model()``'s already-solved output as given and produce an
independent additive term alongside it. Nothing here is wired into
``_addConstraints``/``_init5R1C``/``Q_ia``.

``AttributeBuilder.generate_electricity_profile()`` calls
``occupancy.generate_dhw_draws()`` for every residential building and
stores the result as ``cfg["dhw_liters"]``/``cfg["cooking_active"]``;
``ModelBUEM._addDhwCooking()`` (called from ``_readResults()``, after the
LP solve) calls this module's functions on those series. Both cfg keys
are optional -- their absence (service buildings, or any caller/test
predating this feature) is a no-op.
"""
from __future__ import annotations

import pandas as pd

from buem.config.reference_values import load_dhw_cooking_constants

# Every constant below is read from
# `src/buem/data/reference/dhw_cooking_constants.csv` rather than
# hand-typed here -- edit that CSV to change a value, no Python change
# needed. Module-level names stay stable so
# `from buem.thermal.dhw_cooking import DHW_DELTA_T_K` etc. keeps
# working regardless of the underlying value. See
# `buem.config.reference_values` and `scripts/extract_dhw_reference_values.py`
# for the loader and provenance-extraction tooling.
_CONSTANTS = load_dhw_cooking_constants()

WATER_DENSITY_KG_PER_L = _CONSTANTS["WATER_DENSITY_KG_PER_L"]
"""Water density, kg/L. Treated as constant across the domestic
hot-water temperature range."""

WATER_SPECIFIC_HEAT_KJ_PER_KGK = _CONSTANTS["WATER_SPECIFIC_HEAT_KJ_PER_KGK"]
"""Specific heat capacity of water, kJ/(kg*K)."""

DHW_DELTA_T_K = _CONSTANTS["DHW_DELTA_T_K"]
"""Cold-mains-to-delivery temperature rise, Kelvin, for converting a
drawn volume (e.g. ``occupancy.generate_dhw_draws()``'s stochastic
output) into energy. Source: EN 12831-3:2017 operating-conditions data
-- delivery/tap temperature 42 degC, cold-mains temperature 11.2 degC.
Cross-checks against McKenna & Thomson (2016, Applied Energy 165, Sec.
3.5.2), which independently assumes a 10 degC cold-mains temperature.

Distinct from :data:`DHW_DELTA_T_K_REFERENCE`: EN 12831-3 uses two
different temperature pairs for two different purposes -- this one for
converting a real drawn volume to energy, the other for normalizing a
per-person/day table volume into an annual "need" figure independent of
any real system's actual temperatures.

Not per-fixture (a kitchen-sink draw is typically delivered cooler than
a shower); override per call for finer per-fixture ΔT if needed."""

DHW_DELTA_T_K_REFERENCE = _CONSTANTS["DHW_DELTA_T_K_REFERENCE"]
"""EN 12831-3:2017's reference-temperature pair for normalizing a
per-person/day table volume (e.g. Annex Table B.5) into a comparable
annual energy need -- not the operating-conditions ΔT above. Reference
hot-water temperature 60 degC, reference cold-water temperature 13.5 degC.
Feeds :func:`dhw_energy_kwh_annual_fallback` only."""

DHW_LITERS_PER_PERSON_DAY_EN12831_3 = _CONSTANTS["DHW_LITERS_PER_PERSON_DAY_EN12831_3"]
"""EN 12831-3:2017 Annex Table B.5 reference DHW volume for
single-family dwellings, average case, liters/person/day.

Preferred over :data:`DHW_ANNUAL_KWH_PER_PERSON_NTA8800`: recomputing
that figure independently from NTA 8800's own cited 40.3 L/person/day
and its implicit ΔT does not reproduce the cited 545 kWh/year, an
internal-consistency check the NTA 8800 figure fails. This EN 12831-3
figure is self-consistent by construction -- volume and both reference
temperatures come from the same source document."""

DHW_ANNUAL_KWH_PER_PERSON_EN12831_3 = (
    DHW_LITERS_PER_PERSON_DAY_EN12831_3
    * WATER_DENSITY_KG_PER_L
    * WATER_SPECIFIC_HEAT_KJ_PER_KGK
    * DHW_DELTA_T_K_REFERENCE
    / 3600.0
    * 365.0
)
"""Derived from :data:`DHW_LITERS_PER_PERSON_DAY_EN12831_3` and
:data:`DHW_DELTA_T_K_REFERENCE` (~1085 kWh/person/year). Default
``annual_kwh_per_person`` for :func:`dhw_energy_kwh_annual_fallback`."""

DHW_ANNUAL_KWH_PER_PERSON_NTA8800 = _CONSTANTS["DHW_ANNUAL_KWH_PER_PERSON_NTA8800"]
"""NTA 8800's Dutch national default DHW energy need, kWh/person/year
(~40.3 L/person/day at an implicit, unstated ΔT -- see
:data:`DHW_LITERS_PER_PERSON_DAY_EN12831_3` for the internal-consistency
check this figure fails). Not independently verified against primary
NTA 8800 text (a paid NEN publication). Kept for reference/comparison
only; not the default -- see :func:`dhw_energy_kwh_annual_fallback`."""


def dhw_energy_kwh(
    dhw_liters: pd.Series,
    *,
    delta_t_k: float = DHW_DELTA_T_K,
) -> pd.Series:
    """Convert an hourly DHW draw-volume series (liters) into hourly useful
    -heat energy (kWh) via E = V * rho * c * deltaT.

    Parameters
    ----------
    dhw_liters:
        Hourly-indexed liters series -- typically
        ``occupancy.generate_dhw_draws(...)["dhw_liters_total"]``, or a
        single fixture's own ``dhw_liters_<fixture>`` column if per-fixture
        energy (e.g. a different ΔT for kitchen-sink vs. shower draws) is
        ever wanted instead of one blended total.
    delta_t_k:
        Cold-mains-to-delivery temperature rise, Kelvin. Defaults to
        :data:`DHW_DELTA_T_K`; override for a specific known cold-mains or
        delivery-temperature assumption.

    Returns
    -------
    An hourly kWh series, same index as ``dhw_liters``, named ``"dhw_kWh"``.
    Sum it for an annual total, or add it directly to
    ``ModelBUEM.sim_model()``'s ``heating_kWh`` output -- additive, never a
    replacement for it.
    """
    joules_per_liter = WATER_DENSITY_KG_PER_L * WATER_SPECIFIC_HEAT_KJ_PER_KGK * 1000.0 * delta_t_k
    kwh = dhw_liters * joules_per_liter / 3_600_000.0
    return kwh.rename("dhw_kWh")


DHW_COLD_MAINS_T_C = _CONSTANTS["DHW_COLD_MAINS_T_C"]
"""Cold-mains inlet temperature, degC -- the reference every per-fixture
delivery temperature is differenced against."""

DHW_DELIVERY_T_BY_FIXTURE: dict[str, float] = {
    "basin": _CONSTANTS["DHW_DELIVERY_T_BASIN_C"],
    "kitchen_sink": _CONSTANTS["DHW_DELIVERY_T_KITCHEN_SINK_C"],
    "shower": _CONSTANTS["DHW_DELIVERY_T_SHOWER_C"],
    "bath": _CONSTANTS["DHW_DELIVERY_T_BATH_C"],
}
"""Delivery temperature per fixture, degC, keyed by the suffix of
``occupancy.generate_dhw_draws()``'s own ``dhw_liters_<fixture>``
columns.

occupancy resolves its draws per fixture, and each is delivered at its
own temperature -- a kitchen sink runs far hotter than a wash basin.
Applying one blended figure to the summed total therefore misprices every
draw except the one it happens to match. See :func:`dhw_energy_kwh_by_fixture`.
"""


def dhw_energy_kwh_by_fixture(
    draws: pd.DataFrame,
    *,
    cold_mains_t_c: float = DHW_COLD_MAINS_T_C,
    delivery_t_by_fixture: dict[str, float] | None = None,
) -> pd.Series:
    """Convert occupancy's per-fixture DHW draw volumes into hourly energy,
    pricing each fixture at its own delivery temperature.

    ``occupancy.generate_dhw_draws()`` returns ``dhw_liters_basin``,
    ``dhw_liters_kitchen_sink``, ``dhw_liters_shower``,
    ``dhw_liters_bath`` and ``dhw_liters_total``. Using only the total, as
    :func:`dhw_energy_kwh` does, forces one blended temperature rise onto
    draws that genuinely differ -- overstating basin and shower energy
    against a kitchen-sink-weighted figure, understating it against a
    shower-weighted one.

    Falls back to the blended calculation on ``dhw_liters_total`` when no
    per-fixture column is present, so a caller holding only a total series
    still gets an answer.

    Returns an hourly kWh series named ``"dhw_kWh"``.
    """
    temperatures = delivery_t_by_fixture or DHW_DELIVERY_T_BY_FIXTURE
    joules_per_liter_per_k = WATER_DENSITY_KG_PER_L * WATER_SPECIFIC_HEAT_KJ_PER_KGK * 1000.0

    total: pd.Series | None = None
    for fixture, delivery_t in temperatures.items():
        column = f"dhw_liters_{fixture}"
        if column not in draws:
            continue
        delta_t = delivery_t - cold_mains_t_c
        if delta_t <= 0:
            raise ValueError(
                f"Delivery temperature for {fixture!r} ({delivery_t} degC) is not "
                f"above the cold-mains temperature ({cold_mains_t_c} degC)."
            )
        energy = draws[column] * joules_per_liter_per_k * delta_t / 3_600_000.0
        total = energy if total is None else total + energy

    if total is None:
        if "dhw_liters_total" not in draws:
            raise ValueError(
                "draws carries neither per-fixture dhw_liters_<fixture> columns "
                f"nor dhw_liters_total (found: {list(draws.columns)})."
            )
        return dhw_energy_kwh(draws["dhw_liters_total"])
    return total.rename("dhw_kWh")


def dhw_energy_kwh_annual_fallback(
    num_persons: float,
    *,
    annual_kwh_per_person: float = DHW_ANNUAL_KWH_PER_PERSON_EN12831_3,
) -> float:
    """Flat annual DHW energy estimate (kWh/year), for use only when no real
    ``occupancy.generate_dhw_draws()`` liters series is available. Prefer
    :func:`dhw_energy_kwh` whenever one is -- this is a coarser,
    non-stochastic, non-hourly stand-in, comparable in role to
    `gas_conversion.py`'s CBS split: a documented placeholder, not a model
    of any specific building's actual behavior. Pass
    ``annual_kwh_per_person=DHW_ANNUAL_KWH_PER_PERSON_NTA8800`` to use the
    NTA 8800 figure instead of the default EN 12831-3 one.
    """
    if num_persons <= 0:
        raise ValueError(f"num_persons must be positive, got {num_persons}")
    return num_persons * annual_kwh_per_person


SPACE_HEATING_SHARE_OF_GAS = _CONSTANTS["SPACE_HEATING_SHARE_OF_GAS"]
"""Fraction of a typical Dutch household's total gas consumption used for
space heating specifically (vs. domestic hot water ~20%, cooking ~2%).
Source: CBS's 2016 national household energy breakdown -- a blended
national average, not housing-type-specific. Single source of truth for
this and the two constants below; `buem.analysis.netherlands.
gas_conversion` (the Netherlands CBS-validation script) imports all three
from here rather than duplicating them, since the CBS split is the same
physical assumption whether it's stripping DHW/cooking off CBS's side of
a validation comparison or deriving a per-building cooking-energy total
for `cooking_annual_kwh_from_heating` below."""

DHW_SHARE_OF_GAS = _CONSTANTS["DHW_SHARE_OF_GAS"]
"""Fraction of a typical Dutch household's total gas consumption used for
domestic hot water. Same CBS 2016 source/caveats as
``SPACE_HEATING_SHARE_OF_GAS``. Not currently consumed by this module's
own functions (DHW energy here comes from occupancy's real liters output,
not this ratio) -- kept alongside the other two shares so all three
CBS-derived numbers live in one place, and available for a caller that
wants the DHW-share cross-check `gas_conversion.py`'s own docstring
describes."""

COOKING_SHARE_OF_GAS = _CONSTANTS["COOKING_SHARE_OF_GAS"]
"""Fraction of a typical Dutch household's total gas consumption used for
cooking. Same CBS 2016 source/caveats as ``SPACE_HEATING_SHARE_OF_GAS``.
The smallest and least-precisely-attributable of the three shares -- CBS's
own figure is already a rounded 2%, and gas vs. electric hob prevalence
isn't broken out anywhere used here. Feeds
:func:`cooking_annual_kwh_from_heating` below."""


COOKING_HEAT_GAIN_FRACTION = _CONSTANTS["COOKING_HEAT_GAIN_FRACTION"]
"""Share of cooking energy input that becomes internal heat gain in the
dwelling.

Cooking has two distinct energy roles, and buem models them separately.
The *input* is fuel or electricity consumed by the hob, oven and other
kitchen appliances, and appears on the bill under whichever carrier
supplies it. The *gain* is the part of that input which the food does not
absorb and which reaches the zone as sensible or latent heat -- cooking
is strongly exothermic and cannot capture its own heat. The two are not
equal: an extraction hood removes part of the heat, and energy stored in
the food leaves the balance when it is eaten or cools elsewhere.

Applies to both carriers. A gas hob's combustion heat enters the room
exactly as an electric hob's does, even though only the electric one
registers on the electricity meter -- see
``AttributeBuilder.generate_electricity_profile()``.
"""

DHW_HEAT_RECOVERY_FRACTION = _CONSTANTS["DHW_HEAT_RECOVERY_FRACTION"]
"""Share of DHW energy recovered into the dwelling as internal gain
rather than leaving down the drain -- hot water standing in a sink or
bath gives up part of its heat to the room.

Default 0.0: the mechanism is real but small, and its size depends on how
long water stands before draining and on whether drain-water heat
recovery is fitted. Parameterised so it can be explored rather than
assumed."""

COOKING_ANNUAL_KWH_PER_HOUSEHOLD = _CONSTANTS["COOKING_ANNUAL_KWH_PER_HOUSEHOLD"]
"""Useful cooking energy for one household-year (kWh), the default
magnitude :func:`cooking_gas_energy_kwh` distributes across occupancy's
own ``cooking_active`` hours. A regional figure -- see the constants CSV
for its derivation and for why a stock with different gas-versus-electric
hob prevalence should override it."""


def cooking_annual_kwh_from_heating(
    heating_kwh_annual: float,
    *,
    cooking_share: float = COOKING_SHARE_OF_GAS,
    space_heating_share: float = SPACE_HEATING_SHARE_OF_GAS,
) -> float:
    """Derive an annual gas-cooking energy total (kWh) by applying a
    national cooking:heating gas ratio to *this building's own* simulated
    ``heating_kWh``.

    **Not the default**, and deliberately so: anchoring cooking to
    simulated heating makes the cooking figure inherit whatever error the
    heating figure carries, so a building whose heating is overestimated
    reports proportionally overestimated cooking, and the two errors can
    never be told apart in a comparison against measured gas. Prefer
    :data:`COOKING_ANNUAL_KWH_PER_HOUSEHOLD`, which is independent of the
    solve.

    Retained for reproducing results computed under the earlier
    convention, and for the case where a caller genuinely wants cooking
    expressed as a share of that building's own modelled gas use.
    """
    if heating_kwh_annual < 0:
        raise ValueError(
            f"heating_kwh_annual must be non-negative, got {heating_kwh_annual}"
        )
    return heating_kwh_annual * (cooking_share / space_heating_share)


def cooking_gas_energy_kwh(
    cooking_active: pd.Series,
    *,
    annual_total_kwh: float,
) -> pd.Series:
    """Distribute a known/assumed annual gas-cooking energy total (kWh)
    across the hours ``occupancy``'s ``cooking_active`` signal flags as
    cooking, proportionally (every flagged hour gets an equal share).

    This function does **not** decide *how much* annual energy cooking
    represents -- that figure has to come from the caller (e.g.
    ``buem.analysis.netherlands.gas_conversion``'s CBS-derived
    heating:DHW:cooking split applied to this building's own simulated
    heating demand, or a literature-sourced fixed value). What this
    function adds on top of a flat national percentage is real per-building
    *timing*: hours occupancy's stochastic kitchen-equipment model actually
    flags as active get the energy, not a uniform 1/8760th every hour.

    ``cooking_active`` reflects **electric** kitchen-equipment activity
    (occupancy's ``"kitchen"`` equipment category: hob/oven/microwave/
    kettle) -- using it to time a *gas*-cooking energy term assumes the
    household's cooking-activity timing is a reasonable proxy for gas-hob
    usage timing even where the actual hob is gas rather than electric.
    That is a documented modeling assumption, not a physical gas-hob model
    (no per-burner power rating, no distinction between gas- and
    electric-hob households).

    Parameters
    ----------
    cooking_active:
        Hourly-indexed boolean/0-1 series, e.g.
        ``ElectricityConsumptionProfile.get_profile()["cooking_active"]``
        or ``to_buem_profiles()``'s optional 5th key.
    annual_total_kwh:
        The total gas-cooking energy (kWh) to distribute across the year.

    Returns
    -------
    An hourly kWh series, same index as ``cooking_active``, named
    ``"cooking_gas_kWh"``. If no hour is ever flagged active, falls back to
    a flat distribution across every hour rather than silently discarding
    the energy.
    """
    if annual_total_kwh < 0:
        raise ValueError(f"annual_total_kwh must be non-negative, got {annual_total_kwh}")
    weights = cooking_active.astype(float)
    weight_sum = weights.sum()
    if weight_sum <= 0:
        weights = pd.Series(1.0, index=cooking_active.index)
        weight_sum = weights.sum()
    return (weights / weight_sum * annual_total_kwh).rename("cooking_gas_kWh")


__all__ = [
    "COOKING_ANNUAL_KWH_PER_HOUSEHOLD",
    "COOKING_HEAT_GAIN_FRACTION",
    "COOKING_SHARE_OF_GAS",
    "DHW_ANNUAL_KWH_PER_PERSON_EN12831_3",
    "DHW_ANNUAL_KWH_PER_PERSON_NTA8800",
    "DHW_DELTA_T_K",
    "DHW_DELTA_T_K_REFERENCE",
    "DHW_HEAT_RECOVERY_FRACTION",
    "DHW_LITERS_PER_PERSON_DAY_EN12831_3",
    "DHW_SHARE_OF_GAS",
    "SPACE_HEATING_SHARE_OF_GAS",
    "WATER_DENSITY_KG_PER_L",
    "WATER_SPECIFIC_HEAT_KJ_PER_KGK",
    "cooking_annual_kwh_from_heating",
    "cooking_gas_energy_kwh",
    "dhw_energy_kwh",
    "dhw_energy_kwh_annual_fallback",
]
