"""
Load and validate config/printers.toml.

The whole file is checked at startup so mistakes fail loudly and early, with a
message that names the file, the printer/label/template and the field - e.g.

    config/printers.toml: printers[1] (id "d30") > labels[0] (id "12x40"):
    "length_mm" is required for a fixed-size label

The result is a tree of frozen dataclasses (think C# records): read-only after
loading, so the rest of the app can pass them around without defensive copies.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labelpi.templates import Template, TemplateError, validate_template

PRINTER_TYPES = ("brother_pt", "phomemo", "mock")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid. The message says where."""


# ---------------------------------------------------------------------------
# The config objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    max_upload_mb: float = 5


@dataclass(frozen=True)
class LabelConfig:
    """
    One label size a printer can print on.

    Continuous labels (Brother tape): height is fixed by `print_height_px`,
    length follows the content. Fixed labels (Phomemo die-cut): `width_mm` x
    `length_mm`. Fields that don't apply to a kind are None.
    """

    id: str
    name: str
    continuous: bool
    margin_mm: float
    # Continuous only
    tape_width_mm: float | None = None
    print_height_px: int | None = None
    # Fixed only
    width_mm: float | None = None
    length_mm: float | None = None
    offset_mm: float = 0.0


@dataclass(frozen=True)
class PrinterConfig:
    id: str
    display_name: str
    type: str
    address: str | None
    dpi: int
    labels: tuple[LabelConfig, ...]

    def label(self, label_id: str) -> LabelConfig | None:
        """The label with this id, or None."""
        for label in self.labels:
            if label.id == label_id:
                return label
        return None


@dataclass(frozen=True)
class Config:
    source: Path
    server: ServerConfig
    printers: tuple[PrinterConfig, ...]
    # Extra starting templates from [[templates]]. The live list is in
    # settings.py (the page can add/edit/delete); these only seed it.
    templates: tuple[Template, ...]

    def printer(self, printer_id: str) -> PrinterConfig | None:
        for printer in self.printers:
            if printer.id == printer_id:
                return printer
        return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_config(path: str | Path) -> Config:
    """Read and validate the TOML file at `path`. Raises ConfigError."""
    path = Path(path)
    try:
        with path.open("rb") as f:  # tomllib insists on binary mode
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found (copy config/printers.example.toml)") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from None

    table = _Table(data, where=str(path))
    server = _read_server(table.optional_table("server"))
    printers = tuple(_read_printer(t) for t in table.table_list("printers", required=True))
    templates = tuple(_read_template(t) for t in table.table_list("templates"))

    _check_unique([p.id for p in printers], "printer", str(path))
    _check_unique([t.id for t in templates], "template", str(path))
    return Config(source=path, server=server, printers=printers, templates=templates)


def _read_server(t: _Table | None) -> ServerConfig:
    if t is None:
        return ServerConfig()
    defaults = ServerConfig()
    return ServerConfig(
        host=t.string("host", default=defaults.host),
        port=t.integer("port", default=defaults.port, minimum=1, maximum=65535),
        max_upload_mb=t.number("max_upload_mb", default=defaults.max_upload_mb, positive=True),
    )


def _read_printer(t: _Table) -> PrinterConfig:
    printer_id = t.identifier("id")
    t.describe_as(f'id "{printer_id}"')

    printer_type = t.string("type")
    if printer_type not in PRINTER_TYPES:
        t.fail("type", f"must be one of {', '.join(PRINTER_TYPES)} (got {printer_type!r})")

    address = t.string("address", default=None)
    if address is not None and not _MAC_RE.match(address):
        t.fail("address", f"must be a Bluetooth MAC like AA:BB:CC:DD:EE:FF (got {address!r})")
    if address is None and printer_type != "mock":
        t.fail("address", f'is required for a "{printer_type}" printer')

    labels = tuple(_read_label(lt) for lt in t.table_list("labels", required=True))
    _check_unique([label.id for label in labels], "label", t.where)

    return PrinterConfig(
        id=printer_id,
        display_name=t.string("display_name", default=printer_id),
        type=printer_type,
        address=address,
        dpi=t.integer("dpi", minimum=50, maximum=1200),
        labels=labels,
    )


def _read_label(t: _Table) -> LabelConfig:
    label_id = t.identifier("id")
    t.describe_as(f'id "{label_id}"')
    continuous = t.boolean("continuous")
    common = {
        "id": label_id,
        "name": t.string("name", default=label_id),
        "continuous": continuous,
        "margin_mm": t.number("margin_mm", default=1.0, minimum=0),
    }
    if continuous:
        return LabelConfig(
            **common,
            tape_width_mm=t.number("tape_width_mm", positive=True, why="for a continuous label"),
            print_height_px=t.integer("print_height_px", minimum=1, why="for a continuous label"),
        )
    return LabelConfig(
        **common,
        width_mm=t.number("width_mm", positive=True, why="for a fixed-size label"),
        length_mm=t.number("length_mm", positive=True, why="for a fixed-size label"),
        offset_mm=t.number("offset_mm", default=0.0),
    )


def _read_template(t: _Table) -> Template:
    template_id = t.identifier("id")
    t.describe_as(f'id "{template_id}"')
    text = t.string("text")
    try:
        validate_template(text)
    except TemplateError as exc:
        t.fail("text", str(exc))
    return Template(id=template_id, name=t.string("name", default=template_id), text=text)


def _check_unique(ids: list[str], what: str, where: str) -> None:
    seen: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            raise ConfigError(f'{where}: duplicate {what} id "{item_id}"')
        seen.add(item_id)


# ---------------------------------------------------------------------------
# A small typed reader over a TOML table that knows where it is in the file,
# so every error message can say exactly which field is wrong.
# ---------------------------------------------------------------------------
_MISSING = object()


class _Table:
    def __init__(self, data: dict[str, Any], where: str, nested: bool = False):
        self._data = data
        self.where = where
        self._nested = nested  # False only for the top level of the file

    def describe_as(self, text: str) -> None:
        """Append e.g. 'id "d30"' to the location, once we know the id."""
        self.where = f"{self.where} ({text})"

    def fail(self, key: str, problem: str) -> None:
        raise ConfigError(f'{self.where}: "{key}" {problem}')

    def _get(self, key: str, default: Any, why: str) -> Any:
        if key in self._data:
            return self._data[key]
        if default is _MISSING:
            self.fail(key, f"is required{' ' + why if why else ''}")
        return default

    def string(self, key: str, default: Any = _MISSING, why: str = "") -> Any:
        value = self._get(key, default, why)
        if value is not None and value is not default and not isinstance(value, str):
            self.fail(key, f"must be text (got {value!r})")
        return value

    def identifier(self, key: str) -> str:
        value = self.string(key)
        if not _ID_RE.match(value):
            self.fail(key, f"must be letters, digits, '-' or '_' (got {value!r})")
        return value

    def boolean(self, key: str, default: Any = _MISSING) -> Any:
        value = self._get(key, default, "")
        if value is not default and not isinstance(value, bool):
            self.fail(key, f"must be true or false (got {value!r})")
        return value

    def integer(
        self,
        key: str,
        default: Any = _MISSING,
        minimum: int | None = None,
        maximum: int | None = None,
        why: str = "",
    ) -> Any:
        value = self._get(key, default, why)
        if value is default:
            return value
        # bool is a subclass of int in Python, so reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(key, f"must be a whole number (got {value!r})")
        if minimum is not None and value < minimum:
            self.fail(key, f"must be at least {minimum} (got {value})")
        if maximum is not None and value > maximum:
            self.fail(key, f"must be at most {maximum} (got {value})")
        return value

    def number(
        self,
        key: str,
        default: Any = _MISSING,
        positive: bool = False,
        minimum: float | None = None,
        why: str = "",
    ) -> Any:
        value = self._get(key, default, why)
        if value is default:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(key, f"must be a number (got {value!r})")
        if positive and value <= 0:
            self.fail(key, f"must be greater than 0 (got {value})")
        if minimum is not None and value < minimum:
            self.fail(key, f"must be at least {minimum} (got {value})")
        return float(value)

    def optional_table(self, key: str) -> _Table | None:
        value = self._data.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            self.fail(key, "must be a [table]")
        return _Table(value, where=f"{self.where}: [{key}]", nested=True)

    def table_list(self, key: str, required: bool = False) -> list[_Table]:
        """An array of tables, e.g. [[printers]]. Each gets its index in `where`."""
        value = self._data.get(key)
        if value is None:
            if required:
                self.fail(key, f"needs at least one [[{key}]] entry")
            return []
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            self.fail(key, f"must be written as [[{key}]] tables")
        if required and not value:
            self.fail(key, f"needs at least one [[{key}]] entry")
        # Top level: "file.toml: printers[0]"; nested: "... printers[0] > labels[1]"
        separator = " >" if self._nested else ":"
        return [
            _Table(v, where=f"{self.where}{separator} {key}[{i}]", nested=True)
            for i, v in enumerate(value)
        ]
