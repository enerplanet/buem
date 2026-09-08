# Changelog

All notable changes to BuEM are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `cooking_carrier`/`include_dhw` were not forwarded from a request at
  all -- `geojson_validator.py`'s `building.*` metadata whitelist never
  included them, so `AttributeBuilder` always saw the `"electric"`
  default regardless of what a caller sent, and `kitchen` (added in
  6.1.0) could never read anything but 0 through `/api/process`. Now
  forwarded from `building.cooking_carrier`/`building.include_dhw`,
  alongside the other occupancy-generation inputs (`num_persons`,
  `archetype`, `residential_units`).

## [6.1.0] - 2026-09-08

Merges upstream's [6.0.0](#600---2026-09-01) (real per-type occupancy,
occupancy-driven cooking, per-fixture DHW, glazing classes) on top of
this fork's own buem-gateway contract-pinning work below, plus the
`b_transmission` fix and `hot_water`/`kitchen` response fields new in
this release.

**Known gap, not fixed here**: `cooking_carrier` (upstream's new field
governing whether `kitchen` reports gas energy at all -- defaults
`"electric"`, in which case `kitchen` is legitimately zero) has no path
through the pinned v5 request schema/`geojson_validator.py` conversion.
Every request through `/api/process` today gets the `"electric"`
default, so `kitchen` reads 0 until a schema field for it exists.

### Fixed

- Per-element `b_transmission` was silently dropped whenever a
  component's `U` was uniform across its elements (the normal case for
  ignis-derived payloads, which give one `U` per surface type).
  `geojson_validator.py` promoted `U` to the component level in that
  case but never did the same for `b_transmission`, and
  `model_buem.py`'s conductance calc only reads `b_transmission` from
  the component once a component-level `U` exists -- so a non-default
  value (TABULA's documented 0.5 for ground-contact floors) was treated
  as 1.0, overestimating that component's heat loss by up to 2x. Now
  promoted alongside `U`, same uniformity check.

### Added

- `hot_water`/`kitchen` in `thermal_load_profile.summary` and
  `.timeseries` -- `model_buem.py` already computed `dhw_kWh`/
  `cooking_gas_kWh` every run but nothing surfaced them in the
  response. `hot_water` is fuel-agnostic heat demand, folded into
  `total_energy_demand` like heating/cooling; `kitchen` is
  gas-carrier cooking energy on its own fuel channel (`kWh_gas`/
  `kW_gas` units), excluded from `total_energy_demand`. Matches
  buem-gateway's `v6-draft` field names.

### Changed

- **The BUEM-EnerPlanET API contract is now a pinned copy of
  `enerplanet/buem-gateway`'s `schemas/v5/` (tag `v6.0.0`), not a
  locally-maintained version tree.** buem previously kept its own copy of
  the contract (`json_schema/versions/v1`..`v4`) in step with
  buem-gateway's by hand; the two drifted (disagreeing on the version
  number, and on whether `weather` was required at all). buem-gateway is
  now the single source of truth. See `json_schema/README.md` for the
  pinning/re-sync procedure and CI's new "Contract drift check" step.
- `geojson_validator.py`'s request validation is now `jsonschema`
  structural validation against the pinned schema, replacing the
  hand-written `marshmallow` schema classes that duplicated (and
  sometimes silently diverged from) the contract's own shape. Its request
  -> internal-format conversion logic is unchanged.
- Consequences of validating against the real contract instead of buem's
  own copy:
  - `building_type` is no longer enum-checked at request validation (the
    pinned contract leaves it free text). An unrecognised value now
    surfaces later, as a clear error from `AttributeBuilder` when it
    tries to resolve an occupancy profile from it, instead of failing at
    the request boundary.
  - `weather.year`'s per-provider range check is removed -- the pinned
    contract treats `year`/`provider` as informational metadata on an
    inline timeseries, not a selector to validate against archive
    coverage.
  - `weather.index`/`weather.variables` (a pre-resolved, caller-supplied
    hourly timeseries) are now **required** on every request, enforced by
    the schema itself. A request with no inline weather now fails
    validation immediately with a clear message, instead of reaching
    `AttributeBuilder.generate_weather_profile()` and failing there (or,
    with `BUEM_WEATHER_FALLBACK` unset, silently triggering buem's own
    per-location fetch). `weather.provider`/`weather.year` alone, with no
    inline data, is no longer accepted -- matches buem-gateway's own
    contract, which "calls no weather service and does not accept a
    provider/year selector in place of the data".
  - The legacy v2 request shape (`buem.building_attributes` /
    `buem.child_components`) is no longer reachable: the pinned schema
    requires `buem.building`, so a v2-shaped request fails validation
    before ever reaching the (now-removed) v2 conversion path.

### Removed

- `src/buem/integration/json_schema/versions/` (`v1`..`v4`),
  `VERSIONING.md`, `SCHEMA_OVERVIEW.md` -- superseded by the pinned copy
  above.
- `schema_cli.py import-version` -- there is no version tree to import
  into any more; re-sync the pinned copy per `json_schema/README.md`
  instead.

### Found, not fixed here

- **`solver.compute_cooling` is defined by the pinned contract but not
  implemented by this deployment.** The contract gives it real semantics
  (cooling constraint omitted by default; enforced, and returned, only
  when `true`). `main.py::run_model` accepts a `compute_cooling` argument
  but `ModelBUEM` never reads `cfg["compute_cooling"]`, and
  `geojson_processor.py` never passes the request's value through --
  heating and cooling are always computed and returned regardless.
  `geojson_validator.py` still rejects `compute_cooling: true` rather
  than silently returning a response that doesn't match what was
  requested, but the rejection message now says why: a real
  contract-implementation gap, not an unwired draft field. Implementing
  the real toggle is model work, out of scope for this contract-plumbing
  change.

### Fixed

- Caller-supplied inline `buem.weather` ({index, variables}) is parsed and
  used again -- upstream's v5 restructuring moved weather resolution into
  `AttributeBuilder.generate_weather_profile()`, which only recognised a
  `buem.weather.profile` file path, silently discarding inline data and
  falling through to buem's own per-location fetch instead.
  `GeoJsonValidator._weather_from_payload` restores the parsing;
  `use_provided_weather` is set the same way the file-path branch already did.
- `BUEM_WEATHER_FALLBACK=false` now also skips the module-import weather
  fetch in `cfg_attribute.py`, not just `generate_weather_profile()`'s
  per-request fetch. The import fetch was unconditional, so a deployment
  with no local archive and no `WEATHER_API_URL` could not import buem at
  all (gunicorn workers failed to boot) even though every request carried
  its own weather. With the flag off, the module-level default weather is
  `None` and the occupancy profiles use a plain 8760-hour range for their
  index. `BUEM_DEFAULT_WEATHER_PROVIDER` overrides the provider for this
  one fetch when the flag is on.
- `weather_cache._fetch_remote` sends `variables=T,GHI,DHI,DNI`. weather
  >= 2.0.0's `/v1/weather/point` rejects a request naming neither
  `variables` nor `use_case`, so the remote backend returned 400 for
  every call.

### Added

- `BUEM_WEATHER_FALLBACK` (default: unset/true) gates
  `generate_weather_profile()`'s own fetch. Deployments where an
  upstream caller (an Orchestrator) always supplies weather set this to
  `false`, so a missing `buem.weather` fails loudly instead of buem
  silently resolving it via its own real fetch.

### Changed

- **Breaking**: `/api/process` now requires `buem.weather` on every
  feature -- a request with no weather (or a `weather` block missing
  `index`/all of T,GHI,DNI,DHI) fails loudly with a per-feature error
  instead of silently substituting `AttributeBuilder`'s generic default
  weather. A scientific model producing a plausible-looking result from
  the wrong (or missing) input, with no signal that it happened, is worse
  than an explicit failure. See #11.

### Added

- `buem.weather` field in the v4 request schema (`index`/`T`/`GHI`/`DNI`/`DHI`)
  -- caller-supplied hourly weather timeseries, shaped to match
  `weather serve`'s `GET /v1/weather/point?format=json` response, so an
  Orchestrator (or any caller) can hand BuEM pre-resolved weather with no
  BuEM-side parsing beyond `geojson_processor.py::_weather_from_payload()`.
  See #10.

### Changed

- `geojson_processor.py` now uses a request's `buem.weather` block when
  present, instead of resolving weather itself.

### Removed

- **Breaking for any caller relying on it**: `geojson_processor.py`'s
  per-request MERRA-2 lookup (`_load_feature_weather()`,
  `BUEM_WEATHER_DIR`-based, via `MerraWeatherData`) is gone. It was
  fork-local dev scaffolding added before `weather serve` existed, not
  part of upstream BuEM's design -- upstream's `geojson_processor.py`
  already only reads weather from the payload. `AttributeBuilder`'s own
  package-wide default weather (`cfg_attribute.py`, still MERRA-2-backed)
  is unaffected; this only removes the per-request, location-specific
  lookup the payload path now replaces. See #10.

## [6.0.0] - 2026-09-01

Major bump: every simulated figure changes. Household occupancy is no
longer a flat 4 persons, cooking energy comes from occupancy's appliance
model rather than from simulated heating, domestic hot water is priced
per fixture, and glazing is resolved by product class per construction
era. Any previously-published figure should be regenerated.

The headline validation finding is that buem's heating overshoot is
largely a **refurbishment-state data gap, not a model defect**: buildings
modelled with a refurbished envelope reproduce CBS to within 1%, while
those modelled as-built run about 4x, and 46% of the Loenen stock carries
no energy label and is therefore forced to as-built. See
`docs/source/validation/envelope_and_refurbishment.rst`.

### Added

- **Occupancy per building type, sourced from CBS.**
  `buem.analysis.netherlands.cbs_household_size` derives mean occupants
  per dwelling from CBS 85035NED + 86064NED + 85140NED, resolving the
  dwelling-type axis that no single published table provides. Values live
  in the editable `data/reference/num_persons_by_building_type.csv` and
  resolve most-specific-first: `(country, region, type)` ->
  `(country, *, type)` -> `(*, *, type)` -> `DEFAULT_NUM_PERSONS`.
  Regenerate the Dutch rows with `scripts/refresh_nl_num_persons.py`.
- **Fractional household sizes are preserved, not rounded.** A per-type
  figure is a population mean, so `AttributeBuilder` generates the two
  bracketing integer household sizes and blends `Q_ig`/`elecLoad`/
  `dhw_liters`. Rounding would have quantised the whole table onto a
  handful of integers.
- **Glazing reference table** (`data/reference/glazing_reference.csv`):
  single / double_uncoated / HR / HR+ / HR++ / HR+++ with matched U- and
  g-values. An envelope table's `U_Window` may name a class instead of a
  number, keeping conduction and solar transmittance consistent.
- **Era-resolved CBS reference.**
  `buem.analysis.netherlands.cbs_era_reference` queries table 85140NED,
  which resolves construction era as well as dwelling type -- the axis
  buem's own archetypes are keyed on.
- **ISO 13790 section 13 intermittency**, opt-in and off by default.
  `buem.config.setback` builds an hourly `comfortT_lb` from
  `occ_nothome`/`occ_sleeping` and a named profile in
  `data/reference/setback_profiles.csv`. Off by default because buem's
  18-21 degC band already represents observed behaviour.
- **`cooking_carrier`** (`electric`/`gas`/`none`) and **`include_dhw`**
  flags; **`region_code`** attribute, threaded through `batch.py`
  (`--region-code`) and `validation.py`.
- New scripts: `compare_era_type_vs_cbs.py`,
  `validate_household_size.py`, `refresh_nl_num_persons.py`.

### Changed

- **`num_persons` defaults to a real per-type figure** rather than a flat
  4 -- roughly 1.6x too high for houses and 3x for apartments against CBS.
  An explicit caller value still wins.
- **Cooking energy comes from occupancy's own appliance model.** A
  kitchen-only generation gives both timing and magnitude from the
  household's own stochastic draws. Previously it was derived from the
  building's simulated *heating*, which made it inherit any heating
  error. Cooking is now also split into the carrier that pays for it and
  the share that becomes internal gain (`COOKING_HEAT_GAIN_FRACTION`),
  rather than crediting a hob's entire input as room heat.
- **DHW is priced per fixture.** occupancy resolves basin, kitchen-sink,
  shower and bath draws separately and each is delivered at its own
  temperature; buem previously applied one blended delta-T to the total.
- **Envelope reference consolidated** into
  `data/reference/nl_envelope_reference.csv`. The duplicated per-region
  `u_value_reference.csv` copies are removed;
  `resolve_envelope_reference()` still honours a region-local file or an
  explicit path first.
- **Substituted defaults are now named in a warning** rather than applied
  silently (`safe_series_float`, the service-reference reader),
  deduplicated per `(column, default)` per process. Genuine ISO 13790
  conventions are marked as intentional so real gaps stand out.
- Regression guard for building 52203 (merra-2): 41,816.4 -> 42,411.9 kWh.

### Fixed

- Two defects in the Dutch as-built envelope table: pre-1964 terraced
  houses used solid-wall values where the same era's other types used
  uninsulated-cavity ones, and the 1965-1974 floor R_c was *worse* than
  the pre-1964 one.
- `occupancy` upgraded to 6.0.0, whose occupant-scaled appliance use
  lowers modelled heating by about 1%.

## [5.0.0] - 2026-08-21

Major bump: every previously-published Netherlands CBS validation figure
for both Loenen and Heeten is superseded and retracted by this release.
`buem`'s own simulated output and API behaviour are unchanged; what
changed is which buildings the Netherlands residential population
includes, which moves every headline comparison figure substantially.

### Fixed

- **Residential classification counted non-dwelling structures as
  houses.** Cross-checking buem's Loenen/Heeten residential building
  counts against real government housing statistics found both datasets
  1.7–2.2x too large: 3,101 "residential" buildings in Loenen against an
  official 1,424–1,435 residential addresses; 2,671 in Heeten against an
  official 1,568–1,578. Root cause: BAG registers every physical
  structure as its own Pand — garden sheds, garages, farm outbuildings —
  and `nl_building_classifier.classify_all()` had no minimum-registration
  check on the residential path, only on buildings already flagged
  `is_greenhouse_or_warehouse`/`is_glass_roof`. Fixed using a signal
  already available in the pipeline: the RIVM energy-labels GeoPackage's
  `aant_verblijfsobj` field is null (not literally `0`) for a Pand with
  no dwelling unit registered under it at all — a Pand matched in that
  data with a null or zero unit count is now excluded from residential
  classification, the same treatment a flagged greenhouse already got. A
  Pand entirely absent from the RIVM data stays ambiguous, not excluded.
  Confirmed correct, not just plausible: every AB/MFH building has a
  registered unit (100%, always formally registered) vs. only 37–50% of
  "SFH"-classified buildings; buildings under 30 m² footprint have one
  only ~5% of the time. Filtering on this reproduces the real village
  address counts almost exactly: Loenen 1,461 (official 1,424–1,435),
  Heeten 1,570 (official 1,568–1,578).
- **This inflated the previously-published validation figures in the
  wrong direction.** A garden shed has near-zero absolute heating demand,
  so it also gets a near-zero buem/CBS ratio; diluted into a population
  of thousands, those non-dwellings were pulling the reported median
  *down*, not up. Loenen's median buem/CBS ratio moves from the
  previously-published 0.98 ("essentially matches CBS") to **2.19** on
  the real housing stock; Heeten moves from 1.77 to **2.42**. The
  earlier fixes in this validation effort (window U-value correction,
  TABULA refurbishment variants, comfort-setpoint correction, DHW/cooking
  modelling, dwelling-count repair) are all still real and still
  applied — they were being measured against the wrong population.
  One consequence: Loenen and Heeten looked structurally different
  before this fix (issue #14's ~80% relative gap, explained at the time
  by a real Heeten house-size difference, which still holds). After the
  identical fix on both regions, they converge to within ~10% of each
  other — most of the apparent Heeten anomaly was the same contamination
  problem, present to a different degree in each region's raw data.
  `docs/source/validation/{loenen,heeten}_cbs.rst` rewritten with the
  correction kept in full alongside the new figures. (#15)

### Added

- **`intensity_kwh_m2`** on `buem.analysis.netherlands.validation
  .per_building_ratios()`/`stratified_ratio_table()` — the same
  numerator divided by a building's real floor area rather than its
  dwelling count, with no CBS dependency. This is what surfaced Heeten's
  real house-size difference from Loenen, and is the fairer way to
  compare two regions/eras directly against each other.

## [4.2.0] - 2026-08-20

### Fixed

- **Heeten's RIVM data gap** (#13). Heeten's building data was missing
  real dwelling counts and refurbishment-variant selection entirely —
  both need the raw RIVM energy-labels GeoPackage, not found on this
  machine when the Heeten validation was first published. Located since
  (the same nationwide export Loenen's own data was built from). New
  `scripts/reclassify_with_rivm_labels.py` re-runs the existing, already-
  tested `nl_archetype_mapper.map_buildings()` against it. Effect on
  Heeten: refurbishment variant went from 100% as-built to a real
  2,360/262/53 split; dwelling counts went from 0 real RIVM-sourced to
  2,322. Median buem/CBS ratio improved 2.03 → 1.81 (total) / 2.07 → 1.77
  (heating-only); still meaningfully above Loenen's contemporaneous
  0.98/1.79 at the time, tracked as issue #14 (later superseded — see
  5.0.0). `RIVM_ENERGY_LABELS_GPKG` added to `.env.example`.

## [4.1.0] - 2026-08-20

### Added

- **Service (non-residential) buildings can be simulated** (#7). They were
  classified and routed to `occupancy.ServiceBuildingProfile` correctly,
  but never reached it: `LOD2Mapper` resolves a building's thermal
  description from its matched TABULA row, TABULA is a *residential*
  typology, and so every service building returned `None` and was
  skipped. Both paths now produce a
  `buem.buildings.mapping.archetype_spec.ArchetypeSpec`, so geometry,
  opening synthesis and element assembly are identical regardless of
  where the description came from. Non-residential buildings resolve
  through the new `service_building_reference.csv` — one row per
  (service type, construction era), covering all 8 of occupancy's
  registered types. Envelope U-values follow the same Bouwbesluit
  year-class series already cross-checked for the residential path
  (its thermal requirements are not residential-specific); the use
  parameters (room height, ventilation, infiltration, heating-reduction)
  differ by category and are first-pass engineering values, flagged as
  such. Loenen's two warehouses now simulate at ~273 kWh/m², comparable
  to the residential NL.01 mean of 266.
- **Refurbishment measures whose published performance has aged can be
  corrected** (#5). TABULA states each measure as it stood when the
  typology was compiled: NL's standard window measure
  `NL.Window.Ins.01` assumes R = 0.556 (U = 1.80, plain HR glazing),
  while Dutch stock refurbished to that same label tier today typically
  has HR++ at 1.1–1.2. `refurbishment_measure_reference.csv` corrects
  the measure itself rather than patching each affected archetype, so it
  applies everywhere that measure is used. Verified on a real variant-2
  building: `U_window` 1.800 → 1.149, with wall, roof and door
  untouched. This compounds with the 50%-of-wall-area glazing, since
  excess window conductance scales with both the U-value error and the
  glazed area.
- **Dwelling counts that cannot be right are repaired from floor area**
  (#6). `nl_archetype_mapper.repair_dwelling_counts()`, applied via
  `scripts/repair_nl_dwelling_counts.py`. A single BAG *Pand* can
  legitimately be a whole terrace or block housing many households, but
  RIVM sometimes registers only part of its sub-units — 169 of 3,105
  Loenen buildings (5.4%) implied over 500 m² per dwelling, the worst at
  42,204 m². Three columns are written so a derived value can never be
  mistaken for registered data: `residential_units_recorded` (preserved),
  `residential_units_source` (`rivm` / `floor_area_estimate`), and the
  repaired `residential_units`. Two guards keep it conservative: it never
  reduces a registered count, does not act where the implied dwelling
  size is already plausible, and skips non-residential buildings entirely
  — a warehouse has no dwellings, and giving it a derived count scales
  occupancy's service-building profile by a household multiplier that
  does not exist (caught this way: a 2,125 m² warehouse's electricity
  went up 18×). Re-running recomputes from the registered value rather
  than compounding an earlier repair. Result: 167 repaired, 0 implausible
  remaining.
- **Heeten's first population-complete buem-vs-CBS validation**, and a new
  reusable per-building-ratio analysis to produce it. `buem.analysis
  .netherlands.validation` gains `per_building_ratios()` (one row per
  building carrying its own buem/CBS ratio, rather than one row per
  group — needed for a population median or a breakdown below group
  level, since CBS's 81528NED table has no construction-year dimension
  to compare against directly) and `stratified_ratio_table()` (median/
  mean/n by `(building_type, construction_year_class)`, built on top of
  it), plus `service_building_intensity_table()` for non-residential
  buildings, which CBS has no reference for at all. `per_building_ratios`
  takes an explicit `metric` (`"total"` or `"heating_only"`) since the
  two are not interchangeable and give visibly different numbers on the
  same data — `"heating_only"` reproduces the previously-published
  per-building median (Loenen: 0.98), `"total"` matches the count-weighted
  headline (Loenen: 1.78/1.79).
  Both Loenen (3,101 residential + 2 service) and Heeten (2,671
  residential + 3 service) were run through the full whole-population
  pipeline for this. Loenen's figures reproduce the already-published
  ones (count-weighted 1.79 vs. published 1.78, median 0.98 vs. published
  0.98) — a real cross-check that this new code path is correct, not
  just internally consistent. Heeten is a first, and materially higher:
  median 2.03–2.07 vs. Loenen's 0.98–1.18, because Heeten is missing two
  of the three corrections Loenen has — see "Fixed" below. Both regions'
  service buildings simulate: Loenen 286 kWh/m² (2 warehouses), Heeten
  289 kWh/m² (3 warehouses), consistent with each other and with Loenen's
  previously-published figure.
  Live-verified (not assumed from prior notes) that the
  `occupancy.ServiceBuildingProfile` connection this all depends on is
  still working against the currently-installed package (v5.0.0):
  `test_dummy_fixture_runs_end_to_end[...office...]` and
  `test_bundled_reference_tables_cover_every_occupancy_service_type` both
  exercise it directly and pass, and both batch runs above simulated real
  warehouses through the same path.

### Fixed

- **Heeten's dwelling counts are now populated** (previously absent
  entirely, not merely sometimes wrong as Loenen's were) via the same
  `repair_dwelling_counts()` fallback Loenen's own repair uses when no
  registered count exists: 349 of 2,671 buildings derived from floor
  area, the remaining 2,322 default to 1 (a real dwelling for the vast
  majority of Dutch SFH/TH stock, matching what RIVM's own data showed
  for Loenen's equivalent buildings). This is a real, warranted
  correction, not a full fix — see the known limitation below.
- The three small country-level reference tables (`u_value_reference
  .csv`, `service_building_reference.csv`,
  `refurbishment_measure_reference.csv`) are copied into
  `netherlands/Heeten/` alongside `tabula.csv`, so its batch run gets the
  same window-U-value correction and service-building capability
  Loenen's has, rather than silently falling back to TABULA's
  uncorrected values and skipping every service building (both are
  `_load_region_table`'s documented graceful-degrade behavior when a
  region directory doesn't carry its own copy).

### Known limitation

- **Heeten's refurbishment-variant selection is not real.** 638 of 2,671
  buildings (23.9%, matching Loenen's own real-label coverage almost
  exactly) have a matched RIVM energy label, but none of them select a
  refurbished TABULA envelope variant from it — every Heeten building
  simulates as-built. The label *class* needed to pick standard-vs-nZEB
  (`nl_archetype_mapper.label_to_refurbishment_variant`) was never
  persisted for Heeten's data and cannot be recovered without the raw
  RIVM energy-labels GeoPackage, which is not present on this machine (a
  Heeten CityJSON geometry export exists locally at
  `D:\test\envelope-extractor\data\envelope\heeten.city.json`, confirmed
  by inspection to carry only 3D BAG geometry — roof/wall/volume/
  construction-year attributes — no RIVM label or dwelling-count fields,
  so it does not close this gap). Loenen's own history is the best
  estimate of the size of this effect: migrating just refurbishment-
  variant selection (before the window-U or dwelling-count fixes existed)
  moved its label-matched subset's mean ratio from 4.96 to 4.22 — Heeten
  is still missing this migration entirely, on top of the other two
  fixes above. Tracked in a new issue rather than fixed here, since
  fixing it needs a real data source this session does not have.

### Changed

- **Netherlands region data consolidated under one parent directory.**
  `src/buem/data/buildings/netherlands/` (Loenen) and the previously
  separate `src/buem/data/buildings/netherlands_heeten/` are now
  `src/buem/data/buildings/netherlands/Loenen/` and
  `.../netherlands/Heeten/` respectively — regions stay separate
  directories (per-municipality CBS benchmarks don't merge either way),
  just nested under a common parent. All `--data-dir` defaults
  (`buem.analysis.batch`, `buem.analysis.netherlands.validation`,
  `scripts/run_region_batch.sh`, `scripts/repair_nl_dwelling_counts.py`,
  `scripts/refresh_nl_archetype_variants.py`) and docs/tests referencing
  the old paths are updated. The three small editable reference tables
  (`u_value_reference.csv`, `service_building_reference.csv`,
  `refurbishment_measure_reference.csv`) move with Loenen's data — they
  were built and validated against Loenen and have not been copied to
  Heeten.
- **`tests/` no longer holds non-test scripts.** Several files matched
  pytest's `test_*.py` collection glob by name only — no `test_`
  functions, driven instead by a `main()`/`if __name__` entry point
  (manual smoke tests, a worker-count benchmark, a debug harness, one
  script that posts to a local dev server). Moved to `scripts/`:
  `run_test.py` → `smoke_test_model.py`, `test_energy.py` →
  `smoke_test_energy.py`, `test_scaling.py` → `benchmark_worker_scaling.py`,
  `test_worker_debug.py` → `debug_worker_pool.py`, `test_postgeojson.py`
  → `manual_api_smoke_test.py` (also fixed a hardcoded, machine-specific
  absolute path it wrote its output to). `pyproject.toml`'s
  `--ignore=tests/run_test.py` addopt is no longer needed and is removed.
  `tests/test_wget.py` (a standalone COSMO-REA6 GRIB downloader with no
  `buem` imports and no test functions, superseded once weather became a
  compulsory fetch through the `weather` package) is deleted outright.
  `tests/test_geojson_integration.py` keeps its real pytest coverage but
  drops ~140 lines of dead CLI-runner code (`main()`, `argparse`,
  `run_all_tests()`/`print_summary()`) that pytest never executed.

## [4.0.0] - 2026-08-19

Major bump: the flat `building_attributes` request format is no longer
accepted (v4-only), and the comfort-band default change moves every
simulated result by roughly 17–18%. Both are breaking for existing
clients and for anything comparing against previous output.

### Fixed

- **`setup.ps1` could not run at all on Windows PowerShell 5.1** — every
  command failed with a cascade of parse errors. The file was UTF-8
  without a BOM and contained 388 box-drawing characters; PowerShell 5.1
  reads a `.ps1` as cp1252 unless it carries a BOM, and `U+2500`'s
  encoding contains byte `0x94`, which cp1252 maps to a closing quotation
  mark — terminating a string early and breaking the parse. Both
  `setup.ps1` and `setup.bat` are now pure ASCII, which fixes them under
  any codepage rather than only the ones that happen to agree. Verified:
  `help`, `version` and `validate` all work through both scripts.
- **ReadTheDocs builds.** `.readthedocs.yaml` installed the package
  (`pip install .[docs]`), which fails twice over: pip refuses on the
  configured Python 3.13 against `requires-python = ">=3.14"`, and even
  past that, importing `buem` triggers `weather`'s real archive fetch,
  which no docs builder has. The documentation is hand-written prose with
  **zero** autodoc directives, so the package was never needed; RTD now
  installs `docs/requirements.txt` only, and `conf.py` resolves the
  version from git tags via setuptools-scm when `buem` is absent. This
  was a configuration problem, not a Sphinx one — MkDocs would have hit
  the identical wall.
- **`readme.md` told users `weather` and `occupancy` were optional
  extras** (`pip install buem[occupancy,weather]`). Both have been
  compulsory entries in the main `dependencies` list since 2026-08-03 and
  2026-08-07; there are no such extras to install. Also added the
  `WEATHER_DATA_DIR` step that Quick Start omitted — without it `buem`
  cannot import at all.

### Added

- **Whole-region batch runs.** `buem.analysis.batch` gained a pluggable
  building source: `--source csv --data-dir <region>` runs a
  `CsvBuildingSource` region (e.g. the 3,101 residential Loenen
  buildings) through the existing `ProcessPoolExecutor` pipeline, where
  it previously only read the German TABULA workbook. The German path is
  unchanged and remains the default. Also new on that runner:
  `--residential-only`/`--labeled-only` (filtering on the columns the
  Netherlands pipeline produces, raising rather than silently running the
  unfiltered population when a source cannot honour the filter),
  `--u-value-overrides` (defaulting to `u_value_reference.csv` inside
  `--data-dir`, so a batch run and a validation run of the same region
  apply identical U-values), and `--resume`, which skips building ids
  already present in the output and carries their rows forward.
  Per-building rows now also carry `dhw_kWh`, `cooking_gas_kWh`,
  `residential_units`, and the `neighbour_status`/`construction_year_class`/
  `matched_via_label`/`refurbishment_variant` grouping columns.
  Measured throughput: ~1.8 buildings/s on 16 workers, about half an hour
  for all of Loenen.
- **`validation --from-parquet`** aggregates a completed batch run against
  CBS without simulating anything
  (`buem.analysis.netherlands.validation.aggregate_parquet`). This is the
  population-complete counterpart to the existing sampled path, which
  takes the first N buildings in file order — an order that is not random
  with respect to construction era, skewing terraced houses' sample to
  80% oldest-class against 21% of the real population and inflating their
  reported intensity by roughly 1.9x. Aggregating a run that covered every
  building removes that bias by construction, on every dimension at once,
  and makes `--labeled-only` a slice of the same simulation rather than a
  separate one. Both paths share the CBS lookup, conversion and reporting
  code.
- **`geometry_utils.region_center_lat_lon()`** — the mean real centroid of
  a set of building rows, used to place a region's one shared weather
  fetch. Extracted from `validation.py`, which now calls it, so the batch
  runner cannot fetch a different location than the validator for the same
  region. Raises when no row carries geometry rather than falling back to
  a module default, which would simulate a region against another
  country's climate.
- **First population-complete buem-vs-CBS comparison for Loenen.** All
  3,101 residential buildings simulated in one pass (0 skipped, 0 errors,
  ~25 min), against a previously-recorded 2.92 from a 3–5-building-per-
  group sample. Excluding 167 buildings whose recorded dwelling count is
  demonstrably wrong, the building-count-weighted ratio is **1.81** and
  the **median building sits at 1.03** — half of Loenen is within a few
  percent of its CBS category, and the residual is a right tail rather
  than a uniform offset. 99 % of the stock (2,341 SFH and 558 TH) falls
  between 1.51× and 2.50×.
  Two things the sampled runs could not show: those 167 buildings (5.4 %,
  worst at 42,204 m² per dwelling, mean ratio 24.0) are a **data**
  problem rather than a modelling one, inflating the all-population
  figure from 1.81 to 3.01; and, once they are removed, **label coverage
  is not a leading explanation** — unlabelled buildings agree slightly
  *better* than labelled ones (1.74 vs. 2.03), reversing the raw-figure
  impression. Full tables and configuration in
  `docs/source/validation/loenen_cbs.rst`.
- **`scripts/run_region_batch.sh`** — Linux wrapper for a whole-region run:
  checks `WEATHER_DATA_DIR`, pins BLAS to one thread per worker (nested
  thread pools oversubscribe the cores), sizes `--workers` to the host,
  and detaches under `nohup` so an SSH session can drop mid-run.
- **`building.window_to_wall_ratio`** (v4 schema, forwarded by the
  validator, exposed as an `AttributeSpec` and threaded through
  `CfgBuilding`): a single caller-supplied number in `[0, 1)` applied
  uniformly to every exposed wall when a request supplies no explicit
  window geometry. An out-of-range value raises rather than falling back
  to the default — silently substituting a different ratio would model a
  building the caller did not describe. `None` resolves to
  `DEFAULT_WINDOW_TO_WALL_RATIO` in exactly one place
  (`uniform_window_ratios()`), so a supplied value and the default cannot
  diverge.
- **`building.residential_units` added to the v4 request schema** (with
  sign-off) and forwarded by `geojson_validator.py`, so a request can
  express how many dwellings a feature represents. `building.num_persons`'
  description clarified in the same edit: it is occupants *per dwelling*,
  not the building's total occupancy.
- **`buem validate` now checks that weather data is actually reachable.**
  It previously verified `BUEM_WEATHER_DIR`/`BUEM_RESULTS_DIR`/
  `BUEM_LOG_DIR` only, so it could report PASS in an environment where
  `buem` cannot import a single building. It now confirms either
  `WEATHER_API_URL` or a `WEATHER_DATA_DIR`/`BUEM_WEATHER_DATA_DIR` that
  exists, and reports which. An unrecognised on-disk archive layout is a
  `[WARN]`, not a failure — layouts vary between providers and `weather`
  versions, so it is a hint rather than proof of breakage.
- **Dwelling-count plausibility reporting** in
  `validation.aggregate_parquet`. CBS publishes consumption *per
  dwelling*, so every comparison divides by `residential_units`; where
  that count is missing or wrong the quotient cannot match any CBS
  category however good the model is. Buildings implying more than
  `IMPLAUSIBLE_M2_PER_DWELLING` (500 m²) per dwelling are now always
  logged, and `--max-m2-per-dwelling` excludes them. Exclusion is
  deliberately not the default, since it changes the headline number, and
  nothing overwrites the recorded count — inventing a plausible one would
  replace a visibly missing value with an invisible guess.
- **A dated validation-results section in the documentation**
  (`docs/source/validation/`), so each run is recorded with the
  configuration that produced it and can be compared against a later
  re-run like for like. `docs/source/modules/netherlands.rst` now points
  there instead of carrying the numbers itself.

### Changed

- **The thermal model no longer prints.** `ModelBUEM`'s per-building
  diagnostics — component configuration, POA irradiance, solar gains, LP
  size, solver and status — went to stdout unconditionally, which at
  whole-community scale is thousands of interleaved lines from concurrent
  workers. All now go to the module logger at DEBUG (the air-change-rate
  and dotenv warnings at WARNING), so a caller decides whether to see
  them.
- **A numpy `RuntimeWarning` now fails the test suite**
  (`filterwarnings = ["error::RuntimeWarning"]`). Such a warning inside a
  solve means a NaN or ±inf reached an aggregation — physically
  meaningless output that still produces a number, so it must fail loudly
  rather than scroll past in the warnings summary. The suite is clean
  under this rule.
- The `slow` pytest marker is registered in `pyproject.toml`, removing the
  `PytestUnknownMarkWarning` it raised on every run.

- **Weather is no longer range-checked inside the thermal model.**
  `ModelBUEM._calcRadiation()` carried two `print()`-based warnings on
  GHI/temperature ranges; these are removed. The model consumes weather
  exactly as supplied and never masks, clips or adjusts it. The
  equivalent check moved to the request boundary as
  `GeoJsonValidator._check_weather_profile_ranges()`, applied to
  caller-supplied `buem.weather.profile` payloads where a client can act
  on the feedback. Reported as warnings (never errors), with wide
  bounds (`WEATHER_PROFILE_PLAUSIBLE_RANGES`) aimed at unit mix-ups and
  corrupt data rather than merely unusual values.

- **Only the current (v4) request format is accepted.** The superseded
  flat `building_attributes` shape — alone, or combined with
  `child_components` — is now rejected at validation with a message
  naming the field to migrate to, instead of being silently converted.
  `BuemSchema.require_v2_or_v3` becomes `require_building_envelope`.
  `building_attributes` remains buem's *internal* representation
  downstream (`_convert_v3_to_v2()` still produces it); only its use as
  an *input* is withdrawn. The two bundled examples were rewritten into
  v4 and consolidated as `src/buem/integration/sample_request.geojson`
  (migration: `scripts/migrate_sample_requests_to_v4.py`) — a
  version-suffixed filename no longer conveys anything now that one
  format is supported. `json_schema/versions/v1,v2,v3/` are retained
  on disk as reference; nothing loads them.
- **Window geometry no longer depends on TABULA's per-direction window
  columns.** Windows are sized as a fraction of each exposed wall's own
  area (`building_registry.DEFAULT_WINDOW_TO_WALL_RATIO`, applied via
  the new `element_factory.uniform_window_ratios()`), inheriting that
  wall's real azimuth and tilt. TABULA's Dutch typology places its
  entire reference window area on East/West with North and South at
  exactly zero — an abstract front/back-facade convention, not a claim
  about orientation — so reading it by real compass direction gave
  genuinely south-facing walls no glazing at all and collapsed
  whole-building window-to-wall ratio to a few percent against a ~49%
  intent. Sizing from wall area removes that mismatch, needs no
  assumption about how a reference building was oriented, and unifies
  the archetype-matched and fallback paths, which previously used
  different rules (`FALLBACK_WINDOW_RATIO_PER_DIRECTION` is retired).
  Doors still use TABULA's door-to-wall ratio, which carries no
  orientation assumption.

- **Default indoor comfort dead-band is now 18–21 °C** (was 21–24 °C).
  New `building_registry.DEFAULT_COMFORT_T_LB`/`DEFAULT_COMFORT_T_UB` are
  the single source of truth, consumed by `ThermalProperties`' dataclass
  defaults and the matching `AttributeSpec`s. These represent *observed
  occupant behavior* rather than a standardized calculation setpoint:
  real households heat to a lower average indoor temperature than
  reference calculations assume, a well-documented cause of calculated
  demand exceeding metered consumption. `LOD2Mapper` accordingly no
  longer overrides `comfortT_lb` from the matched TABULA archetype's
  `theta_i` — that value is TABULA's own reference-calculation setpoint
  and is a constant 20 °C across every Dutch archetype, carrying no
  per-building information. Pass explicit `comfortT_lb`/`comfortT_ub`
  (scalars or per-timestep Series) to model a specific building's
  setpoints or a real setback schedule. **This lowers simulated heating
  demand by roughly 17–18%** (the standing regression building goes from
  49,467.8 to 40,614.6 kWh).

- **TABULA refurbishment variants are now modeled.** Each TABULA
  archetype carries three variant rows (`Number_BuildingVariant` 1/2/3:
  as-built, standard refurbishment, nZEB refurbishment); previously only
  the as-built variant was ever selected, so a renovated building was
  modeled with its original construction-era envelope. New
  `tabula_helpers.apply_refurbishment_measures()` converts a variant's
  own predefined-measure columns (`Code_MeasureType_<Component>_1`,
  `R_PredefinedMeasure_<Component>_1`) into adjusted U-values —
  `Add` measures compound resistances in series
  (`U_new = 1/(1/U_old + R)`), `Replace`/`ReplaceInsulation` measures
  treat the measure's R as the new total (`U_new = 1/R`) — applied in
  `LOD2Mapper.map_building()` after the editable override table.
  `lookup_tabula_archetype()` gained a `variant_number` parameter.
  For the Netherlands, `nl_archetype_mapper` now uses the real RIVM
  energy label to select the *variant* rather than to reassign the
  construction-year class: the construction year always determines the
  era (a renovated 1980 building is still structurally a 1980 building),
  and the gap between the label-implied performance tier and the
  era-typical tier selects variant 2 (1–2 tiers better) or 3 (3+ tiers)
  via the new `label_to_refurbishment_variant()`. New
  `refurbishment_variant` output column. Migration of the bundled Loenen
  dataset via new `scripts/refresh_nl_archetype_variants.py`: of 742
  label-matched buildings, 322 now use the standard-refurbishment variant
  and 86 the nZEB variant.
- **Multi-dwelling internal gains are now scaled to the whole building.**
  New `residential_units` `AttributeSpec`: for AB/MFH buildings, where
  one building id represents a whole apartment block (matching TABULA's
  own AB/MFH archetypes, which carry `n_Apartment` counts of 15–56),
  `AttributeBuilder.generate_electricity_profile()` previously generated
  internal gains for a *single household* and applied them to the
  whole-block envelope. `Q_ig`/`elecLoad`/`dhw_liters` are now scaled by
  the dwelling count (the dimensionless `occ_nothome`/`occ_sleeping`
  fractions and any caller-supplied `elecLoad` are deliberately not).
  Currently reachable through the analysis path (`validation.py`) only:
  the v3 request schema has no dwelling-count field, so the live API
  path is unchanged pending a contract decision.
- **`validation.py --labeled-only`**: restricts the buem-vs-CBS
  comparison to buildings with a real RIVM energy label — the subset
  whose current envelope performance (including refurbishment) is known
  rather than inferred from construction year alone, and therefore the
  fairest subset to validate against measured consumption statistics.
  Combined effect of the two changes above on the buem-vs-CBS
  overestimate for Apeldoorn/2018: mean ratio 4.96 → 4.02 on an
  identical sample, 3.13 with `--labeled-only`.
- **Domestic hot water / gas-cooking energy, wired end-to-end for
  residential buildings**: `buem.thermal.dhw_cooking` — `dhw_energy_kwh()`
  (V·ρ·c·ΔT, liters → kWh, `DHW_DELTA_T_K = 30.8` K — real EN 12831-3
  operating-condition temperatures, 42 °C delivery / 11.2 °C cold mains),
  `dhw_energy_kwh_annual_fallback()` (default now EN 12831-3 Annex Table
  B.5's self-consistent 55 L/person/day figure, ~1085 kWh/person/yr — the
  previous NTA 8800 545 kWh/person/yr default is still available
  explicitly, but was found to fail its own internal-consistency check),
  `cooking_gas_energy_kwh()`/`cooking_annual_kwh_from_heating()`
  (distributes a CBS-ratio-derived annual total across `cooking_active`
  -flagged hours). Deliberately **not** part of `ModelBUEM`'s 5R1C solve —
  additive post-processing only. `AttributeBuilder` now calls
  `occupancy.generate_dhw_draws()` (v5.0.0) for every residential building
  (not service buildings — no DHW model there yet); `ModelBUEM
  ._addDhwCooking()` (new, called from `_readResults()` after the LP
  solve) converts the resulting liters/cooking-activity series into new
  `self.dhw_kWh`/`self.cooking_gas_kWh` and `"DHW Load"`/`"Cooking Gas
  Load"` `detailedResults` columns — `None`/absent with zero behavior
  change for any cfg that doesn't carry the two new optional
  `dhw_liters`/`cooking_active` keys (new `AttributeSpec` entries,
  `cfg_attribute.py`). `buem.analysis.netherlands.validation` now reports
  a second comparison alongside the original — buem's own `heating_kWh +
  dhw_kWh + cooking_gas_kWh` vs. CBS's real unstripped gas total — run for
  real against Apeldoorn (GM0200): DHW/cooking modeling modestly improves
  most groups' ratios but does not close issue #3's 2-7x gap, confirming
  the gap's dominant cause lies elsewhere. Fully tested
  (`tests/test_dhw_cooking.py`, `tests/test_nl_validation.py`, real
  end-to-end in `test_building_types.py`). See
  `.claude/dhw_cooking_heat_handoff.md`. `gas_conversion.py` gained two new real constants,
  `DHW_SHARE_OF_GAS`/`COOKING_SHARE_OF_GAS` (0.20/0.02 — previously
  docstring-only), so a per-building cooking-energy total can be derived
  from the CBS ratio instead of a new invented constant.
- **`buem.config.reference_values`**: a CSV-backed loader
  (`load_dhw_cooking_constants()`) for `src/buem/data/reference/
  dhw_cooking_constants.csv` — the single, user-editable point of
  configuration for `dhw_cooking.py`/`gas_conversion.py`'s deterministic
  constants (water properties, both DHW ΔT figures, the EN 12831-3 and
  legacy NTA 8800 annual-fallback figures, the three CBS gas shares, gas
  calorific value, boiler efficiency), mirroring `occupancy`'s own
  `dhw_tapping_categories.csv` pattern. New `scripts/
  extract_dhw_reference_values.py` regenerates the EN-12831-3-sourced
  rows from the source workbook (present locally at `src/buem/data/
  reference/`, deliberately not committed — unverified redistribution
  license). Same public constant names/imports as before; only where the
  values come from changed. Tested: `tests/test_reference_values.py`.
- **`DEFAULT_SEED` removed entirely**: per explicit user direction ("we
  should remove seeds completely from buem"), `building_registry.py`'s
  `DEFAULT_SEED = 42` is gone, `ATTRIBUTE_SPECS["seed"].default` is now
  `None`. Every occupancy call buem makes (`HouseholdProfile`/
  `ElectricityConsumptionProfile`/`ServiceBuildingProfile`/
  `generate_dhw_draws()`) now lets occupancy v5.0.0's own
  `derive_default_seed()` own reproducibility completely — no buem-side
  seed bookkeeping left. `test_hash_determinism` re-verified green under
  the new behavior.
- **`buem.analysis.netherlands.construction_year_stratification`**: a
  real investigation into issue #3's buem-vs-CBS heating gap, stratified
  by TABULA construction-year class/insulation level rather than just
  building type. Found `validation.py`'s "first N buildings in file
  order" sample selection is not representative of the real construction-
  era mix (TH sampled 80% the oldest/worst-insulated class vs. 21% of the
  real population) — correcting for this sampling skew alone drops TH's
  reported ~6.4x ratio to ~3.4-4.3x and SFH's from 2.00x/3.45x to
  1.80x/3.10x, a substantial (not complete) explanation for the gap,
  independent of DHW/cooking. See `.claude/dhw_cooking_heat_handoff.md`.

## [3.2.0] - 2026-08-18

### Added

- **`buem.analysis.netherlands`**: a buem-vs-CBS validation script,
  built and run for real. `cbs_reference.py` queries CBS's real
  regional gas/electricity consumption statistics (table 81528NED) live
  via stdlib `urllib` (no new dependency); `gas_conversion.py` converts
  gas m³ to useful heat kWh via three separately-documented factors
  (official 9.769 kWh/m³ calorific value; a 78% space-heating share,
  since CBS's figure also includes hot water/cooking and `ModelBUEM`
  simulates space heating only; a 90% boiler-efficiency assumption);
  `validation.py` runs real buildings through the full simulation
  pipeline grouped by housing type and reports the comparison. The
  first real run immediately found and fixed a real bug in the
  comparison itself (not a buem heating-model error): apartment-
  building groups compared a whole multi-unit *building*'s simulated
  total against CBS's *per-dwelling* figure. Fixed by carrying RIVM's
  real dwelling-unit count through as a new `residential_units` column
  and normalizing before comparing — dropped those groups' ratios from
  10-80× off to 2.4-9.8×. Round 2: `--period` now defaults to matching
  `--weather-year` (verified real CBS 2018 data exists via the same live
  API, resolving the 2024-CBS-vs-2018-weather mismatch without needing
  new weather-archive access); re-ran with matched years and a
  5×-larger sample specifically to test whether small-sample noise
  explained the remaining gap — it didn't (TH's two `neighbour_status`
  subgroups agree to within 0.2% across 5 different buildings each).
  AB/MFH/TH settled into a persistent ~2-7× too-high band that survived
  both fixes — genuinely still open, see `.claude/residential/open.md`.
- **Non-residential buildings linked to occupancy's service-building
  path** instead of full exclusion — `nl_building_classifier` now checks
  a flagged building's real footprint area (`MIN_SERVICE_BUILDING_
  FOOTPRINT_M2`, 50 m²) before routing: large real structures (2,125 m²/
  1,718 m² in Loenen) link to occupancy's `"warehouse"` service type via
  a new `service_building_type` column; buildings too small to be real
  occupied structures (16 m²/5 m² — garden greenhouses, not commercial
  buildings) are excluded from both residential and service modeling
  rather than force-fit into either.
- **Netherlands pipeline verified against a second, independent
  community (Heeten, Overijssel) with zero code changes** —
  `src/buem/data/buildings/netherlands_heeten/`, 2,675 buildings, same
  real-data results as Loenen (100% RIVM match, 23.9% label coverage,
  100% TABULA archetype match, real end-to-end `LOD2Mapper` mapping).
- **PostgreSQL/3DCityDB access confirmed** and used to independently
  corroborate the Netherlands duplicate-row bug's origin: the live
  database's `city2tabula.lod2_building_feature`/`lod2_child_feature_
  surface` row counts match the old buggy CSV export exactly, confirming
  the bug lives in the database itself, not just an export step; its
  `lod3_*` tables exist but are empty, confirming no real LOD3 window/
  door geometry is available anywhere for this region.
- **Real CBS regional energy-consumption benchmark data found** (CBS
  opendata table 81528NED, freely queryable via OData) — real 2024
  average gas/electricity consumption for Apeldoorn municipality (Loenen's
  own), broken down by exactly the housing-type categories
  `nl_building_classifier` already reproduces. Documented as a ready-to-
  use validation reference in `.claude/residential/open.md`; not yet
  built into an actual comparison script.
- **Netherlands TABULA archetype linking + editable U-value table**: three
  new modules close the gap the CityJSON regeneration (below) deliberately
  left open. `nl_building_classifier` derives building type/attachment
  status by reproducing CBS's own published `woningtype` methodology
  (connected-building count from this session's geometric party-wall
  detection + residential-unit count from RIVM). `rivm_energy_labels`
  queries the real Dutch energy-labels GeoPackage (~11.35M rows, ~3.2 GB)
  via targeted `sqlite3` queries, never a full-table load. `nl_archetype_
  mapper` links each building to a real bundled TABULA archetype row
  (construction year, or a real energy label as an override where
  present — 24% of Loenen) via `tabula_helpers.lookup_tabula_archetype()`
  (now parameterized onto any TABULA sheet, not hardcoded to the German
  one). `LOD2Mapper` gained an optional `u_value_overrides` parameter so
  the new, plain-CSV `u_value_reference.csv` (year-class × building-type
  → wall/roof/floor/window/door U-values, extracted from real TABULA NL
  data and cross-validated against independently-researched Bouwbesluit/
  NTA 8800 Rc-value history) is actually consulted, not just documentation
  — and `geometry_utils.building_lat_lon()` is finally wired into
  `LOD2Mapper` itself (a pre-existing gap). Verified end-to-end: 3,101/
  3,101 residential Loenen buildings (100%) now get a real TABULA match,
  and `LOD2Mapper.map_building()` succeeds with real U-values, synthesized
  windows/doors/ventilation, and real coordinates where it previously
  returned `None` for every Netherlands building. See `docs/source/
  modules/netherlands.rst` (new page) and `.claude/residential/
  resolved.md` for the full methodology and evidence.
- **`PostgresBuildingSource` reads connection config from `.env`**
  (`BUEM_PG_HOST`/`PORT`/`DATABASE`/`USER`/`PASSWORD`) instead of only
  accepting hardcoded constructor arguments — `database`/`user`/`password`
  are now required (explicit arg or env var); previously defaulted
  silently to `localhost`/`postgres`/`postgres`/`city2tabula_germany`,
  which could attempt a connection with wrong credentials instead of
  failing clearly. See `.env.example`.
- **`CsvBuildingSource` + `geometry_utils`**: a third building-data source
  (alongside `ExcelBuildingSource`/`PostgresBuildingSource`), reading
  plain CSV exports of the same three tables — for regions with a CSV
  drop instead of an Excel workbook or a live Postgres connection (built
  for a real Netherlands/Loenen dataset). `geometry_utils.py` decodes a
  building's real `(latitude, longitude)` from its `building_centroid_geom`
  WKB geometry (hand-rolled point decoder, no new dependency for that
  part) and reprojects via `pyproj` (new dependency, added to
  `infrastructure/env/buem_env.yml`). See `.claude/residential/
  resolved.md` for the full writeup, including a real-data incident along
  the way (an initial CSV export was missing pre-computed surface
  geometry columns the pipeline needs; resolved by re-exporting the
  correct table, not by buem growing a new 3D geometry engine).
- **`buem.analysis` package**: reusable (not scratchpad) tooling for
  comparing buem's simulated heating/cooling/electricity demand — and the
  raw T/GHI/DNI/DHI weather inputs themselves — across weather providers
  for one or many buildings picked directly from the bundled TABULA Excel/
  PostgreSQL source. `weather_providers.py` (multi-provider extraction,
  reusing `weather_cache`'s feather-cache convention, with logged fallbacks
  for known archive gaps), `building_selection.py` (filtered building
  picking + a wall-exposure sanity check), `provider_comparison.py` (runs
  one building through the full `AttributeBuilder`/`CfgBuilding`/
  `ModelBUEM` pipeline once per provider), `summarize.py` (annual/monthly
  aggregation + pairwise GHI difference metrics), `report.py` (a
  self-contained HTML report, run and opened locally, no external tooling
  required), `batch.py` (multiprocessed many-building runs to Parquet,
  `python -m buem.analysis.batch`, portable to any machine with the same
  env vars — deliberately not itself wired to any specific remote host).
- **Per-provider weather year-range validation**: each weather provider's
  processed archive only covers a specific calendar-year window (tightened
  2026-08-14 per consultation — a data-availability fact about each
  archive, not a buem design choice): `era5-land` 1980-2025, `cosmo-rea6`
  1995-2018, `merra-2` 1950-2025. `weather.year` outside the resolved
  provider's range (explicit or the `merra-2` default) is now rejected at
  validation time with a clear message naming the valid range, instead of
  an opaque `FileNotFoundError` three layers deep during `AttributeBuilder`.
  Enforced both by `geojson_validator.py` (the real gate) and declaratively
  in `versions/v4/request_schema.json` via `allOf`/`if`/`then`, for any
  other consumer validating directly against the schema file.
- **`buem.weather.profile` gains a `json` format** (now the default —
  confirmed 2026-08-14 as EnerPlanET's actual format): an array of hourly
  records, each `{time, T, GHI, DHI, DNI}`. `csv`/`parquet` remain
  supported for other/internal callers.
- **Window/door azimuth and tilt now always match their parent wall/roof**:
  new `buem.buildings.mapping.live_synthesis.normalize_opening_azimuths()`,
  wired into `CfgBuilding.to_cfg_dict()` alongside `synthesize_missing_openings()`.
  A caller-supplied Window/Door element whose `azimuth`/`tilt` disagrees
  with the Wall/Roof element it declares as its `surface`/`parent_id` is
  corrected to the parent's value (logged as a warning), rather than
  silently modeling a window facing a different direction than the wall
  it's embedded in. No-op for internally-synthesized openings (already
  consistent by construction); ventilation is excluded (azimuth/tilt play
  no role in the ISO 13790 model there).

### Changed

**BREAKING: live API contract promoted from v3 to v4.**
`geojson_validator.py` now validates and converts against
`versions/v4/` instead of `versions/v3/` (2026-08-14; sign-off: this
session, see CLAUDE.md "Guardrails" — promoted on the basis that the
EnerPlanET developer told the user, 2026-08-13, they are already working
on their own v5). `versions/v3/` is retained as an archived, deprecated
snapshot. See `versions/v4/DRAFT.md`'s promotion note and
`VERSIONING.md`'s new `v4.0.0` entry for the full detail, including which
two schema-documented fields were deliberately left unwired (rejected,
not silently ignored, if sent — `weather.use_percentile`/`percentile`,
`solver.compute_cooling`).

- **BREAKING**: `building.building_type` is now validated against an
  enforced set (4 TABULA residential codes + occupancy's 8 registered
  service-building types, read live from `RESIDENTIAL_BUILDING_TYPES` and
  `occupancy.SERVICE_BUILDING_TYPES` — not a hand-copied enum). A request
  with an unrecognized `building_type` is now rejected at validation
  time, instead of failing later and less legibly inside
  `AttributeBuilder.generate_electricity_profile()`.
- New optional `building.equipment` field reaches real requests:
  per-item household-equipment inclusion/exclusion, `{equipment_id: bool}`
  (reshaped from an earlier `{mode, items}` draft to be more expressive
  and closer to the original ask — see `AttributeBuilder
  ._resolve_equipment_table()`). `true` guarantees an item is treated as
  owned; `false` guarantees exclusion; an omitted id keeps occupancy's own
  archetype-adjusted default. Residential `building_type` only — ignored,
  with a logged warning, for service-building types (`occupancy
  .ServiceBuildingProfile` has no per-item equipment selection yet, see
  `.claude/occupancy_module_activities.md`). New `buem.config
  .building_registry.HOUSEHOLD_EQUIPMENT_TYPES` (29 ids) validates
  selector keys.
- `buem.weather.provider`/`year` now reach real requests (previously only
  a genuine v2-format request or a direct `AttributeBuilder` call could
  set these). Deliberately does *not* fall back to the request's
  `start_time` calendar year despite `DRAFT.md`'s originally-documented
  intent for `year` — confirmed against real fixtures this can silently
  request a year with no local weather archive, turning a working request
  into a hard failure; `year` is forwarded only when the caller explicitly
  supplies `buem.weather.year`.
- New file-based profile inputs, both reaching real requests: **`buem
  .weather.profile`** (caller-supplied T/GHI/DHI/DNI timeseries, CSV or
  Parquet, first column as timestamp index) and **`buem.inputs
  .electricity_load_profile`** (caller-supplied hourly electricity array,
  CSV/JSON/gzipped-JSON, `kWh`/`kW`/`Wh` unit-converted, indexed against
  the request's resolved year and reconciled with buem's own half-hour-
  offset weather index via the same nearest+tolerance reindex already used
  elsewhere). New `buem.integration.scripts.profile_file_loader` module;
  new `BUEM_DATA_DIR` env var convention (`buem.env`) for where a real
  deployment mounts these files. Both raise a clear validation error (not
  a crash, not a silent fallback) for a missing/malformed file.
- New `buem.config.building_registry` module: the pure, side-effect-free
  constants (`RESIDENTIAL_BUILDING_TYPES`, `HOUSEHOLD_EQUIPMENT_TYPES`,
  etc.) split out of `cfg_attribute.py` specifically so
  `geojson_validator.py` can use them for request-structure validation
  without pulling in `cfg_attribute.py`'s own eager weather fetch as an
  import-time side effect. `cfg_attribute.py` re-exports the same names
  unchanged — no change for existing importers.
- **`use_provided_elecLoad` now preserves occupancy-generated
  `Q_ig`/`occ_nothome`/`occ_sleeping`**: previously this flag skipped
  calling `occupancy` entirely, keeping whatever was already in
  `merged_attrs` for all four profile series. It now still runs a real
  `HouseholdProfile`/`ServiceBuildingProfile` generation and substitutes
  only `elecLoad`, via `occupancy.to_buem_profiles(elec_load=...)`
  (available since occupancy v3.1.0, previously unused by buem) — a
  behavior change for anyone relying on the old total-bypass semantics.

### Added

- **`buem.buildings.datasources.cityjson_extractor`**: extracts clean LOD2
  building geometry (walls/roofs/floors with real area/tilt/azimuth,
  building-level aggregates, storeys, a real lat/lon-capable centroid, and
  geometrically-detected party walls) directly from a CityJSON (3D BAG)
  source — replacing the Netherlands/Loenen dataset's dependency on
  city2tabula's pipeline entirely, after that pipeline turned out to carry
  two independent, compounding duplicate-geometry bugs (see the `Fixed`
  entries below and `.claude/residential/resolved.md`'s "Netherlands
  (Loenen) building data" entry for the full story). Deliberately does
  **not** attempt TABULA archetype matching — every regenerated row's
  `tabula_variant_code_id` is null pending a separate, explicitly-deferred
  discussion; `LOD2Mapper.map_building()` cannot succeed for any
  Netherlands building until that lands. `src/buem/data/buildings/
  netherlands/lod2_building_feature.csv`/`lod2_child_feature_surface.csv`
  were regenerated with it; old city2tabula CSVs kept for reference under
  `netherlands/_city2tabula_backup/`. Verified exhaustively against 3D
  BAG's own aggregate attributes for all 3,105 buildings (median ratio
  1.000, one explained outlier — a real two-`BuildingPart` building). See
  the module's own docstring for the full methodology and calibration
  evidence.

### Fixed

- **Roof solar gain was silently azimuth-independent for German-path
  buildings** — `LOD2Mapper` hardcoded every roof element's azimuth to
  0.0 on a stale claim ("no role in solar calcs for roofs") that turned
  out to be false: `model_buem._calcRadiation()` passes every element's
  real tilt and azimuth through `pvlib.irradiance.get_total_irradiance()`,
  so roof solar gain genuinely depends on orientation. The real German
  LOD2 database has usable azimuth data for 64.8% of roof surfaces
  (10,732/16,558) that was being silently discarded — every non-flat
  German roof was modeled as if it faced due north. Fixed: roofs now use
  the same real-value/negative-NaN-fallback handling walls already had.
  Netherlands was unaffected (already computed real per-plane roof
  azimuth from CityJSON geometry). `docs/source/modules/buildings.rst`'s
  stale assumption-table entry corrected to match.
- **`cfg_attribute.py`'s re-export contract, regressed by an automated
  lint fix and caught before shipping** — a `ruff check --fix` pass
  deleted 3 of its 10 `building_registry.py` re-exports
  (`DEFAULT_ARCHETYPE_BY_BUILDING_TYPE`/`HOUSEHOLD_EQUIPMENT_TYPES`/
  `RESIDENTIAL_BUILDING_TYPES`) because ruff's per-file "unused import"
  check can't see them being imported *from* this module elsewhere
  (`attribute_builder.py`, several tests) — caught immediately by
  `mypy src` right after. Restored, plus an explicit `__all__` added
  (none existed before) so ruff recognizes the re-exports as
  intentional going forward. Full local CI mirror (ruff/mypy/181
  pytest/`buem validate`) clean after.
- **`cityjson_extractor` silently over-counted area for any face with a
  hole** (courtyards, light-wells) — its Newell's-method area computation
  read only the exterior ring, ignoring interior rings entirely. Found by
  re-auditing the extractor after a direct question about whether real
  geometry (not city.json attributes) was actually being used. Affects
  144 faces / 128 buildings (4.1% of 3,105) in the current Loenen
  dataset, up to ~37% over-count for one individual face. Fixed: sum
  every ring's raw Newell vector across the whole face before taking the
  magnitude (verified first, on real data, that interior rings wind
  opposite the exterior ring, so this correctly subtracts hole area via
  vector cancellation). Dataset regenerated with the fix. New unit tests
  in `tests/test_cityjson_extractor.py`. See `.claude/residential/
  open.md` for the fuller writeup, including why 3D BAG's own aggregate
  attributes turned out not to be a clean ground-truth check for the
  affected buildings (they don't subtract hole area either).
- **A wall too small to receive a window (`< MIN_WALL_AREA_FOR_WINDOWS`,
  5 m²) still had its would-be window area subtracted from its own opaque
  `net_area`** — `element_factory.synthesize_openings()` computed
  `window_area` from the TABULA ratio for every wall, then
  `create_windows()` separately skipped building an actual window element
  for small walls; `WallInfo.net_area` used the same (still nonzero)
  `window_area` regardless, so that area belonged to neither the opaque
  wall nor a window element — silently missing from the envelope
  entirely. Found while verifying window/door/ventilation area
  bookkeeping end-to-end. Fixed: `window_area` is now forced to `0.0` for
  a wall under the size cutoff, consistent with the existing
  `azimuth_known` handling in the same loop. Affects both `LOD2Mapper`
  (offline path) and `live_synthesis` (live path, full-synthesis case) —
  they share this function. New regression test in
  `tests/test_live_synthesis.py`.
- **`CsvBuildingSource` was silently loading ~2× too much wall/roof/floor
  area per Netherlands/Loenen building** (Layer 1 of a two-layer
  duplicate-row bug in the underlying export): `lod2_child_feature_surface
  .csv` rows identical in every column except `id`/`child_row_id` are now
  dropped on load (`_dedup_surfaces()`), and `lod2_building_feature.csv`'s
  own `area_total_wall`/`area_total_roof`/`area_total_floor`/
  `surface_count_wall`/`surface_count_roof` columns — which carried the
  same bug independently, as stored aggregates rather than values derived
  from `surfaces` at read time — are recomputed from the deduped surfaces
  (`_recompute_building_aggregates()`), so `LOD2Mapper`'s `A_ref`
  computation isn't fed a stale, still-inflated value even after the
  surface-row fix. Verified against building 37206: 16 walls / 4 roofs /
  4 floors / `A_ref`=150.7 m² → 8 walls / 2 roofs / 2 floors / `A_ref`=
  75.35 m². **A second, still-open duplication layer was found while
  verifying this fix against real CityJSON ground truth — see
  `.claude/residential/open.md`; Netherlands/Loenen heating-demand
  numbers are not yet trustworthy end-to-end.** See
  `.claude/residential/resolved.md`.
- **MILP big-M design-load computation silently swallowed failures**:
  `_addConstraints_sequential()` (runs on every `sim_model()` call, LP or
  MILP) wrapped `calcDesignHeatLoad()` in a bare `try/except: design =
  1000.0` — any failure was replaced with a made-up value, no log, no
  error. Found via a user-prompted audit for exactly this pattern.
  Fixed: now logs a warning and re-raises instead of substituting a
  silent fallback. See `.claude/resolved.md`.
- **Two `model_buem.py` crashes on real party-wall buildings**, both
  found via `buem.analysis.batch`'s smoke test right after the
  `validate_cfg()` fix below: (1) `_init5R1C()`'s opaque-component
  solar-gain loops read only a component-level `Walls`/`Doors`/`Roof`
  `U`, crashing with `TypeError` on any component that only has
  per-element `U` (no single component-level default — e.g. a mix of
  exposed and party walls); fixed via a new
  `_resolve_opaque_element_u()` helper that falls back to each element's
  own `U`, mirroring how `_initEnvelop()` already handled this correctly
  for the H/conductance calc a few lines earlier in the same method.
  (2) A 0-exposed-wall building (nowhere to synthesize a window) ended
  up with a configured-but-empty `Windows` component, which raised
  `ValueError: No window elements found but windows are configured`
  instead of the zero solar gain that's physically correct here —
  inconsistent with the code's own adjacent `H_windows=0.0`/Floor-gain
  handling. Fixed: zero windows now means zero window solar gain, not an
  error. Verified against the exact real buildings that originally
  crashed (44424/42868/60342/18763/30542, all now `status="ok"`). See
  `.claude/residential/resolved.md` for the fuller writeup.
- **`summarize.summarize_weather()` crashed on a single-provider run**:
  `_pairwise_ghi_diff()` builds one row per *pair* of providers, so a
  single-provider call (e.g. `report.build_html_report()` for just
  `merra-2`) produces zero rows — `pd.DataFrame([])` has no columns at
  all, so the subsequent `.set_index("pair")` raised `KeyError: "None of
  ['pair'] are in the columns"`. Found by `tests/test_analysis.py`'s new
  end-to-end report test (deliberately single-provider, to keep the real
  simulation it drives fast). Fixed: the zero-pairs case now returns an
  explicitly-shaped empty `DataFrame` (right columns, `"pair"` index
  name) instead of falling through to the crash.
- **`validate_cfg()` rejected legitimate `U=0` party walls outright**:
  the per-component and per-element wall-U checks used `float(u) <= 0`,
  treating zero the same as a negative (physically impossible) value.
  But `U=0` is buildings.rst's own documented convention for a shared/
  party wall (`b_transmission=0`, no heat transfer modeled to an adjacent
  heated space) — confirmed physically and numerically safe throughout
  `model_buem.py`, where `U` is only ever a multiplicative factor, never
  a divisor. Since `AttributeBuilder.build()` calls `validate_cfg()` on
  every real request, this meant **any building with any party wall
  failed validation and could never be simulated at all — live API
  traffic included, not just batch/analysis tooling.** Found via
  `buem.analysis.batch`'s smoke test on real TH/MFH buildings with party
  walls. Fixed: both checks now use `float(u) < 0` (reject only negative
  U). See `.claude/residential/resolved.md` for the fuller writeup,
  including two further, distinct, deliberately-unfixed bugs this same
  investigation surfaced on the same buildings (`.claude/residential/
  open.md`).
- **`LOD2Mapper` never read the matched TABULA archetype's own indoor
  setpoint (`theta_i`)**: every offline-pipeline-mapped building silently
  used `ThermalProperties`' generic 21.0°C `comfortT_lb` default regardless
  of what its archetype actually specified — a continuous +1°C bias (no
  TABULA-equivalent night/weekend setback either) versus TABULA's own
  reference calculation. Found while investigating a large buem-vs-TABULA
  heating-demand gap (buem ~51,000-55,000 kWh/year vs. TABULA's own
  36,639 kWh for `DE.N.SFH.01.Gen`, `theta_i=20.0` for that archetype).
  Fixed: `comfortT_lb` now reads `theta_i` from the matched row (falling
  back to 21.0 unchanged when absent). `comfortT_ub` is untouched — no
  TABULA row equivalent; TABULA's residential reference calculation is
  heating-only. Real offsetting factors remain uninvestigated further
  (real vs. TABULA-reference floor/roof area for a given matched building,
  real hourly weather vs. TABULA's fixed national reference climate,
  occupancy-driven vs. TABULA's flat `phi_int` internal gains) — this fix
  addresses the one clearly-code-verified gap, not the full discrepancy.
- **`cfg_building.py` broke import on Python 3.11** (reported by the
  EnerPlanET developer, 2026-08-01, reproduced against a fresh 3.11.14
  `buem_env`): `CfgBuilding.from_json_file()` referenced its own
  enclosing class as a bare, unquoted return-type annotation with no
  `from __future__ import annotations` in the file — raises `NameError:
  name 'CfgBuilding' is not defined` at class-definition time on 3.11
  (masked on 3.14 by PEP 649/749 lazy annotation evaluation), breaking
  every import of the module and therefore the whole API server. Fixed
  by adding `from __future__ import annotations`, matching the
  convention already used elsewhere in this codebase (`live_synthesis.py`,
  `element_factory.py`, `tabula_helpers.py`). Audited the rest of the
  codebase for the same self-referencing-return-type pattern — one other
  superficially similar case (`ExcelBuildingSource.from_parquet()`) was
  already safe (that file already has the future import). Does **not**
  change `requires-python` — a separate, still-open cross-repo Python
  version alignment question, not decided here.
- **`h_room` (room height) hard-rejected real non-residential buildings
  above 5.0m** (reported by the EnerPlanET developer, 2026-08-01,
  reproduced against a real THD Deggendorf campus building with
  `room_height = 5.03m`, a mensa/communal space): the ISO 13790 sanity
  bound was `0 < h_room <= 5.0m`, sized for households (~3m). Widened to
  `0 < h_room <= 20.0m` (and `comfortT_lb`/`comfortT_ub` similarly widened
  to `[5, 35]°C`, was `[15, 30]`) to admit non-residential room heights —
  warehouses, gyms, lecture halls routinely exceed 5m. Already present in
  the working tree before this entry was written; documented here and in
  `docs/source/modules/thermal.rst` for the historical record.

## [3.1.0] - 2026-08-11

### Added

- **Internal LOD2 → LOD3 envelope synthesis**: windows/doors/ventilation
  are now computed internally by buem whenever a caller supplies wall/
  roof/floor geometry without them, instead of silently defaulting to
  zero — via new `buem.buildings.mapping.live_synthesis
  .synthesize_missing_openings()`, wired into `CfgBuilding.to_cfg_dict()`
  so both the live API path (`AttributeBuilder` → `CfgBuilding`) and the
  config-only/demo path are covered by one change. Reuses the same
  TABULA-ratio window/door/ventilation sizing rules
  `LOD2Mapper`'s offline Excel/PostgreSQL batch pipeline already
  implemented (`docs/source/modules/buildings.rst`), now shared via new
  `element_factory.synthesize_openings()`. Resolves a real TABULA
  archetype via new `tabula_helpers.lookup_tabula_archetype()` (matched
  from `building_type`/`construction_period`/`country`, or an explicit
  `bldg_tabula_id` override) against the bundled reference sheet; falls
  back to new, clearly-flagged safe-default ratios (15% window-to-wall
  per direction, 5% door-to-wall, logged as a warning) when no archetype
  matches, rather than leaving a building with zero glazing. Never
  overrides an explicitly-supplied, non-empty component. **No API
  contract or schema changes** — `building_type`/`construction_period`/
  `country` were already forwarded end-to-end by
  `geojson_validator.py::_convert_v3_to_v2()`; the only gap was that
  `CfgBuilding` silently dropped `construction_period`/`country` because
  they weren't registered `ATTRIBUTE_SPECS`, now fixed.
- `weather_cache.get_or_fetch_weather()` gained a second fetch backend:
  `_fetch_remote()` calls `weather`'s own point-query HTTP API
  (`UU-BUEM/weather`'s `GET /v1/weather/point`) instead of reading local
  processed archives directly, selected by whether `WEATHER_API_URL` is
  set (`WEATHER_API_KEY` sent as an `X-API-Key` header); unset, behavior
  is unchanged from the existing local-archive path. Answers the
  production-BUEM-microservice half of the still-open "how does buem
  reach weather's archives" question (see `CLAUDE.md`'s "Open
  follow-ups") for any deployment that can reach that HTTP API but not
  the archive filesystem directly — actually configuring
  `WEATHER_API_URL`/`WEATHER_API_KEY` for a real deployment remains
  separate, unstarted work. New `requests` dependency.
- New optional `archetype` building attribute, passed to
  `occupancy.HouseholdProfile` for residential buildings. When omitted,
  `cfg_attribute.DEFAULT_ARCHETYPE_BY_BUILDING_TYPE` maps `building_type`
  (`SFH`/`TH`/`MFH`/`AB`) to one of occupancy's registered archetypes as a
  first-pass default (a heuristic, not a derivation — `num_persons`
  remains the dominant signal).
- `num_persons`/`archetype` added to the `versions/v4/` draft schema's
  `building` object, next to the existing `capacity` field (tier 2, not
  yet reconciled with EnerPlanET). `seed` deliberately not added — see
  below.
- Floor-area-normalized internal gains for service buildings:
  `AttributeBuilder.generate_electricity_profile()` now passes `A_ref` as
  `floor_area_m2` to occupancy's `to_buem_profiles()`, which blends an
  area-normalized equipment/lighting component into `Q_ig` (all 8 service
  types now carry a `gain_w_per_m2`). Residential unaffected. Closes
  `occupancy_gains_handoff.md` Gap 1 on buem's side.
- `geojson_validator.py::_convert_v3_to_v2()` now forwards
  `capacity`/`num_persons`/`archetype` from a v3 request's `building`
  object into `building_attributes` (tier-1 file, edited with explicit
  user direction). Closes `occupancy_gains_handoff.md` Gap 2.
  Deliberately excludes `seed` — see "Changed" below.
- `tests/test_building_types.py::test_v4_building_type_enum_matches_occupancy`:
  a drift guard asserting the `versions/v4/` draft schema's
  `building_type` enum matches `occupancy.SERVICE_BUILDING_TYPES` exactly.
  Closes `occupancy_gains_handoff.md` Gap 3.
- All of the above re-verified (2026-08-10) against
  [`occupancy` v3.1.0](https://github.com/UU-BUEM/occupancy/releases/tag/v3.1.0)
  (commit `3a99029`), the real tagged/pushed release, not just the local
  working tree it was originally developed and tested against —
  `buem_env`'s `occupancy` reinstalled fresh from the `git+...@main` pin
  already declared in `pyproject.toml`/`buem_env.yml` (no pin change
  needed). Full pytest suite (21/21), `buem validate`, and manual Gap 1/2
  checks all pass identically.

### Removed

- Dead `cfg_attribute.py` attributes, never read anywhere else in the
  codebase: `A_Window_North`/`East`/`South`/`West`/`Horizontal`, `roofs`.
- `seed` removed from the `versions/v4/` draft schema (was briefly added,
  now reverted) and excluded from `_convert_v3_to_v2()`'s forwarding — an
  internal RNG-reproducibility knob, not an EnerPlanET-contract concept.
  `cfg_attribute.py`'s `ATTRIBUTE_SPECS["seed"]` documents this
  explicitly. See `.claude/occupancy_gains_handoff.md`'s "Seed ownership"
  note for the proposal that `occupancy` itself own a deterministic
  default instead of buem managing/exposing one.

### Changed

- `cfg_attribute.py`'s module-level demo `components` default now carries
  gross `Walls`/`Roof`/`Floor` geometry only (no hand-picked `Windows`/
  `Doors`/`Ventilation`) — the new internal synthesis pipeline fills the
  rest, so this example follows `buildings.rst`'s documented rules
  instead of arbitrary numbers. New `country`/`construction_period`
  `ATTRIBUTE_SPECS` (both optional, defaults `"NL"`/`""`); `bldg_tabula_id`
  (previously declared but unused) is now a real TABULA-archetype lookup
  override.
- `WallInfo` and front/back wall identification moved from
  `lod2_mapper.py` to `element_factory.py` (shared with the new live-path
  synthesis); `LOD2Mapper.map_building`'s window/door/ventilation logic
  refactored to call the new shared `synthesize_openings()` — behavior-
  preserving for the offline pipeline (covered by a new end-to-end test
  against the bundled reference workbook, `tests/test_live_synthesis.py`).
- **`occupancy` (UU-BUEM/occupancy) is now a compulsory dependency**, not
  an optional extra — moved from `[project.optional-dependencies]` to
  core `dependencies` in `pyproject.toml`/`buem_env.yml`. All `try/except
  ImportError` guards around `import occupancy` are removed; it's
  imported unconditionally like weather/pandas/pvlib. This mostly
  formalizes existing behavior: the real per-request path
  (`AttributeBuilder.generate_electricity_profile`) already had no
  fallback and raised if occupancy was missing.
- The synthetic sinusoidal fallback profile (`cfg_attribute.py`'s
  module-level example-house defaults) is retired along with the guards
  that triggered it — that was the only code path where it still fired.

### Fixed

- `tests/test_live_synthesis.py`'s 4 tests exercising the bundled TABULA
  reference workbook now skip cleanly (`requires_bundled_workbook`,
  mirroring `test_cache.py`'s existing `_skip_reason` pattern) in any
  environment without it, instead of failing — found immediately by this
  version's own CI run: the workbook is `.gitignore`d (repo-wide `*.xlsx`
  rule) and was never actually part of the git repository, only present
  on the developer machine these tests were first written against. A
  pre-existing gap (the offline `LOD2Mapper` pipeline needed this same
  file since it was written, just never had test coverage before), not
  introduced by this release — see `CLAUDE.md`'s new
  `tabula-workbook-access` open follow-up for the still-unresolved
  underlying question (real TABULA-archetype matching only works on a
  machine that happens to have this file today).

## [3.0.0] - 2026-08-04

### Added

- `latitude`/`longitude`/`year`/`weather_provider` now flow end-to-end from
  a request to the weather fetch: `year` defaults to a request's own
  `start_time` calendar year when not explicit (`geojson_processor.py`);
  `year`/`weather_provider` added as optional, validated fields on
  `geojson_validator.py`'s `BuildingAttributesSchema` (v2 request format).
  Mirrored (documentation only, not yet wired into any live request path)
  in the `versions/v4/` draft schema as a new `buem.weather: {provider,
  year}` object.

### Changed

- **`weather` (UU-BUEM/weather) is now a compulsory dependency**, not an
  optional extra — moved from `[project.optional-dependencies]` to core
  `dependencies` in `pyproject.toml`/`buem_env.yml`. All `try/except
  ImportError` guards around `import weather` are removed; it's imported
  unconditionally like pandas/pvlib. `occupancy` remains optional.
- **The bundled offline weather CSV is retired**
  (`src/buem/data/weather/COSMO_Year__ix_390_650.csv`, one static
  COSMO-REA6 grid cell) — deleted, along with every code path that read
  it. `cfg_attribute.py`'s module-level weather default is now a real
  `weather.get_point_weather()` fetch for a documented default location
  (`DEFAULT_LATITUDE`/`DEFAULT_LONGITUDE`/`DEFAULT_YEAR`/
  `DEFAULT_WEATHER_PROVIDER`), cached locally (gitignored feather file)
  exactly like any other building's fetch.
- `model_buem.py::_calcRadiation`'s defensive DNI-to-extraterrestrial clip
  and hard 1200 W/m² POA cap are removed — DNI/DHI/GHI are used as
  provided by the weather fetch, trusting it's already physically bounded,
  instead of buem re-sanitising a second time.
- `DEFAULT_WEATHER_PROVIDER` switched `era5-land` → `merra-2` (see Known
  Issues below for why).

### Removed

- **`allow_weather_fallback` removed entirely** from `AttributeBuilder`
  and `GeoJsonProcessor` (breaking: passing this keyword now raises
  `TypeError`). A failed per-building weather fetch always raises now —
  there is no fallback, since substituting any other location's weather
  (real fetch or static file) would silently model the wrong building.

### Known Issues

- **The `weather` package's real-simulation path is not fully verified
  yet.** Comparing `era5-land`/`merra-2`/`cosmo-rea6` at buem's own
  default test cell surfaced three upstream bugs in `weather`
  (`merra-2`'s `T` column NaN outside one month; `cosmo-rea6` point-query
  raising on a dataset-concat error; `era5-land` returning an implausible
  GHI spike from an unrepaired month-boundary de-accumulation issue).
  `merra-2` and `cosmo-rea6` are fixed in `weather`'s upstream working
  tree but **not yet released/installed here** — running the full test
  suite against the currently-pinned `weather` version fails 4/14 tests
  with `RuntimeError: Problem data contains NaN` (merra-2's still-present
  bug). `era5-land` remains blocked on its own unrepaired archive
  regardless. Do not treat a real thermal-model run against any
  `weather_provider` as verified until `weather` is upgraded past these
  fixes and the affected archive(s) are repaired.

## [2.0.1] - 2026-07-31

### Fixed

- `attribute_builder.py::_reindex_to_weather` raised `TypeError` when
  reindexing an occupancy-generated series (tz-naive) onto a
  caller-supplied weather block (tz-aware, from
  `geojson_processor._weather_from_payload`'s `"Z"`-suffixed
  timestamps). Normalize onto the weather index's tz before reindexing.

## [2.0.0] - 2026-07-31

### Added

- Services (non-residential) buildings are now routed through
  occupancy's `ServiceBuildingProfile` instead of being forced through
  `HouseholdProfile` — `AttributeBuilder.generate_electricity_profile`
  branches on `building_type`: TABULA residential codes
  (`RESIDENTIAL_BUILDING_TYPES`: `SFH`/`MFH`/`TH`/`AB`) use the existing
  household path; any of occupancy's 8 service-building ids
  (supermarket/office/restaurant/school/hotel/bakery/warehouse/clinic)
  use the new path. New `building_type`/`capacity` `AttributeSpec`s.
- `ModelBUEM`'s `comfortT_lb`/`comfortT_ub` now accept a per-timestep
  `pd.Series` (in addition to the existing scalar), letting a building
  express a real occupied/unoccupied setpoint schedule (e.g. a school
  closed nights/weekends/summer) instead of only the coarse annual
  `F_red_htr` reduction factor.
- `AttributeBuilder(..., allow_weather_fallback=True)` opts back into
  lenient bundled-weather substitution on a per-location fetch failure
  (see Changed, below, for the new default).
- Six non-residential dummy fixtures
  (`src/buem/data/buildings/dummy/*.json`) updated from inert placeholder
  `building_type` codes to real occupancy type ids.
- `tests/test_building_types.py`, `tests/test_attribute_builder_strictness.py`.
- `docs/` (Sphinx/ReadTheDocs source) reintegrated — removed during the
  `v1.1` submodule-extraction refactor, never recreated until now.
  `pyproject.toml` `docs` extra (`sphinx`, `sphinx-rtd-theme`) restored.
  Content updated for drift accumulated since removal (occupancy/weather
  package split, v3 API schema, this release's changes); `modules/
  results.rst`/`technology.rst` now clearly flagged as documenting
  currently-nonexistent modules rather than presented as working.
- `src/buem/integration/json_schema/versions/v4/` — draft (not agreed
  with EnerPlanET) proposal for a `building_type` enum + `capacity`
  field; inert, not wired into any live validation path. See its
  `DRAFT.md`.
- `.claude/` known-issues/decisions log (mirrors `occupancy`'s/
  `weather`'s own convention) and `.claude/release-workflow.md`.

### Changed

- **Breaking**: `AttributeBuilder.build()` now raises `ValueError` if
  `latitude`/`longitude`/`components`/`A_ref` weren't explicitly supplied
  via `payload_attrs` or `db_fetcher`, instead of silently substituting
  `ATTRIBUTE_SPECS`' generic ~100 m² example-house defaults. Does not
  affect the live GeoJSON API in practice — `geojson_validator.py`'s
  v3→v2 conversion already unconditionally populates all four keys
  (with its own fallbacks) before `AttributeBuilder` ever sees the
  payload — but is a breaking change for any code calling
  `AttributeBuilder` directly with a partial `payload_attrs`.
- **Breaking**: a `db_fetcher` that raises now propagates (wrapped
  `RuntimeError`) instead of logging a warning and silently continuing
  with generic defaults.
- **Breaking**: `generate_weather_profile`'s per-location weather fetch
  now raises by default when the `weather` package is installed but has
  no data for the specific requested location/year/provider (previously
  silently substituted the bundled reference-location CSV). Fetches that
  fail because `weather`'s own optional extras are absent (e.g. xarray/
  netcdf4) remain a lenient fallback, unchanged. See `allow_weather_fallback` above.
- Profile/weather-index reindexing (`Q_ig`/`elecLoad`/`occ_nothome`/
  `occ_sleeping`) now raises if any timestep can't align within a
  30-minute tolerance, instead of silently zero-filling — plain
  `method="nearest"` reindexing never produces `NaN`, so the previous
  `fill_value=0.0` could silently paper over a real year/timezone
  mismatch.
- `ModelBUEM._initEnvelop`'s `h_room` sanity bound widened from 5.0 m to
  20.0 m (was blocking legitimate tall non-residential spaces —
  warehouses, sports halls, industrial halls).
- `ModelBUEM._init5R1C`'s `comfortT_lb`/`comfortT_ub` sanity range
  widened from [15, 30] °C to [5, 35] °C (was blocking legitimate
  frost-protection-only setpoints for lightly-conditioned industrial/
  warehouse space).

### Fixed

- `capacity` (service-building sizing) is now explicitly cast with
  `int()`, matching `num_persons` — previously a string `capacity` from a
  JSON payload would reach `ServiceBuildingProfile`'s `self.capacity <=
  0` check and raise an unrelated-looking `TypeError`.
- `BUEM_RESULTS_DIR`/`BUEM_LOG_DIR` are now created by `load_env()`
  directly instead of only being `os.environ.setdefault`, fixing a CI
  "Smoke test CLI" failure (`buem validate` reported `BUEM_LOG_DIR
  [MISSING]`) on a genuinely fresh checkout with no leftover `results/`/
  `logs/` directories from a prior run.
- Resolved all remaining `ruff` (180) and `mypy` (74) findings repo-wide,
  no suppressions; applied `ruff --fix` repo-wide (import sorting,
  `Optional[X]` → `X | None`, unused vars/imports).

## [1.2.1] - 2026-07-30

### Changed

- Removed the `numpy<3`/`pandas<3` upper-bound caps from
  `infrastructure/env/buem_env.yml` (floors only now: `numpy>=1.26`,
  `pandas>=2.0`), matching the same change in `occupancy`'s/`weather`'s own
  `pyproject.toml`. The caps weren't protecting anything in practice — both
  sibling repos had already drifted past them — and `buem`'s test suite,
  plus occupancy's (69/69) and weather's (55 passed/2 skipped) full pytest
  suites, all pass clean on numpy 2.4-2.5/pandas 3.0.x.
- `gunicorn` removed from `infrastructure/env/buem_env.yml` — it's Unix-only
  (fork()-based), has no win-64 conda-forge build, and was breaking
  `conda env update` on Windows dev machines. Now installed directly in
  `infrastructure/container/Dockerfile`'s builder stage instead, where it's
  actually used (API server CMD); `pyproject.toml`'s `server` extra still
  declares it for anyone installing outside conda.

### Fixed

- `weather_env`'s `mkl` BLAS build was colliding with `cupy`'s bundled CUDA
  DLLs on Windows, crashing numpy on plain import (unrelated to buem's own
  code, but blocked verifying the pin change above against a real weather
  install). Fixed upstream in `weather`'s own `infrastructure/env/
  weather_env.yml` (pinned `libblas=*=*openblas`); `cupy` also removed
  there as unused.

## [1.2.0] - 2026-07-29

### Fixed

- **Occupancy integration was dead code**: `cfg_attribute.py`/`attribute_builder.py`
  imported a package/module path (`buem_occupancy.occupancy_profile.OccupancyProfile`)
  that has never existed, so the `try/except ImportError` always failed silently
  and every build used a synthetic sinusoidal electricity-load fallback —
  regardless of whether `occupancy` was actually installed. Now imports the
  real `occupancy` package (`HouseholdProfile`, `ElectricityConsumptionProfile`,
  `to_buem_profiles`) and uses `to_buem_profiles()` to derive all four series
  `ModelBUEM` requires (`Q_ig`, `elecLoad`, `occ_nothome`, `occ_sleeping`) from
  one real occupancy simulation, instead of hand-rolling three of them as
  hardcoded sine curves regardless of `occupancy`'s availability. The
  sinusoidal fallback is kept, but now only used when `occupancy` genuinely
  isn't installed.
- `readme.md`/`pyproject.toml` told users to `pip install buem-occupancy` /
  `pip install buem-weather` — packages/repos that don't exist. Now
  `pip install buem[occupancy,weather]`, matching the extras actually
  declared in `pyproject.toml`.
- Duplicate stale weather CSV/feather files at `src/buem/data/` (root),
  superseded by `src/buem/data/weather/` since an earlier reorg; two test
  scripts (`tests/run_test.py`, `tests/test_energy.py`) were still pointing
  `BUEM_WEATHER_DIR` at the stale root location, which is why the
  duplicates were never cleaned up. Both fixed to the canonical path.

### Added

- **Dynamic per-location weather**: `AttributeBuilder` now fetches a
  location-specific `T`/`GHI`/`DHI`/`DNI` DataFrame per building via the
  optional `weather` package's `get_point_weather(lat, lon, year,
  provider=...)`, instead of always using one bundled static CSV
  regardless of building location. Falls back gracefully (with a warning)
  to the bundled CSV when `weather` isn't installed or no processed
  archive exists for the requested location/year — existing zero-optional-
  dependency behaviour is unchanged. New `weather_provider` (default
  `"era5-land"`) and `use_provided_weather` attributes.
- Location-keyed weather cache (`buem.config.weather_cache`), replacing
  the old single-global-feather-cache assumption; `parallelization/
  parallel_run.py`'s pre-warm step now pre-fetches every distinct
  `(lat, lon, year, provider)` across a batch before forking workers.
- `BUEM_WEATHER_DATA_DIR` env var — points at the `weather` package's own
  pre-processed provider archive root.
- `.github/workflows/ci.yml` — conda-based CI (lint, type check, tests
  with coverage, CLI smoke test), matching the convention already used by
  `UU-BUEM/occupancy` and `UU-BUEM/weather`.

### Changed

- **Restructured environment/container files under `infrastructure/`**
  (`infrastructure/env/buem_env.yml`, `infrastructure/container/
  {Dockerfile,docker-compose.yml,entrypoint.sh}`), replacing the old flat
  root-level `environment.yml`/`environment_docker.yml`/`Dockerfile`/
  `docker-compose.yml`, matching `UU-BUEM/occupancy` and `UU-BUEM/weather`'s
  layout. `environment.yml`/`environment_docker.yml` are merged into one
  file (sibling convention). `setup.ps1`/`setup.bat` updated accordingly,
  and both gained an `env-update` command that creates-or-updates the
  conda env from `infrastructure/env/buem_env.yml` — the mechanism that
  keeps `occupancy`/`weather` current (see below).
- `occupancy`/`weather` are now installed via a direct git reference
  (PEP 508 direct URL — neither is published to PyPI/conda yet) in both
  `infrastructure/env/buem_env.yml` and `pyproject.toml`'s optional
  dependencies, tracked at `@main` rather than a pinned tag, so `setup.ps1
  env-update` / `conda env update ... --prune` always pulls in their
  latest pushed changes. Pin to a specific released tag instead if you
  need a reproducible, non-moving environment.

## [1.1] - 2026-06-25

### Changed

- **Major refactoring**: `occupancy`, `weather`, and `technology` removed
  as internal submodules (`src/buem/occupancy/`, `src/buem/weather/`,
  `src/buem/technology/`, ~8,800 deleted lines) and split into independent
  repos within the UU-BUEM organisation (`UU-BUEM/occupancy`,
  `UU-BUEM/weather`). `cfg_attribute.py`/`attribute_builder.py` switched to
  optional `try/except` imports; weather CSV loading was kept inline
  (pvlib DISC reconstruction) so buem has no hard dependency on the
  external `weather` package for basic operation. `pyproject.toml` gained
  `occupancy`/`weather` optional-dependency extras and dropped
  weather-pipeline-only deps (`cfgrib`, `dask`, `eccodes`, `netcdf4`,
  `pyproj`, `sympy`, `xarray`). The `buem weather` CLI subcommand was
  removed (the pipeline it drove now lives in `UU-BUEM/weather`).
- Building module: added `F_red_htr` (intermittent heating reduction,
  ISO 13790 §13.2.2) and `b_transmission` to `model_buem.py`/
  `cfg_attribute.py`.

## [1.0.2] - 2026-04-14

### Added

- **Weather — Documentation**: Dedicated `docs/source/modules/weather/`
  subsection with pages for pipeline steps, grid and projections (rotated
  pole vs WGS84), container deployment, CLI reference, and CSV weather data.
- `CHANGELOG.md` at project root following Keep a Changelog format.

### Changed

- Weather version history in `docs/source/modules/weather/index.rst` now
  links to `CHANGELOG.md` instead of duplicating entries.

## [1.0.1] - 2026-04-14

### Added

- **Weather — Container deployment**: Deps-only container strategy for
  Apptainer (HPC) and Docker (VMs).  Source code is bind-mounted at runtime;
  image rebuild only needed when `weather_env.yml` changes.
- **Weather — Monthly output naming**: Output files are named by month
  (`COSMO_REA6_2018_Jan.nc`) or month range (`COSMO_REA6_2018_Jan-Mar.nc`).
  Full-year runs produce `COSMO_REA6_2018.nc`.
- **Weather — Cleanup flag**: `--cleanup` option removes downloaded and
  decompressed intermediate files after a successful export.
- **Weather — Documentation**: Dedicated `docs/source/modules/weather/`
  section covering the pipeline, grid projections, containerisation, and
  CLI reference.

### Changed

- `weather.def` and `Dockerfile.weather` no longer bake source code into
  the image (deps-only).
- `run_pipeline_container.sh` bind-mounts `~/buem/src` into the container
  at `/app/src`.
- `build_container.sh` header updated for deps-only workflow.

## [1.0.0] - 2026-04-10

### Added

- **Weather module** (`buem.weather`): End-to-end COSMO-REA6 processing
  pipeline — download, decompress, transform, and export to NetCDF-4.
- Five raw attributes: SWDIFDS_RAD, SWDIRS_RAD, T_2M, U_10M, V_10M.
- Four derived fields: GHI, DHI, T (°C), WS_10M.
- Dask threaded scheduler with `time=168` chunking for memory-safe
  processing on HPC (16 cores, 28 GiB).
- CLI via `buem weather run/info/validate` and `python -m buem.weather`.
- Shell scripts for non-container SLURM jobs (`common.sh`, `run_pipeline.sh`).
- `weather_env.yml` conda environment specification.
- `CsvWeatherData.reconstruct_dni_from_ghi()` — pvlib DISC-based DNI
  reconstruction replacing the divergent `(GHI-DHI)/cos(θ)` formula.

[Unreleased]: https://github.com/UU-BUEM/buem/compare/v3.1.0...HEAD
[3.1.0]: https://github.com/UU-BUEM/buem/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/UU-BUEM/buem/compare/v2.0.1...v3.0.0
[2.0.1]: https://github.com/UU-BUEM/buem/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/UU-BUEM/buem/compare/v1.2.1...v2.0.0
[1.2.1]: https://github.com/UU-BUEM/buem/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/UU-BUEM/buem/compare/v1.1...v1.2.0
[1.1]: https://github.com/UU-BUEM/buem/compare/v1.0.2...v1.1
[1.0.2]: https://github.com/UU-BUEM/buem/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/UU-BUEM/buem/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/UU-BUEM/buem/releases/tag/v1.0.0
