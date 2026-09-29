from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from labelpi.render import (
    MIN_FONT_PX,
    RenderError,
    canvas_for,
    fit_image,
    render_text,
)

BROTHER_DPI = 180
PHOMEMO_DPI = 203


def black_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the black pixels, or None if the image is all white."""
    # In mode "1", black is 0; invert so black becomes the non-zero "content".
    return Image.eval(image.convert("L"), lambda p: 255 - p).getbbox()


# --- canvas ----------------------------------------------------------------
def test_continuous_canvas(tape_label):
    canvas = canvas_for(tape_label, BROTHER_DPI)
    assert canvas.width is None and canvas.height == 64
    assert canvas.margin == round(2 * BROTHER_DPI / 25.4)
    assert canvas.inner_height == 64  # margins only at the ends of tape


def test_continuous_canvas_with_length(tape_label):
    assert canvas_for(tape_label, BROTHER_DPI, length_mm=50).width == round(50 * BROTHER_DPI / 25.4)


def test_fixed_canvas(die_label):
    canvas = canvas_for(die_label, PHOMEMO_DPI)
    assert (canvas.width, canvas.height) == (320, 96)  # 40 x 12 mm at 8 px/mm
    assert canvas.inner_height == 96 - 2 * canvas.margin


def test_length_mm_not_allowed_on_fixed_labels(die_label):
    with pytest.raises(RenderError, match="fixed size"):
        canvas_for(die_label, PHOMEMO_DPI, length_mm=30)


def test_tape_length_is_capped(tape_label):
    with pytest.raises(RenderError, match="limit"):
        canvas_for(tape_label, BROTHER_DPI, length_mm=1000)


# --- text ------------------------------------------------------------------
def test_text_on_tape_fills_the_band_height(tape_label):
    image = render_text("Server rack 2", tape_label, BROTHER_DPI)
    assert image.mode == "1"
    assert image.height == 64
    left, top, right, bottom = black_box(image)
    assert bottom - top > 64 * 0.6  # auto-sized to the band, not tiny


def test_tape_length_follows_the_text(tape_label):
    short = render_text("Hi", tape_label, BROTHER_DPI)
    long = render_text("Hello there, this is longer", tape_label, BROTHER_DPI)
    assert long.width > short.width * 3


def test_tape_keeps_end_margins(tape_label):
    image = render_text("Margins", tape_label, BROTHER_DPI)
    margin = canvas_for(tape_label, BROTHER_DPI).margin
    left, _, right, _ = black_box(image)
    assert left >= margin - 1 and right <= image.width - margin + 1


def test_text_on_fixed_label_stays_inside_margins(die_label):
    image = render_text("A rather long line of text", die_label, PHOMEMO_DPI)
    assert image.size == (320, 96)
    margin = canvas_for(die_label, PHOMEMO_DPI).margin
    left, top, right, bottom = black_box(image)
    assert left >= margin and top >= margin
    assert right <= 320 - margin and bottom <= 96 - margin


def test_more_lines_means_smaller_text(die_label):
    one = black_box(render_text("Jar", die_label, PHOMEMO_DPI))
    three = black_box(render_text("Jar\nJar\nJar", die_label, PHOMEMO_DPI))
    one_height = one[3] - one[1]
    three_line_height = (three[3] - three[1]) / 3
    assert three_line_height < one_height


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_alignment(die_label, align):
    left, _, right, _ = black_box(render_text("ab\nabcdefgh", die_label, PHOMEMO_DPI, align=align))
    # Render the short line alone to see where alignment put it.
    short = black_box(render_text("ab\n", die_label, PHOMEMO_DPI, align=align))
    centre = 320 / 2
    if align == "left":
        assert short[0] < centre - 40
    elif align == "right":
        assert short[2] > centre + 40
    else:
        assert abs((short[0] + short[2]) / 2 - centre) < 6


def test_explicit_font_size(die_label):
    small = black_box(render_text("Hi", die_label, PHOMEMO_DPI, font_size=12))
    assert small[3] - small[1] < 20


def test_font_size_too_big_is_rejected(die_label):
    with pytest.raises(RenderError, match="doesn't fit at font_size"):
        render_text("Hi", die_label, PHOMEMO_DPI, font_size=200)


def test_text_too_long_for_fixed_label(die_label):
    with pytest.raises(RenderError, match="doesn't fit"):
        render_text("x" * 400, die_label, PHOMEMO_DPI)


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_text_is_rejected(tape_label, text):
    with pytest.raises(RenderError, match="empty"):
        render_text(text, tape_label, BROTHER_DPI)


def test_bad_align(tape_label):
    with pytest.raises(RenderError, match="align"):
        render_text("x", tape_label, BROTHER_DPI, align="justify")


def test_rendering_is_deterministic(die_label):
    a = render_text("Same\ntext", die_label, PHOMEMO_DPI)
    b = render_text("Same\ntext", die_label, PHOMEMO_DPI)
    assert a.tobytes() == b.tobytes()


def test_min_font_constant_is_sane():
    assert MIN_FONT_PX >= 4


# --- images ----------------------------------------------------------------
def wide_logo() -> Image.Image:
    """400 x 100 black rectangle on white."""
    image = Image.new("RGB", (400, 100), "white")
    ImageDraw.Draw(image).rectangle([10, 10, 389, 89], fill="black")
    return image


def test_image_on_tape_scales_to_band(tape_label):
    image = fit_image(wide_logo(), tape_label, BROTHER_DPI)
    assert image.mode == "1" and image.height == 64
    margin = canvas_for(tape_label, BROTHER_DPI).margin
    assert image.width == 256 + 2 * margin  # 400x100 scaled by 0.64 -> 256x64


def test_image_on_fixed_label_is_contained_and_centred(die_label):
    image = fit_image(wide_logo(), die_label, PHOMEMO_DPI)
    assert image.size == (320, 96)
    left, top, right, bottom = black_box(image)
    margin = canvas_for(die_label, PHOMEMO_DPI).margin
    assert left >= margin and right <= 320 - margin  # never cropped
    assert abs((left + right) / 2 - 160) <= 2  # centred
    assert abs((top + bottom) / 2 - 48) <= 2


def test_tall_image_on_fixed_label_keeps_aspect(die_label):
    tall = Image.new("L", (50, 200), 0)  # all black, 1:4
    left, top, right, bottom = black_box(fit_image(tall, die_label, PHOMEMO_DPI))
    height, width = bottom - top, right - left
    assert abs(height / width - 4) < 0.3


def test_transparency_becomes_white(die_label):
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))  # fully transparent black
    assert black_box(fit_image(image, die_label, PHOMEMO_DPI)) is None


def test_invert(die_label):
    white = Image.new("L", (100, 30), 255)
    assert black_box(fit_image(white, die_label, PHOMEMO_DPI)) is None
    assert black_box(fit_image(white, die_label, PHOMEMO_DPI, invert=True)) is not None


def test_dither_gives_a_pattern_for_grey(die_label):
    grey = Image.new("L", (100, 30), 100)
    plain = fit_image(grey, die_label, PHOMEMO_DPI)
    dithered = fit_image(grey, die_label, PHOMEMO_DPI, dither=True)
    assert dithered.mode == "1"
    plain_black = plain.convert("L").histogram()[0]
    dithered_black = dithered.convert("L").histogram()[0]
    assert 0 < dithered_black < plain_black  # some dots, not a solid block
