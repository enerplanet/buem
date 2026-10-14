import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from flask_swagger_ui import get_swaggerui_blueprint

from buem import __version__
from buem.apis.files_api import bp as files_bp
from buem.apis.model_api import bp as model_bp
from buem.env import load_env

# load .env and apply defaults (no-op if already done)
load_env()

# prefer env var; fallback to project-local path for Windows
DEFAULT_LOG = Path(__file__).resolve().parents[2] / "logs" / "buem_api.log"
LOG_FILE = Path(os.environ.get("BUEM_LOG_FILE") or DEFAULT_LOG)

# Swagger UI configuration
SWAGGER_URL = "/api/docs"
OPENAPI_URL = "/api/openapi.yaml"
OPENAPI_DIR = Path(__file__).resolve().parent

def create_app():
    app = Flask(__name__)
    app.register_blueprint(model_bp)
    app.register_blueprint(files_bp)  # register files endpoint

    # Serve openapi.yaml and register Swagger UI
    @app.route(OPENAPI_URL)
    def openapi_spec():
        return send_from_directory(str(OPENAPI_DIR), "openapi.yaml", mimetype="text/yaml")

    swagger_bp = get_swaggerui_blueprint(
        SWAGGER_URL,
        OPENAPI_URL,
        config={"app_name": "BuEM API"},
    )
    app.register_blueprint(swagger_bp, url_prefix=SWAGGER_URL)

    # centralized logging - rotates to limit disk usage
    logdir = LOG_FILE.parent
    if not logdir.exists():
        logdir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)

    # set app logger to DEBUG in dev; production can override via env
    app.logger.setLevel(logging.DEBUG)
    app.logger.addHandler(handler)

    # configure root logger and Werkzeug (HTTP request) logger to use same handler
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    logging.basicConfig(level=logging.DEBUG)
    # avoid duplicate handlers if already attached
    if handler not in root_logger.handlers:
        root_logger.addHandler(handler)

    werk_logger = logging.getLogger('werkzeug')
    werk_logger.setLevel(logging.DEBUG)
    if handler not in werk_logger.handlers:
        werk_logger.addHandler(handler)

    # The image sets BUEM_VERSION and BUEM_COMMIT at build time: it carries
    # neither .git nor _version.py, so __version__ there is only the fallback.
    version = os.environ.get("BUEM_VERSION") or __version__
    commit = os.environ.get("BUEM_COMMIT") or "unknown"

    @app.after_request
    def stamp_version(response):
        response.headers["X-BuEM-Version"] = version
        response.headers["X-BuEM-Commit"] = commit
        return response

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "version": version, "commit": commit}), 200

    app.logger.info(f"BUEM API starting, log: {LOG_FILE}")
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)