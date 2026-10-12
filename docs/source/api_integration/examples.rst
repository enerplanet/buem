Integration Examples
====================

Starting the Server
-------------------

.. code-block:: bash

    # Conda
    conda activate buem_env
    python -m buem.apis.api_server

    # Docker
    docker compose up

Health Check
------------

.. code-block:: bash

    curl http://localhost:5000/api/health

Process a GeoJSON File
----------------------

.. code-block:: bash

    curl -X POST http://localhost:5000/api/process \
       -H "Content-Type: application/json" \
       -d @src/buem/integration/sample_request_template.geojson

To include hourly time-series in the response:

.. code-block:: bash

    curl -X POST "http://localhost:5000/api/run?include_timeseries=true" \
       -H "Content-Type: application/json" \
       --data-binary @payload.json

Python Helper
-------------

BuEM ships a convenience script for submitting requests:

.. code-block:: bash

    python -m buem.integration.send_geojson \
        src/buem/integration/sample_request_template.geojson \
        --include-timeseries

Minimal Python Client
---------------------

.. code-block:: python

    import requests

    # v3 request shape -- see request_format.rst. Location comes from
    # geometry.coordinates only; every quantity is {value, unit}.
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "id": "B001",
            "geometry": {"type": "Point", "coordinates": [5.0, 52.0]},
            "properties": {
                "start_time": "2018-01-01T00:00:00Z",
                "end_time": "2018-12-31T23:00:00Z",
                "buem": {
                    "building": {
                        "A_ref": {"value": 100.0, "unit": "m2"},
                        "envelope": {
                            "elements": [
                                {
                                    "id": "W1", "type": "wall",
                                    "area": {"value": 80, "unit": "m2"},
                                    "azimuth": {"value": 180, "unit": "deg"},
                                    "tilt": {"value": 90, "unit": "deg"},
                                    "U": {"value": 1.5, "unit": "W/(m2K)"},
                                },
                                {
                                    "id": "V1", "type": "ventilation",
                                    "air_changes": {"value": 0.5, "unit": "1/h"},
                                },
                            ]
                        },
                    }
                },
            },
        }],
    }

    resp = requests.post("http://localhost:5000/api/process", json=payload, timeout=60)
    resp.raise_for_status()
    result = resp.json()

    summary = result["features"][0]["properties"]["buem"]["thermal_load_profile"]["summary"]
    print(f"Heating: {summary['heating']['total']['value']:.0f} kWh/yr")
    print(f"Cooling: {summary['cooling']['total']['value']:.0f} kWh/yr")

Downloading Time-Series Files
-----------------------------

When ``include_timeseries=true``, the response contains a ``timeseries_file`` path.
Retrieve it with:

.. code-block:: bash

    curl -O http://localhost:5000/api/files/buem_ts_<hash>.json.gz

Result Forwarding
-----------------

Add ``forward_url`` to the request payload to have BuEM POST results to an
external endpoint automatically:

.. code-block:: json

    {
        "forward_url": "https://example.com/receiver",
        "include_timeseries": false
    }

Environment Variables
---------------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Description
   * - ``BUEM_WEATHER_DIR``
     - Path to weather CSV directory
   * - ``BUEM_CBC_EXE``
     - Path to CBC solver binary (MILP only)
   * - ``BUEM_RESULTS_DIR``
     - Directory for saved result files (auto-created on load if missing)
   * - ``BUEM_LOG_DIR``
     - Log directory (auto-created on load if missing)
   * - ``BUEM_LOG_FILE``
     - Log file path (default: ``logs/buem_api.log``)
