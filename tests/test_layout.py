from __future__ import annotations

from datetime import datetime

import pytest
from PIL import Image

from labelpi.assets import AssetStore
from labelpi.layout import (
    MAX_ELEMENTS,
    layout_assets,
    layout_fields,
    normalise_layout,
    render_layout,
)
from labelpi.render import RenderError
from labelpi.starters import STARTER_TEMPLATES
from labelpi.templates import TemplateError

NOW = datetime(2026, 9, 28, 14, 5)
DIE_DPI = 203  # 12 x 50 mm at 203 dpi, 1 mm margin -> 400 x 96 px, area 8..392 x 8..88
TAPE_DPI = 180


def text(**extra):
    return {"type": "text", "text": "Hi", **extra}


def render(layout, label, dpi=DIE_DPI, fields=None, assets=None, **kwargs):
    return render_layout(normalise_layout(layout), label, dpi, NOW, fields or {}, assets, **kwargs)


def black(image: Image.Image, x: int, y: int) -> bool:
    return image.getpixel((x, y)) == 0  # mode "1": 0 = black


def black_pixels(image: Image.Image) -> int:
    return image.convert("L").histogram()[0]


# --- normalising -----------------------------------------------------------------
def test_defaults_are_filled_in():
    layout = normalise_layout({"elements": [text()]})
    assert layout["tape_length_mm"] == 40
    assert layout["background"] == {
        "fill": "white",
        "frame": "none",
        "frame_mm": 0.4,
        "radius_mm": 1.5,
        "image": None,
    }
    assert layout["elements"] == [
        {
            "type": "text",
            "x": 0.0,
            "y": 0.0,
            "w": 100.0,
            "h": 100.0,
            "text": "Hi",
            "align": "center",
            "valign": "middle",
            "font": "bold",
            "size_mm": 0.0,
            "color": "black",
        }
    ]


def test_normalising_twice_changes_nothing():
    once = normalise_layout({"elements": [text(x=10), {"type": "line"}]})
    assert normalise_layout(once) == once


@pytest.mark.parametrize(
    "layout, message",
    [
        ("nope", "layout must be an object"),
        ({"elements": []}, "empty"),
        ({"elements": {}}, "must be a list"),
        ({"elements": [text()], "colour": 1}, "unknown setting 'colour'"),
        ({"elements": [{"type": "star"}]}, "element 1: type must be one of"),
        ({"elements": [text(), text(x=120)]}, "element 2 (text): x must be between 0 and 100"),
        ({"elements": [text(w=0)]}, "w must be between"),
        ({"elements": [text(x="1")]}, "x must be a number"),
        ({"elements": [text(x=True)]}, "x must be a number"),
        ({"elements": [text(color="red")]}, "color must be one of black, white"),
        ({"elements": [text(font="comic")]}, "font must be one of"),
        ({"elements": [text(text="")]}, "text is required"),
        ({"elements": [text(text="{dat:%d}")]}, "unknown placeholder"),
        ({"elements": [text(colour="white")]}, "unknown setting 'colour'"),
        ({"elements": [{"type": "icon"}]}, "icon is required"),
        ({"elements": [{"type": "icon", "icon": "fa-solid:nope"}]}, "unknown icon"),
        ({"elements": [{"type": "image"}]}, "asset (an uploaded image id) is required"),
        ({"elements": [{"type": "line", "stroke_mm": 0}]}, "stroke_mm must be between"),
        ({"elements": [{"type": "rect", "fill": "grey"}]}, "fill must be one of"),
        ({"elements": [{"type": "image", "asset": "a", "dither": 1}]}, "true or false"),
        ({"background": {"frame": "wavy"}}, "frame must be one of"),
        ({"background": {"image": "abc"}}, "background image must be an object"),
        ({"background": {"image": {}}}, "background image: asset"),
        ({"elements": [text()] * (MAX_ELEMENTS + 1)}, f"at most {MAX_ELEMENTS}"),
        ({"elements": [text()], "tape_length_mm": 1}, "tape_length_mm must be between"),
    ],
)
def test_invalid_layouts(layout, message):
    with pytest.raises(TemplateError, match=message.replace("(", r"\(").replace(")", r"\)")):
        normalise_layout(layout)


def test_asset_check():
    layout = {"elements": [{"type": "image", "asset": "abc"}]}
    normalise_layout(layout)  # no checker -> not checked
    normalise_layout(layout, asset_exists=lambda a: a == "abc")
    with pytest.raises(TemplateError, match='no uploaded image "abc"'):
        normalise_layout(layout, asset_exists=lambda a: False)


def test_background_alone_is_a_layout():
    assert normalise_layout({"background": {"frame": "line"}})["elements"] == []


def test_fields_and_assets():
    layout = normalise_layout(
        {
            "background": {"image": {"asset": "bg"}},
            "elements": [
                text(text="{field:Item} {date:%d}"),
                {"type": "image", "asset": "logo"},
                text(text="{field:Who}\n{field:Item}"),
            ],
        }
    )
    assert layout_fields(layout) == ["Item", "Who"]
    assert layout_assets(layout) == {"bg", "logo"}


# --- rendering -------------------------------------------------------------------
def test_size_and_mode(die_label, tape_label):
    image = render({"elements": [text()]}, die_label)
    assert image.size == (400, 96) and image.mode == "1"
    # Tape: band height, length from tape_length_mm (or the request).
    tape = render({"elements": [text()], "tape_length_mm": 30}, tape_label, TAPE_DPI)
    assert tape.size == (round(30 * TAPE_DPI / 25.4), 64)
    tape = render({"elements": [text()]}, tape_label, TAPE_DPI, length_mm=50)
    assert tape.width == round(50 * TAPE_DPI / 25.4)


def test_filled_rect_covers_its_box(die_label):
    # Left half of the printable area (x 8..200), full height (y 8..88).
    image = render({"elements": [{"type": "rect", "w": 50, "fill": "black"}]}, die_label)
    assert black(image, 8, 8) and black(image, 199, 87)
    assert not black(image, 201, 50)
    assert not black(image, 4, 50)  # the margin stays white


def test_outline_rect_is_hollow(die_label):
    image = render({"elements": [{"type": "rect", "stroke_mm": 0.5}]}, die_label)
    assert black(image, 8, 50) and black(image, 391, 50)
    assert not black(image, 200, 48)


def test_line(die_label):
    image = render({"elements": [{"type": "line", "y1": 50, "y2": 50}]}, die_label)
    assert black(image, 20, 48) and black(image, 300, 48)
    assert not black(image, 20, 20)


def test_ellipse(die_label):
    image = render({"elements": [{"type": "ellipse", "fill": "black"}]}, die_label)
    assert black(image, 200, 48)  # centre
    assert not black(image, 9, 9)  # corner of its box


def test_black_background_with_white_text(die_label):
    image = render({"background": {"fill": "black"}, "elements": [text(color="white")]}, die_label)
    assert black(image, 10, 10)
    assert not black(image, 2, 2)  # margin
    white = image.convert("L").histogram()[255]
    assert white > 76 * 16  # more than just the margin: the text shows up white


@pytest.mark.parametrize("frame", ["line", "rounded", "double"])
def test_frames(die_label, frame):
    image = render({"background": {"frame": frame}}, die_label)
    assert black(image, 200, 8) and black(image, 8, 48)  # on the edge of the area
    assert not black(image, 200, 48)


def test_icon_is_drawn_inside_its_box(die_label):
    image = render(
        {"elements": [{"type": "icon", "w": 20, "icon": "fa-solid:snowflake"}]}, die_label
    )
    assert black_pixels(image) > 500
    xs = [x for x in range(400) for y in range(96) if black(image, x, y)]
    assert min(xs) >= 8 and max(xs) < 8 + 78  # 20 % of the 384 px area, + rounding


def test_text_shrinks_to_fit_and_respects_size(die_label):
    big = render({"elements": [text(text="Hi")]}, die_label)
    capped = render({"elements": [text(text="Hi", size_mm=3)]}, die_label)
    assert black_pixels(capped) < black_pixels(big)


def test_text_alignment(die_label):
    def ink_x(image):
        left, _, right, _ = image.convert("L").point(lambda p: 255 - p).getbbox()
        return left, right

    left = ink_x(render({"elements": [text(text="Hi", size_mm=4, align="left")]}, die_label))
    right = ink_x(render({"elements": [text(text="Hi", size_mm=4, align="right")]}, die_label))
    assert left[0] < 20 and left[1] < 200  # hugging the left edge of the area (x = 8)
    assert right[1] > 380 and right[0] > 200  # hugging the right edge (x = 392)


def test_empty_field_leaves_the_box_empty(die_label):
    image = render({"elements": [text(text="{field:Item}")]}, die_label)
    assert black_pixels(image) == 0
    filled = render({"elements": [text(text="{field:Item}")]}, die_label, fields={"Item": "Soup"})
    assert black_pixels(filled) > 0


def test_text_that_cannot_fit_names_the_element(die_label):
    layout = {"elements": [text(), text(text="x" * 100, w=5, h=5)]}
    with pytest.raises(RenderError, match=r"element 2 \(text\): the text doesn't fit"):
        render(layout, die_label)


def test_images(die_label, tmp_path):
    store = AssetStore(tmp_path / "assets")
    asset = store.add(Image.new("RGB", (100, 100), "black")).id
    for fit, expected_black in (("contain", 80 * 80), ("cover", 192 * 80), ("stretch", 192 * 80)):
        image = render(
            {"elements": [{"type": "image", "asset": asset, "w": 50, "fit": fit}]},
            die_label,
            assets=store,
        )
        assert abs(black_pixels(image) - expected_black) < 200, fit
    inverted = render(
        {"elements": [{"type": "image", "asset": asset, "invert": True}]}, die_label, assets=store
    )
    assert black_pixels(inverted) == 0


def test_dithered_background_image(die_label, tmp_path):
    store = AssetStore(tmp_path / "assets")
    grey = store.add(Image.new("L", (50, 50), 128)).id
    plain = render({"background": {"image": {"asset": grey}}}, die_label, assets=store)
    dithered = render(
        {"background": {"image": {"asset": grey, "dither": True}}}, die_label, assets=store
    )
    area = 384 * 80
    assert black_pixels(plain) in (0, area)  # threshold: all one colour
    assert 0.3 * area < black_pixels(dithered) < 0.7 * area  # dithered: about half


def test_missing_image_is_a_render_error(die_label, tmp_path):
    store = AssetStore(tmp_path / "assets")
    with pytest.raises(RenderError, match="missing"):
        render({"elements": [{"type": "image", "asset": "0" * 16}]}, die_label, assets=store)


# --- starters ------------------------------------------------------------------------
@pytest.mark.parametrize("template", STARTER_TEMPLATES, ids=lambda t: t.id)
def test_starters_render_on_both_labels(template, die_label, tape_label):
    fields = {name: "Chicken soup" for name in template.fields}
    assert template.layout is not None
    for label, dpi in ((die_label, DIE_DPI), (tape_label, TAPE_DPI)):
        image = render_layout(template.layout, label, dpi, NOW, fields, None)
        assert black_pixels(image) > 100


def test_starter_fields():
    by_id = {t.id: t for t in STARTER_TEMPLATES}
    assert by_id["today"].fields == []
    assert by_id["freezer"].fields == ["Item"]
    assert by_id["food"].fields == ["Food"]
