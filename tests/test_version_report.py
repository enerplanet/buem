"""The API reports the BuEM release and commit on /api/health and as headers
on every response, so a stored result records the version that computed it."""
from buem.apis.api_server import create_app


def test_health_and_headers_carry_the_build_version(monkeypatch):
    monkeypatch.setenv("BUEM_VERSION", "6.9.0")
    monkeypatch.setenv("BUEM_COMMIT", "abc1234")
    client = create_app().test_client()

    health = client.get("/api/health")
    assert health.get_json() == {"status": "ok", "version": "6.9.0", "commit": "abc1234"}

    other = client.get("/api/openapi.yaml")
    assert other.headers["X-BuEM-Version"] == "6.9.0"
    assert other.headers["X-BuEM-Commit"] == "abc1234"


def test_version_falls_back_to_the_package_version(monkeypatch):
    from buem import __version__

    monkeypatch.delenv("BUEM_VERSION", raising=False)
    monkeypatch.delenv("BUEM_COMMIT", raising=False)
    body = create_app().test_client().get("/api/health").get_json()
    assert body["version"] == __version__
    assert body["commit"] == "unknown"


def test_each_result_records_the_build_version(monkeypatch):
    from test_period_window import _occupancy_only_feature

    from buem.integration.scripts.geojson_processor import GeoJsonProcessor

    monkeypatch.setenv("BUEM_WEATHER_FALLBACK", "false")
    monkeypatch.setenv("BUEM_VERSION", "6.9.0")
    monkeypatch.setenv("BUEM_COMMIT", "abc1234")
    feature, result = _occupancy_only_feature("2018-01-01T00:00:00Z", "2018-01-01T23:00:00Z")
    out = GeoJsonProcessor(payload={})._process_single_feature(feature, result)
    metadata = out["properties"]["buem"]["model_metadata"]
    assert metadata["model_version"] == "6.9.0"
    assert metadata["model_commit"] == "abc1234"
