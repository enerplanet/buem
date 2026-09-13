"""
Tests for the live-path LOD2 -> LOD3 envelope synthesis
(buem.buildings.mapping.live_synthesis / tabula_helpers.lookup_tabula_archetype
/ element_factory.synthesize_openings), and a basic end-to-end regression
check for the LOD2Mapper offline-pipeline refactor these were extracted
from. None of this touches weather/occupancy, so no BUEM_WEATHER_DATA_DIR
setup is needed (unlike tests/test_building_types.py).
"""
import pytest

from buem.buildings.mapping.element_factory import (
    WallInfo,
    identify_front_back,
    synthesize_openings,
)
from buem.buildings.mapping.live_synthesis import (
    FALLBACK_DOOR_RATIO,
    FALLBACK_DOOR_U,
    FALLBACK_WINDOW_U,
    normalize_opening_azimuths,
    synthesize_missing_openings,
)
from buem.buildings.pipeline import DEFAULT_WORKBOOK
from buem.config.building_registry import DEFAULT_WINDOW_TO_WALL_RATIO

# The bundled TABULA reference workbook is *.xlsx-gitignored (repo-wide rule,
# predates this test file) -- present on a dev machine that's run the offline
# pipeline before, but not part of a fresh git checkout (confirmed: this is
# why these tests failed in CI on first push, 2026-08-11 -- nothing exercised
# ExcelBuildingSource/LOD2Mapper in CI before this file existed, so the gap
# was never caught). Real TABULA-archetype matching (tabula_helpers
# .lookup_tabula_archetype()) and the offline batch pipeline both need this
# file at runtime, not just for these tests -- see CLAUDE.md's "Open
# follow-ups" for the now-flagged, not-yet-decided fix (mirroring how
# weather's archive-access gap was eventually resolved with a small
# committed CI fixture). Skip cleanly rather than assert something false
# for any environment without the file, instead of failing outright.
_workbook_missing_reason = (
    None if DEFAULT_WORKBOOK.exists()
    else f"bundled TABULA workbook not present: {DEFAULT_WORKBOOK}"
)
requires_bundled_workbook = pytest.mark.skipif(
    _workbook_missing_reason is not None, reason=_workbook_missing_reason or "",
)
from buem.buildings.mapping.tabula_helpers import lookup_tabula_archetype


def _walls_only_components(area_1=53.0, area_2=80.0):
    """A minimal components dict with Walls only -- Windows/Doors/Ventilation
    deliberately absent, mirroring a request that omits LOD3 detail."""
    return {
        "Walls": {
            "U": 1.61,
            "b_transmission": 1.0,
            "elements": [
                {"id": "Wall_1", "area": area_1, "azimuth": 180.0, "tilt": 90.0},
                {"id": "Wall_2", "area": area_2, "azimuth": 0.0, "tilt": 90.0},
            ],
        },
        "Roof": {"U": 1.54, "elements": [{"id": "Roof_1", "area": 60.0, "azimuth": 180.0, "tilt": 30.0}]},
        "Floor": {"U": 1.72, "elements": [{"id": "Floor_1", "area": 50.0, "azimuth": 0.0, "tilt": 180.0}]},
    }


# ── element_factory: pure wall-geometry reasoning (no TABULA/pandas needed) ──


def test_identify_front_back_largest_area_is_front():
    walls = [
        WallInfo(wall_id="w1", surface_feature_id=1, area=40.0, azimuth=180.0, is_shared=False),
        WallInfo(wall_id="w2", surface_feature_id=2, area=75.0, azimuth=0.0, is_shared=False),
    ]
    front, back = identify_front_back(walls)
    assert front.wall_id == "w2"  # larger area
    assert back.wall_id == "w1"  # opposite azimuth (180 vs 0)


def test_identify_front_back_no_exposed_walls():
    assert identify_front_back([]) == (None, None)


def test_synthesize_openings_shared_wall_excluded_and_net_area_reduced():
    front = WallInfo(wall_id="w1", surface_feature_id=1, area=50.0, azimuth=180.0, is_shared=False)
    back = WallInfo(wall_id="w2", surface_feature_id=2, area=50.0, azimuth=0.0, is_shared=False)
    elements = synthesize_openings(
        [front, back], front, back,
        window_ratios={"north": 0.1, "east": 0.1, "south": 0.2, "west": 0.1},
        door_ratio=0.05,
        window_U=2.8, window_g_gl=0.5, door_U=3.0, n_air_use=0.5,
    )
    types = [e.element_type for e in elements]
    assert types.count("window") == 2  # one per exposed wall
    assert types.count("door") == 1  # front wall only
    assert types.count("ventilation") == 2  # front+back cross-ventilation

    # Wall net_area reflects subtracted window/door/vent area (buildings.rst
    # "Window Sizing"/"Door Sizing" -- net area is the caller's responsibility
    # to apply, WallInfo just tracks it).
    assert front.net_area < front.area
    assert front.window_area == pytest.approx(0.2 * 50.0)
    assert front.door_area == pytest.approx(0.05 * 50.0)


def test_synthesize_openings_skips_window_on_unknown_azimuth_wall():
    """Regression test for the 2026-08-16 fix: a wall with azimuth_known=
    False (LOD2Mapper's "unknown orientation" sentinel, or any other
    caller that can't determine a real azimuth) must not receive a
    window -- a fabricated orientation shouldn't drive where a
    solar-gain-relevant opening goes, even though the wall still counts
    fully as opaque envelope area."""
    front = WallInfo(wall_id="w1", surface_feature_id=1, area=50.0, azimuth=180.0, is_shared=False)
    unknown = WallInfo(
        wall_id="w2", surface_feature_id=2, area=50.0, azimuth=0.0, is_shared=False,
        azimuth_known=False,
    )
    elements = synthesize_openings(
        [front, unknown], front, unknown,
        window_ratios={"north": 0.1, "east": 0.1, "south": 0.2, "west": 0.1},
        door_ratio=0.05,
        window_U=2.8, window_g_gl=0.5, door_U=3.0, n_air_use=0.5,
    )
    window_surfaces = [e.surface for e in elements if e.element_type == "window"]
    assert "w1" in window_surfaces
    assert "w2" not in window_surfaces  # unknown-azimuth wall got no window
    assert unknown.window_area == 0.0
    assert unknown.direction == "unknown"
    # Still counts fully as opaque envelope area -- net_area only reflects
    # its (unaffected) ventilation opening, not a fabricated window/door.
    assert unknown.net_area == pytest.approx(unknown.area - unknown.vent_area)


def test_synthesize_openings_small_wall_gets_no_window_and_keeps_full_area():
    """Regression test: a wall below MIN_WALL_AREA_FOR_WINDOWS (5 m2) must
    not receive a window element (create_windows()'s own cutoff, unchanged)
    -- AND its window_area must stay 0 so net_area isn't shrunk for a
    window that was never actually created. Before this fix, window_area
    was assigned from the ratio regardless of the cutoff, so the "removed"
    area belonged to neither the opaque wall (net_area shrank) nor a
    window (none was built) -- it silently vanished from the envelope."""
    front = WallInfo(wall_id="w1", surface_feature_id=1, area=50.0, azimuth=180.0, is_shared=False)
    small = WallInfo(wall_id="w2", surface_feature_id=2, area=3.0, azimuth=0.0, is_shared=False)
    elements = synthesize_openings(
        [front, small], front, small,
        window_ratios={"north": 0.2, "east": 0.1, "south": 0.2, "west": 0.1},
        door_ratio=0.05,
        window_U=2.8, window_g_gl=0.5, door_U=3.0, n_air_use=0.5,
    )
    window_surfaces = [e.surface for e in elements if e.element_type == "window"]
    assert "w2" not in window_surfaces  # too small for a window element
    assert small.window_area == 0.0
    # Full gross area preserved as opaque -- nothing silently lost.
    assert small.net_area == pytest.approx(small.area - small.vent_area)


def test_synthesize_openings_skips_door_when_front_wall_azimuth_unknown():
    front_unknown = WallInfo(
        wall_id="w1", surface_feature_id=1, area=60.0, azimuth=0.0, is_shared=False,
        azimuth_known=False,
    )
    back = WallInfo(wall_id="w2", surface_feature_id=2, area=50.0, azimuth=180.0, is_shared=False)
    elements = synthesize_openings(
        [front_unknown, back], front_unknown, back,
        window_ratios={"north": 0.1, "east": 0.1, "south": 0.2, "west": 0.1},
        door_ratio=0.05,
        window_U=2.8, window_g_gl=0.5, door_U=3.0, n_air_use=0.5,
    )
    assert not any(e.element_type == "door" for e in elements)
    assert front_unknown.door_area == 0.0


# ── tabula_helpers.lookup_tabula_archetype (bundled reference sheet) ─────────


@requires_bundled_workbook
def test_lookup_tabula_archetype_real_match():
    row = lookup_tabula_archetype("AB", "03", "DE")
    assert row is not None
    assert row["Code_BuildingSizeClass"] == "AB"
    assert row["Code_Country"] == "DE"
    assert row["Code_ConstructionYearClass"] == "DE.03"
    assert ".Gen." in row["Code_BuildingVariant"]


@requires_bundled_workbook
def test_lookup_tabula_archetype_accepts_prefixed_year_class_too():
    bare = lookup_tabula_archetype("AB", "03", "DE")
    prefixed = lookup_tabula_archetype("AB", "DE.03", "DE")
    assert bare["id"] == prefixed["id"]


def test_lookup_tabula_archetype_no_match_returns_none():
    # Country not covered by the bundled (Germany-only) reference sheet --
    # true whether or not the workbook itself is present in this
    # environment (missing sheet and "sheet present but no match" both
    # correctly return None), so this one needs no skip marker.
    assert lookup_tabula_archetype("MFH", "1965-1974", "NL") is None
    # Literal year-range string (EnerPlanET's actual v3 format) doesn't match
    # TABULA's class-code format either -- see CLAUDE.md's known-gap note.
    assert lookup_tabula_archetype("MFH", "1965-1974", "DE") is None


@requires_bundled_workbook
def test_lookup_tabula_archetype_explicit_code_override():
    row = lookup_tabula_archetype(
        "AB", None, None, bldg_tabula_id="DE.N.AB.03.Gen.ReEx.001.001",
    )
    assert row is not None
    assert row["Code_BuildingVariant"] == "DE.N.AB.03.Gen.ReEx.001.001"


# ── live_synthesis.synthesize_missing_openings ───────────────────────────────


def test_synthesize_missing_openings_real_tabula_match():
    comps = _walls_only_components()
    result = synthesize_missing_openings(
        comps, building_type="AB", construction_period="03", country="DE",
    )
    assert result["Windows"]["elements"], "expected synthesized windows"
    assert result["Doors"]["elements"], "expected a synthesized door"
    assert result["Ventilation"]["elements"], "expected synthesized ventilation"

    # Full set was missing -> Walls area should shrink to net opaque area.
    wall_areas = {e["id"]: e["area"] for e in result["Walls"]["elements"]}
    assert wall_areas["Wall_1"] < comps["Walls"]["elements"][0]["area"]
    assert wall_areas["Wall_2"] < comps["Walls"]["elements"][1]["area"]

    # Original input is untouched (function returns a new dict).
    assert "Windows" not in comps


def test_synthesize_missing_openings_fallback_when_no_tabula_match(caplog):
    comps = _walls_only_components(area_1=53.0, area_2=80.0)
    with caplog.at_level("WARNING"):
        result = synthesize_missing_openings(
            comps, building_type="MFH", construction_period="1965-1974", country="NL",
        )
    assert "safe-default ratios" in caplog.text

    # Front wall = largest area (Wall_2, 80 m2, north) -> door there.
    door = result["Doors"]["elements"][0]
    assert door["surface"] == "Wall_2"
    assert door["area"] == pytest.approx(FALLBACK_DOOR_RATIO * 80.0)

    # Windows are sized from the wall's own area, independent of whether
    # an archetype matched.
    south_window = next(w for w in result["Windows"]["elements"] if w["surface"] == "Wall_1")
    assert south_window["area"] == pytest.approx(DEFAULT_WINDOW_TO_WALL_RATIO * 53.0)


def test_uniform_window_ratios_is_orientation_independent():
    """Every direction gets the same ratio, so a wall's glazing follows
    its own area and real azimuth rather than a reference archetype's
    assumed orientation."""
    from buem.buildings.mapping.element_factory import uniform_window_ratios

    ratios = uniform_window_ratios()
    assert set(ratios) == {"north", "east", "south", "west"}
    assert set(ratios.values()) == {DEFAULT_WINDOW_TO_WALL_RATIO}

    assert uniform_window_ratios(0.25)["south"] == 0.25
    # None means "use the default", so the default is resolved in exactly
    # one place and cannot diverge from a caller-supplied value.
    assert uniform_window_ratios(None) == uniform_window_ratios()
    # An out-of-range value raises rather than silently reverting to the
    # default, which would model a building the caller did not describe.
    for bad in (-0.1, 1.0, 1.5):
        with pytest.raises(ValueError, match="window_to_wall_ratio"):
            uniform_window_ratios(bad)


def test_default_window_to_wall_ratio_matches_published_stock():
    """The default is sourced from measured European residential stock
    (0.15 to 0.25 across TABULA, TEASER, City Energy Analyst and facade
    segmentation), not chosen for convenience. See enerplanet/buem#17 and
    the sources recorded beside the constant."""
    assert DEFAULT_WINDOW_TO_WALL_RATIO == pytest.approx(0.20)
    assert 0.15 <= DEFAULT_WINDOW_TO_WALL_RATIO <= 0.25


def test_synthesized_windows_inherit_host_wall_azimuth():
    """A south-facing wall must receive south-facing glazing -- the
    orientation mismatch that motivated dropping TABULA's per-direction
    window columns."""
    comps = _walls_only_components(area_1=53.0, area_2=80.0)
    result = synthesize_missing_openings(
        comps, building_type="MFH", construction_period="1965-1974", country="NL",
    )
    walls_by_id = {w["id"]: w for w in comps["Walls"]["elements"]}
    assert result["Windows"]["elements"], "every exposed wall should receive glazing"
    for win in result["Windows"]["elements"]:
        host = walls_by_id[win["surface"]]
        assert win["azimuth"] == pytest.approx(host["azimuth"])
        assert win["area"] == pytest.approx(DEFAULT_WINDOW_TO_WALL_RATIO * host["area"])


def test_caller_glazing_properties_override_fallbacks():
    """window_U/window_g_gl/door_U replace the values the synthesis would
    otherwise take from the archetype row or the fallbacks."""
    comps = _walls_only_components()
    result = synthesize_missing_openings(
        comps, building_type="MFH", construction_period="1965-1974", country="NL",
        window_U=0.8, window_g_gl=0.35, door_U=1.4,
    )
    assert result["Windows"]["U"] == pytest.approx(0.8)
    assert result["Windows"]["g_gl"] == pytest.approx(0.35)
    assert result["Doors"]["U"] == pytest.approx(1.4)
    # Synthesized elements carry geometry only and inherit the component's
    # U, so the override reaches every window through that one value.
    assert all("U" not in w for w in result["Windows"]["elements"])

    # Omitted properties still resolve as before.
    default_result = synthesize_missing_openings(
        comps, building_type="MFH", construction_period="1965-1974", country="NL",
    )
    assert default_result["Windows"]["U"] == pytest.approx(FALLBACK_WINDOW_U)
    assert default_result["Doors"]["U"] == pytest.approx(FALLBACK_DOOR_U)

    # Sizing is untouched: only the properties change.
    assert (
        [w["area"] for w in result["Windows"]["elements"]]
        == [w["area"] for w in default_result["Windows"]["elements"]]
    )


def test_synthesize_missing_openings_preserves_explicit_override():
    comps = _walls_only_components()
    comps["Windows"] = {
        "U": 1.1, "g_gl": 0.6,
        "elements": [{"id": "Win_custom", "area": 3.3, "surface": "Wall_1", "azimuth": 180.0, "tilt": 90.0}],
    }
    result = synthesize_missing_openings(
        comps, building_type="AB", construction_period="03", country="DE",
    )
    # Windows untouched (explicit, non-empty).
    assert result["Windows"] == comps["Windows"]
    # Doors/Ventilation were missing -> still synthesized.
    assert result["Doors"]["elements"]
    assert result["Ventilation"]["elements"]
    # Partial override -> Walls area left as originally supplied (no
    # reliable way to know how much area the caller already accounted for).
    wall_areas = {e["id"]: e["area"] for e in result["Walls"]["elements"]}
    assert wall_areas["Wall_1"] == 53.0
    assert wall_areas["Wall_2"] == 80.0


def test_synthesize_missing_openings_noop_when_all_present():
    comps = _walls_only_components()
    comps["Windows"] = {"elements": [{"id": "w", "area": 1.0, "surface": "Wall_1", "azimuth": 180.0, "tilt": 90.0}]}
    comps["Doors"] = {"elements": [{"id": "d", "area": 1.0, "surface": "Wall_1", "azimuth": 180.0, "tilt": 90.0}]}
    comps["Ventilation"] = {"elements": [{"id": "v", "air_changes": 0.5}]}
    result = synthesize_missing_openings(
        comps, building_type="AB", construction_period="03", country="DE",
    )
    assert result is comps  # early-return, unchanged


def test_synthesize_missing_openings_noop_without_walls():
    comps = {"Roof": {"U": 1.5, "elements": []}}
    result = synthesize_missing_openings(
        comps, building_type="AB", construction_period="03", country="DE",
    )
    assert result is comps


# ── normalize_opening_azimuths: window/door inherit parent surface azimuth/tilt ──


def test_normalize_opening_azimuths_corrects_mismatched_window():
    """A caller-supplied window whose azimuth/tilt disagree with its
    declared parent wall must be corrected to the wall's values, not left
    inconsistent or rejected."""
    comps = _walls_only_components()  # Wall_1 azimuth=180, Wall_2 azimuth=0
    comps["Windows"] = {
        "elements": [{"id": "Win_1", "area": 3.0, "surface": "Wall_1", "azimuth": 90.0, "tilt": 45.0}],
    }
    result = normalize_opening_azimuths(comps)
    win = result["Windows"]["elements"][0]
    assert win["azimuth"] == 180.0
    assert win["tilt"] == 90.0
    # area/id untouched -- only azimuth/tilt are corrected
    assert win["area"] == 3.0
    assert win["id"] == "Win_1"


def test_normalize_opening_azimuths_noop_when_already_consistent():
    comps = _walls_only_components()
    comps["Windows"] = {
        "elements": [{"id": "Win_1", "area": 3.0, "surface": "Wall_1", "azimuth": 180.0, "tilt": 90.0}],
    }
    result = normalize_opening_azimuths(comps)
    assert result["Windows"] == comps["Windows"]


def test_normalize_opening_azimuths_leaves_unlinked_elements_alone():
    """No `surface` reference, or one that doesn't resolve, is left as-is --
    nothing to normalize against."""
    comps = _walls_only_components()
    comps["Windows"] = {
        "elements": [
            {"id": "Win_no_parent", "area": 2.0, "azimuth": 45.0, "tilt": 90.0},
            {"id": "Win_unknown_parent", "area": 2.0, "surface": "Wall_99", "azimuth": 45.0, "tilt": 90.0},
        ],
    }
    result = normalize_opening_azimuths(comps)
    assert result["Windows"]["elements"][0]["azimuth"] == 45.0
    assert result["Windows"]["elements"][1]["azimuth"] == 45.0


def test_normalize_opening_azimuths_skylight_inherits_roof_tilt():
    """A window whose parent is a Roof element (a skylight) inherits the
    roof's own tilt (e.g. 30 degrees), not a hardcoded vertical 90."""
    comps = _walls_only_components()  # Roof_1: azimuth=180, tilt=30
    comps["Windows"] = {
        "elements": [{"id": "Skylight_1", "area": 1.5, "surface": "Roof_1", "azimuth": 0.0, "tilt": 0.0}],
    }
    result = normalize_opening_azimuths(comps)
    win = result["Windows"]["elements"][0]
    assert win["azimuth"] == 180.0
    assert win["tilt"] == 30.0


def test_normalize_opening_azimuths_ignores_ventilation():
    """Ventilation azimuth/tilt play no role in the ISO 13790 model
    (air-change-rate only) -- left untouched even if inconsistent."""
    comps = _walls_only_components()
    comps["Ventilation"] = {
        "elements": [{"id": "Vent_1", "surface": "Wall_1", "azimuth": 999.0, "air_changes": 0.5}],
    }
    result = normalize_opening_azimuths(comps)
    assert result["Ventilation"] == comps["Ventilation"]


def test_normalize_opening_azimuths_is_noop_after_synthesis():
    """Internally-synthesized Windows/Doors already inherit their parent
    wall's azimuth by construction (element_factory.py) -- running
    normalize_opening_azimuths afterwards must not change anything."""
    comps = _walls_only_components()
    synthesized = synthesize_missing_openings(
        comps, building_type="AB", construction_period="03", country="DE",
    )
    normalized = normalize_opening_azimuths(synthesized)
    assert normalized["Windows"] == synthesized["Windows"]
    assert normalized["Doors"] == synthesized["Doors"]


# ── LOD2Mapper: end-to-end regression check against the bundled workbook ────
# (previously untested -- the extraction of synthesize_openings/
# identify_front_back out of lod2_mapper.py had no direct coverage before.)


@requires_bundled_workbook
def test_lod2mapper_end_to_end_with_bundled_workbook():
    from buem.buildings.datasources.excel_source import ExcelBuildingSource
    from buem.buildings.mapping.lod2_mapper import LOD2Mapper

    source = ExcelBuildingSource(DEFAULT_WORKBOOK)
    building_ids = source.get_building_ids(limit=5)
    assert building_ids, "bundled workbook should contain at least one building"

    mapper = LOD2Mapper(source, country="DE")
    buildings = mapper.map_all(building_ids=building_ids)
    assert buildings, "expected at least one successfully-mapped building"

    bldg = buildings[0]
    assert bldg.walls(), "expected wall elements"
    # A building with any exposed (non-shared) wall >= the min window-eligible
    # size should get at least one window -- not asserted unconditionally
    # since a fully party-walled building legitimately has none.
    for wall in bldg.walls():
        assert wall.area >= 0.0
    for vent in bldg.ventilation_elements():
        assert vent.air_changes is not None


@requires_bundled_workbook
def test_lod2mapper_uses_real_roof_azimuth_not_hardcoded_zero():
    """Regression test for the 2026-08-18 fix: LOD2Mapper used to hardcode
    every roof element's azimuth to 0.0 on the (checked-and-found-wrong)
    claim that roof azimuth has no role in the model --
    model_buem._calcRadiation() actually passes every element's own
    azimuth through pvlib, so this silently modeled every non-flat German
    roof as due-north-facing. Building 30542 (used elsewhere in this
    session's regression suite) has real, non-zero roof azimuths in the
    source DB -- must survive into the mapped Building, not collapse to
    a uniform placeholder."""
    from buem.buildings.datasources.excel_source import ExcelBuildingSource
    from buem.buildings.mapping.lod2_mapper import LOD2Mapper

    source = ExcelBuildingSource(DEFAULT_WORKBOOK)
    mapper = LOD2Mapper(source, country="DE")
    bldg = mapper.map_building(30542)
    assert bldg is not None
    roof_azimuths = [e.azimuth for e in bldg.elements if e.element_type == "roof"]
    assert roof_azimuths, "expected at least one roof element"
    assert any(az != 0.0 for az in roof_azimuths), (
        "expected at least one non-zero roof azimuth from real source data"
    )


@requires_bundled_workbook
def test_lod2mapper_uses_buem_comfort_defaults_not_tabula_theta_i():
    """A mapped building keeps buem's own comfort dead-band rather than
    the matched archetype's ``theta_i``.

    TABULA's ``theta_i`` is the setpoint its *reference calculation*
    assumes (a constant 20 degC across every Dutch archetype, carrying no
    per-building information), whereas buem's default represents observed
    occupant behavior -- see building_registry.DEFAULT_COMFORT_T_LB."""
    from buem.buildings.datasources.excel_source import ExcelBuildingSource
    from buem.buildings.mapping.lod2_mapper import LOD2Mapper
    from buem.config.building_registry import (
        DEFAULT_COMFORT_T_LB,
        DEFAULT_COMFORT_T_UB,
    )

    source = ExcelBuildingSource(DEFAULT_WORKBOOK)
    mapper = LOD2Mapper(source, country="DE")
    bldg = mapper.map_building(52203)  # SFH, DE.N.SFH.01.Gen, theta_i=20.0
    assert bldg is not None
    assert bldg.thermal.comfortT_lb == DEFAULT_COMFORT_T_LB
    assert bldg.thermal.comfortT_ub == DEFAULT_COMFORT_T_UB


def test_thermal_properties_comfort_defaults_come_from_registry():
    """ThermalProperties' own dataclass defaults must track the single
    source of truth, so the two cannot drift apart."""
    from buem.buildings.building import ThermalProperties
    from buem.config.building_registry import (
        DEFAULT_COMFORT_T_LB,
        DEFAULT_COMFORT_T_UB,
    )

    props = ThermalProperties()
    assert props.comfortT_lb == DEFAULT_COMFORT_T_LB
    assert props.comfortT_ub == DEFAULT_COMFORT_T_UB
    assert props.comfortT_lb < props.comfortT_ub
