"""
The /api blueprint - every endpoint the web page and scripts use.

Each print endpoint follows the same path:
    look up printer + label (404) -> render an image (400)
    -> ?preview=1: printer.prepare() and return the PNG, no lock
    -> otherwise: take the printer's lock (409), prepare, print (503/500)

Errors are JSON: {"error": "<kind>", "detail": "<human-readable>"}.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, send_file
from PIL import Image, UnidentifiedImageError

from labelpi.app import AppState
from labelpi.config import LabelConfig
from labelpi.printers import Printer, PrinterBusy, PrinterError, PrinterUnavailable
from labelpi.render import RenderError, expand_placeholders, fit_image, render_text

log = logging.getLogger(__name__)
api = Blueprint("api", __name__, url_prefix="/api")

# Refuse images that would decode to more pixels than this. A small PNG can
# expand to gigabytes in memory ("decompression bomb"); the Pi has 512 MB.
Image.MAX_IMAGE_PIXELS = 25_000_000
ALLOWED_FORMATS = {"PNG", "JPEG", "GIF", "BMP"}


class ApiError(Exception):
    """Raised inside a handler; turned into a JSON error response below."""

    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


@api.errorhandler(ApiError)
def _api_error(exc: ApiError):
    return jsonify(error=exc.error, detail=exc.detail), exc.status


def bad_request(detail: str) -> ApiError:
    return ApiError(400, "bad_request", detail)


def not_found(detail: str) -> ApiError:
    return ApiError(404, "not_found", detail)


def _state() -> AppState:
    return current_app.extensions["labelpi"]


def _now() -> datetime:
    """Current time for shortcuts. Tests replace it via app.config["LABELPI_NOW"]."""
    clock: Callable[[], datetime] = current_app.config.get("LABELPI_NOW", datetime.now)
    return clock()


# ---------------------------------------------------------------------------
# Read-only endpoints
# ---------------------------------------------------------------------------
@api.get("/printers")
def list_printers():
    registry = _state().registry
    return jsonify(
        [
            {
                "id": p.id,
                "name": p.display_name,
                "busy": registry.is_busy(p.id),
                "labels": [_label_json(label) for label in p.labels],
            }
            for p in registry.all()
        ]
    )


def _label_json(label: LabelConfig) -> dict[str, Any]:
    data: dict[str, Any] = {"id": label.id, "name": label.name, "continuous": label.continuous}
    if label.continuous:
        data["tape_width_mm"] = label.tape_width_mm
    else:
        data["width_mm"] = label.width_mm
        data["length_mm"] = label.length_mm
    return data


@api.get("/shortcuts")
def list_shortcuts():
    return jsonify([{"id": s.id, "name": s.name} for s in _state().config.shortcuts])


# ---------------------------------------------------------------------------
# Print endpoints
# ---------------------------------------------------------------------------
@api.post("/print/text")
def print_text():
    body = _json_body()
    printer, label = _printer_and_label(body.get("printer"), body.get("label"))
    text = _required_str(body, "text")
    image = _render(
        lambda: render_text(
            text,
            label,
            printer.config.dpi,
            align=_optional(body, "align", str, "center"),
            font_size=_optional(body, "font_size", int, None),
            length_mm=_optional(body, "length_mm", float, None),
        )
    )
    return _preview_or_print(printer, label, image)


@api.post("/print/shortcut")
def print_shortcut():
    body = _json_body()
    printer, label = _printer_and_label(body.get("printer"), body.get("label"))
    shortcut_id = _required_str(body, "shortcut")
    shortcut = _state().config.shortcut(shortcut_id)
    if shortcut is None:
        raise not_found(f'unknown shortcut "{shortcut_id}"')
    image = _render(
        lambda: render_text(expand_placeholders(shortcut.text, _now()), label, printer.config.dpi)
    )
    return _preview_or_print(printer, label, image)


@api.post("/print/image")
def print_image():
    form = request.form
    printer, label = _printer_and_label(form.get("printer"), form.get("label"))
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise bad_request('"file" is required (multipart/form-data upload)')
    source = _open_image(upload.stream)
    length_mm = form.get("length_mm")
    try:
        length = float(length_mm) if length_mm not in (None, "") else None
    except ValueError:
        raise bad_request(f'"length_mm" must be a number (got {length_mm!r})') from None
    image = _render(
        lambda: fit_image(
            source,
            label,
            printer.config.dpi,
            dither=_form_bool(form, "dither"),
            invert=_form_bool(form, "invert"),
            length_mm=length,
        )
    )
    return _preview_or_print(printer, label, image)


def _preview_or_print(printer: Printer, label: LabelConfig, image: Image.Image) -> Response:
    if _is_preview():
        # Preview shows exactly what would be sent, after the backend's
        # prepare(). It doesn't take the lock: previews work while printing.
        return _png_response(printer.prepare(image, label))

    started = time.monotonic()
    try:
        with _state().registry.claim(printer.id):
            printer.print(printer.prepare(image, label), label)
    except PrinterBusy:
        raise ApiError(409, "busy", f"{printer.id} is printing") from None
    except PrinterUnavailable as exc:
        log.warning("printer %s unavailable: %s", printer.id, exc)
        raise ApiError(503, "unavailable", str(exc) or f"{printer.id} is not reachable") from None
    except PrinterError as exc:
        log.error("printer %s error: %s", printer.id, exc)
        raise ApiError(500, "printer_error", str(exc)) from None
    except Exception as exc:  # anything unexpected: log the traceback, answer 500
        log.exception("unexpected failure printing on %s", printer.id)
        raise ApiError(500, "printer_error", f"unexpected error: {exc}") from None
    ms = round((time.monotonic() - started) * 1000)
    log.info("printed on %s (%s) in %d ms", printer.id, label.id, ms)
    return jsonify(status="printed", printer=printer.id, ms=ms)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _is_preview() -> bool:
    return request.args.get("preview", "").lower() in ("1", "true", "yes")


def _png_response(image: Image.Image) -> Response:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", max_age=0)


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise bad_request("body must be a JSON object (Content-Type: application/json)")
    return body


def _printer_and_label(printer_id: Any, label_id: Any) -> tuple[Printer, LabelConfig]:
    if not isinstance(printer_id, str) or not printer_id:
        raise bad_request('"printer" is required')
    if not isinstance(label_id, str) or not label_id:
        raise bad_request('"label" is required')
    printer = _state().registry.get(printer_id)
    if printer is None:
        raise not_found(f'unknown printer "{printer_id}"')
    label = printer.config.label(label_id)
    if label is None:
        raise not_found(f'printer "{printer_id}" has no label "{label_id}"')
    return printer, label


def _required_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise bad_request(f'"{key}" is required and must be a string')
    return value


def _optional(body: dict[str, Any], key: str, kind: type, default: Any) -> Any:
    """Optional JSON field; null/missing -> default. Ints are fine where floats are wanted."""
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool):  # bool is a kind of int in Python; never accept it here
        raise bad_request(f'"{key}" must be a {kind.__name__}')
    if kind is float and isinstance(value, int):
        return float(value)
    if not isinstance(value, kind):
        raise bad_request(f'"{key}" must be a {kind.__name__} (got {value!r})')
    return value


def _form_bool(form: Any, key: str) -> bool:
    value = (form.get(key) or "").strip().lower()
    if value in ("", "0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    raise bad_request(f'"{key}" must be true or false (got {value!r})')


def _open_image(stream: Any) -> Image.Image:
    """Decode the upload fully now, so broken files fail here as a 400."""
    try:
        image = Image.open(stream)
        if image.format not in ALLOWED_FORMATS:
            raise bad_request(f"unsupported image type {image.format}; use PNG, JPEG, GIF or BMP")
        image.load()
    except ApiError:
        raise
    except Image.DecompressionBombError:
        raise bad_request("image has too many pixels") from None
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise bad_request(f"not a readable image: {exc}") from None
    return image


def _render(make_image: Callable[[], Image.Image]) -> Image.Image:
    """Run a render function, turning RenderError (doesn't fit, empty...) into 400."""
    try:
        return make_image()
    except RenderError as exc:
        raise bad_request(str(exc)) from None
