"""Occupancy-driven heating setpoint setback -- ISO 13790 section 13
intermittency, for the hourly method.

The standard handles intermittent heating two ways depending on the
calculation method. A monthly or seasonal method cannot see the hours, so
it applies a reduction factor (ISO 13790 section 13.2.2; TABULA's
``F_red_htr1`` is such a factor, and buem uses it only in its
degree-day comparison estimator for exactly that reason). An **hourly**
method, which buem's 5R1C solve is, takes intermittency into account
directly by varying the setpoint hour by hour (section 13.2.1). Applying
a reduction factor on top of hour-varying setpoints would count the same
effect twice.

``ModelBUEM`` already accepts ``comfortT_lb``/``comfortT_ub`` as an
hourly series rather than a scalar, so nothing in the solver needs to
change; this module only builds the series.

Off by default, deliberately
----------------------------
buem's default comfort band is 18-21 degC rather than TABULA's
standardized 20-24 degC calculation setpoint, and that lower bound was
chosen to represent *observed* occupant behaviour. Much of what a setback
profile describes is therefore already inside the default band, and
stacking one on top double-counts it. These profiles exist for scenario
work -- "what would this stock do under night setback?" -- and should not
be switched on for a validation run, where a lower setpoint would flatter
the result while masking the refurbishment-coverage effect that actually
drives the buem-vs-CBS gap.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from buem.config.reference_values import SetbackProfile, resolve_setback_profile

logger = logging.getLogger(__name__)


def build_setback_setpoints(
    base_lb: float,
    occ_nothome: pd.Series,
    occ_sleeping: pd.Series,
    profile: SetbackProfile,
) -> pd.Series:
    """Hourly heating setpoint under one setback profile.

    ``occ_nothome`` and ``occ_sleeping`` are *fractions* of occupants, not
    flags, so the reduction scales smoothly with how much of the household
    is away or asleep rather than switching at a threshold. Their sum is
    not guaranteed to stay within one, which is why the combined reduction
    is capped by ``max_setback_k`` before ``min_setpoint_c`` applies as an
    absolute floor.

    Returns an hourly series suitable for ``cfg["comfortT_lb"]``.
    """
    away = occ_nothome.astype(float).clip(lower=0.0, upper=1.0)
    asleep = occ_sleeping.astype(float).clip(lower=0.0, upper=1.0)

    reduction = (
        away * profile.away_setback_k + asleep * profile.asleep_setback_k
    ).clip(upper=profile.max_setback_k)

    setpoints = (base_lb - reduction).clip(lower=profile.min_setpoint_c)
    return setpoints.rename("comfortT_lb")


def apply_setback(cfg: dict) -> dict:
    """Replace ``cfg["comfortT_lb"]`` with an hourly setback series when
    ``cfg["setback_profile"]`` names one. Mutates and returns ``cfg``.

    A no-op unless a profile is named -- see the module docstring for why
    that is the default. Also a no-op, with a warning, when the occupancy
    presence signals are missing: a setback that silently did nothing
    would make a scenario run look like it had been applied.

    Raises if ``comfortT_lb`` is already an hourly series, rather than
    overwriting it: that means a caller supplied its own schedule, and
    quietly discarding it would lose a deliberate input.
    """
    profile = resolve_setback_profile(cfg.get("setback_profile"))
    if profile is None:
        return cfg

    base_lb = cfg.get("comfortT_lb")
    if isinstance(base_lb, pd.Series | np.ndarray):
        # ValueError, not TypeError: the type is legitimate, the
        # combination is not -- two conflicting schedules were supplied.
        raise ValueError(  # noqa: TRY004
            "setback_profile was given, but comfortT_lb is already an hourly "
            "schedule. Supply one or the other, not both -- applying a profile "
            "on top would discard the schedule the caller set."
        )
    if base_lb is None:
        raise ValueError("setback_profile requires comfortT_lb to be set.")

    occ_nothome = cfg.get("occ_nothome")
    occ_sleeping = cfg.get("occ_sleeping")
    if not isinstance(occ_nothome, pd.Series) or not isinstance(occ_sleeping, pd.Series):
        logger.warning(
            "setback_profile=%r was requested but occ_nothome/occ_sleeping are "
            "not available, so no setback was applied. The result is a "
            "constant-setpoint run despite the profile being named.",
            profile.profile_name,
        )
        return cfg

    setpoints = build_setback_setpoints(
        float(base_lb), occ_nothome, occ_sleeping, profile,
    )
    cfg["comfortT_lb"] = setpoints
    logger.info(
        "Applied setback profile %r: comfortT_lb %.1f -> %.1f..%.1f degC "
        "(mean %.2f)", profile.profile_name, float(base_lb),
        float(setpoints.min()), float(setpoints.max()), float(setpoints.mean()),
    )
    return cfg


__all__ = ["apply_setback", "build_setback_setpoints"]
