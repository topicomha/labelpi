"""
Turn text and uploaded images into 1-bit label images (templates.py fills in
templates first; the result is plain text rendered here).

Everything that gets printed goes through here, and preview uses exactly the
same functions, so what you see is what prints.

Coordinates: images are drawn "as you read the label" - x runs along the
label's length, y across its width. Rotating for a particular printer is the
backend's job (Printer.prepare), never this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont, ImageOps

if TYPE_CHECKING:  # only for type hints; avoids a circular import at runtime
    from labelpi.config import LabelConfig

FONT_PATH = Path(__file__).parent / "fonts" / "DejaVuSans-Bold.ttf"
MIN_FONT_PX = 6  # below this, text isn't readable on a thermal printer
LINE_GAP = 0.15  # space between lines, as a fraction of the font size
MAX_CONTINUOUS_PX = 3000  # sanity cap for tape length (~42 cm at 180 dpi)
THRESHOLD = 128  # grey values below this print as black


class RenderError(ValueError):
    """The request can't be rendered (text doesn't fit, bad image...). API -> 400."""


# ---------------------------------------------------------------------------
# Canvas: how big the image for a label is
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Canvas:
    """
    Pixel size of a label image.

    width is None for continuous labels without a requested length: the
    image is then as long as its content (plus margins).
    For continuous labels the margin applies only at the two ends; the height
    is already exactly the printable band.
    """

    width: int | None
    height: int
    margin: int
    continuous: bool

    @property
    def inner_height(self) -> int:
        return self.height if self.continuous else self.height - 2 * self.margin

    @property
    def inner_width(self) -> int | None:
        return None if self.width is None else self.width - 2 * self.margin


def canvas_for(label: LabelConfig, dpi: int, length_mm: float | None = None) -> Canvas:
    """Work out the image size for `label` on a printer with `dpi`."""
    px_per_mm = dpi / 25.4
    margin = round(label.margin_mm * px_per_mm)

    if label.continuous:
        width = None
        if length_mm is not None:
            if length_mm <= 0:
                raise RenderError("length_mm must be greater than 0")
            width = round(length_mm * px_per_mm)
            _check_length(width)
            if width <= 2 * margin:
                raise RenderError(f"length_mm {length_mm} is shorter than the margins")
        return Canvas(width=width, height=label.print_height_px, margin=margin, continuous=True)

    if length_mm is not None:
        raise RenderError(f'label "{label.id}" has a fixed size; length_mm only applies to tape')
    return Canvas(
        width=round(label.length_mm * px_per_mm),
        height=round(label.width_mm * px_per_mm),
        margin=margin,
        continuous=False,
    )


def _check_length(width_px: int) -> None:
    if width_px > MAX_CONTINUOUS_PX:
        raise RenderError(f"label would be {width_px} px long; the limit is {MAX_CONTINUOUS_PX}")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def load_font(size: int) -> ImageFont.FreeTypeFont:
    """The bundled font, so rendering is identical on every machine."""
    return ImageFont.truetype(str(FONT_PATH), size)


@dataclass(frozen=True)
class _TextBlock:
    font: ImageFont.FreeTypeFont
    line_widths: tuple[int, ...]
    line_height: int
    gap: int

    @property
    def width(self) -> int:
        return max(self.line_widths)

    @property
    def height(self) -> int:
        n = len(self.line_widths)
        return n * self.line_height + (n - 1) * self.gap


def _measure(lines: list[str], size: int) -> _TextBlock:
    font = load_font(size)
    ascent, descent = font.getmetrics()
    widths = tuple(round(font.getlength(line)) for line in lines)
    return _TextBlock(font, widths, ascent + descent, round(size * LINE_GAP))


def _fits(block: _TextBlock, max_w: int | None, max_h: int) -> bool:
    return block.height <= max_h and (max_w is None or block.width <= max_w)


def _largest_font(lines: list[str], max_w: int | None, max_h: int) -> _TextBlock:
    """
    Binary search for the biggest font size whose text block fits.

    Bigger font -> bigger block, always, so the search is valid. Upper bound:
    a font can't be taller than the space it has to fit in.
    """
    low, high = MIN_FONT_PX, max(MIN_FONT_PX, max_h)
    if not _fits(_measure(lines, low), max_w, max_h):
        raise RenderError("text doesn't fit on this label, even at the smallest font size")
    while low < high:
        mid = (low + high + 1) // 2  # round up so the loop always moves
        if _fits(_measure(lines, mid), max_w, max_h):
            low = mid
        else:
            high = mid - 1
    return _measure(lines, low)


def render_text(
    text: str,
    label: LabelConfig,
    dpi: int,
    align: str = "center",
    font_size: int | None = None,
    length_mm: float | None = None,
) -> Image.Image:
    """
    Render `text` (lines separated by \\n) as a 1-bit image for `label`.

    The font is as large as fits unless `font_size` is given; if that size
    doesn't fit, RenderError.
    """
    if align not in ("left", "center", "right"):
        raise RenderError(f"align must be left, center or right (got {align!r})")
    lines = text.split("\n")
    if not any(line.strip() for line in lines):
        raise RenderError("text is empty")

    canvas = canvas_for(label, dpi, length_mm)
    max_w, max_h = canvas.inner_width, canvas.inner_height

    if font_size is None:
        block = _largest_font(lines, max_w, max_h)
    else:
        if font_size < MIN_FONT_PX:
            raise RenderError(f"font_size must be at least {MIN_FONT_PX}")
        block = _measure(lines, font_size)
        if not _fits(block, max_w, max_h):
            raise RenderError(f"text doesn't fit at font_size {font_size}")

    width = canvas.width if canvas.width is not None else block.width + 2 * canvas.margin
    _check_length(width)
    image = Image.new("L", (width, canvas.height), 255)
    draw = ImageDraw.Draw(image)

    y = (canvas.height - block.height) // 2
    for line, line_width in zip(lines, block.line_widths, strict=True):
        if align == "left":
            x = canvas.margin
        elif align == "right":
            x = width - canvas.margin - line_width
        else:
            x = (width - line_width) // 2
        draw.text((x, y), line, font=block.font, fill=0)  # anchor: top-left of ascender
        y += block.line_height + block.gap

    return to_one_bit(image)


# ---------------------------------------------------------------------------
# Calibration ruler
# ---------------------------------------------------------------------------
DEFAULT_RULER_TAPE_MM = 50.0


def render_ruler(label: LabelConfig, dpi: int, length_mm: float | None = None) -> Image.Image:
    """
    A mm ruler along the whole label, for measuring where printing lands.

    0 is the first column of the image (a solid bar), with a tick every mm,
    longer ones every 5 mm and a number every 10 mm, plus a line along both
    long edges to check the width. Margins are ignored on purpose: the ruler
    shows the image itself. Compare it with the label's physical edges, then
    set length_mm / offset_mm in printers.toml. On tape it is `length_mm`
    long (default 50 mm).
    """
    px_per_mm = dpi / 25.4
    if label.continuous:
        length = DEFAULT_RULER_TAPE_MM if length_mm is None else length_mm
        if length <= 0:
            raise RenderError("length_mm must be greater than 0")
        width, height = round(length * px_per_mm), label.print_height_px
        _check_length(width)
    else:
        if length_mm is not None:
            raise RenderError(
                f'label "{label.id}" has a fixed size; length_mm only applies to tape'
            )
        length = label.length_mm
        width, height = round(label.length_mm * px_per_mm), round(label.width_mm * px_per_mm)

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width - 1, 1], fill=0)  # top edge line
    draw.rectangle([0, height - 2, width - 1, height - 1], fill=0)  # bottom edge line
    draw.rectangle([0, 0, 5, height - 1], fill=0)  # the 0 mm bar
    font = load_font(max(MIN_FONT_PX, height // 4))
    last_mm = int(length)
    for mm in range(1, last_mm + 1):
        x = min(width - 1, round(mm * px_per_mm) - 1)
        if mm % 10 == 0:
            tick = height // 2
        elif mm % 5 == 0:
            tick = height // 3
        else:
            tick = height // 6
        draw.line([(x, 0), (x, tick)], fill=0, width=2 if mm % 5 == 0 else 1)
        if mm % 10 == 0:
            anchor = "rb" if mm == last_mm else "mb"  # keep the last number on the label
            draw.text((x, height - 6), str(mm), font=font, fill=0, anchor=anchor)
    return to_one_bit(image)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def fit_image(
    source: Image.Image,
    label: LabelConfig,
    dpi: int,
    dither: bool = False,
    invert: bool = False,
    length_mm: float | None = None,
) -> Image.Image:
    """
    Scale an uploaded image to fit `label` (never cropped) and make it 1-bit.

    Transparent areas become white. On continuous tape without a requested
    length, the image is scaled to the band height and the label is as long as
    it needs to be.
    """
    canvas = canvas_for(label, dpi, length_mm)
    grey = _flatten_to_grey(source)
    if invert:
        grey = ImageOps.invert(grey)

    max_w, max_h = canvas.inner_width, canvas.inner_height
    scale = max_h / grey.height
    if max_w is not None:
        scale = min(scale, max_w / grey.width)
    new_size = (max(1, round(grey.width * scale)), max(1, round(grey.height * scale)))
    resized = grey.resize(new_size, Image.Resampling.LANCZOS)

    width = canvas.width if canvas.width is not None else new_size[0] + 2 * canvas.margin
    _check_length(width)
    result = Image.new("L", (width, canvas.height), 255)
    # Centre the picture in the label.
    result.paste(resized, ((width - new_size[0]) // 2, (canvas.height - new_size[1]) // 2))
    if dither:
        # Floyd-Steinberg: better for photos. Pure-white padding stays white.
        return result.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    return to_one_bit(result)


def _flatten_to_grey(source: Image.Image) -> Image.Image:
    """Apply EXIF rotation (phone photos), put transparency on white, go greyscale."""
    image = ImageOps.exif_transpose(source)
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, rgba)
    return image.convert("L")


def to_one_bit(grey: Image.Image) -> Image.Image:
    """Plain threshold to black/white (no dithering) - crisp text and line art."""
    return grey.point(lambda p: 255 if p >= THRESHOLD else 0).convert("1", dither=Image.Dither.NONE)
