"""
Many-building batch runner: single-provider heating/cooling/electricity
demand for every (or a filtered subset of) building in a building source,
written incrementally to a single Parquet file.

Two sources are supported, selected by ``--source``. ``excel`` reads the
bundled TABULA/city2tabula workbook (the German path). ``csv`` reads a
``CsvBuildingSource`` region directory such as
``src/buem/data/buildings/netherlands/Loenen``, which is what makes a
whole-community run possible: simulating every building once and writing
the per-building results means downstream analysis
(``buem.analysis.netherlands.validation --from-parquet``) aggregates the
true population rather than a sample, so no sampling bias exists to
correct for.

Deliberately single-provider (unlike ``provider_comparison.py``, which
runs one building through all providers) -- per the user's own
simplification ("we do not need to run all 3 weather data" for a
full-scale batch). One weather DataFrame is fetched once in the main
process and shared read-only across every worker/building, rather than
re-fetched per building: buem's own thermal properties, not the weather
feed, are what's under study at batch scale.

Mirrors ``buem.parallelization.parallel_run.ParallelBuildingProcessor``'s
``ProcessPoolExecutor`` + per-worker heavy-import pre-warm pattern, but
doesn't reuse that class directly -- its own pre-warm step assumes one
building file per building and re-derives lat/lon/provider per file via
``_extract_building_attrs()``, neither of which applies here (buildings
come from ``ExcelBuildingSource`` by feature id, and every building in
one batch run shares the single weather fetch made up front).

No hardcoded paths: the workbook path and ``WEATHER_DATA_DIR`` come from
``DEFAULT_WORKBOOK``/environment variables exactly as everywhere else in
this codebase, so this module runs unchanged on a bigger machine (e.g.
sd26) later -- submitting/running it there is the user's own operational
step, not something this module does.

CLI
---
    python -m buem.analysis.batch --limit 20 --provider merra-2 --output results.parquet

    python -m buem.analysis.batch --source csv \\
        --data-dir src/buem/data/buildings/netherlands/Loenen \\
        --country NL --residential-only --resume --output loenen.parquet

See ``tests/test_analysis.py`` for a small (2-3 building) smoke test of
this same path.
"""
from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from buem.buildings.pipeline import DEFAULT_WORKBOOK
from buem.config.building_registry import (
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_WEATHER_PROVIDER,
    DEFAULT_YEAR,
)
from buem.config.reference_values import resolve_envelope_reference

logger = logging.getLogger(__name__)

# Errors an individual building's mapping/attribute-build/solve step can
# raise without this being a bug in the batch runner itself -- caught per
# building so one bad row doesn't abort the whole run. Mirrors
# parallel_run.py's own exception tuple, with RuntimeError added (CVXPY's
# own input-validation failures, e.g. "Problem data contains NaN", raise
# this -- see CLAUDE.md's weather data-quality notes).
_PER_BUILDING_ERRORS = (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, RuntimeError)

_RESULT_COLUMNS = [
    "building_feature_id",
    "status",
    "building_type",
    "construction_period",
    "A_ref",
    "n_walls",
    "n_exposed",
    "heating_kWh",
    "cooling_kWh",
    "elec_kWh",
    "dhw_kWh",
    "cooking_gas_kWh",
    # Carried straight through from the source table rather than recomputed,
    # so a downstream aggregation can group and filter (by housing type for
    # the CBS comparison, by era or label for a stratified view) without
    # re-opening the building source.
    "neighbour_status",
    "construction_year_class",
    "matched_via_label",
    "refurbishment_variant",
    "residential_units",
    "residential_units_source",
    "error",
]

# Source-table columns copied verbatim onto each result row. Absent columns
# (the German Excel source has none of these) are left null rather than
# treated as an error.
_PASSTHROUGH_COLUMNS = (
    "neighbour_status",
    "construction_year_class",
    "matched_via_label",
    "refurbishment_variant",
    # Whether this building's dwelling count is registered or derived
    # (see nl_archetype_mapper.repair_dwelling_counts) -- carried so an
    # analysis can separate the two rather than treating a repaired
    # count as though RIVM had supplied it.
    "residential_units_source",
)

# Module-level, set once per worker process by _worker_init -- avoids both
# re-opening the (multi-MB) Excel workbook / re-instantiating LOD2Mapper
# for every single building (matching parallel_run.py's own "heavy setup
# once per worker, not once per task" convention), and re-pickling the
# shared weather DataFrame once per *building* rather than once per
# *worker* -- at full 5,236-building scale that's the difference between
# ~dozens of serializations and thousands.
_WORKER_MAPPER = None
_WORKER_WEATHER: pd.DataFrame | None = None
_WORKER_REGION_CODE: str | None = None


def build_source(source_kind: str, source_path: str | Path):
    """Construct the building source named by *source_kind*.

    ``"excel"`` is the bundled TABULA/city2tabula workbook; ``"csv"`` is a
    ``CsvBuildingSource`` region directory. Both expose the same accessor
    interface, so everything downstream of this call is identical.
    """
    if source_kind == "excel":
        from buem.buildings.datasources.excel_source import ExcelBuildingSource

        return ExcelBuildingSource(source_path)
    if source_kind == "csv":
        from buem.buildings.datasources.csv_source import CsvBuildingSource

        return CsvBuildingSource(source_path)
    raise ValueError(f"Unknown source kind {source_kind!r} -- expected 'excel' or 'csv'.")


def _worker_init(
    source_kind: str,
    source_path: str,
    country: str,
    weather_df: pd.DataFrame,
    u_value_overrides: pd.DataFrame | None,
    service_reference: pd.DataFrame | None,
    measure_overrides: pd.DataFrame | None,
    region_code: str | None = None,
) -> None:
    """Runs once per spawned worker process (see module docstring)."""
    global _WORKER_MAPPER, _WORKER_WEATHER, _WORKER_REGION_CODE

    import cvxpy  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401

    from buem.buildings.mapping.lod2_mapper import LOD2Mapper

    source = build_source(source_kind, source_path)
    _WORKER_MAPPER = LOD2Mapper(
        source, country=country,
        u_value_overrides=u_value_overrides,
        service_building_reference=service_reference,
        refurbishment_measure_overrides=measure_overrides,
    )
    _WORKER_WEATHER = weather_df
    _WORKER_REGION_CODE = region_code


def _source_row(building_feature_id: int) -> pd.Series | None:
    """This building's own row in the worker's building table, or ``None``
    if it isn't there (no source carries every id in every table)."""
    assert _WORKER_MAPPER is not None
    bdf = _WORKER_MAPPER.source.buildings
    matches = bdf[bdf["building_feature_id"] == building_feature_id]
    return matches.iloc[0] if len(matches) else None


def _residential_units(source_row: pd.Series | None) -> float:
    """Real dwelling count for a building, defaulting to 1.0.

    1.0 makes the gain scaling a no-op for SFH/TH (already one dwelling
    per building) and for any source without the column at all, so the
    German Excel path is unaffected.
    """
    if source_row is None or "residential_units" not in source_row:
        return 1.0
    value = source_row["residential_units"]
    if pd.isna(value) or float(value) <= 0:
        return 1.0
    return float(value)


def _process_one_building(building_feature_id: int, use_milp: bool) -> dict[str, Any]:
    """Map, build, and simulate one building against the worker-shared
    weather DataFrame set by ``_worker_init``."""
    from buem.analysis.building_selection import wall_exposure
    from buem.analysis.provider_comparison import building_attrs_from
    from buem.config.cfg_building import CfgBuilding
    from buem.integration.scripts.attribute_builder import AttributeBuilder
    from buem.thermal.model_buem import ModelBUEM

    row: dict[str, Any] = {col: None for col in _RESULT_COLUMNS}
    row["building_feature_id"] = building_feature_id

    try:
        assert _WORKER_MAPPER is not None, "_worker_init() must run before _process_one_building() (pool initializer)"
        building = _WORKER_MAPPER.map_building(building_feature_id)
        if building is None:
            row["status"] = "skipped"
            row["error"] = "LOD2Mapper.map_building() returned None (no TABULA match or unmappable geometry)"
            return row

        exposure = wall_exposure(building)
        row["building_type"] = building.identity.building_type
        row["construction_period"] = building.identity.construction_period
        row["A_ref"] = round(building.computed_A_ref(), 2)
        row["n_walls"] = exposure.n_walls
        row["n_exposed"] = exposure.n_exposed

        source_row = _source_row(building_feature_id)
        units = _residential_units(source_row)
        row["residential_units"] = units
        for col in _PASSTHROUGH_COLUMNS:
            if source_row is not None and col in source_row:
                value = source_row[col]
                row[col] = None if pd.isna(value) else str(value)

        attrs = dict(building_attrs_from(building))
        attrs["weather"] = _WORKER_WEATHER
        attrs["use_provided_weather"] = True
        # An MFH/AB building_feature_id is a whole multi-unit block, matching
        # TABULA's own AB/MFH archetypes. Occupancy generates one dwelling's
        # gains, so they must be scaled to the block before the solve or a
        # 28-dwelling building receives roughly 1/28th of its real internal
        # gains. Results stay whole-building; the per-dwelling division for a
        # per-dwelling reference belongs to the aggregation step.
        attrs["residential_units"] = units
        # Selects this region's own occupants-per-dwelling rows from
        # data/reference/num_persons_by_building_type.csv where the run
        # names a region; None falls back to the country-wide figures.
        attrs["region_code"] = _WORKER_REGION_CODE

        merged = AttributeBuilder(payload_attrs=attrs).build()
        cfg = CfgBuilding(merged).to_cfg_dict()
        model = ModelBUEM(cfg)
        model.sim_model(use_milp=use_milp)

        row["heating_kWh"] = round(float(pd.Series(model.heating_load).sum()), 2)
        row["cooling_kWh"] = round(float(pd.Series(model.cooling_load).abs().sum()), 2)
        row["elec_kWh"] = round(float(merged["elecLoad"].sum()), 2)
        row["dhw_kWh"] = round(float(model.dhw_kWh.sum()), 2) if model.dhw_kWh is not None else 0.0
        row["cooking_gas_kWh"] = (
            round(float(model.cooking_gas_kWh.sum()), 2) if model.cooking_gas_kWh is not None else 0.0
        )
        row["status"] = "ok"

    except _PER_BUILDING_ERRORS as exc:
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"

    return row


@dataclass
class BatchConfig:
    """Configuration for one ``run_batch()`` call."""

    source_kind: str = "excel"
    workbook_path: str | Path = DEFAULT_WORKBOOK
    data_dir: str | Path | None = None
    u_value_overrides_path: str | Path | None = None
    country: str = "DE"
    # Statistical region whose own occupants-per-dwelling figures apply
    # (a CBS "RegioS" code such as "GM0200" for a Dutch municipality).
    # None uses the country-wide rows -- see
    # buem.config.reference_values.resolve_num_persons.
    region_code: str | None = None
    # Ignored when the source carries real geometry: a CSV region derives
    # its own centre so a Netherlands run doesn't fetch German weather.
    latitude: float = DEFAULT_LATITUDE
    longitude: float = DEFAULT_LONGITUDE
    year: int = DEFAULT_YEAR
    provider: str = DEFAULT_WEATHER_PROVIDER
    limit: int | None = None
    workers: int | None = None
    use_milp: bool = False
    flush_every: int = 200
    building_ids: list[int] | None = None
    residential_only: bool = False
    labeled_only: bool = False
    resume: bool = False

    @property
    def source_path(self) -> str | Path:
        """Whichever of ``data_dir``/``workbook_path`` this source uses."""
        if self.source_kind == "csv":
            if self.data_dir is None:
                raise ValueError("source_kind='csv' requires data_dir.")
            return self.data_dir
        return self.workbook_path


def _load_region_table(config: BatchConfig, filename: str, purpose: str) -> pd.DataFrame | None:
    """Load an optional per-region reference table from the source directory.

    Absent is normal, not an error: a region without the table simply
    keeps whatever behaviour applies when it is missing (unmodelled
    service buildings, uncorrected refurbishment measures).
    """
    if config.source_kind != "csv":
        return None
    path = Path(config.source_path) / filename
    if not path.exists():
        logger.info("No %s at %s -- %s.", filename, path, purpose)
        return None
    logger.info("Loaded %s from %s", filename, path)
    return pd.read_csv(path)


def _load_u_value_overrides(config: BatchConfig) -> pd.DataFrame | None:
    """Load the human-editable as-built envelope table, if there is one.

    Delegates the precedence -- explicit path, then a region-local table,
    then the packaged national one -- to
    :func:`buem.config.reference_values.resolve_envelope_reference`, so a
    batch run and a validation run of the same region resolve the same
    table rather than silently diverging.
    """
    return resolve_envelope_reference(
        config.u_value_overrides_path,
        config.source_path if config.source_kind == "csv" else None,
        country=config.country,
    )


def _read_completed_rows(output_path: Path) -> list[dict[str, Any]]:
    """Rows already present in an existing output file, for a resumed run.

    ``ParquetWriter`` truncates whatever it opens, so resuming means
    reading the finished rows back and re-emitting them ahead of the new
    ones rather than appending in place. At whole-community scale this is
    a few thousand small rows -- negligible next to one building's solve.
    An unreadable or absent file simply means nothing has been done yet.
    """
    if not output_path.exists():
        return []
    try:
        done = pd.read_parquet(output_path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s for resume (%s) -- starting from scratch.", output_path, exc)
        return []
    # Re-shape onto the current column set: an older file may predate a
    # column, and a stale extra column must not reach the writer's schema.
    rows: list[dict[str, Any]] = []
    for record in done.to_dict(orient="records"):
        row: dict[str, Any] = {col: None for col in _RESULT_COLUMNS}
        for col in _RESULT_COLUMNS:
            value = record.get(col)
            if value is not None and not (isinstance(value, float) and pd.isna(value)):
                row[col] = value
        rows.append(row)
    return rows


def _select_building_ids(source, config: BatchConfig) -> list[int]:
    """Resolve the id list for one run.

    ``config.building_ids`` wins outright (a caller-filtered subset, or a
    resume list with completed ids already removed). Otherwise the source's
    own ids are taken, narrowed by ``residential_only``/``labeled_only``
    where those columns exist -- both are Netherlands-pipeline columns, so
    requesting a filter a source cannot honour raises rather than silently
    running the unfiltered population.
    """
    if config.building_ids is not None:
        logger.info("Batch: %d caller-supplied building id(s)", len(config.building_ids))
        return list(config.building_ids)

    bdf = source.buildings
    for flag, column in (("residential_only", "is_residential"), ("labeled_only", "matched_via_label")):
        if not getattr(config, flag):
            continue
        if column not in bdf.columns:
            raise ValueError(
                f"--{flag.replace('_', '-')} needs a {column!r} column, which this source has not got. "
                "Run nl_archetype_mapper.map_buildings() on this data_dir first."
            )
        bdf = bdf[bdf[column] == True]  # noqa: E712 -- pandas mask, not an identity test

    building_ids = bdf["building_feature_id"].tolist()
    if config.limit is not None:
        building_ids = building_ids[: config.limit]
    logger.info("Batch: %d building id(s) from %s", len(building_ids), config.source_path)
    return [int(bid) for bid in building_ids]


def _batch_weather(source, config: BatchConfig) -> pd.DataFrame:
    """Fetch the one weather series the whole batch shares.

    Placed at the region's own mean centroid when the source carries real
    geometry, falling back to the configured latitude/longitude otherwise
    (the German Excel source has no centroid for most rows).
    """
    from buem.analysis.weather_providers import extract_provider_weather
    from buem.buildings.mapping.geometry_utils import region_center_lat_lon

    lat, lon = config.latitude, config.longitude
    try:
        lat, lon = region_center_lat_lon(source.buildings)
        logger.info("Region center derived from real geometry: (%.4f, %.4f)", lat, lon)
    except ValueError:
        logger.info("No real geometry in this source -- using configured (%.4f, %.4f).", lat, lon)

    logger.info(
        "Fetching %s weather for (%.4f, %.4f), %d -- shared across the whole batch.",
        config.provider, lat, lon, config.year,
    )
    return extract_provider_weather(lat, lon, config.year, providers=(config.provider,))[config.provider]


def run_batch(config: BatchConfig, output_path: str | Path) -> Path:
    """Run every (or the first ``config.limit``) building id in the
    configured source through a single-provider simulation, writing one row
    per building to *output_path* (Parquet) incrementally as results
    complete -- so a long run's progress survives even if it's interrupted
    partway through.

    Returns
    -------
    Path
        *output_path*, resolved.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source = build_source(config.source_kind, config.source_path)
    u_value_overrides = _load_u_value_overrides(config)
    service_reference = _load_region_table(
        config, "service_building_reference.csv",
        "non-residential buildings will be skipped",
    )
    measure_overrides = _load_region_table(
        config, "refurbishment_measure_reference.csv",
        "TABULA's own published measure performance will be used unchanged",
    )
    weather_df = _batch_weather(source, config)
    building_ids = _select_building_ids(source, config)

    carried_rows: list[dict[str, Any]] = []
    if config.resume:
        carried_rows = _read_completed_rows(output_path)
        already_done = {int(r["building_feature_id"]) for r in carried_rows if r["building_feature_id"] is not None}
        if already_done:
            building_ids = [bid for bid in building_ids if bid not in already_done]
            logger.info(
                "Resuming: %d building(s) already in %s, %d left to run.",
                len(already_done), output_path, len(building_ids),
            )
    total = len(building_ids)

    workers = config.workers
    schema = pa.schema([
        ("building_feature_id", pa.int64()),
        ("status", pa.string()),
        ("building_type", pa.string()),
        ("construction_period", pa.string()),
        ("A_ref", pa.float64()),
        ("n_walls", pa.int64()),
        ("n_exposed", pa.int64()),
        ("heating_kWh", pa.float64()),
        ("cooling_kWh", pa.float64()),
        ("elec_kWh", pa.float64()),
        ("dhw_kWh", pa.float64()),
        ("cooking_gas_kWh", pa.float64()),
        ("neighbour_status", pa.string()),
        ("construction_year_class", pa.string()),
        ("matched_via_label", pa.string()),
        ("refurbishment_variant", pa.string()),
        ("residential_units", pa.float64()),
        ("residential_units_source", pa.string()),
        ("error", pa.string()),
    ])

    start = time.time()
    n_ok = n_skipped = n_error = 0
    pending_rows: list[dict[str, Any]] = list(carried_rows)

    def _flush(writer: pq.ParquetWriter) -> None:
        nonlocal pending_rows
        if not pending_rows:
            return
        table = pa.Table.from_pylist(pending_rows, schema=schema)
        writer.write_table(table)
        pending_rows = []

    with pq.ParquetWriter(str(output_path), schema) as writer, ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(
            config.source_kind, str(config.source_path), config.country,
            weather_df, u_value_overrides, service_reference, measure_overrides,
            config.region_code,
        ),
    ) as executor:
        # The writer truncates whatever it opened, so the rows carried over
        # from a previous run must land before anything else -- including
        # when nothing is left to run and the loop below never executes.
        _flush(writer)
        future_to_id = {
            executor.submit(_process_one_building, bid, config.use_milp): bid
            for bid in building_ids
        }
        for completed, future in enumerate(as_completed(future_to_id), start=1):
            bid = future_to_id[future]
            try:
                row = future.result()
            except _PER_BUILDING_ERRORS as exc:
                row = {col: None for col in _RESULT_COLUMNS}
                row["building_feature_id"] = bid
                row["status"] = "error"
                row["error"] = f"worker raised {type(exc).__name__}: {exc}"

            pending_rows.append(row)
            if row["status"] == "ok":
                n_ok += 1
            elif row["status"] == "skipped":
                n_skipped += 1
            else:
                n_error += 1

            if completed % max(1, config.flush_every) == 0 or completed == total:
                _flush(writer)
                elapsed = time.time() - start
                logger.info(
                    "%d/%d done (ok=%d skipped=%d error=%d) -- %.1fs elapsed, %.2f buildings/s",
                    completed, total, n_ok, n_skipped, n_error, elapsed,
                    completed / elapsed if elapsed > 0 else 0.0,
                )

    logger.info(
        "Batch complete: %d ok, %d skipped, %d error -- wrote %s",
        n_ok, n_skipped, n_error, output_path,
    )
    return output_path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run buem's thermal model across many buildings for one weather provider."
    )
    parser.add_argument("--source", type=str, default="excel", choices=["excel", "csv"], help="Building source: the bundled TABULA workbook, or a CsvBuildingSource region directory.")
    parser.add_argument("--workbook", type=str, default=str(DEFAULT_WORKBOOK), help="TABULA/city2tabula workbook path (--source excel).")
    parser.add_argument("--data-dir", type=str, default=None, help="CsvBuildingSource directory, e.g. src/buem/data/buildings/netherlands/Loenen (--source csv).")
    parser.add_argument("--u-value-overrides", type=str, default=None, help="U-value override table (default: u_value_reference.csv inside --data-dir).")
    parser.add_argument("--residential-only", action="store_true", help="Only buildings flagged is_residential.")
    parser.add_argument("--labeled-only", action="store_true", help="Only buildings with a real energy label (matched_via_label).")
    parser.add_argument("--resume", action="store_true", help="Skip building ids already present in --output and keep their rows.")
    parser.add_argument("--country", type=str, default="DE", help="TABULA country code for archetype matching.")
    parser.add_argument("--region-code", type=str, default=None,
                        help="Statistical region whose occupants-per-dwelling figures apply, "
                             "e.g. 'GM0200' (Apeldoorn). Omit to use the country-wide figures.")
    parser.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--provider", type=str, default=DEFAULT_WEATHER_PROVIDER, choices=["merra-2", "cosmo-rea6", "era5-land"])
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N building ids (omit for all). Ignored if --building-ids is set.")
    parser.add_argument("--building-ids", type=str, default=None, help="Comma-separated explicit building_feature_id list, e.g. '52203,44100'. Overrides --limit.")
    parser.add_argument("--workers", type=int, default=None, help="Worker process count (default: auto).")
    parser.add_argument("--use-milp", action="store_true", help="Pass use_milp=True to ModelBUEM.sim_model().")
    parser.add_argument("--flush-every", type=int, default=200, help="Write to Parquet every N completed buildings.")
    parser.add_argument("--output", type=str, default="batch_results.parquet")
    return parser


def main(argv: list[str] | None = None) -> Path:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    args = _build_arg_parser().parse_args(argv)
    config = BatchConfig(
        source_kind=args.source,
        workbook_path=args.workbook,
        data_dir=args.data_dir,
        u_value_overrides_path=args.u_value_overrides,
        country=args.country,
        region_code=args.region_code,
        latitude=args.latitude,
        longitude=args.longitude,
        year=args.year,
        provider=args.provider,
        limit=args.limit,
        building_ids=[int(x) for x in args.building_ids.split(",")] if args.building_ids else None,
        workers=args.workers,
        use_milp=args.use_milp,
        flush_every=args.flush_every,
        residential_only=args.residential_only,
        labeled_only=args.labeled_only,
        resume=args.resume,
    )
    return run_batch(config, args.output)


if __name__ == "__main__":
    main()


__all__ = ["BatchConfig", "build_source", "main", "run_batch"]
