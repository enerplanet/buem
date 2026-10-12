Request Data Format
===================

BuEM accepts building data in GeoJSON format, per the **EnerPlanET API
contract** (``src/buem/integration/json_schema/request_schema.json``, a
pinned copy of ``enerplanet/buem-gateway``'s ``schemas/v5/`` — see
:doc:`index` for how this schema is governed). This section describes
the current (contract v5) request shape.

.. note::
   This is a contract owned by ``enerplanet/buem-gateway``, not a schema
   buem defines or changes unilaterally — see
   ``src/buem/integration/json_schema/README.md`` for the pinning/re-sync
   procedure, and the repo root ``CLAUDE.md``'s "Guardrails" section for
   how contract changes are proposed.

GeoJSON Structure
------------------

**Top level**

.. code-block:: javascript

    {
      "type": "FeatureCollection",
      "timeStamp": "2024-02-24T12:00:00Z",
      "features": [/* ... */]
    }

**Feature structure** — location is sourced *exclusively* from
``geometry.coordinates`` (not from a separate latitude/longitude field):

.. code-block:: javascript

    {
      "type": "Feature",
      "id": "building_001",
      "geometry": {
        "type": "Point",
        "coordinates": [5, 52]
      },
      "properties": {
        "start_time": "2018-01-01T00:00:00Z",
        "end_time": "2019-01-01T00:00:00Z",
        "resolution": "60",
        "resolution_unit": "minutes",
        "buem": {
          "building": {/* ... */},
          "solver": {/* ... */}
        }
      }
    }

``start_time`` and ``end_time`` bound the simulation period, start inclusive
and end exclusive, so a full year runs from 1 January 00:00 to the next
1 January 00:00. A request that selects neither heating nor cooling
(see ``buem.outputs``) returns exactly that period; a request that solves
the thermal model returns the whole weather year.

Measurement values
--------------------

Every measurable quantity is an object with an explicit unit, not a bare
number:

.. code-block:: json

    { "value": 100, "unit": "m2" }

SI units are assumed when ``unit`` is omitted. Dimensionless ratios use
``unit: "-"``.

``building`` node
-------------------

Classification fields sit directly on ``building``; geometry and thermal
performance are in nested ``envelope``/``thermal`` objects.

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Field
     - Type
     - Description
   * - ``A_ref``
     - area quantity
     - Reference floor area — derived from floor elements if omitted
   * - ``h_room``
     - length quantity
     - Average room height
   * - ``building_type``
     - string
     - TABULA residential size class (``SFH``/``MFH``/``TH``/``AB``) *or*
       an occupancy service-building type id (``supermarket``/``office``/
       ``restaurant``/``school``/``hotel``/``bakery``/``warehouse``/
       ``clinic``) — see :doc:`../modules/occupancy`. Currently free-text
       in the agreed v3 contract (no enum yet).
   * - ``neighbour_status``
     - enum
     - ``B_Alone`` / ``B_N1`` / ``B_N2`` — attached-neighbour count
   * - ``envelope``
     - object
     - See below
   * - ``thermal``
     - object
     - Building-wide thermal parameters (see below); all fields optional

``envelope.elements[]``
-------------------------

A single flat list — no more separate ``Walls``/``Roof``/``Windows``
component groups. Every surface (including windows/doors) is one entry
with both geometry and thermal properties together:

.. code-block:: json

    {
      "id": "Wall_1",
      "type": "wall",
      "area": { "value": 30, "unit": "m2" },
      "azimuth": { "value": 0, "unit": "deg" },
      "tilt": { "value": 90, "unit": "deg" },
      "U": { "value": 1.6, "unit": "W/(m2K)" },
      "b_transmission": { "value": 1, "unit": "-" }
    }

``type`` is one of ``wall``/``roof``/``floor``/``window``/``door``/
``ventilation``. Windows and doors additionally require ``parent_id``
(the wall/roof they're embedded in) and, for windows, ``g_gl`` (solar
heat gain coefficient). Ventilation elements only need ``air_changes``.
``area``/``azimuth``/``tilt`` are required for every non-ventilation type.

``thermal`` node
-------------------

All optional (model defaults apply when omitted): ``n_air_infiltration``,
``n_air_use``, ``c_m``, ``thermal_class`` (``light``/``medium``/``heavy``),
``comfortT_lb``, ``comfortT_ub``, ``design_T_min``, ``F_sh_hor``,
``F_sh_vert``, ``F_f``, ``F_w``, ``phi_int``, ``q_w_nd``, ``F_red_htr``.
See :doc:`../modules/thermal` for what each parameter does physically.

``solver`` node
------------------

Execution settings, not physics: ``use_milp`` (default ``false``),
``parallel_thermal`` (default ``true``), ``use_chunked_processing``
(default ``true``).

Complete example
-------------------

See ``src/buem/integration/json_schema/example_request.json`` in the
repository for a full worked single-building request.

Next Steps
----------

Continue to :doc:`response_format` to understand the output data format.
