Docker Setup
============

Quick Start
-----------

.. code-block:: bash

   git clone https://github.com/UU-BUEM/buem.git
   cd buem
   docker compose up

The API is now available at ``http://localhost:5000``.  Test it:

.. code-block:: bash

   curl http://localhost:5000/api/health

Environment Variables
---------------------

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Variable
     - Default
     - Description
   * - ``BUEM_WEATHER_DIR``
     - ``/app/data/weather``
     - Weather CSV directory (mounted read-only)
   * - ``BUEM_RESULTS_DIR``
     - ``/app/results``
     - Output directory for timeseries files
   * - ``BUEM_CBC_EXE``
     - ``/opt/conda/envs/buem_env/bin/cbc``
     - Path to the CBC solver binary (used by MILP path)

Volume Mounts
-------------

The provided ``docker-compose.yml`` mounts:

- ``./src/buem/data/weather`` → ``/app/data/weather`` (read-only)
- ``./src/buem/logs`` → ``/app/logs``
- ``./results`` → ``/app/results``

Manual Build
------------

.. code-block:: bash

   docker build -t buem:latest .
   docker run -d --name buem-api \
     -p 5000:5000 \
     -v $(pwd)/src/buem/data/weather:/app/data/weather:ro \
     -v $(pwd)/results:/app/results \
     buem:latest

Health Check
------------

The Compose file includes a health check that calls ``/api/health`` every
30 s.  Monitor with:

.. code-block:: bash

   docker compose ps
   docker compose logs api

.. code-block:: yaml

    services:
      buem:
        # ... buem configuration
        deploy:
          replicas: 3
      
      nginx:
        image: nginx:alpine
        ports:
          - "80:80"
        volumes:
          - ./nginx.conf:/etc/nginx/nginx.conf

Troubleshooting
---------------

**Common Issues**

1. **Weather data not found**: Ensure proper volume mounting and file permissions
2. **Port conflicts**: Check that port 5000 is available
3. **Memory issues**: Increase container memory limits for large building datasets

**Debug Mode**

Run container in debug mode:

.. code-block:: bash

    docker run -it --rm \\
      -p 5000:5000 \\
      -e FLASK_ENV=development \\
      buem:latest

Next Steps
----------

Once your container is running, proceed to :doc:`api_endpoints` to learn about the available API endpoints.