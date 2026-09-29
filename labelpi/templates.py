"""
Label templates: text with blanks to fill in and dates to work out.

    Freezer
    {field:Item}
    Frozen {date:%d %b %Y}
    Use by {date+3m:%b %Y}

Placeholders
    {field:Name}           a box on the page called "Name"; what you type goes here
    {date:<format>}        today, formatted with strftime (%d %b %Y -> 28 Sep 2026)
    {time:<format>}        same as date; the name just says what you meant (%H:%M)
    {date+N<unit>:<fmt>}   today plus/minus N days (d), weeks (w), months (m) or
                           years (y) - e.g. {date+3d:...}, {date-1w:...}, {date+6m:...}.
                           A bare number means days: {date+90:...}

Anything else in braces is an error, so a typo is caught when the template is
saved instead of printing "{dat:...}" on a label.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from labelpi.render import RenderError

MAX_FIELD_NAME = 40
MAX_FIELD_VALUE = 200

_DATE_RE = re.compile(r"\{(date|time)(?:([+-])(\d{1,4})([dwmy]?))?:([^{}]*)\}")
_FIELD_RE = re.compile(r"\{field:([^{}]*)\}")


class TemplateError(RenderError):
    """A template is malformed, or a field value is unusable. API -> 400."""


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    text: str

    @property
    def fields(self) -> list[str]:
        return template_fields(self.text)


def template_fields(text: str) -> list[str]:
    """Field names in order of first appearance, each once. Validates the template."""
    validate_template(text)
    names: list[str] = []
    for match in _FIELD_RE.finditer(text):
        name = match.group(1).strip()
        if name not in names:
            names.append(name)
    return names


def validate_template(text: str) -> None:
    """Raise TemplateError if `text` isn't a valid template."""
    if not text.strip():
        raise TemplateError("template text is empty")
    for match in _FIELD_RE.finditer(text):
        name = match.group(1).strip()
        if not name:
            raise TemplateError(f"{match.group(0)!r} needs a name, e.g. {{field:Item}}")
        if len(name) > MAX_FIELD_NAME:
            raise TemplateError(f"field name {name!r} is longer than {MAX_FIELD_NAME} characters")
    for match in _DATE_RE.finditer(text):
        if not match.group(5).strip():
            raise TemplateError(f"{match.group(0)!r} needs a format, e.g. {{date:%d %b %Y}}")
    leftover = _DATE_RE.sub("", _FIELD_RE.sub("", text))
    if "{" in leftover or "}" in leftover:
        raise TemplateError(
            "unknown placeholder - use {field:Name}, {date:...}, {date+3d:...} or {time:...}"
        )
    # Try every date format once, so a bad % code is caught now, not at print time.
    fill_template(text, datetime(2000, 1, 31, 12, 0), {}, _validating=True)


def fill_template(
    text: str, now: datetime, fields: dict[str, str], *, _validating: bool = False
) -> str:
    """
    Replace every placeholder. Missing fields become empty lines, so a
    half-filled template still previews while you type.
    """
    if not _validating:
        validate_template(text)

    def date_value(match: re.Match[str]) -> str:
        sign, amount, unit, fmt = match.group(2), match.group(3), match.group(4), match.group(5)
        when = now
        if sign:
            n = int(amount) * (1 if sign == "+" else -1)
            when = shift_date(now, n, unit or "d")
        try:
            return when.strftime(fmt)
        except ValueError as exc:  # e.g. an unsupported % code on Windows
            raise TemplateError(f"bad date format in {match.group(0)!r}: {exc}") from None

    def field_value(match: re.Match[str]) -> str:
        value = str(fields.get(match.group(1).strip(), ""))
        if len(value) > MAX_FIELD_VALUE:
            raise TemplateError(f"{match.group(1).strip()} is too long")
        return value

    return _FIELD_RE.sub(field_value, _DATE_RE.sub(date_value, text))


def shift_date(when: datetime, n: int, unit: str) -> datetime:
    """Move `when` by n days/weeks/months/years. Months keep the day where
    possible: 31 Jan + 1 month -> 28/29 Feb (the last day that exists)."""
    if unit == "d":
        return when + timedelta(days=n)
    if unit == "w":
        return when + timedelta(weeks=n)
    months = n if unit == "m" else n * 12
    year, month_index = divmod(when.month - 1 + months, 12)
    year += when.year
    month = month_index + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


# Ready-made templates a fresh install starts with (config [[templates]] and
# anything saved from the page come on top). Keep them short: labels are small.
STARTER_TEMPLATES = (
    Template("today", "Today's date", "{date:%d %b %Y}"),
    Template("opened", "Opened", "Opened\n{date:%d %b %Y}"),
    Template("food", "Food", "{field:Food}\nMade {date:%d %b}\nUse by {date+3d:%d %b}"),
    Template("freezer", "Freezer", "{field:Item}\nFrozen {date:%d %b %Y}\nUse by {date+3m:%b %Y}"),
    Template("container", "Container", "{field:Contents}\n{date:%d %b %Y}"),
)
