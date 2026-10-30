Docker Installation
===================

Running BuEM in Docker containers for production deployment and development.

Quick Start with Docker Compose
-------------------------------

.. code-block:: bash

    # Clone repository
    git clone https://github.com/UU-BUEM/buem.git
    cd buem
    
    # Start BuEM services (builds automatically)
    docker compose up
    
    # Test the API (in another terminal)
    curl http://localhost:5000/api/health

.. note::
   Docker Compose will automatically build the image if it doesn't exist, 
   so no separate build step is required.

Docker Compose Configuration
----------------------------

The included ``docker-compose.yml`` provides a complete setup:

.. code-block:: yaml

    version: '3.8'
    services:
      api:
        build: .
        ports:
          - "5000:5000"
        environment:
          - PYTHONPATH=/app
          - BUEM_WEATHER_DIR=/app/data/weather
        volumes:
          - ./src/buem/data:/app/data:ro
          - ./logs:/app/logs
        healthcheck:
          test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
          interval: 30s
          timeout: 10s
          retries: 3

Manual Docker Build
-------------------

Building the Docker image manually:

.. code-block:: bash

    # Build image
    docker build -t buem:latest .
    
    # Run container
    docker run -d \
        --name buem-api \
        -p 5000:5000 \
        -v $(pwd)/src/buem/data:/app/data:ro \
        buem:latest
    
    # View logs
    docker logs buem-api

Development Container
---------------------

For development with volume mounting:

.. code-block:: bash

    # Development mode with live reloading
    docker run -d \
        --name buem-dev \
        -p 5000:5000 \
        -v $(pwd)/src:/app/src \
        -v $(pwd)/data:/app/data \
        -e FLASK_ENV=development \
        -e FLASK_DEBUG=1 \
        buem:latest

Custom Configuration
--------------------

**Environment Variables:**

- ``BUEM_WEATHER_DIR``: Path to weather data directory
- ``BUEM_API_PORT``: API server port (default: 5000)
- ``BUEM_LOG_LEVEL``: Logging level (DEBUG, INFO, WARNING, ERROR)
- ``BUEM_CONFIG_FILE``: Custom configuration file path

**Volume Mounts:**

.. code-block:: bash

    # Required data volumes
    -v /path/to/weather/data:/app/data/weather:ro
    
    # Optional configuration override
    -v /path/to/config:/app/config:ro
    
    # Persistent logs
    -v /path/to/logs:/app/logs

Production Deployment
---------------------

For production environments:

.. code-block:: yaml

    version: '3.8'
    services:
      api:
        image: buem:production
        restart: unless-stopped
        ports:
          - "5000:5000"
        environment:
          - BUEM_LOG_LEVEL=WARNING
          - GUNICORN_WORKERS=4
        volumes:
          - weather-data:/app/data/weather:ro
          - ./logs:/app/logs
        deploy:
          replicas: 3
          resources:
            limits:
              cpus: '0.5'
              memory: 512M

For complete deployment examples, see :doc:`../deployment/production_deployment`.