"""Tests for occupancy-driven heating setpoint setback (ISO 13790 s13).

The feature is opt-in, so the tests that matter most are the ones proving
it stays off unless asked for, and that a mistake asking for it is loud
rather than silent.
"""
from __future__ import annotations

import pandas as pd
import pytest

from buem.config.reference_values import load_setback_profiles, resolve_setback_profile
from buem.config.setback import apply_setback, build_setback_setpoints


def _presence(away: list[float], asleep: list[float]) -> tuple[pd.Series, pd.Series]:
    index = pd.date_range("2018-01-01", periods=len(away), freq="h")
    return pd.Series(away, index=index), pd.Series(asleep, index=index)


# ── the reference table ──────────────────────────────────────────────────


def test_setback_table_loads_and_carries_the_documented_profiles():
    table = load_setback_profiles()
    assert {"none", "night_only", "night_and_away", "deep"} <= set(table)
    assert table["none"].away_setback_k == 0.0
    assert table["none"].asleep_setback_k == 0.0


def test_none_and_missing_both_mean_no_setback():
    assert resolve_setback_profile(None) is None
    assert resolve_setback_profile("none") is None
    assert resolve_setback_profile("") is None


def test_unknown_profile_raises_rather_than_silently_doing_nothing():
    """A typo must not quietly turn a scenario run into a baseline one."""
    with pytest.raises(ValueError, match="Unknown setback_profile"):
        resolve_setback_profile("nite_only")


# ── the profile builder ──────────────────────────────────────────────────


def test_setback_scales_with_the_fraction_of_occupants_not_present():
    """occ_* are fractions, not flags, so half a household away gets half
    the setback rather than all or none of it."""
    away, asleep = _presence([0.0, 0.5, 1.0], [0.0, 0.0, 0.0])
    profile = load_setback_profiles()["night_and_away"]

    setpoints = build_setback_setpoints(20.0, away, asleep, profile)

    assert setpoints.iloc[0] == pytest.approx(20.0)
    assert setpoints.iloc[1] == pytest.approx(18.5)  # 20 - 0.5 * 3
    assert setpoints.iloc[2] == pytest.approx(17.0)  # 20 - 3


def test_combined_away_and_asleep_is_capped_by_max_setback():
    away, asleep = _presence([1.0], [1.0])
    profile = load_setback_profiles()["night_and_away"]  # 3 + 3, capped at 4

    setpoints = build_setback_setpoints(20.0, away, asleep, profile)

    assert setpoints.iloc[0] == pytest.approx(16.0)


def test_setpoint_never_drops_below_the_floor():
    away, asleep = _presence([1.0], [1.0])
    profile = load_setback_profiles()["deep"]  # would reach 12 degC unfloored

    setpoints = build_setback_setpoints(18.0, away, asleep, profile)

    assert setpoints.iloc[0] == pytest.approx(profile.min_setpoint_c)


# ── wiring ───────────────────────────────────────────────────────────────


def _cfg(**overrides):
    away, asleep = _presence([0.0, 1.0], [0.0, 0.0])
    cfg = {"comfortT_lb": 18.0, "comfortT_ub": 21.0,
           "occ_nothome": away, "occ_sleeping": asleep}
    cfg.update(overrides)
    return cfg


def test_no_profile_leaves_the_setpoint_a_scalar():
    """The default path must be untouched -- this is what keeps every
    existing result reproducible."""
    cfg = apply_setback(_cfg())
    assert cfg["comfortT_lb"] == 18.0


def test_named_profile_produces_an_hourly_schedule():
    cfg = apply_setback(_cfg(setback_profile="night_and_away"))
    assert isinstance(cfg["comfortT_lb"], pd.Series)
    assert cfg["comfortT_lb"].iloc[0] == pytest.approx(18.0)
    assert cfg["comfortT_lb"].iloc[1] == pytest.approx(15.0)


def test_conflicting_explicit_schedule_raises():
    """A caller-supplied hourly setpoint is a deliberate input; a profile
    must not silently discard it."""
    index = pd.date_range("2018-01-01", periods=2, freq="h")
    cfg = _cfg(setback_profile="deep", comfortT_lb=pd.Series([19.0, 19.0], index=index))
    with pytest.raises(ValueError, match="already an hourly schedule"):
        apply_setback(cfg)


def test_missing_presence_signals_warns_and_leaves_setpoint_alone(caplog):
    """A scenario that quietly ran without its setback would be worse than
    one that failed, so this must be visible in the log."""
    cfg = _cfg(setback_profile="deep")
    cfg.pop("occ_nothome")

    with caplog.at_level("WARNING"):
        result = apply_setback(cfg)

    assert result["comfortT_lb"] == 18.0
    assert "no setback was applied" in caplog.text
