"""
Regression tests for two 2026-08-15 fixes in
``buem.thermal.model_buem.ModelBUEM._init5R1C()``'s opaque-component
solar-gain calc (see CHANGELOG.md / .claude/residential/resolved.md for
the fuller writeup; both found via ``buem.analysis.batch``'s smoke test
on real party-wall buildings):

1. ``_resolve_opaque_element_u()`` -- a component with only per-element U
   (no component-level default, e.g. a mix of exposed and party walls)
   used to crash with ``TypeError`` because the solar-gain loop read
   ``self.bU.get(comp, 1.0)`` directly, which returns ``None`` (not the
   1.0 default) when the *key* exists with value ``None`` -- exactly
   ``_initEnvelop()``'s own documented sentinel for "per-element U was
   used". Fixed to fall back to each element's own U in that case.
2. A configured-but-empty ``Windows`` component (0 exposed walls, nowhere
   to synthesize a window) used to raise ``ValueError`` outright. Fixed
   to zero solar gain instead, consistent with the H_windows=0.0
   fallback immediately below it and Floor's own "explicitly zero"
   treatment a few lines further down the same method.

These are pure unit tests against ``ModelBUEM`` directly -- no bundled
workbook or real weather archive needed, unlike ``tests/test_analysis
.py``'s real end-to-end coverage of the same two fixes on actual
buildings (52203/44424/30542/18763).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from buem.thermal.model_buem import ModelBUEM


def _minimal_model_for_envelop(walls: dict) -> ModelBUEM:
    """A ``ModelBUEM`` with just enough cfg for ``_initEnvelop()`` to run
    -- no weather/radiation needed, since ``_resolve_opaque_element_u()``
    only reads ``self.bU``/the element dict, populated by ``_initEnvelop``
    alone."""
    weather = pd.DataFrame(
        {"T": [5.0], "GHI": [0.0], "DHI": [0.0], "DNI": [0.0]},
        index=pd.date_range("2018-01-01", periods=1, freq="h"),
    )
    cfg = {
        "weather": weather,
        "A_ref": 100.0,
        "h_room": 2.5,
        "n_air_infiltration": 0.5,
        "n_air_use": 0.5,
        "components": {
            "Walls": walls,
            "Roof": {"U": 0.3, "elements": [{"id": "roof1", "area": 50.0}]},
            "Floor": {"U": 0.4, "elements": [{"id": "floor1", "area": 50.0}]},
            "Windows": {"U": 1.2, "elements": [{"id": "win1", "area": 2.0}]},
            "Doors": {"U": 1.5, "elements": [{"id": "door1", "area": 1.5}]},
        },
    }
    model = ModelBUEM(cfg)
    model._initEnvelop()
    return model


def test_resolve_opaque_element_u_prefers_component_level():
    model = _minimal_model_for_envelop(
        {"U": 1.8, "elements": [{"id": "w1", "area": 10.0}]}
    )
    e = model.component_elements["Walls"][0]
    assert model._resolve_opaque_element_u("Walls", e) == pytest.approx(1.8)


def test_resolve_opaque_element_u_falls_back_to_per_element_u():
    """The exact regression: no component-level U, elements carry their
    own -- self.bU["Walls"] is None (_initEnvelop's documented sentinel),
    used to crash the old ``self.bU.get("Walls", 1.0)`` read."""
    model = _minimal_model_for_envelop({
        "elements": [
            {"id": "w1", "area": 10.0, "U": 0.3, "b_transmission": 1.0},
            {"id": "w2", "area": 8.0, "U": 0.0, "b_transmission": 0.0},  # party wall
        ],
    })
    assert model.bU["Walls"] is None  # confirms the sentinel this fix handles

    e1, e2 = model.component_elements["Walls"]
    assert model._resolve_opaque_element_u("Walls", e1) == pytest.approx(0.3)
    assert model._resolve_opaque_element_u("Walls", e2) == pytest.approx(0.0)


def test_resolve_opaque_element_u_raises_clearly_if_truly_missing():
    """Defensive case that shouldn't occur in practice (_initEnvelop
    already requires per-element U when component-level U is absent) --
    confirms a clear error rather than a silent bad value if it ever did."""
    model = _minimal_model_for_envelop({
        "elements": [{"id": "w1", "area": 10.0, "U": 0.3}],
    })
    fake_element_missing_u = {"id": "w_bad", "area": 5.0}
    with pytest.raises(ValueError, match="no per-element U"):
        model._resolve_opaque_element_u("Walls", fake_element_missing_u)


def test_windows_component_configured_but_empty_gives_zero_gain_not_error():
    """Regression test for the second fix: a 0-exposed-wall building
    ends up with Windows.elements=[] -- must produce zero solar gain,
    not raise. Runs a real (tiny, single-timestep) _init5R1C() pass."""
    weather = pd.DataFrame(
        {"T": [5.0], "GHI": [100.0], "DHI": [50.0], "DNI": [200.0]},
        index=pd.date_range("2018-06-21 12:00", periods=1, freq="h"),
    )
    cfg = {
        "weather": weather,
        "A_ref": 100.0,
        "h_room": 2.5,
        "n_air_infiltration": 0.5,
        "n_air_use": 0.5,
        "latitude": 52.0,
        "longitude": 5.0,
        "F_sh_hor": 0.8,
        "F_sh_vert": 0.75,
        "F_w": 0.9,
        "F_f": 0.2,
        "g_gl_n_Window": 0.6,
        "thermalClass": "medium",
        "c_m": 165.0,
        "comfortT_lb": 20.0,
        "comfortT_ub": 24.0,
        "components": {
            "Walls": {"U": 0.0, "elements": [
                {"id": "w1", "area": 10.0, "azimuth": 0.0, "tilt": 90.0, "b_transmission": 0.0},
            ]},
            "Roof": {"U": 0.3, "elements": [
                {"id": "roof1", "area": 50.0, "azimuth": 0.0, "tilt": 30.0},
            ]},
            "Floor": {"U": 0.4, "elements": [
                {"id": "floor1", "area": 50.0, "azimuth": 0.0, "tilt": 0.0},
            ]},
            "Windows": {"U": 1.2, "elements": []},  # 0 exposed walls -> nowhere to synthesize a window
            "Doors": {"U": 1.5, "elements": []},
        },
    }
    model = ModelBUEM(cfg)
    model._initEnvelop()
    model._init5R1C()  # calls _calcRadiation internally; must not raise

    assert np.all(model.profiles["bQ_sol_Windows"] == 0.0)


def test_shading_factor_reduces_window_solar_gain():
    """F_sh_vert applies to glazing, not only to opaque surfaces. The
    obstacles, overhangs and horizon it represents reduce the radiation
    reaching a window (enerplanet/buem#27)."""
    import copy as _copy

    from buem.config.cfg_attribute import cfg as DEFAULT_CFG

    base = _copy.deepcopy(DEFAULT_CFG)
    base["F_sh_vert"] = 1.0
    unshaded = ModelBUEM(base)
    unshaded._initEnvelop()
    unshaded._initPara()
    unshaded._init5R1C()

    shaded_cfg = _copy.deepcopy(DEFAULT_CFG)
    shaded_cfg["F_sh_vert"] = 0.5
    shaded = ModelBUEM(shaded_cfg)
    shaded._initEnvelop()
    shaded._initPara()
    shaded._init5R1C()

    assert float(shaded.profiles["bQ_sol_Windows"].sum()) < float(
        unshaded.profiles["bQ_sol_Windows"].sum()
    )
