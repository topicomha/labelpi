from __future__ import annotations

from datetime import datetime

import pytest

from labelpi.templates import (
    TemplateError,
    fill_template,
    shift_date,
    template_fields,
    validate_template,
)

FROZEN = datetime(2026, 9, 28, 14, 5, 9)  # a Monday


def fill(text, **fields):
    return fill_template(text, FROZEN, fields)


# --- dates ---------------------------------------------------------------------
def test_date_and_time():
    assert fill("{date:%Y-%m-%d}") == "2026-09-28"
    assert fill("at {time:%H:%M}") == "at 14:05"
    assert fill("Opened\n{date:%d %b %Y}") == "Opened\n28 Sep 2026"


def test_plain_text_is_unchanged():
    assert fill("plain text") == "plain text"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("{date+3d:%d %b}", "01 Oct"),
        ("{date+3:%d %b}", "01 Oct"),  # bare number = days
        ("{date-1w:%d %b}", "21 Sep"),
        ("{date+3m:%d %b %Y}", "28 Dec 2026"),
        ("{date+6m:%b %Y}", "Mar 2027"),
        ("{date+1y:%Y}", "2027"),
        ("{date-30d:%d %b}", "29 Aug"),
    ],
)
def test_date_offsets(text, expected):
    assert fill(text) == expected


def test_month_end_clamps():
    assert shift_date(datetime(2026, 1, 31), 1, "m") == datetime(2026, 2, 28)
    assert shift_date(datetime(2028, 1, 31), 1, "m") == datetime(2028, 2, 29)  # leap year
    assert shift_date(datetime(2026, 11, 15), 3, "m") == datetime(2027, 2, 15)
    assert shift_date(datetime(2026, 3, 31), -1, "m") == datetime(2026, 2, 28)


# --- fields --------------------------------------------------------------------
def test_fields_are_listed_once_in_order():
    text = "{field:Item}\n{field:Notes}\nfor {field:Item}"
    assert template_fields(text) == ["Item", "Notes"]


def test_fields_are_filled():
    assert fill("{field:Item}\nFrozen", Item="Chicken soup") == "Chicken soup\nFrozen"


def test_same_field_twice_gets_same_value():
    assert fill("{field:A} and {field:A}", A="x") == "x and x"


def test_missing_field_is_blank():
    assert fill("{field:Item}\nFrozen") == "\nFrozen"


def test_field_names_are_trimmed():
    assert template_fields("{field: Item }") == ["Item"]
    assert fill("{field: Item }", Item="Soup") == "Soup"


def test_too_long_field_value():
    with pytest.raises(TemplateError, match="too long"):
        fill("{field:Item}", Item="x" * 500)


# --- validation ----------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "{dat:%Y}",
        "{date}",
        "{date:}",
        "{now}",
        "{field:}",
        "{field}",
        "{date+3x:%d}",
        "stray } brace",
        "{",
        "{field:" + "x" * 41 + "}",
    ],
)
def test_bad_templates(bad):
    with pytest.raises(TemplateError):
        validate_template(bad)


def test_template_error_is_a_render_error():
    """The API turns RenderError into 400, so template errors get that for free."""
    from labelpi.render import RenderError

    assert issubclass(TemplateError, RenderError)
