Error Handling
==============

All API errors return a consistent JSON envelope:

.. code-block:: json

   {
     "error": {
       "code": "ERROR_TYPE",
       "message": "Human-readable description",
       "details": ["..."]
     }
   }

HTTP Status Codes
-----------------

.. list-table::
   :header-rows: 1
   :widths: 10 90

   * - Code
     - Meaning
   * - 400
     - Invalid JSON or malformed GeoJSON
   * - 404
     - File or endpoint not found
   * - 422
     - Valid JSON but building attributes fail validation
   * - 500
     - Model execution error or unexpected failure

Error Categories
----------------

``VALIDATION_ERROR``
  Missing or out-of-range building attributes
  (e.g. negative U-value, missing ``components``).

``GEOJSON_ERROR``
  Structural problems with the GeoJSON payload
  (e.g. missing ``type``, ``features`` not an array).

``MODEL_ERROR``
  Thermal solver failure
  (e.g. LP infeasible, weather file missing).

``FILE_ERROR``
  Requested timeseries file not found on disk.

Debugging Checklist
-------------------

1. Validate JSON syntax and GeoJSON structure before sending.
2. Ensure all component U-values are positive and areas > 0.
3. Check that total window area does not exceed its parent wall area.
4. Verify ``BUEM_WEATHER_DIR`` is set and the CSV file exists.
5. Review container logs: ``docker compose -f infrastructure/container/docker-compose.yml logs api``.
6. Verify file permissions for results directory.

Error Recovery Strategies
-------------------------

**Retry Logic**

For transient server errors (5xx), implement exponential backoff:

.. code-block:: python

    import time
    import requests

    def api_call_with_retry(url, data, max_retries=3):
        for attempt in range(max_retries):
            try:
                response = requests.post(url, json=data)
                if response.status_code < 500:
                    return response
                time.sleep(2 ** attempt)
            except requests.RequestException:
                time.sleep(2 ** attempt)
        return None

**Batch Processing Recovery**

For large batches, process features individually on errors:

.. code-block:: python

    def process_building_batch(buildings):
        successful = []
        failed = []
        
        # Try batch first
        try:
            response = api_call({"type": "FeatureCollection", "features": buildings})
            return response
        except Exception:
            # Fall back to individual processing
            for building in buildings:
                try:
                    result = api_call({"type": "Feature", **building})
                    successful.append(result)
                except Exception as e:
                    failed.append({"building": building["id"], "error": str(e)})
        
        return {"successful": successful, "failed": failed}

**Validation Pre-check**

Validate requests locally before sending to API:

.. code-block:: python

    def validate_building_request(feature):
        errors = []
        
        # Check required structure
        if "properties" not in feature:
            errors.append("Missing properties")
        
        attrs = feature.get("properties", {}).get("buem", {}).get("building_attributes", {})
        
        # Check required attributes -- matches AttributeBuilder.REQUIRED_FROM_CALLER
        # server-side (src/buem/integration/scripts/attribute_builder.py):
        # these have no safe generic default and raise if omitted, unlike
        # thermal/comfort/ventilation settings which fall back to sensible
        # config defaults.
        required = ["latitude", "longitude", "A_ref", "components"]
        for field in required:
            if field not in attrs:
                errors.append(f"Missing required field: {field}")
        
        # Check numeric ranges
        if "latitude" in attrs:
            lat = attrs["latitude"]
            if not -90 <= lat <= 90:
                errors.append(f"Invalid latitude: {lat}")
        
        return errors

Production Considerations
-------------------------

**Logging and Monitoring**

- Log all API responses with status codes
- Monitor error rates and patterns  
- Set up alerts for high error rates or specific error types
- Track processing times for performance monitoring

**Error Notification**

.. code-block:: python

    def handle_api_error(response):
        if response.status_code >= 400:
            error_data = response.json().get("error", {})
            
            # Log structured error data
            logger.error("BuEM API Error", extra={
                "error_code": error_data.get("code"),
                "message": error_data.get("message"),
                "details": error_data.get("details"),
                "status_code": response.status_code
            })
            
            # Send alerts for critical errors
            if response.status_code >= 500:
                send_alert(f"BuEM API server error: {error_data.get('message')}")

Next Steps
----------

Continue to :doc:`examples` for complete integration examples showing error handling in practice.