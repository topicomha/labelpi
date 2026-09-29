"""
Ready-made templates a fresh install starts with. [[templates]] in
printers.toml and anything saved from the page come on top.

Designed for small, wide labels (12 x 50 mm, 12 mm tape): positions are
percentages, so 10 % of the height is ~1 mm and 10 % of the length ~4 mm.
"""

from __future__ import annotations

from labelpi.layout import normalise_layout
from labelpi.templates import Template


def _starter(template_id: str, name: str, layout: dict) -> Template:
    return Template(template_id, name, layout=normalise_layout(layout))


STARTER_TEMPLATES = (
    _starter(
        "today",
        "Today's date",
        {
            "background": {"frame": "rounded"},
            "elements": [
                {"type": "text", "x": 4, "y": 12, "w": 92, "h": 76, "text": "{date:%d %b %Y}"},
            ],
        },
    ),
    _starter(
        "opened",
        "Opened",
        {
            "elements": [
                # Badge and date text sized so that on tape (auto length)
                # both need about the same length: no gap between them.
                {"type": "rect", "w": 40, "fill": "black", "radius_mm": 1},
                {
                    "type": "text",
                    "x": 2,
                    "y": 25,
                    "w": 36,
                    "h": 50,
                    "text": "OPENED",
                    "font": "condensed",
                    "color": "white",
                },
                {"type": "text", "x": 43, "y": 20, "w": 57, "h": 60, "text": "{date:%d %b %Y}"},
            ],
        },
    ),
    _starter(
        "food",
        "Food",
        {
            "elements": [
                {"type": "icon", "w": 14, "icon": "fa-solid:utensils"},
                {
                    "type": "text",
                    "x": 17,
                    "w": 83,
                    "h": 55,
                    "text": "{field:Food}",
                    "align": "left",
                },
                {"type": "line", "x1": 17, "y1": 60, "x2": 100, "y2": 60},
                {
                    "type": "text",
                    "x": 17,
                    "y": 66,
                    "w": 83,
                    "h": 34,
                    "align": "left",
                    "font": "condensed",
                    "text": "Made {date:%d %b}  Use by {date+3d:%d %b}",
                },
            ],
        },
    ),
    _starter(
        "freezer",
        "Freezer",
        {
            "elements": [
                {"type": "icon", "w": 14, "icon": "fa-solid:snowflake"},
                {
                    "type": "text",
                    "x": 17,
                    "w": 83,
                    "h": 55,
                    "text": "{field:Item}",
                    "align": "left",
                },
                {"type": "line", "x1": 17, "y1": 60, "x2": 100, "y2": 60},
                {
                    "type": "text",
                    "x": 17,
                    "y": 66,
                    "w": 83,
                    "h": 34,
                    "align": "left",
                    "font": "condensed",
                    "text": "Frozen {date:%d %b %y}  Use by {date+3m:%b %Y}",
                },
            ],
        },
    ),
    _starter(
        "container",
        "Container",
        {
            "background": {"frame": "double", "frame_mm": 0.3},
            "elements": [
                {"type": "text", "x": 4, "y": 12, "w": 92, "h": 50, "text": "{field:Contents}"},
                {
                    "type": "text",
                    "x": 4,
                    "y": 64,
                    "w": 92,
                    "h": 24,
                    "font": "regular",
                    "text": "{date:%d %b %Y}",
                },
            ],
        },
    ),
)
