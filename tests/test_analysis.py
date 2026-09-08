"""
Tests for ``buem.analysis`` — the reusable weather-provider comparison /
building-batch tooling built to replace one-off scratchpad scripts (see
``.claude/weather_provider_analysis_handoff.md`` for the full session
write-up these modules came out of).

Three tiers, cheapest first:

1. **Pure/unit tests** (no I/O) — ``building_selection.wall_exposure()``
   against a hand-built ``Building``, and ``weather_providers``' pure
   helpers (``_expected_hours``, ``_fix_cosmo_hour_labels``).
2. **Mocked-fallback tests** — ``weather_providers``' cosmo-rea6/era5-land
   fallback paths, exercised via ``monkeypatch`` rather than requiring the
   real archive gaps that trigger them in practice (CI only has a merra-2
   fixture — see ``CLAUDE.md``'s "Open follow-ups" — so a real cosmo-rea6/
   era5-land archive isn't guaranteed to exist anywhere this runs). Also
   ``summarize.py``'s aggregation + GHI-diff math against small synthetic
   ``ProviderRunResult``s — this is a direct regression test for the
   merra-2-vs-others ``NaN`` bug fixed this session (mismatched half-hour
   vs. on-the-hour indices under a plain pandas subtraction).
3. **Real end-to-end tests**, gated behind ``requires_bundled_workbook``
   (mirrors ``test_live_synthesis.py``) since they need
   ``ExcelBuildingSource``/``LOD2Mapper``, and a real weather fetch at
   buem's documented default location/year/provider (52.0, 5.0, 2018,
   merra-2) — the one combination CI's weather fixture actually covers.
   Not separately skip-gated on weather-archive availability, matching
   ``test_building_types.py``'s existing convention (assumes
   ``WEATHER_DATA_DIR`` is configured; fails loudly, not skipped, if not).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from buem.buildings.building import Building, BuildingIdentity
from buem.buildings.components.base import EnvelopeElement
from buem.buildings.pipeline import DEFAULT_WORKBOOK

_workbook_missing_reason = (
    None if DEFAULT_WORKBOOK.exists()
    else f"bundled TABULA workbook not present: {DEFAULT_WORKBOOK}"
)
requires_bundled_workbook = pytest.mark.skipif(
    _workbook_missing_reason is not None, reason=_workbook_missing_reason or "",
)


# ── building_selection: pure dataclass reasoning, no I/O ────────────────


def _building_with_walls(b_transmissions: list[float]) -> Building:
    elements = [
        EnvelopeElement(
            id=f"wall_{i}", element_type="wall", area=10.0,
            U=0.3 if bt else 0.0, b_transmission=bt,
        )
        for i, bt in enumerate(b_transmissions)
    ]
    return Building(identity=BuildingIdentity(building_feature_id="test"), elements=elements)


def test_wall_exposure_all_shared():
    from buem.analysis.building_selection import wall_exposure

    summary = wall_exposure(_building_with_walls([0.0, 0.0, 0.0]))
    assert (summary.n_walls, summary.n_exposed, summary.n_shared) == (3, 0, 3)
    assert not summary.has_exposed_wall
    assert not summary.is_fully_exposed


def test_wall_exposure_all_exposed():
    from buem.analysis.building_selection import wall_exposure

    summary = wall_exposure(_building_with_walls([1.0, 1.0]))
    assert (summary.n_walls, summary.n_exposed, summary.n_shared) == (2, 2, 0)
    assert summary.has_exposed_wall
    assert summary.is_fully_exposed


def test_wall_exposure_mixed():
    from buem.analysis.building_selection import wall_exposure

    summary = wall_exposure(_building_with_walls([1.0, 0.0, 1.0, 0.0]))
    assert (summary.n_walls, summary.n_exposed, summary.n_shared) == (4, 2, 2)
    assert summary.has_exposed_wall
    assert not summary.is_fully_exposed


# ── weather_providers: pure helpers ──────────────────────────────────────


def test_expected_hours_regular_year():
    from buem.analysis.weather_providers import _expected_hours

    assert _expected_hours(2018) == 8760


def test_expected_hours_leap_year():
    from buem.analysis.weather_providers import _expected_hours

    assert _expected_hours(2020) == 8784


def test_fix_cosmo_hour_labels_shifts_index_back_one_hour():
    from buem.analysis.weather_providers import _fix_cosmo_hour_labels

    idx = pd.date_range("2018-01-01 01:00", periods=3, freq="h")
    df = pd.DataFrame({"T": [1.0, 2.0, 3.0]}, index=idx)

    fixed = _fix_cosmo_hour_labels(df)

    assert list(fixed.index) == list(pd.date_range("2018-01-01 00:00", periods=3, freq="h"))
    assert df.index[0] == pd.Timestamp("2018-01-01 01:00")  # original untouched


# ── weather_providers: mocked fallback paths ─────────────────────────────


def _flat_weather_df(index: pd.DatetimeIndex, **overrides) -> pd.DataFrame:
    n = len(index)
    data = {"T": [0.0] * n, "GHI": [0.0] * n, "DHI": [0.0] * n, "DNI": [0.0] * n}
    data.update(overrides)
    return pd.DataFrame(data, index=index)


def test_extract_one_provider_cosmo_falls_back_to_annual_file(monkeypatch):
    """A short monthly-archive result (missing a month) should trigger the
    annual-export-file fallback and come back complete, hour-label-fixed."""
    from buem.analysis import weather_providers as wp

    short_df = _flat_weather_df(pd.date_range("2018-01-01", periods=100, freq="h"))
    full_index = pd.date_range("2018-01-01 01:00", periods=8760, freq="h")
    full_df = _flat_weather_df(full_index)

    monkeypatch.setattr(wp, "get_or_fetch_weather", lambda *a, **k: short_df)
    monkeypatch.setattr(wp, "_cosmo_annual_fallback", lambda *a, **k: full_df)

    result = wp._extract_one_provider(52.0, 5.0, 2018, "cosmo-rea6")

    assert len(result) == 8760
    assert result.index[0] == pd.Timestamp("2018-01-01 00:00")  # hour-label fix applied


def test_extract_one_provider_cosmo_raises_if_annual_file_also_incomplete(monkeypatch):
    from buem.analysis import weather_providers as wp

    short_df = _flat_weather_df(pd.date_range("2018-01-01", periods=100, freq="h"))
    still_short_df = _flat_weather_df(pd.date_range("2018-01-01", periods=200, freq="h"))

    monkeypatch.setattr(wp, "get_or_fetch_weather", lambda *a, **k: short_df)
    monkeypatch.setattr(wp, "_cosmo_annual_fallback", lambda *a, **k: still_short_df)

    with pytest.raises(FileNotFoundError, match="still incomplete"):
        wp._extract_one_provider(52.0, 5.0, 2018, "cosmo-rea6")


def test_extract_one_provider_era5_land_applies_boundary_fix_on_runtime_error(monkeypatch):
    from buem.analysis import weather_providers as wp

    fixed_df = _flat_weather_df(pd.date_range("2018-01-01", periods=8760, freq="h"))

    def _raise_unrepaired(*a, **k):
        raise RuntimeError("era5-land archive has 1 unrepaired month(s)...")

    monkeypatch.setattr(wp, "get_or_fetch_weather", _raise_unrepaired)
    monkeypatch.setattr(wp, "_era5_land_boundary_fix", lambda *a, **k: fixed_df)

    result = wp._extract_one_provider(52.0, 5.0, 2018, "era5-land")

    assert len(result) == 8760


def test_extract_one_provider_era5_land_reraises_unrelated_runtime_error(monkeypatch):
    from buem.analysis import weather_providers as wp

    def _raise_other(*a, **k):
        raise RuntimeError("some unrelated failure")

    monkeypatch.setattr(wp, "get_or_fetch_weather", _raise_other)

    with pytest.raises(RuntimeError, match="unrelated failure"):
        wp._extract_one_provider(52.0, 5.0, 2018, "era5-land")


def test_extract_provider_weather_rejects_nan(monkeypatch):
    from buem.analysis import weather_providers as wp

    bad_index = pd.date_range("2018-01-01", periods=8760, freq="h")
    bad_df = _flat_weather_df(bad_index, T=[np.nan] * 8760)

    monkeypatch.setattr(wp, "get_or_fetch_weather", lambda *a, **k: bad_df)

    with pytest.raises(ValueError, match="NaN"):
        wp.extract_provider_weather(52.0, 5.0, 2018, providers=("merra-2",))


def test_extract_provider_weather_rejects_wrong_row_count(monkeypatch):
    from buem.analysis import weather_providers as wp

    short_index = pd.date_range("2018-01-01", periods=100, freq="h")
    short_df = _flat_weather_df(short_index)

    monkeypatch.setattr(wp, "get_or_fetch_weather", lambda *a, **k: short_df)

    with pytest.raises(ValueError, match="expected 8760"):
        wp.extract_provider_weather(52.0, 5.0, 2018, providers=("era5-land",))


# ── summarize.py: pure aggregation math, hand-built ProviderRunResult ───


def _fake_result(provider, heating, cooling, elec, t, ghi, dni, dhi, index):
    from buem.analysis.provider_comparison import ProviderRunResult

    weather = pd.DataFrame({"T": t, "GHI": ghi, "DNI": dni, "DHI": dhi}, index=index)
    return ProviderRunResult(
        provider=provider,
        weather=weather,
        heating_kW=pd.Series(heating, index=index, name="heating_kW"),
        cooling_kW=pd.Series(cooling, index=index, name="cooling_kW"),
        elec_kW=pd.Series(elec, index=index, name="elec_kW"),
        building_type="SFH",
        construction_period="DE.01",
        A_ref=100.0,
    )


def test_summarize_demand_annual_totals():
    from buem.analysis.summarize import summarize_demand

    index = pd.date_range("2018-01-01", periods=4, freq="MS")
    results = {
        "merra-2": _fake_result(
            "merra-2",
            heating=[1.0, 2.0, 0.0, 0.0], cooling=[0.0, 0.0, -1.0, -2.0],
            elec=[0.5, 0.5, 0.5, 0.5], t=[1, 2, 3, 4], ghi=[10, 20, 30, 40],
            dni=[5, 5, 5, 5], dhi=[1, 1, 1, 1], index=index,
        ),
    }

    demand = summarize_demand(results)

    assert demand.annual_kWh.loc["merra-2", "heating_kWh"] == pytest.approx(3.0)
    assert demand.annual_kWh.loc["merra-2", "cooling_kWh"] == pytest.approx(3.0)  # abs(-1) + abs(-2)
    assert demand.annual_kWh.loc["merra-2", "elec_kWh"] == pytest.approx(2.0)


def test_summarize_weather_ghi_pairwise_diff_no_nan_across_index_offsets():
    """Regression test for the bug fixed this session: merra-2's half-hour-
    offset index vs. cosmo/era5-land's on-the-hour index used to produce
    NaN under a plain pandas subtraction (misaligned index union). Fixed
    by comparing the underlying arrays positionally instead."""
    from buem.analysis.summarize import summarize_weather

    on_hour_index = pd.date_range("2018-01-01 00:00", periods=4, freq="h")
    half_hour_index = pd.date_range("2018-01-01 00:30", periods=4, freq="h")

    results = {
        "merra-2": _fake_result(
            "merra-2", heating=[0] * 4, cooling=[0] * 4, elec=[0] * 4,
            t=[1, 2, 3, 4], ghi=[100, 200, 300, 400], dni=[0] * 4, dhi=[0] * 4,
            index=half_hour_index,
        ),
        "cosmo-rea6": _fake_result(
            "cosmo-rea6", heating=[0] * 4, cooling=[0] * 4, elec=[0] * 4,
            t=[1, 2, 3, 4], ghi=[110, 190, 320, 380], dni=[0] * 4, dhi=[0] * 4,
            index=on_hour_index,
        ),
    }

    weather_summary = summarize_weather(results)
    diff_row = weather_summary.ghi_pairwise_diff.loc["merra-2 vs cosmo-rea6"]

    assert not pd.isna(diff_row["mean_abs_hourly_diff_Wm2"])
    assert diff_row["mean_abs_hourly_diff_Wm2"] == pytest.approx((10 + 10 + 20 + 20) / 4)
    assert not pd.isna(diff_row["annual_total_pct_diff"])


# ── real end-to-end: bundled workbook + real default-location weather ───


@requires_bundled_workbook
def test_provider_comparison_end_to_end_building_52203_merra2():
    """Regression guard on the end-to-end figure for building 52203 with
    the merra-2 provider: 42,411.9 kWh.

    This value tracks buem's modeling assumptions and moves when they
    change deliberately. Most recently, two occupancy-related changes in
    opposite directions: occupancy v6.0.0 made household appliance use
    scale with occupant count, raising a multi-occupant dwelling's
    internal gain and lowering heating about 1%; then num_persons stopped
    defaulting to a flat 4 and began resolving a real per-building-type
    figure (this is an MFH, so 1.54 occupants for the Netherlands rather
    than 4), which removed more internal gain than v6.0.0 added and
    raised heating about 2.5% net. Before those, when the comfort
    dead-band became buem's own 18-21 degC default (representing observed
    occupant behavior rather than TABULA's standardized 20 degC
    calculation setpoint), and when window area became a fraction of each
    wall's own area rather than TABULA's per-direction window columns.

    Its purpose is to catch *unintended* drift in the analysis pipeline,
    so update it only alongside a deliberate modeling change."""
    from buem.analysis.provider_comparison import run_building_across_providers
    from buem.analysis.weather_providers import extract_provider_weather
    from buem.buildings.datasources.excel_source import ExcelBuildingSource
    from buem.buildings.mapping.lod2_mapper import LOD2Mapper
    from buem.config.building_registry import DEFAULT_COMFORT_T_LB

    source = ExcelBuildingSource(DEFAULT_WORKBOOK)
    mapper = LOD2Mapper(source, country="DE")
    building = mapper.map_building(52203)
    assert building is not None
    assert building.thermal.comfortT_lb == DEFAULT_COMFORT_T_LB

    weather = extract_provider_weather(52.0, 5.0, 2018, providers=("merra-2",))
    results = run_building_across_providers(building, weather)

    assert set(results) == {"merra-2"}
    r = results["merra-2"]
    assert float(r.heating_kW.sum()) == pytest.approx(42411.9, rel=0.01)
    assert len(r.heating_kW) == 8760


@requires_bundled_workbook
def test_batch_run_building_52203_merra2_writes_ok_row(tmp_path):
    from buem.analysis.batch import BatchConfig, run_batch

    output_path = tmp_path / "batch_smoke.parquet"
    config = BatchConfig(
        workbook_path=DEFAULT_WORKBOOK,
        country="DE",
        provider="merra-2",
        building_ids=[52203],
        workers=1,
    )

    result_path = run_batch(config, output_path)

    assert result_path == output_path
    df = pd.read_parquet(output_path)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["status"] == "ok"
    assert row["building_feature_id"] == 52203
    # Same regression guard as
    # test_provider_comparison_end_to_end_building_52203_merra2 -- see
    # its docstring for when this value legitimately changes.
    assert row["heating_kWh"] == pytest.approx(42411.9, rel=0.01)


@requires_bundled_workbook
def test_batch_run_previously_crashing_party_wall_buildings_now_succeed(tmp_path):
    """Regression test for the two model_buem.py fixes documented in
    CHANGELOG.md / .claude/residential/resolved.md (2026-08-15), found via
    this exact batch path: 44424/42868/60342/18763 (TH/SFH, partially
    exposed -- per-element-only U crashed the solar-gain calc) and 30542
    (MFH, 0/15 walls exposed -- empty Windows component crashed a
    "no window elements" check). All five must now succeed."""
    from buem.analysis.batch import BatchConfig, run_batch

    output_path = tmp_path / "batch_party_wall_regression.parquet"
    building_ids = [44424, 42868, 60342, 18763, 30542]
    config = BatchConfig(
        workbook_path=DEFAULT_WORKBOOK,
        country="DE",
        provider="merra-2",
        building_ids=building_ids,
        workers=1,
    )

    run_batch(config, output_path)

    df = pd.read_parquet(output_path).set_index("building_feature_id")
    assert set(df.index) == set(building_ids)
    for bid in building_ids:
        row = df.loc[bid]
        assert row["status"] == "ok", f"building {bid}: {row['error']}"
        assert row["heating_kWh"] > 0


# ── batch: source selection, filtering and resume (no simulation) ───────
# These exercise run_batch()'s bookkeeping directly, without a solve, so
# they stay fast enough to run on every commit.


class _FakeSource:
    """Minimal stand-in exposing only what the id-selection step reads."""

    def __init__(self, buildings: pd.DataFrame):
        self.buildings = buildings


def _nl_like_buildings() -> pd.DataFrame:
    return pd.DataFrame({
        "building_feature_id": [1, 2, 3, 4],
        "is_residential": [True, True, False, True],
        "matched_via_label": [True, False, True, False],
    })


def test_select_building_ids_filters_residential_and_labeled():
    from buem.analysis.batch import BatchConfig, _select_building_ids

    source = _FakeSource(_nl_like_buildings())

    assert _select_building_ids(source, BatchConfig(source_kind="csv", data_dir=".")) == [1, 2, 3, 4]
    assert _select_building_ids(
        source, BatchConfig(source_kind="csv", data_dir=".", residential_only=True),
    ) == [1, 2, 4]
    assert _select_building_ids(
        source, BatchConfig(source_kind="csv", data_dir=".", residential_only=True, labeled_only=True),
    ) == [1]


def test_select_building_ids_raises_when_filter_column_absent():
    """Silently running the unfiltered population when a filter cannot be
    honoured would report a population figure as if it were a subset."""
    from buem.analysis.batch import BatchConfig, _select_building_ids

    source = _FakeSource(pd.DataFrame({"building_feature_id": [1, 2]}))
    with pytest.raises(ValueError, match="is_residential"):
        _select_building_ids(source, BatchConfig(source_kind="csv", data_dir=".", residential_only=True))


def test_select_building_ids_prefers_explicit_ids_over_filters():
    from buem.analysis.batch import BatchConfig, _select_building_ids

    source = _FakeSource(_nl_like_buildings())
    config = BatchConfig(source_kind="csv", data_dir=".", building_ids=[3], residential_only=True)
    assert _select_building_ids(source, config) == [3]


def test_read_completed_rows_round_trips_and_reshapes(tmp_path):
    """Resume re-emits finished rows, so they must survive the round trip
    onto the current column set -- including a file written before a
    column existed."""
    from buem.analysis.batch import _RESULT_COLUMNS, _read_completed_rows

    path = tmp_path / "partial.parquet"
    pd.DataFrame([
        {"building_feature_id": 7, "status": "ok", "heating_kWh": 1234.5},
    ]).to_parquet(path)

    rows = _read_completed_rows(path)

    assert len(rows) == 1
    assert set(rows[0]) == set(_RESULT_COLUMNS)
    assert rows[0]["building_feature_id"] == 7
    assert rows[0]["heating_kWh"] == 1234.5
    assert rows[0]["dhw_kWh"] is None


def test_read_completed_rows_empty_when_no_previous_run(tmp_path):
    from buem.analysis.batch import _read_completed_rows

    assert _read_completed_rows(tmp_path / "does_not_exist.parquet") == []


def test_batch_config_source_path_switches_on_kind():
    from buem.analysis.batch import BatchConfig

    assert BatchConfig(source_kind="excel", workbook_path="wb.xlsx").source_path == "wb.xlsx"
    assert BatchConfig(source_kind="csv", data_dir="regions/nl").source_path == "regions/nl"
    with pytest.raises(ValueError, match="requires data_dir"):
        _ = BatchConfig(source_kind="csv").source_path


def test_build_source_rejects_unknown_kind():
    from buem.analysis.batch import build_source

    with pytest.raises(ValueError, match="Unknown source kind"):
        build_source("postgres", "somewhere")


@requires_bundled_workbook
def test_report_build_html_report_writes_self_contained_file(tmp_path):
    from buem.analysis.provider_comparison import run_building_across_providers
    from buem.analysis.report import build_html_report
    from buem.analysis.summarize import summarize_demand, summarize_weather
    from buem.analysis.weather_providers import extract_provider_weather
    from buem.buildings.datasources.excel_source import ExcelBuildingSource
    from buem.buildings.mapping.lod2_mapper import LOD2Mapper

    source = ExcelBuildingSource(DEFAULT_WORKBOOK)
    mapper = LOD2Mapper(source, country="DE")
    building = mapper.map_building(52203)
    assert building is not None

    weather = extract_provider_weather(52.0, 5.0, 2018, providers=("merra-2",))
    results = run_building_across_providers(building, weather)
    demand = summarize_demand(results)
    weather_summary = summarize_weather(results)

    out = build_html_report(
        building, results, demand, weather_summary,
        location_label="52.0N, 5.0E", year=2018,
        output_path=tmp_path / "report.html",
    )

    html = out.read_text(encoding="utf-8")
    assert "<title>" in html
    assert '"merra-2"' in html  # provider key embedded in the JSON payload
    assert "{building_id}" not in html  # no leftover str.format() placeholder
