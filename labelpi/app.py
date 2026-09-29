"""
Flask app factory.

`create_app()` loads the config, builds the printer registry and wires up the
/api blueprint. run.py serves it with waitress (production) or Flask's dev
server (--dev). Tests call create_app() directly with a temp config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from labelpi.assets import AssetStore
from labelpi.config import Config, load_config
from labelpi.icons import VENDOR_DIR
from labelpi.icons import styles as icon_styles
from labelpi.printers import Registry
from labelpi.settings import SettingsStore
from labelpi.starters import STARTER_TEMPLATES

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "printers.toml"


@dataclass
class AppState:
    """What the API needs from the app. Stored in app.extensions["labelpi"]."""

    config: Config
    registry: Registry
    settings: SettingsStore
    assets: AssetStore


def create_app(
    config_path: str | Path | None = None,
    out_dir: Path | None = None,
    settings_path: str | Path | None = None,
) -> Flask:
    """
    Build the app. Config path: the argument, else $LABELPI_CONFIG, else
    config/printers.toml. Settings saved from the page go to `settings_path`,
    else $LABELPI_SETTINGS, else settings.json next to the config. Uploaded
    images go to $LABELPI_ASSETS, else an assets/ folder next to settings.json.
    `out_dir` is where mock printers write PNGs. Raises ConfigError.
    """
    path = Path(config_path or os.environ.get("LABELPI_CONFIG") or DEFAULT_CONFIG)
    config = load_config(path)
    registry = Registry.from_config(config, out_dir=out_dir or Path("out"))
    settings_file = Path(
        settings_path or os.environ.get("LABELPI_SETTINGS") or path.with_name("settings.json")
    )
    assets = AssetStore(Path(os.environ.get("LABELPI_ASSETS") or settings_file.with_name("assets")))
    settings = SettingsStore(
        settings_file, seed_templates=_seed_templates(config), asset_exists=assets.exists
    )

    app = Flask(__name__)
    # Flask rejects bigger request bodies with 413 before we ever see them,
    # so a huge upload can't eat the Pi Zero's 512 MB.
    app.config["MAX_CONTENT_LENGTH"] = int(config.server.max_upload_mb * 1024 * 1024)
    app.extensions["labelpi"] = AppState(
        config=config, registry=registry, settings=settings, assets=assets
    )

    from labelpi.api import api  # imported here to keep module import order simple

    app.register_blueprint(api)
    _json_errors_for_api(app)

    # The web UI: one static page (labelpi/static/) that only talks to /api.
    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    # The icon fonts, so the page's icon picker shows the same icons that get
    # printed. Only the font files - not the rest of labelpi/vendor/.
    @app.get("/vendor/<library>/<filename>")
    def vendor_font(library: str, filename: str):
        if f"{library}/{filename}" not in {style.font for style in icon_styles()}:
            abort(404)
        return send_from_directory(VENDOR_DIR / library, filename, max_age=7 * 24 * 3600)

    return app


def _seed_templates(config: Config) -> list:
    """Starter templates, with [[templates]] from the config added or replacing
    a starter that has the same id."""
    by_id = {t.id: t for t in STARTER_TEMPLATES}
    for template in config.templates:
        by_id[template.id] = template
    return list(by_id.values())


def _json_errors_for_api(app: Flask) -> None:
    """Unknown /api URLs, wrong methods and too-large uploads answer in JSON,
    matching the API's other errors, instead of Flask's HTML pages."""
    names = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}

    @app.errorhandler(HTTPException)
    def handle(exc: HTTPException):
        if not request.path.startswith("/api"):
            return exc  # let Flask render its normal page
        error = names.get(exc.code, "bad_request" if (exc.code or 500) < 500 else "server_error")
        return jsonify(error=error, detail=exc.description), exc.code
