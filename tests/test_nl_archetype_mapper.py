"""
Tests for ``buem.buildings.datasources.nl_archetype_mapper`` -- pure unit
tests against synthetic data plus, where marked, the real bundled NL
TABULA sheet (no CityJSON/RIVM file needed -- ``tabula.csv`` is small and
already git-tracked).
"""
from __future__ import annotations

import pandas as pd
import pytest

from buem.buildings.datasources.nl_archetype_mapper import (
    label_to_construction_class,
    map_buildings,
    year_to_construction_class,
)

NL_DATA_DIR = "src/buem/data/buildings/netherlands/Loenen"

_nl_tabula_missing_reason = None
try:
    _nl_tabula = pd.read_csv(f"{NL_DATA_DIR}/tabula.csv", na_values=["NULL"])
    _nl_tabula = _nl_tabula[_nl_tabula["Code_Country"] == "NL"]
except FileNotFoundError as exc:
    _nl_tabula = None
    _nl_tabula_missing_reason = str(exc)

requires_nl_tabula = pytest.mark.skipif(_nl_tabula is None, reason=_nl_tabula_missing_reason or "")


# ── year_to_construction_class ───────────────────────────────────────────


def test_year_to_construction_class_boundaries():
    assert year_to_construction_class(1900) == "NL.01"
    assert year_to_construction_class(1964) == "NL.01"
    assert year_to_construction_class(1965) == "NL.02"
    assert year_to_construction_class(1974) == "NL.02"
    assert year_to_construction_class(1975) == "NL.03"
    assert year_to_construction_class(1991) == "NL.03"
    assert year_to_construction_class(1992) == "NL.04"
    assert year_to_construction_class(2005) == "NL.04"
    assert year_to_construction_class(2006) == "NL.05"
    assert year_to_construction_class(2014) == "NL.05"
    assert year_to_construction_class(2015) == "NL.06"
    assert year_to_construction_class(2026) == "NL.06"


def test_year_to_construction_class_missing_year_returns_none():
    assert year_to_construction_class(None) is None
    assert year_to_construction_class(float("nan")) is None


# ── label_to_construction_class ──────────────────────────────────────────


def test_label_to_construction_class_known_labels():
    assert label_to_construction_class("G") == "NL.01"
    assert label_to_construction_class("C") == "NL.03"
    assert label_to_construction_class("A+++++") == "NL.06"


def test_label_to_construction_class_missing_or_unknown():
    assert label_to_construction_class(None) is None
    assert label_to_construction_class(float("nan")) is None
    assert label_to_construction_class("not-a-real-label") is None


# ── map_buildings: end-to-end on synthetic data + real TABULA NL sheet ──


def _synthetic_buildings():
    return pd.DataFrame([
        {  # detached, real construction year, no label -> year-driven match
            "bag_pand_id": "A", "construction_year": 1980, "attached_neighbour_id": None,
            "is_greenhouse_or_warehouse": False, "is_glass_roof": False,
        },
        {  # detached, has a real label -> label overrides year
            "bag_pand_id": "B", "construction_year": 1980, "attached_neighbour_id": None,
            "is_greenhouse_or_warehouse": False, "is_glass_roof": False,
        },
        {  # non-residential, large -> excluded from TABULA but linked to a service type
            "bag_pand_id": "C", "construction_year": 2000, "attached_neighbour_id": None,
            "is_greenhouse_or_warehouse": True, "is_glass_roof": False, "footprint_area": 2000.0,
        },
    ])


@requires_nl_tabula
def test_map_buildings_year_driven_match():
    rivm = pd.DataFrame(columns=["bag_pand_id", "aant_verblijfsobj", "dominant_label"])
    result = map_buildings(_synthetic_buildings(), _nl_tabula, rivm)
    row_a = result.set_index("bag_pand_id").loc["A"]
    assert row_a["is_residential"]
    assert row_a["building_type"] == "SFH"
    assert row_a["construction_year_class"] == "NL.03"  # 1980 -> 1975-1991
    assert not row_a["matched_via_label"]
    assert pd.notna(row_a["tabula_variant_code_id"])
    assert row_a["tabula_variant_code"].startswith("NL.N.SFH.03.")
    assert row_a["residential_units"] == 1.0  # no RIVM match -> single-dwelling default


@requires_nl_tabula
def test_map_buildings_residential_units_carries_real_rivm_count():
    """Regression test for the 2026-08-18 fix: a multi-unit building's
    real dwelling count must survive as its own output column, not just
    be consumed internally by classify_all() -- found necessary when a
    validation script needed to normalize a whole-*building* simulation
    result back to a per-*dwelling* figure for a fair CBS comparison
    (real Loenen AB buildings are whole apartment blocks with 257-756 m2
    footprints, not single units)."""
    rivm = pd.DataFrame([{"bag_pand_id": "B", "aant_verblijfsobj": 12.0, "dominant_label": None}])
    result = map_buildings(_synthetic_buildings(), _nl_tabula, rivm)
    row_b = result.set_index("bag_pand_id").loc["B"]
    assert row_b["residential_units"] == 12.0


@requires_nl_tabula
def test_map_buildings_label_selects_refurbishment_variant():
    """A label far better than the era's typical performance keeps the
    real construction era but selects the nZEB refurbishment variant:
    a renovated 1980 building is still structurally a 1980 building."""
    # Building B: construction_year 1980 (era NL.03), label "A+++"
    # (implied tier NL.06) -- a 3-tier gap selects variant 3.
    rivm = pd.DataFrame([{"bag_pand_id": "B", "aant_verblijfsobj": 1.0, "dominant_label": "A+++"}])
    result = map_buildings(_synthetic_buildings(), _nl_tabula, rivm)
    row_b = result.set_index("bag_pand_id").loc["B"]
    assert row_b["construction_year_class"] == "NL.03"
    assert row_b["matched_via_label"]
    assert row_b["refurbishment_variant"] == 3
    assert row_b["tabula_variant_code"] == "NL.N.SFH.03.Gen.ReEx.001.003"


def test_apply_refurbishment_measures_add_and_replace():
    """``Add`` measures compound resistances in series; ``Replace``-type
    measures treat the measure's R as the new construction's total R;
    zero/absent measures leave U unchanged."""
    from buem.buildings.mapping.tabula_helpers import apply_refurbishment_measures

    row = pd.Series({
        "Code_MeasureType_Wall_1": "Add", "R_PredefinedMeasure_Wall_1": 3.5,
        "Code_MeasureType_Window_1": "Replace", "R_PredefinedMeasure_Window_1": 0.5556,
        "Code_MeasureType_Roof_1": "ReplaceInsulation", "R_PredefinedMeasure_Roof_1": 3.5,
        "Code_MeasureType_Floor_1": "0", "R_PredefinedMeasure_Floor_1": 0.0,
    })
    adjusted = apply_refurbishment_measures(row, {
        "Wall": 2.0, "Window": 5.2, "Roof": 2.56, "Floor": 2.9,
    })
    assert adjusted["Wall"] == pytest.approx(1.0 / (1.0 / 2.0 + 3.5))   # 0.25
    assert adjusted["Window"] == pytest.approx(1.0 / 0.5556)            # ~1.8
    assert adjusted["Roof"] == pytest.approx(1.0 / 3.5)                 # ~0.29
    assert adjusted["Floor"] == pytest.approx(2.9)                      # untouched


@requires_nl_tabula
def test_apply_refurbishment_measures_noop_for_as_built_variant():
    from buem.buildings.mapping.tabula_helpers import (
        apply_refurbishment_measures,
        lookup_tabula_archetype,
    )

    row = lookup_tabula_archetype("SFH", "01", "NL", sheet=_nl_tabula, variant_number=1)
    assert row is not None
    base = {"Wall": 2.78, "Roof": 2.56, "Floor": 2.9, "Window": 5.2, "Door": 3.0}
    assert apply_refurbishment_measures(row, base) == base


@requires_nl_tabula
def test_lookup_tabula_archetype_variant_number():
    from buem.buildings.mapping.tabula_helpers import lookup_tabula_archetype

    for variant in (1, 2, 3):
        row = lookup_tabula_archetype("SFH", "01", "NL", sheet=_nl_tabula, variant_number=variant)
        assert row is not None
        assert row["Code_BuildingVariant"] == f"NL.N.SFH.01.Gen.ReEx.001.00{variant}"


def test_label_to_refurbishment_variant_gaps():
    from buem.buildings.datasources.nl_archetype_mapper import label_to_refurbishment_variant

    assert label_to_refurbishment_variant("NL.03", None) == 1        # no label
    assert label_to_refurbishment_variant("NL.03", "NL.03") == 1     # at era level
    assert label_to_refurbishment_variant("NL.03", "NL.01") == 1     # below era level
    assert label_to_refurbishment_variant("NL.03", "NL.04") == 2     # 1 tier better
    assert label_to_refurbishment_variant("NL.03", "NL.05") == 2     # 2 tiers better
    assert label_to_refurbishment_variant("NL.03", "NL.06") == 3     # 3 tiers better
    assert label_to_refurbishment_variant("NL.01", "NL.06") == 3     # 5 tiers better


@requires_nl_tabula
def test_map_buildings_excludes_non_residential():
    rivm = pd.DataFrame(columns=["bag_pand_id", "aant_verblijfsobj", "dominant_label"])
    result = map_buildings(_synthetic_buildings(), _nl_tabula, rivm)
    row_c = result.set_index("bag_pand_id").loc["C"]
    assert not row_c["is_residential"]
    assert pd.isna(row_c["tabula_variant_code_id"])
    # excluded from TABULA, but still linked to occupancy's service-building
    # path -- classify_all()'s own service_building_type column survives
    # map_buildings() unchanged (see nl_building_classifier)
    assert row_c["service_building_type"] == "warehouse"


# ── dwelling-count repair (issue #6) ────────────────────────────────────


def test_estimate_dwelling_units_leaves_plausible_counts_alone():
    """The repair must only act where the data is self-evidently wrong,
    not second-guess merely spacious housing."""
    from buem.buildings.datasources.nl_archetype_mapper import estimate_dwelling_units

    assert estimate_dwelling_units(120.0, "SFH", 1.0) == 1.0
    assert estimate_dwelling_units(2000.0, "AB", 20.0) == 20.0  # 100 m2/dwelling


def test_estimate_dwelling_units_derives_count_for_whole_blocks():
    """A single BAG Pand can be an entire block; 19,241 m2 recorded as two
    dwellings is a missing count, not a 9,600 m2 apartment."""
    from buem.buildings.datasources.nl_archetype_mapper import estimate_dwelling_units

    assert estimate_dwelling_units(19240.0, "MFH", 2.0) == 214.0  # 90 m2 typical
    assert estimate_dwelling_units(1500.0, "SFH", 1.0) == 10.0    # 150 m2 typical


def test_estimate_dwelling_units_never_reduces_a_registered_count():
    """Registered sub-units genuinely exist, so the count is a floor."""
    from buem.buildings.datasources.nl_archetype_mapper import estimate_dwelling_units

    assert estimate_dwelling_units(600.0, "AB", 1.0) >= 1.0
    assert estimate_dwelling_units(100000.0, "AB", 2000.0) == 2000.0


def test_estimate_dwelling_units_handles_missing_area_and_zero_counts():
    from buem.buildings.datasources.nl_archetype_mapper import estimate_dwelling_units

    assert estimate_dwelling_units(0.0, "SFH", 3.0) == 3.0
    assert estimate_dwelling_units(0.0, "SFH", 0.0) == 1.0
    assert estimate_dwelling_units(1000.0, None, 1.0) > 1.0  # unknown type -> 120 m2 default


def test_repair_dwelling_counts_preserves_the_registered_value():
    """A derived count must never be mistaken for registered data."""
    import pandas as pd

    from buem.buildings.datasources.nl_archetype_mapper import repair_dwelling_counts

    df = pd.DataFrame({
        "building_feature_id": [1, 2],
        "building_type": ["MFH", "SFH"],
        "area_total_floor": [6413.5, 120.0],
        "number_of_storeys": [3, 1],
        "residential_units": [2.0, 1.0],
    })
    out = repair_dwelling_counts(df)

    assert list(out["residential_units_recorded"]) == [2.0, 1.0]
    assert list(out["residential_units_source"]) == ["floor_area_estimate", "rivm"]
    assert out.loc[0, "residential_units"] == 214.0
    assert out.loc[1, "residential_units"] == 1.0


def test_repair_dwelling_counts_is_idempotent():
    import pandas as pd

    from buem.buildings.datasources.nl_archetype_mapper import repair_dwelling_counts

    df = pd.DataFrame({
        "building_feature_id": [1],
        "building_type": ["MFH"],
        "area_total_floor": [6413.5],
        "number_of_storeys": [3],
        "residential_units": [2.0],
    })
    once = repair_dwelling_counts(df)
    twice = repair_dwelling_counts(once)

    # Stable value, and the provenance keeps saying "derived" -- the count
    # is still an estimate on the second pass, not something RIVM supplied.
    assert once["residential_units"].tolist() == twice["residential_units"].tolist()
    assert (twice["residential_units_source"] == "floor_area_estimate").all()
    assert twice["residential_units_recorded"].tolist() == [2.0]


def test_repair_dwelling_counts_skips_non_residential():
    """A warehouse has no dwellings. Giving it a derived count would scale
    occupancy's service-building profile by a household multiplier that
    does not exist -- inflating its electricity by whatever the floor area
    divided into (caught this way: a 2,125 m2 warehouse's elec went 18x)."""
    import pandas as pd

    from buem.buildings.datasources.nl_archetype_mapper import repair_dwelling_counts

    df = pd.DataFrame({
        "building_feature_id": [1, 2],
        "building_type": [None, "MFH"],
        "is_residential": [False, True],
        "area_total_floor": [2125.0, 6413.5],
        "number_of_storeys": [1, 3],
        "residential_units": [1.0, 2.0],
    })
    out = repair_dwelling_counts(df)

    assert out.loc[0, "residential_units"] == 1.0
    assert out.loc[0, "residential_units_source"] == "rivm"
    assert out.loc[1, "residential_units"] == 214.0


def test_repair_dwelling_counts_recomputes_from_the_registered_value():
    """Re-running must correct an earlier repair rather than compound it,
    so it always starts from residential_units_recorded when present."""
    import pandas as pd

    from buem.buildings.datasources.nl_archetype_mapper import repair_dwelling_counts

    already_repaired = pd.DataFrame({
        "building_feature_id": [1],
        "building_type": ["MFH"],
        "is_residential": [True],
        "area_total_floor": [6413.5],
        "number_of_storeys": [3],
        "residential_units": [999.0],          # a wrong earlier repair
        "residential_units_recorded": [2.0],   # what RIVM actually said
    })
    out = repair_dwelling_counts(already_repaired)
    assert out.loc[0, "residential_units"] == 214.0


def test_non_residential_buildings_keep_their_construction_era():
    """A service building gets no TABULA archetype -- that typology is
    residential-only -- but its construction era still drives the
    service-building reference lookup, so it must survive."""
    buildings = _synthetic_buildings().copy()
    # Mark every synthetic building non-residential via the RIVM signal
    # (a matched Pand with zero registered dwelling units).
    rivm = pd.DataFrame({
        "bag_pand_id": buildings["bag_pand_id"],
        "aant_verblijfsobj": [0.0] * len(buildings),
        "dominant_label": [None] * len(buildings),
    })
    result = map_buildings(buildings, _nl_tabula, rivm)

    assert not result["is_residential"].any()
    assert result["tabula_variant_code_id"].isna().all()
    # The era is derived from each building's own construction_year.
    known_year = result[result["construction_year"].notna()]
    assert len(known_year) > 0
    assert known_year["construction_year_class"].notna().all()
    assert known_year["construction_year_class"].str.startswith("NL.").all()


def test_year_to_construction_class_rejects_placeholders_not_just_nulls():
    """A 0/-1 sentinel or unparseable cell is missing data. Passing it
    through would return the oldest, most-uninsulated class -- a silent
    fabrication that reads exactly like a real medieval building."""
    from buem.buildings.datasources.nl_archetype_mapper import year_to_construction_class

    for placeholder in (None, float("nan"), 0, -1, "", "abc"):
        assert year_to_construction_class(placeholder) is None, placeholder
    # Genuinely old buildings still classify -- Loenen's oldest is 1550.
    assert year_to_construction_class(1550) == "NL.01"
    assert year_to_construction_class(2024) == "NL.06"


def test_construction_year_survives_classification_for_every_building():
    """The construction year arrives from 3D BAG keyed to the Pand id and
    must still be attached to the same building afterwards -- the era is
    derived from it for both the residential and service paths."""
    buildings = _synthetic_buildings().copy()
    rivm = pd.DataFrame({
        "bag_pand_id": buildings["bag_pand_id"],
        "aant_verblijfsobj": [1.0] * len(buildings),
        "dominant_label": [None] * len(buildings),
    })
    result = map_buildings(buildings, _nl_tabula, rivm)

    assert len(result) == len(buildings)
    before = dict(zip(buildings["bag_pand_id"], buildings["construction_year"], strict=True))
    after = dict(zip(result["bag_pand_id"], result["construction_year"], strict=True))
    assert before.keys() == after.keys()
    for pand_id, year in before.items():
        assert after[pand_id] == year or (pd.isna(after[pand_id]) and pd.isna(year))

