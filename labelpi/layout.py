"""
Layout templates: a background plus a list of elements drawn on top of it.

    {
      "tape_length_mm": null,               # on tape: null = as long as the text needs
      "background": {"fill": "white", "frame": "rounded", "frame_mm": 0.4},
      "elements": [
        {"type": "icon", "x": 0, "y": 0, "w": 25, "h": 100, "icon": "fa-solid:snowflake"},
        {"type": "text", "x": 28, "y": 0, "w": 72, "h": 55, "text": "{field:Item}"},
        {"type": "line", "x1": 28, "y1": 58, "x2": 100, "y2": 58},
        {"type": "text", "x": 28, "y": 62, "w": 72, "h": 38, "text": "Use by {date+3m:%b %Y}"}
      ]
    }

Positions and sizes (x, y, w, h, x1...) are percentages of the printable area
(the label minus its margins), so one template works on any label size. x runs
along the label, y across it - the way you read the label. Thicknesses and
radii are in mm, so lines look the same on every printer. Elements are drawn
in order: later ones on top.

normalise_layout() checks a layout and fills in every default, so what's saved
is complete and a typo ("colour") is an error instead of being ignored.
render_layout() draws it - the same function for preview and print.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageOps

from labelpi import icons
from labelpi.render import (
    FONTS,
    RenderError,
    canvas_for,
    draw_text_block,
    largest_font,
    to_one_bit,
)
from labelpi.templates import TemplateError, fill_template, template_fields

if TYPE_CHECKING:
    from labelpi.assets import AssetStore
    from labelpi.config import LabelConfig

MAX_ELEMENTS = 40
MIN_AUTO_LENGTH_MM = 25.0  # auto-length tape labels are never shorter than this
COLORS = ("black", "white")
FILLS = ("none", "black", "white")
FRAMES = ("none", "line", "rounded", "double")
FITS = ("contain", "cover", "stretch")
ALIGNS = ("left", "center", "right")
VALIGNS = ("top", "middle", "bottom")
_GREY = {"black": 0, "white": 255}

# Every setting an element can have, and its default. A key that isn't listed
# for its type is an error. None as a default means "required".
_BOX = {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0}
ELEMENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "text": {
        **_BOX,
        "text": None,
        "align": "center",
        "valign": "middle",
        "font": "bold",
        "size_mm": 0.0,  # 0 = as big as fits; otherwise the largest size to use
        "color": "black",
    },
    "rect": {**_BOX, "fill": "none", "color": "black", "stroke_mm": 0.3, "radius_mm": 0.0},
    "ellipse": {**_BOX, "fill": "none", "color": "black", "stroke_mm": 0.3},
    "line": {"x1": 0.0, "y1": 50.0, "x2": 100.0, "y2": 50.0, "color": "black", "stroke_mm": 0.3},
    "icon": {**_BOX, "icon": None, "color": "black"},
    "image": {**_BOX, "asset": None, "fit": "contain", "dither": False, "invert": False},
}
BACKGROUND_DEFAULTS: dict[str, Any] = {
    "fill": "white",
    "frame": "none",
    "frame_mm": 0.4,
    "radius_mm": 1.5,  # only for the "rounded" frame
    "image": None,  # or {"asset": id, "fit": ..., "dither": ..., "invert": ...}
}
BACKGROUND_IMAGE_DEFAULTS: dict[str, Any] = {
    "asset": None,
    "fit": "cover",
    "dither": False,
    "invert": False,
}


# ---------------------------------------------------------------------------
# Checking a layout
# ---------------------------------------------------------------------------
def normalise_layout(
    data: Any, asset_exists: Callable[[str], bool] | None = None
) -> dict[str, Any]:
    """
    Check a layout and return a complete copy with all defaults filled in.
    Raises TemplateError with a message that says which element is wrong.
    `asset_exists` checks image ids; None skips that check (e.g. at start-up).
    """
    if not isinstance(data, dict):
        raise TemplateError("layout must be an object")
    _no_unknown_keys(data, {"tape_length_mm", "background", "elements"}, "layout")

    # None = auto: on tape, the label grows to fit its text (see _auto_length_mm).
    tape_length = data.get("tape_length_mm")
    if tape_length is not None:
        tape_length = _number(data, "tape_length_mm", 0, 5, 400, "layout")
    background = _normalise_background(data.get("background") or {}, asset_exists)

    elements = data.get("elements", [])
    if not isinstance(elements, list):
        raise TemplateError("layout elements must be a list")
    if len(elements) > MAX_ELEMENTS:
        raise TemplateError(f"a layout can have at most {MAX_ELEMENTS} elements")
    if not elements and background["image"] is None and background["frame"] == "none":
        raise TemplateError("the layout is empty - add some text, a shape or a background")

    return {
        "tape_length_mm": tape_length,
        "background": background,
        "elements": [
            _normalise_element(element, n, asset_exists) for n, element in enumerate(elements, 1)
        ],
    }


def _normalise_background(data: Any, asset_exists: Callable[[str], bool] | None) -> dict[str, Any]:
    where = "background"
    if not isinstance(data, dict):
        raise TemplateError("background must be an object")
    _no_unknown_keys(data, set(BACKGROUND_DEFAULTS), where)
    result = {
        "fill": _choice(data, "fill", "white", COLORS, where),
        "frame": _choice(data, "frame", "none", FRAMES, where),
        "frame_mm": _number(data, "frame_mm", 0.4, 0.1, 5, where),
        "radius_mm": _number(data, "radius_mm", 1.5, 0, 50, where),
        "image": None,
    }
    image = data.get("image")
    if image is not None:
        if not isinstance(image, dict):
            raise TemplateError('background image must be an object like {"asset": "<id>"}')
        where = "background image"
        _no_unknown_keys(image, set(BACKGROUND_IMAGE_DEFAULTS), where)
        result["image"] = {
            "asset": _asset(image, asset_exists, where),
            "fit": _choice(image, "fit", "cover", FITS, where),
            "dither": _bool(image, "dither", where),
            "invert": _bool(image, "invert", where),
        }
    return result


def _normalise_element(
    data: Any, n: int, asset_exists: Callable[[str], bool] | None
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TemplateError(f"element {n} must be an object")
    kind = data.get("type")
    if kind not in ELEMENT_DEFAULTS:
        raise TemplateError(
            f"element {n}: type must be one of {', '.join(ELEMENT_DEFAULTS)} (got {kind!r})"
        )
    where = f"element {n} ({kind})"
    defaults = ELEMENT_DEFAULTS[kind]
    _no_unknown_keys(data, {"type", *defaults}, where)

    result: dict[str, Any] = {"type": kind}
    for key, default in defaults.items():
        if key in ("x", "y", "x1", "y1", "x2", "y2"):
            result[key] = _number(data, key, default, 0, 100, where)
        elif key in ("w", "h"):
            result[key] = _number(data, key, default, 0.1, 100, where)
        elif key == "stroke_mm":
            # Lines need some thickness; box outlines can be 0 (fill only).
            low = 0.05 if kind == "line" else 0
            result[key] = _number(data, key, default, low, 10, where)
        elif key in ("radius_mm", "size_mm"):
            result[key] = _number(data, key, default, 0, 50, where)
        elif key in ("dither", "invert"):
            result[key] = _bool(data, key, where)
        elif key == "color":
            result[key] = _choice(data, key, default, COLORS, where)
        elif key == "fill":
            result[key] = _choice(data, key, default, FILLS, where)
        elif key == "align":
            result[key] = _choice(data, key, default, ALIGNS, where)
        elif key == "valign":
            result[key] = _choice(data, key, default, VALIGNS, where)
        elif key == "font":
            result[key] = _choice(data, key, default, tuple(FONTS), where)
        elif key == "fit":
            result[key] = _choice(data, key, default, FITS, where)
        elif key == "text":
            result[key] = _text(data, where)
        elif key == "icon":
            result[key] = _icon(data, where)
        elif key == "asset":
            result[key] = _asset(data, asset_exists, where)
    return result


def _no_unknown_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise TemplateError(
            f"{where}: unknown setting {', '.join(map(repr, unknown))} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _number(data: dict[str, Any], key: str, default: float, low: float, high: float, where: str):
    value = data.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TemplateError(f"{where}: {key} must be a number (got {value!r})")
    if not low <= value <= high:
        raise TemplateError(f"{where}: {key} must be between {low:g} and {high:g} (got {value:g})")
    return float(value)


def _choice(data: dict[str, Any], key: str, default: str, options: tuple, where: str) -> str:
    value = data.get(key, default)
    if value not in options:
        raise TemplateError(f"{where}: {key} must be one of {', '.join(options)} (got {value!r})")
    return value


def _bool(data: dict[str, Any], key: str, where: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise TemplateError(f"{where}: {key} must be true or false (got {value!r})")
    return value


def _text(data: dict[str, Any], where: str) -> str:
    value = data.get("text")
    if not isinstance(value, str) or not value.strip():
        raise TemplateError(f"{where}: text is required")
    try:
        template_fields(value)  # validates the {field:..} / {date:..} syntax
    except TemplateError as exc:
        raise TemplateError(f"{where}: {exc}") from None
    return value


def _icon(data: dict[str, Any], where: str) -> str:
    value = data.get("icon")
    if not isinstance(value, str) or not value:
        raise TemplateError(f'{where}: icon is required, e.g. "fa-solid:snowflake"')
    try:
        icons.get_icon(value)
    except icons.IconError as exc:
        raise TemplateError(f"{where}: {exc}") from None
    return value


def _asset(data: dict[str, Any], asset_exists: Callable[[str], bool] | None, where: str) -> str:
    value = data.get("asset")
    if not isinstance(value, str) or not value:
        raise TemplateError(f"{where}: asset (an uploaded image id) is required")
    if asset_exists is not None and not asset_exists(value):
        raise TemplateError(f'{where}: there is no uploaded image "{value}"')
    return value


def layout_fields(layout: dict[str, Any]) -> list[str]:
    """{field:...} names used by the layout's text elements, in order, each once."""
    names: list[str] = []
    for element in layout.get("elements", []):
        if element.get("type") == "text":
            for name in template_fields(element["text"]):
                if name not in names:
                    names.append(name)
    return names


def layout_assets(layout: dict[str, Any]) -> set[str]:
    """Ids of the uploaded images a layout uses."""
    used = {e["asset"] for e in layout.get("elements", []) if e.get("type") == "image"}
    image = (layout.get("background") or {}).get("image")
    if image:
        used.add(image["asset"])
    return used


# ---------------------------------------------------------------------------
# Drawing a layout
# ---------------------------------------------------------------------------
Box = tuple[int, int, int, int]  # left, top, right, bottom in pixels


def render_layout(
    layout: dict[str, Any],
    label: LabelConfig,
    dpi: int,
    now: datetime,
    fields: dict[str, str],
    assets: AssetStore | None,
    length_mm: float | None = None,
) -> Image.Image:
    """
    Draw a (normalised) layout as a 1-bit image for `label`. On continuous
    tape the length is `length_mm`, else the layout's tape_length_mm, else
    as long as its text needs (auto).
    """
    if label.continuous and length_mm is None:
        length_mm = layout["tape_length_mm"]
        if length_mm is None:
            length_mm = _auto_length_mm(layout, label, dpi, now, fields)
    canvas = canvas_for(label, dpi, length_mm)
    assert canvas.width is not None  # layouts always have a length
    image = Image.new("L", (canvas.width, canvas.height), 255)
    # The printable area: margins all round on die-cut labels, only at the
    # two ends on tape (the tape's band is already exactly printable).
    top = 0 if canvas.continuous else canvas.margin
    area: Box = (canvas.margin, top, canvas.width - canvas.margin, canvas.height - top)
    px_per_mm = dpi / 25.4

    _draw_background(image, layout["background"], area, px_per_mm, assets)
    for n, element in enumerate(layout["elements"], 1):
        try:
            _draw_element(image, element, area, px_per_mm, now, fields, assets)
        except RenderError as exc:
            raise RenderError(f"element {n} ({element['type']}): {exc}") from None
    return to_one_bit(image)


def _auto_length_mm(
    layout: dict[str, Any], label: LabelConfig, dpi: int, now: datetime, fields: dict[str, str]
) -> float:
    """
    How long a tape label must be for its text: each text element at the
    largest size its box's *height* allows (and its size_mm cap), with the
    box's share of the length (w %) just wide enough for it. Icons, pictures
    and shapes don't push the length - they scale into their boxes, since
    their width is a % of the very length being worked out. Never shorter
    than MIN_AUTO_LENGTH_MM, so short text keeps the layout's proportions.
    """
    px_per_mm = dpi / 25.4
    band = label.print_height_px
    needed_px = MIN_AUTO_LENGTH_MM * px_per_mm - 2 * round(label.margin_mm * px_per_mm)
    for element in layout["elements"]:
        if element["type"] != "text":
            continue
        lines = fill_template(element["text"], now, fields).split("\n")
        if not any(line.strip() for line in lines):
            continue
        box_height = round(band * element["h"] / 100)
        max_size = round(element["size_mm"] * px_per_mm) if element["size_mm"] else 0
        try:
            block = largest_font(lines, None, box_height, element["font"], max_size)
        except RenderError:
            continue  # too short for any text; drawing it will report that
        # +2 px: pixel boxes are rounded, and the text must still fit after that.
        needed_px = max(needed_px, (block.width + 2) * 100 / element["w"])
    margins = 2 * round(label.margin_mm * px_per_mm)
    return (math.ceil(needed_px) + margins) / px_per_mm


def _draw_background(
    image: Image.Image,
    background: dict[str, Any],
    area: Box,
    px_per_mm: float,
    assets: AssetStore | None,
) -> None:
    draw = ImageDraw.Draw(image)
    fill = _GREY[background["fill"]]
    if fill != 255:
        draw.rectangle(_inclusive(area), fill=fill)
    if background["image"]:
        spec = background["image"]
        _paste_asset(
            image, area, spec["asset"], spec["fit"], spec["dither"], spec["invert"], assets
        )

    frame = background["frame"]
    if frame == "none":
        return
    ink = 255 - fill  # the frame contrasts with the background
    width = _px(background["frame_mm"], px_per_mm)
    if frame == "line":
        draw.rectangle(_inclusive(area), outline=ink, width=width)
    elif frame == "rounded":
        radius = _px(background["radius_mm"], px_per_mm)
        draw.rounded_rectangle(_inclusive(area), radius=radius, outline=ink, width=width)
    elif frame == "double":
        draw.rectangle(_inclusive(area), outline=ink, width=width)
        inner = _inset(area, 2 * width)
        if inner[2] > inner[0] and inner[3] > inner[1]:
            draw.rectangle(_inclusive(inner), outline=ink, width=width)


def _draw_element(
    image: Image.Image,
    element: dict[str, Any],
    area: Box,
    px_per_mm: float,
    now: datetime,
    fields: dict[str, str],
    assets: AssetStore | None,
) -> None:
    draw = ImageDraw.Draw(image)
    kind = element["type"]

    if kind == "line":
        start = _point(area, element["x1"], element["y1"])
        end = _point(area, element["x2"], element["y2"])
        draw.line(
            [start, end], fill=_GREY[element["color"]], width=_px(element["stroke_mm"], px_per_mm)
        )
        return

    box = _box(area, element)
    if kind == "text":
        _draw_text(draw, element, box, px_per_mm, now, fields)
    elif kind in ("rect", "ellipse"):
        fill = None if element["fill"] == "none" else _GREY[element["fill"]]
        width = _px(element["stroke_mm"], px_per_mm) if element["stroke_mm"] > 0 else 0
        outline = _GREY[element["color"]] if width else None
        if kind == "ellipse":
            draw.ellipse(_inclusive(box), fill=fill, outline=outline, width=width)
        else:
            radius = _px(element["radius_mm"], px_per_mm) if element["radius_mm"] > 0 else 0
            draw.rounded_rectangle(
                _inclusive(box), radius=radius, fill=fill, outline=outline, width=width
            )
    elif kind == "icon":
        icons.draw_icon(image, element["icon"], box, _GREY[element["color"]])
    elif kind == "image":
        _paste_asset(
            image,
            box,
            element["asset"],
            element["fit"],
            element["dither"],
            element["invert"],
            assets,
        )


def _draw_text(
    draw: ImageDraw.ImageDraw,
    element: dict[str, Any],
    box: Box,
    px_per_mm: float,
    now: datetime,
    fields: dict[str, str],
) -> None:
    text = fill_template(element["text"], now, fields)
    lines = text.split("\n")
    if not any(line.strip() for line in lines):
        return  # e.g. a field that hasn't been typed yet: leave the box empty
    max_size = round(element["size_mm"] * px_per_mm) if element["size_mm"] else 0
    left, top, right, bottom = box
    try:
        block = largest_font(lines, right - left, bottom - top, element["font"], max_size)
    except RenderError:
        raise RenderError("the text doesn't fit in its box - make the box bigger") from None
    draw_text_block(
        draw, lines, block, box, element["align"], element["valign"], _GREY[element["color"]]
    )


def _paste_asset(
    image: Image.Image,
    box: Box,
    asset_id: str,
    fit: str,
    dither: bool,
    invert: bool,
    assets: AssetStore | None,
) -> None:
    """Scale an uploaded image into box ("contain", "cover" or "stretch")."""
    if assets is None or not assets.exists(asset_id):
        raise RenderError(f'the uploaded image "{asset_id}" is missing')
    left, top, right, bottom = box
    box_w, box_h = right - left, bottom - top
    if box_w < 1 or box_h < 1:
        return
    source = assets.open(asset_id)
    if invert:
        source = ImageOps.invert(source)

    if fit == "stretch":
        picture = source.resize((box_w, box_h), Image.Resampling.LANCZOS)
    elif fit == "cover":
        # Fill the whole box, cutting off what sticks out (centred).
        picture = ImageOps.fit(source, (box_w, box_h), Image.Resampling.LANCZOS)
    else:
        scale = min(box_w / source.width, box_h / source.height)
        size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        picture = source.resize(size, Image.Resampling.LANCZOS)

    if dither:
        # Dither this picture only; the rest of the label stays crisp. The
        # result is already pure black/white, so the final threshold keeps it.
        picture = picture.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
    x = left + (box_w - picture.width) // 2
    y = top + (box_h - picture.height) // 2
    image.paste(picture, (x, y))


# --- geometry helpers ---------------------------------------------------------
def _px(mm: float, px_per_mm: float) -> int:
    """mm -> whole pixels, never less than 1 (a 0.1 mm line still shows)."""
    return max(1, round(mm * px_per_mm))


def _point(area: Box, x_pct: float, y_pct: float) -> tuple[int, int]:
    left, top, right, bottom = area
    return (
        round(left + (right - left) * x_pct / 100),
        round(top + (bottom - top) * y_pct / 100),
    )


def _box(area: Box, element: dict[str, Any]) -> Box:
    """An element's x/y/w/h percentages -> pixel box, clipped to the area."""
    left, top = _point(area, element["x"], element["y"])
    right, bottom = _point(area, element["x"] + element["w"], element["y"] + element["h"])
    return left, top, min(right, area[2]), min(bottom, area[3])


def _inclusive(box: Box) -> Box:
    """Pillow's shapes include their right/bottom edge; our boxes don't."""
    left, top, right, bottom = box
    return left, top, max(left, right - 1), max(top, bottom - 1)


def _inset(box: Box, by: int) -> Box:
    left, top, right, bottom = box
    return left + by, top + by, right - by, bottom - by
