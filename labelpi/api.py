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

from labelpi import icons
from labelpi.app import AppState
from labelpi.assets import AssetError
from labelpi.config import LabelConfig
from labelpi.layout import normalise_layout, render_layout
from labelpi.printers import Printer, PrinterBusy, PrinterError, PrinterUnavailable
from labelpi.render import RenderError, fit_image, render_ruler, render_text
from labelpi.templates import Template, TemplateError, fill_template

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
    """Current time for templates. Tests replace it via app.config["LABELPI_NOW"]."""
    clock: Callable[[], datetime] = current_app.config.get("LABELPI_NOW", datetime.now)
    return clock()


# ---------------------------------------------------------------------------
# Read-only endpoints
# ---------------------------------------------------------------------------
@api.get("/printers")
def list_printers():
    return jsonify([_printer_json(p) for p in _state().registry.all()])


def _printer_json(printer: Printer) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": printer.id,
        "name": printer.display_name,
        "busy": _state().registry.is_busy(printer.id),
        "labels": [_label_json(label) for label in printer.labels],
        "can_chain": printer.can_chain,
    }
    if printer.can_chain:
        data["auto_feed"] = _state().settings.auto_feed(printer.id)
    return data


@api.put("/printers/<printer_id>/settings")
def update_printer_settings(printer_id: str):
    """{"auto_feed": false} -> chain labels (tape printers only). Saved in settings.json."""
    printer = _printer(printer_id)
    body = _json_body()
    if set(body) - {"auto_feed"}:
        raise bad_request('only "auto_feed" can be changed')
    if "auto_feed" in body:
        if not isinstance(body["auto_feed"], bool):
            raise bad_request('"auto_feed" must be true or false')
        if not printer.can_chain:
            raise bad_request(f"{printer.display_name} always feeds each label out")
        _state().settings.set_auto_feed(printer.id, body["auto_feed"])
    return jsonify(_printer_json(printer))


@api.post("/printers/<printer_id>/feed")
def feed(printer_id: str):
    """Feed the tape out to the cutter - after printing labels back to back."""
    printer = _printer(printer_id)
    if not printer.can_chain:
        raise bad_request(f"{printer.display_name} has nothing to feed")
    ms = _on_printer(printer, printer.feed)
    log.info("fed %s in %d ms", printer.id, ms)
    return jsonify(status="fed", printer=printer.id, ms=ms)


def _printer(printer_id: str) -> Printer:
    printer = _state().registry.get(printer_id)
    if printer is None:
        raise not_found(f'unknown printer "{printer_id}"')
    return printer


def _label_json(label: LabelConfig) -> dict[str, Any]:
    data: dict[str, Any] = {"id": label.id, "name": label.name, "continuous": label.continuous}
    if label.continuous:
        data["tape_width_mm"] = label.tape_width_mm
    else:
        data["width_mm"] = label.width_mm
        data["length_mm"] = label.length_mm
    return data


# ---------------------------------------------------------------------------
# Templates: list, create, edit, delete (saved in settings.json)
# ---------------------------------------------------------------------------
@api.get("/templates")
def list_templates():
    return jsonify([_template_json(t) for t in _state().settings.templates()])


@api.post("/templates")
def create_template():
    body = _json_body()
    template = _template_call(
        lambda: _state().settings.add_template(
            body.get("name"), body.get("text"), body.get("layout")
        )
    )
    return jsonify(_template_json(template)), 201


@api.put("/templates/<template_id>")
def update_template(template_id: str):
    body = _json_body()
    template = _template_call(
        lambda: _state().settings.update_template(
            template_id, body.get("name"), body.get("text"), body.get("layout")
        )
    )
    if template is None:
        raise not_found(f'unknown template "{template_id}"')
    return jsonify(_template_json(template))


@api.delete("/templates/<template_id>")
def delete_template(template_id: str):
    if not _state().settings.delete_template(template_id):
        raise not_found(f'unknown template "{template_id}"')
    return "", 204


def _template_json(template: Template) -> dict[str, Any]:
    """A template has either "text" or "layout", plus the fields it asks for."""
    data: dict[str, Any] = {"id": template.id, "name": template.name}
    if template.layout is not None:
        data["layout"] = template.layout
    else:
        data["text"] = template.text
    data["fields"] = template.fields
    return data


def _template_call(action: Callable[[], Any]) -> Any:
    """Run a settings change, turning a bad template/name into 400."""
    try:
        return action()
    except TemplateError as exc:
        raise bad_request(str(exc)) from None


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


@api.post("/print/template")
def print_template():
    """
    Print a saved template ("template": id), or an unsaved one given as
    "text" or "layout" (the editor's live preview uses these), with "fields":
    {"Item": "Chicken soup"} filling in its {field:...} blanks.
    """
    body = _json_body()
    printer, label = _printer_and_label(body.get("printer"), body.get("label"))
    if "template" in body:
        template_id = _required_str(body, "template")
        template = _state().settings.template(template_id)
        if template is None:
            raise not_found(f'unknown template "{template_id}"')
        text, layout = template.text, template.layout
    elif "layout" in body:
        text, layout = "", body["layout"]
    else:
        text, layout = _required_str(body, "text"), None
    fields = body.get("fields") or {}
    if not isinstance(fields, dict) or not all(isinstance(v, str) for v in fields.values()):
        raise bad_request('"fields" must be an object of text values, e.g. {"Item": "Soup"}')
    length_mm = _optional(body, "length_mm", float, None)
    dpi = printer.config.dpi

    if layout is not None:
        assets = _state().assets
        image = _render(
            lambda: render_layout(
                normalise_layout(layout, assets.exists),
                label,
                dpi,
                _now(),
                fields,
                assets,
                length_mm=length_mm,
            )
        )
    else:
        image = _render(
            lambda: render_text(
                fill_template(text, _now(), fields),
                label,
                dpi,
                align=_optional(body, "align", str, "center"),
                length_mm=length_mm,
            )
        )
    return _preview_or_print(printer, label, image)


@api.post("/print/ruler")
def print_ruler():
    """
    Print a mm ruler along the label (render.render_ruler) to calibrate it:
    see where 0 and the end land, then set length_mm / offset_mm in
    printers.toml. Goes through prepare(), so the current offset_mm applies.
    """
    body = _json_body()
    printer, label = _printer_and_label(body.get("printer"), body.get("label"))
    image = _render(
        lambda: render_ruler(
            label, printer.config.dpi, length_mm=_optional(body, "length_mm", float, None)
        )
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


# ---------------------------------------------------------------------------
# Icons: search the bundled icon fonts
# ---------------------------------------------------------------------------
@api.get("/icons")
def search_icons():
    """
    ?q=snow&style=fa-solid&limit=60 -> matching icons, best first, plus the
    list of styles (each style is one font; "font" is its URL, for showing
    the icons on the page). Use an icon's "id" in a layout's icon element.
    """
    style = request.args.get("style") or None
    try:
        limit = int(request.args.get("limit", "60"))
    except ValueError:
        raise bad_request('"limit" must be a whole number') from None
    found = icons.search(request.args.get("q", ""), style=style, limit=limit)
    return jsonify(
        styles=[
            {"id": s.id, "label": s.label, "font": f"/vendor/{s.font}"} for s in icons.styles()
        ],
        icons=[
            {"id": i.id, "style": i.style, "name": i.name, "codepoint": i.codepoint} for i in found
        ],
    )


# ---------------------------------------------------------------------------
# Assets: images uploaded for layouts (backgrounds, logos)
# ---------------------------------------------------------------------------
@api.get("/assets")
def list_assets():
    return jsonify([_asset_json(info) for info in _state().assets.list()])


@api.post("/assets")
def upload_asset():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise bad_request('"file" is required (multipart/form-data upload)')
    source = _open_image(upload.stream)
    try:
        info = _state().assets.add(source)
    except AssetError as exc:
        raise bad_request(str(exc)) from None
    return jsonify(_asset_json(info)), 201


@api.get("/assets/<asset_id>")
def get_asset(asset_id: str):
    try:
        path = _state().assets.path(asset_id)
    except AssetError as exc:
        raise not_found(str(exc)) from None
    return send_file(path, mimetype="image/png", max_age=0)


@api.delete("/assets/<asset_id>")
def delete_asset(asset_id: str):
    users = _state().settings.templates_using_asset(asset_id)
    if users:
        names = ", ".join(f'"{t.name}"' for t in users)
        raise ApiError(409, "in_use", f"this image is used by {names}")
    if not _state().assets.delete(asset_id):
        raise not_found(f'unknown image "{asset_id}"')
    return "", 204


def _asset_json(info: Any) -> dict[str, Any]:
    return {
        "id": info.id,
        "width": info.width,
        "height": info.height,
        "bytes": info.bytes,
        "url": f"/api/assets/{info.id}",
    }


def _preview_or_print(printer: Printer, label: LabelConfig, image: Image.Image) -> Response:
    chain = _chain_for(printer)
    if _is_preview():
        # Preview shows exactly what would be sent, after the backend's
        # prepare(). It doesn't take the lock: previews work while printing.
        return _png_response(printer.prepare(image, label, chain))

    ms = _on_printer(
        printer, lambda: printer.print(printer.prepare(image, label, chain), label, chain)
    )
    log.info("printed on %s (%s, chain=%s) in %d ms", printer.id, label.id, chain, ms)
    return jsonify(status="printed", printer=printer.id, ms=ms, fed=not chain)


def _chain_for(printer: Printer) -> bool:
    """
    Print back to back (no feed-out) for this request? Only printers that can
    chain; the request's "auto_feed" wins over the saved setting.
    """
    if not printer.can_chain:
        return False
    value: Any = None
    if request.is_json:
        body = request.get_json(silent=True)
        value = body.get("auto_feed") if isinstance(body, dict) else None
    elif request.form.get("auto_feed", "") != "":
        value = _form_bool(request.form, "auto_feed")
    if value is None:
        value = _state().settings.auto_feed(printer.id)
    if not isinstance(value, bool):
        raise bad_request('"auto_feed" must be true or false')
    return not value


def _on_printer(printer: Printer, action: Callable[[], None]) -> int:
    """Run a print/feed under the printer's lock, turning failures into API
    errors (409 busy, 503 unavailable, 500 printer error). Returns ms taken."""
    started = time.monotonic()
    try:
        with _state().registry.claim(printer.id):
            action()
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
    return round((time.monotonic() - started) * 1000)


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
