"""
Tests for ``buem.buildings.mapping.nl_building_classifier`` -- pure-Python
unit tests against synthetic adjacency data, no CityJSON/RIVM file needed.
"""
from __future__ import annotations

import pandas as pd

from buem.buildings.mapping.nl_building_classifier import (
    MIN_SERVICE_BUILDING_FOOTPRINT_M2,
    build_adjacency,
    classify_all,
    classify_building_type,
    connected_component_sizes,
)


def _bldg(pid, neighbours=None, units=None, kas=False, glas=False, footprint=0.0):
    return {
        "bag_pand_id": pid,
        "attached_neighbour_id": ";".join(neighbours) if neighbours else None,
        "is_greenhouse_or_warehouse": kas,
        "is_glass_roof": glas,
        "footprint_area": footprint,
    }


# ── classify_building_type: the CBS rules directly ──────────────────────


def test_classify_detached():
    assert classify_building_type(degree=0, component_size=1, n_residential_units=1) == ("SFH", "B_Alone")


def test_classify_semi_detached_pair():
    # degree 1, component of exactly 2 -> twee-onder-een-kap -> SFH/B_N1
    assert classify_building_type(degree=1, component_size=2, n_residential_units=1) == ("SFH", "B_N1")


def test_classify_corner_house():
    # degree 1, but part of a row of 3+ -> hoekwoning -> TH/B_N1
    assert classify_building_type(degree=1, component_size=4, n_residential_units=1) == ("TH", "B_N1")


def test_classify_mid_terrace():
    assert classify_building_type(degree=2, component_size=5, n_residential_units=1) == ("TH", "B_N2")


def test_classify_multi_family_small():
    building_type, status = classify_building_type(degree=0, component_size=1, n_residential_units=3)
    assert building_type == "MFH"
    assert status == "B_Alone"


def test_classify_multi_family_large_is_apartment_block():
    building_type, _ = classify_building_type(degree=0, component_size=1, n_residential_units=20)
    assert building_type == "AB"


def test_classify_missing_units_defaults_to_single_family_path():
    # None/0 units -> treated as 1 (the common case), not "unknown multi-unit"
    assert classify_building_type(degree=0, component_size=1, n_residential_units=None) == ("SFH", "B_Alone")
    assert classify_building_type(degree=0, component_size=1, n_residential_units=0) == ("SFH", "B_Alone")


# ── adjacency graph + connected components ───────────────────────────────


def test_build_adjacency_is_symmetric():
    df = pd.DataFrame([_bldg("A", ["B"]), _bldg("B", None)])  # only A lists B
    graph = build_adjacency(df)
    assert graph["A"] == {"B"}
    assert graph["B"] == {"A"}  # symmetric even though only recorded on A's row


def test_connected_component_sizes_row_of_three():
    # A - B - C, a row of 3 (B is mid-terrace, A/C are corner units)
    df = pd.DataFrame([_bldg("A", ["B"]), _bldg("B", ["A", "C"]), _bldg("C", ["B"])])
    graph = build_adjacency(df)
    sizes = connected_component_sizes(graph)
    assert sizes == {"A": 3, "B": 3, "C": 3}


def test_connected_component_sizes_isolated_building():
    df = pd.DataFrame([_bldg("A", None)])
    sizes = connected_component_sizes(build_adjacency(df))
    assert sizes["A"] == 1


# ── classify_all: end-to-end on a small synthetic dataset ───────────────


def test_classify_all_end_to_end_row_of_three_plus_detached_plus_apartment():
    df = pd.DataFrame([
        _bldg("detached", None),
        _bldg("corner1", ["mid"]),
        _bldg("mid", ["corner1", "corner2"]),
        _bldg("corner2", ["mid"]),
        _bldg("apartments", None, units=12),
        _bldg("big_warehouse", None, kas=True, footprint=2000.0),
        _bldg("garden_shed", None, glas=True, footprint=16.0),
    ])
    units = {"apartments": 12.0}
    result = classify_all(df, units)

    by_id = result.set_index("bag_pand_id")
    assert tuple(by_id.loc["detached", ["building_type", "neighbour_status"]]) == ("SFH", "B_Alone")
    assert tuple(by_id.loc["corner1", ["building_type", "neighbour_status"]]) == ("TH", "B_N1")
    assert tuple(by_id.loc["mid", ["building_type", "neighbour_status"]]) == ("TH", "B_N2")
    assert tuple(by_id.loc["corner2", ["building_type", "neighbour_status"]]) == ("TH", "B_N1")
    assert by_id.loc["apartments", "building_type"] == "AB"

    # large flagged building -> real service_building_type, still not residential
    assert by_id.loc["big_warehouse", "is_residential"] == False  # noqa: E712
    assert pd.isna(by_id.loc["big_warehouse", "building_type"])
    assert by_id.loc["big_warehouse", "service_building_type"] == "warehouse"

    # tiny flagged building -> excluded from both residential AND service modeling
    assert by_id.loc["garden_shed", "is_residential"] == False  # noqa: E712
    assert pd.isna(by_id.loc["garden_shed", "building_type"])
    assert pd.isna(by_id.loc["garden_shed", "service_building_type"])

    # ordinary residential buildings never get a service_building_type
    assert pd.isna(by_id.loc["detached", "service_building_type"])


def test_classify_all_excludes_buildings_with_no_registered_residential_unit():
    """A Pand matched in RIVM's data (present in units_by_pand_id) but
    with zero or null residential units registered under it is a real
    non-dwelling (a shed/garage/outbuilding), not an ambiguous case --
    excluded from residential classification the same as a flagged
    greenhouse/warehouse, distinct from no RIVM match at all."""
    df = pd.DataFrame([
        _bldg("shed_zero", None, footprint=15.0),
        _bldg("shed_null", None, footprint=20.0),
        _bldg("real_house_no_match", None, footprint=90.0),
        _bldg("real_house_matched", None, footprint=110.0),
    ])
    units = {"shed_zero": 0.0, "shed_null": float("nan"), "real_house_matched": 1.0}
    result = classify_all(df, units)
    by_id = result.set_index("bag_pand_id")

    assert by_id.loc["shed_zero", "is_residential"] == False  # noqa: E712
    assert pd.isna(by_id.loc["shed_zero", "building_type"])
    assert by_id.loc["shed_null", "is_residential"] == False  # noqa: E712
    assert pd.isna(by_id.loc["shed_null", "building_type"])

    # No RIVM match at all stays ambiguous, not excluded -- unlike a
    # matched-but-zero/null record, "unknown" isn't treated as evidence
    # of non-residential.
    assert by_id.loc["real_house_no_match", "is_residential"] == True  # noqa: E712
    assert by_id.loc["real_house_no_match", "building_type"] == "SFH"

    assert by_id.loc["real_house_matched", "is_residential"] == True  # noqa: E712
    assert by_id.loc["real_house_matched", "building_type"] == "SFH"


def test_classify_all_glass_roof_large_enough_also_links_to_service_type():
    """A real large glass-roofed structure (e.g. a genuine greenhouse
    complex) should link to a service type too, not just b3_kas_warenhuis
    ones -- Loenen's own is_glas_dak buildings happen to both be tiny, but
    the rule itself isn't flag-specific, only size-specific."""
    df = pd.DataFrame([_bldg("real_greenhouse", None, glas=True, footprint=500.0)])
    result = classify_all(df, {})
    assert result.iloc[0]["service_building_type"] == "warehouse"


def test_min_service_building_footprint_constant_is_between_real_loenen_cases():
    """Sanity check on the threshold itself against the real evidence it
    was set from (2,125/1,718 m² real commercial structures vs.
    16/5 m² garden structures) -- not a finely-tuned cutoff, just needs
    to sit clearly between the two real clusters."""
    assert 16.0 < MIN_SERVICE_BUILDING_FOOTPRINT_M2 < 1718.0


def _use_summary(n_residential, n_non_residential, functions, area=200.0):
    from buem.buildings.datasources.bag_use_function import PandUseSummary
    return PandUseSummary(
        n_residential_units=n_residential,
        n_non_residential_units=n_non_residential,
        function_counts=functions,
        non_residential_area_m2=area,
    )


def _one_building(pand_id="NL.IMBAG.Pand.1", footprint=200.0):
    return pd.DataFrame([{
        "bag_pand_id": pand_id,
        "footprint_area": footprint,
        "attached_neighbour_id": None,
        "is_greenhouse_or_warehouse": False,
        "is_glass_roof": False,
    }])


def test_purely_non_residential_use_routes_to_a_service_type():
    """A building whose every registered BAG unit is non-residential is a
    service building, even though its unit count is indistinguishable
    from a dwelling's."""
    df = _one_building()
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 1.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(0, 1, {"winkelfunctie": 1})},
    )
    assert not result["is_residential"].iloc[0]
    assert result["service_building_type"].iloc[0] == "supermarket"
    assert result["building_type"].iloc[0] is None


def test_mixed_use_building_stays_residential():
    """A shop with a flat above keeps its dwelling: buem models one use
    per building, so claiming it for the service path would discard the
    residential half."""
    df = _one_building()
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 2.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(1, 1, {"winkelfunctie": 1})},
    )
    assert result["is_residential"].iloc[0]
    assert result["service_building_type"].iloc[0] is None


def test_dominant_use_function_wins_over_a_minor_one():
    """The most-registered function decides the type."""
    df = _one_building()
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 4.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(
            0, 4, {"kantoorfunctie": 3, "winkelfunctie": 1})},
    )
    assert result["service_building_type"].iloc[0] == "office"


def test_unmappable_use_function_is_not_forced_into_a_type():
    """sportfunctie has no counterpart among occupancy's eight service
    types, so the building is left unmodeled rather than mis-typed."""
    df = _one_building()
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 1.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(0, 1, {"sportfunctie": 1})},
    )
    assert not result["is_residential"].iloc[0]
    assert result["service_building_type"].iloc[0] is None


def test_classification_without_use_data_is_unchanged():
    """Omitting the extract leaves the pre-existing two-signal behaviour
    intact, so a region with no BAG fetch still classifies."""
    df = _one_building()
    result = classify_all(df, units_by_pand_id={"NL.IMBAG.Pand.1": 1.0})
    assert result["is_residential"].iloc[0]
    assert result["building_type"].iloc[0] == "SFH"


def test_derive_service_capacity_scales_with_floor_area():
    """Capacity tracks real floor area, and reproduces occupancy's own
    per-type default at that type's reference size."""
    from buem.config.building_registry import derive_service_capacity

    assert derive_service_capacity("hotel", 2400.0) == 120
    assert derive_service_capacity("hotel", 52.0) == 3
    assert derive_service_capacity("office", 600.0) == 40
    # A type with no published density leaves the choice to occupancy.
    assert derive_service_capacity("not_a_type", 500.0) is None
    # Unusable floor area does the same rather than inventing a number.
    assert derive_service_capacity("hotel", 0.0) is None


def test_small_logiesfunctie_is_not_typed_as_a_hotel():
    """A 60 m2 recreational cabin is not occupancy's staffed-hotel
    profile, so it is left unmodelled rather than overstated."""
    df = _one_building(footprint=60.0)
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 1.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(0, 1, {"logiesfunctie": 1})},
    )
    assert not result["is_residential"].iloc[0]
    assert result["service_building_type"].iloc[0] is None


def test_large_logiesfunctie_is_still_a_hotel():
    df = _one_building(footprint=1200.0)
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 1.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(0, 1, {"logiesfunctie": 1})},
    )
    assert result["service_building_type"].iloc[0] == "hotel"


def test_small_logiesfunctie_falls_through_to_another_registered_use():
    """The size gate skips the hotel mapping without abandoning a
    building that also carries a mappable function."""
    df = _one_building(footprint=60.0)
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 2.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(
            0, 2, {"logiesfunctie": 2, "winkelfunctie": 1})},
    )
    assert result["service_building_type"].iloc[0] == "supermarket"


def test_large_pand_with_no_verblijfsobject_is_flagged_ancillary():
    """A 900 m2 in-use Pand carrying no registered unit is a barn or hall,
    not a shed -- it must be counted, not silently dropped."""
    df = _one_building(footprint=900.0)
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 0.0},
        use_by_pand_id={"NL.IMBAG.Pand.2": _use_summary(1, 0, {"woonfunctie": 1})},
    )
    assert not result["is_residential"].iloc[0]
    assert result["service_building_type"].iloc[0] is None
    assert bool(result["is_ancillary_structure"].iloc[0])


def test_small_pand_with_no_verblijfsobject_is_not_ancillary():
    """Below the threshold the same category is garden sheds and garages,
    whose demand is negligible."""
    df = _one_building(footprint=25.0)
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 0.0},
        use_by_pand_id={"NL.IMBAG.Pand.2": _use_summary(1, 0, {"woonfunctie": 1})},
    )
    assert not bool(result["is_ancillary_structure"].iloc[0])


def test_typed_service_building_is_never_ancillary():
    """The flag marks buildings with no recorded use at all, so it must
    not overlap the ones that do get simulated."""
    df = _one_building(footprint=900.0)
    result = classify_all(
        df,
        units_by_pand_id={"NL.IMBAG.Pand.1": 1.0},
        use_by_pand_id={"NL.IMBAG.Pand.1": _use_summary(0, 1, {"winkelfunctie": 1})},
    )
    assert result["service_building_type"].iloc[0] == "supermarket"
    assert not bool(result["is_ancillary_structure"].iloc[0])


def test_ancillary_flag_is_all_false_without_use_function_data():
    """The flag means "known to have no registered unit". With no use
    data supplied that is unknown, not true, so nothing is flagged."""
    df = _one_building(footprint=900.0)
    result = classify_all(df, units_by_pand_id={"NL.IMBAG.Pand.1": 0.0})
    assert not result["is_ancillary_structure"].any()

