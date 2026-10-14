Response Format
===============

BuEM returns a GeoJSON FeatureCollection, echoing the full request
structure and appending ``thermal_load_profile``/``model_metadata`` to
each feature's ``properties.buem`` node — per
``src/buem/integration/json_schema/response_schema.json`` (see
:doc:`request_format` for the shared measurement-quantity/contract
conventions).

Top-Level Response
-------------------

.. code-block:: javascript

   {
     "type": "FeatureCollection",
     "processed_at": "2024-02-24T12:00:03Z",
     "processing_elapsed_s": 3.12,
     "metadata": {
       "total_features": 1,
       "successful_features": 1,
       "failed_features": 0,
       "validation_warnings": 0
     },
     "features": [/* ... */]
   }

Per-Feature ``buem`` node
----------------------------

- ``building`` — echoed from the request (envelope + thermal as nested
  objects)
- ``solver`` — echoed from the request
- ``thermal_load_profile`` — computed results (below)
- ``model_metadata`` — diagnostics about the run (below)

thermal_load_profile.summary
-------------------------------

Each of ``heating``/``cooling``/``electricity`` carries the same
statistical shape over the simulation period — every value is a
``{value, unit}`` measurement quantity, not a bare number:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Field
     - Description
   * - ``total``
     - Total energy over the period (kWh)
   * - ``max`` / ``min`` / ``mean`` / ``median`` / ``std``
     - Instantaneous power statistics (kW)

Plus, at the ``summary`` level: ``total_energy_demand`` (heating +
cooling + electricity), ``peak_heating_load``, ``peak_cooling_load``, and
``energy_intensity`` (total energy demand per ``A_ref``, kWh/m²).

Internally the solver produces a single signed ``Q_HC`` series — positive
for heating, negative for cooling; the API reports cooling as an absolute
value in ``summary.cooling``.

Timeseries (optional)
------------------------

When the request is processed with ``?include_timeseries=true``,
``thermal_load_profile.timeseries`` carries the full hourly arrays
(``timestamps``, ``heating``, ``cooling``, ``electricity``, all sharing
one ``unit``, default ``kW``); otherwise
``thermal_load_profile.timeseries_file`` gives a download path for a
server-side-saved file instead of inlining 8760 points per building. See
:doc:`api_endpoints` for the file-download endpoint.

model_metadata
----------------

Diagnostics, not physics: ``model_version`` and ``model_commit`` (the
release and commit that computed the result, as on ``/api/health``),
``solver_used`` (e.g.
``scipy-sparse``, ``OSQP``, ``CBC``), ``processing_time``,
``weather_year``, ``parallel_thermal``, ``use_chunked_processing``,
``validation_warnings``.

Feature-Level Errors
-----------------------

If a building fails validation or execution, that feature is counted in
``metadata.failed_features`` and its ``properties.buem`` carries an error
description instead of ``thermal_load_profile`` — see
:doc:`error_handling` for the error envelope shape.

Complete example
-------------------

See ``src/buem/integration/json_schema/example_response.json`` in the
repository for a full worked response.
