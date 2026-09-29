from __future__ import annotations

import pytest
from PIL import Image

from labelpi import icons


def test_every_style_has_a_font_and_icons():
    styles = icons.styles()
    assert {s.id for s in styles} >= {"fa-solid", "fa-regular", "fa-brands", "tabler", "mdi"}
    for style in styles:
        assert (icons.VENDOR_DIR / style.font).is_file(), style.font
        assert icons.search("", style=style.id, limit=1), style.id


def test_every_library_has_its_licence():
    for style in icons.styles():
        folder = (icons.VENDOR_DIR / style.font).parent
        assert (folder / "LICENSE.txt").is_file(), folder


def test_get_icon():
    icon = icons.get_icon("fa-solid:snowflake")
    assert icon.codepoint == 0xF2DC and icon.id == "fa-solid:snowflake"
    with pytest.raises(icons.IconError, match="unknown icon"):
        icons.get_icon("fa-solid:no-such-icon")
    with pytest.raises(icons.IconError):
        icons.get_icon("snowflake")  # needs the style


def test_search_ranks_exact_names_first():
    results = icons.search("snowflake")
    assert results[0].name == "snowflake"
    assert all("snowflake" in f"{i.name} {i.search}" for i in results)


def test_search_uses_keywords_and_every_word():
    # "cold" isn't in the name, but it's one of the snowflake's search words.
    assert "fa-solid:snowflake" in [i.id for i in icons.search("cold", style="fa-solid")]
    assert icons.search("zzzz qqqq") == []


def test_search_filters_and_limits():
    results = icons.search("", style="mdi", limit=5)
    assert len(results) == 5 and {i.style for i in results} == {"mdi"}
    assert len(icons.search("", limit=100000)) == icons.MAX_RESULTS


@pytest.mark.parametrize(
    "icon_id", ["fa-solid:snowflake", "fa-regular:star", "tabler:fridge", "mdi:food-apple"]
)
def test_draw_icon_fits_in_box(icon_id):
    image = Image.new("L", (100, 60), 255)
    icons.draw_icon(image, icon_id, (10, 10, 50, 50))
    ink = image.point(lambda p: 255 if p < 128 else 0).getbbox()
    assert ink is not None, "nothing was drawn"
    left, top, right, bottom = ink
    assert left >= 10 and top >= 10 and right <= 50 and bottom <= 50
    # As large as fits: the icon touches two opposite sides (within rounding).
    assert right - left >= 36 or bottom - top >= 36


def test_draw_icon_in_white():
    image = Image.new("L", (40, 40), 0)
    icons.draw_icon(image, "fa-solid:square", (0, 0, 40, 40), color=255)
    assert image.getpixel((20, 20)) == 255
