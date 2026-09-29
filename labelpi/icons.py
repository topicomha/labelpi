"""
Bundled icon fonts: find icons by name/keyword and draw them into a box.

Icons come from icon *fonts* (unmodified TTF files in labelpi/vendor/, each
with its licence) - every icon is one character. Pillow draws a character from
a TTF just like text, so icons need no extra dependency and scale to any size
without blurring.

An icon is named "<style>:<name>", e.g. "fa-solid:snowflake",
"tabler:fridge", "mdi:food-apple". vendor/icons.json (made by
tools/build_icon_index.py) lists every icon's character code and search words.
The index is ~1 MB, so it's only loaded the first time an icon is needed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VENDOR_DIR = Path(__file__).parent / "vendor"
INDEX_PATH = VENDOR_DIR / "icons.json"
MAX_RESULTS = 200


@dataclass(frozen=True)
class Style:
    id: str  # "fa-solid"
    label: str  # "Font Awesome solid"
    font: str  # path relative to labelpi/vendor/


@dataclass(frozen=True)
class Icon:
    style: str
    name: str
    codepoint: int
    search: str  # lower-case search words
    haystack: str = ""  # name + search words: what a search looks in

    @property
    def id(self) -> str:
        return f"{self.style}:{self.name}"


class IconError(ValueError):
    """Unknown icon id."""


class _Index:
    """Everything in icons.json, loaded once and shared by all requests."""

    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.styles = [Style(s["id"], s["label"], s["font"]) for s in data["styles"]]
        self.icons: list[Icon] = []
        self.by_id: dict[str, Icon] = {}
        for style, entries in data["icons"].items():
            for name, codepoint, search in entries:
                icon = Icon(style, name, codepoint, search, f"{name} {search}")
                self.icons.append(icon)
                self.by_id[icon.id] = icon
        # Sorted once by name, so an empty search is just the first N.
        self.icons.sort(key=lambda icon: icon.name)


_index: _Index | None = None
_index_lock = threading.Lock()


def _get_index() -> _Index:
    global _index  # module-level cache, like a static field in C#
    with _index_lock:
        if _index is None:
            _index = _Index(INDEX_PATH)
        return _index


def styles() -> list[Style]:
    return list(_get_index().styles)


def get_icon(icon_id: str) -> Icon:
    """Look up "style:name"; IconError if there's no such icon."""
    icon = _get_index().by_id.get(icon_id)
    if icon is None:
        raise IconError(f'unknown icon "{icon_id}" - search for one with GET /api/icons?q=...')
    return icon


def search(query: str, style: str | None = None, limit: int = 60) -> list[Icon]:
    """
    Icons matching every word of `query`, best first: exact name, then name
    starting with the query, then name containing it, then search words.
    An empty query lists icons alphabetically.
    """
    limit = max(1, min(limit, MAX_RESULTS))
    words = query.lower().split()
    candidates = [i for i in _get_index().icons if not style or i.style == style]
    if not words:
        return candidates[:limit]
    # The list is already in name order and sort() is stable, so sorting by
    # score alone keeps names alphabetical within each score.
    phrase = "-".join(words)
    scored: list[tuple[int, Icon]] = []
    for icon in candidates:
        score = _score(icon, words, phrase)
        if score is not None:
            scored.append((score, icon))
    scored.sort(key=lambda item: item[0])
    return [icon for _, icon in scored[:limit]]


def _score(icon: Icon, words: list[str], phrase: str) -> int | None:
    """Lower is better; None = doesn't match. `phrase` is the words joined by "-"."""
    if not all(word in icon.haystack for word in words):
        return None  # most icons stop here, after one cheap check
    if icon.name == phrase:
        return 0
    if icon.name.startswith(phrase):
        return 1
    if all(word in icon.name for word in words):
        return 2
    return 3  # matched on search words only


@lru_cache(maxsize=16)
def _font(style_id: str, size: int) -> ImageFont.FreeTypeFont:
    style = next(s for s in _get_index().styles if s.id == style_id)
    return ImageFont.truetype(str(VENDOR_DIR / style.font), size)


def draw_icon(
    image: Image.Image, icon_id: str, box: tuple[int, int, int, int], color: int = 0
) -> None:
    """
    Draw an icon as large as fits in `box` (left, top, right, bottom),
    centred, keeping its shape. `color` is 0 (black) or 255 (white).
    """
    icon = get_icon(icon_id)
    left, top, right, bottom = box
    box_w, box_h = right - left, bottom - top
    if box_w < 1 or box_h < 1:
        return
    char = chr(icon.codepoint)

    # Measure the glyph's ink at a reference size, then scale so it fits the
    # box. (Icon fonts pad their glyphs, so the font's own box is too big.)
    reference = 200
    ref_ink = _ink_box(_font(icon.style, reference), char)
    if ref_ink is None:
        return  # an empty glyph: nothing to draw
    ink_w, ink_h = ref_ink[2] - ref_ink[0], ref_ink[3] - ref_ink[1]
    size = max(1, int(reference * min(box_w / ink_w, box_h / ink_h)))
    font = _font(icon.style, size)
    ink = _ink_box(font, char)
    # Shrink by a pixel at a time if rounding made it a touch too big.
    while size > 1 and ink and (ink[2] - ink[0] > box_w or ink[3] - ink[1] > box_h):
        size -= 1
        font = _font(icon.style, size)
        ink = _ink_box(font, char)
    if ink is None:
        return

    x = left + (box_w - (ink[2] - ink[0])) // 2 - ink[0]
    y = top + (box_h - (ink[3] - ink[1])) // 2 - ink[1]
    ImageDraw.Draw(image).text((x, y), char, font=font, fill=color)


def _ink_box(font: ImageFont.FreeTypeFont, char: str) -> tuple[int, int, int, int] | None:
    """
    The pixels a character actually inks, relative to where draw.text puts
    it - found by drawing it once. None if it inks nothing.
    """
    left, top, right, bottom = font.getbbox(char)
    if right <= left or bottom <= top:
        return None
    scratch = Image.new("L", (right - left, bottom - top), 0)
    ImageDraw.Draw(scratch).text((-left, -top), char, font=font, fill=255)
    ink = scratch.getbbox()
    if ink is None:
        return None
    return ink[0] + left, ink[1] + top, ink[2] + left, ink[3] + top
